"""Committed queue mirrors in temporary stores; never live radio or power tests."""

import asyncio
import sys
import threading

import pytest

from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.store import Database, PostCommitError, StoreError, Transaction
from outpost.store.members import MemberRepo
from outpost.transport.governor import OutboundItem
from outpost.transport.models import TrafficClass
from outpost.watch import CheckinService
from tests.integration.test_durable_outbox import durable_governor

pytestmark = pytest.mark.production_wiring


@pytest.fixture
async def durable(tmp_path):
    database, governor, radio = await durable_governor(tmp_path / "outpost.db", VirtualClock())
    try:
        yield database, governor, radio
    finally:
        await database.close()


def work(text, *, supersedes=None, queue_key=None):
    return OutboundItem(
        text, "^all", 0, TrafficClass.REPLY, supersedes=supersedes, queue_key=queue_key
    )


def queued(governor):
    return [item.item_id for item in governor.queued_items()]


async def states(database):
    return [
        tuple(row) for row in await database.read("SELECT id,state FROM outbound_work ORDER BY id")
    ]


@pytest.mark.parametrize("hold", [False, True])
async def test_supersession_is_invisible_until_commit_and_rollback_preserves_original(
    durable, hold
):
    database, governor, radio = durable
    original = await governor.admit(work("original", queue_key="incident:probe"))
    metric = governor.metrics.enqueued[TrafficClass.REPLY]
    with pytest.raises(RuntimeError, match="rollback"):
        async with database.transaction() as tx:
            result = await governor.admit_many_result(
                [work("replacement", supersedes="incident:probe")], hold=hold, transaction=tx
            )
            assert result.admitted == 1
            assert queued(governor) == [original]
            assert await states(database) == [(original, "pending")]
            assert governor.metrics.enqueued[TrafficClass.REPLY] == metric
            assert len(await tx.read("SELECT id FROM outbound_work")) == 2
            raise RuntimeError("rollback")
    assert queued(governor) == [original]
    assert await states(database) == [(original, "pending")]
    assert governor.metrics.enqueued[TrafficClass.REPLY] == metric
    assert not governor._held_ids and not radio.sent
    await radio.connect()
    assert (await governor.tick()).item_id == original


@pytest.mark.parametrize("hold", [False, True])
async def test_commit_publishes_entire_batch_then_supersedes_old_work(durable, hold):
    database, governor, radio = durable
    original = await governor.admit(work("original", queue_key="incident:probe"))
    async with database.transaction() as tx:
        ids = await governor.admit_many(
            [work("one", supersedes="incident:probe"), work("two")],
            hold=hold,
            transaction=tx,
        )
        assert queued(governor) == [original]
    assert queued(governor) == ids
    assert await states(database) == [
        (original, "superseded"),
        *((item_id, "held" if hold else "pending") for item_id in ids),
    ]
    assert governor._held_ids == (set(ids) if hold else set())
    await radio.connect()
    if hold:
        assert await governor.tick() is None and not radio.sent
        await governor.release_work(ids)
    assert (await governor.tick()).item_id == ids[0]


async def test_multiple_admissions_publish_in_commit_order_and_only_final_replacement_is_queued(
    durable,
):
    database, governor, _ = durable
    old = await governor.admit(work("old", queue_key="chain"))
    async with database.transaction() as tx:
        middle = await governor.admit_many(
            [work("middle", supersedes="chain", queue_key="chain")], hold=True, transaction=tx
        )
        final = await governor.admit_many([work("final", supersedes="chain")], transaction=tx)
        assert queued(governor) == [old]
    assert queued(governor) == final
    assert not governor._held_ids
    assert await states(database) == [
        (old, "superseded"),
        (middle[0], "superseded"),
        (final[0], "pending"),
    ]


async def test_caller_mutation_before_publication_cannot_change_committed_payload(
    durable, monkeypatch
):
    database, governor, _ = durable
    item = work("stored content")
    original = governor.outbox.admit_many

    async def mutate(*args, **kwargs):
        item.text = "not stored"
        item.dest = "!wrong"
        return await original(*args, **kwargs)

    monkeypatch.setattr(governor.outbox, "admit_many", mutate)
    async with database.transaction() as tx:
        ids = await governor.admit_many([item], transaction=tx)
        item.channel = 99
        item.binary_payload = b"wrong payload"
    published = governor.queued_items()[0]
    assert published is item
    assert (published.text, published.dest, published.channel, published.binary_payload) == (
        "stored content",
        "^all",
        0,
        None,
    )
    row = (
        await database.read("SELECT text,destination,channel FROM outbound_work WHERE id=?", ids)
    )[0]
    assert tuple(row) == (published.text, published.dest, published.channel)
    assert published.item_id == item.item_id == ids[0]


@pytest.mark.parametrize("failure", ["write", "commit"])
async def test_writer_or_commit_failure_never_publishes_or_counts_new_admissions(
    durable, monkeypatch, failure
):
    database, governor, _ = durable
    old = await governor.admit(work("original", queue_key="replace"))
    metric = governor.metrics.enqueued[TrafficClass.REPLY]
    operation = database._writer_write if failure == "write" else database._commit
    if failure == "write":

        async def fail(*args, **kwargs):
            await operation(*args, **kwargs)
            raise RuntimeError("write failed")

        monkeypatch.setattr(database, "_writer_write", fail)
    else:

        def fail():
            raise RuntimeError("commit failed")

        monkeypatch.setattr(database, "_commit", fail)
    with pytest.raises(RuntimeError, match="failed"):
        await governor.admit_many([work("replacement", supersedes="replace")])
    assert queued(governor) == [old]
    assert await states(database) == [(old, "pending")]
    assert governor.metrics.enqueued[TrafficClass.REPLY] == metric
    assert not governor._publication_failed


@pytest.mark.parametrize("after_commit", [False, True])
@pytest.mark.parametrize("owned", [False, True])
async def test_repeated_cancellation_settles_commit_and_publication_before_next_writer(
    durable, monkeypatch, after_commit, owned
):
    database, governor, _ = durable
    old = await governor.admit(work("original", queue_key="replace"))
    reached, release, next_writer = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = database._writer_call
    ordering = []
    ids = []

    async def pause(operation):
        if operation == database._commit:
            if after_commit:
                result = await original(operation)
            reached.set()
            await release.wait()
            return result if after_commit else await original(operation)
        return await original(operation)

    async def admit():
        if owned:
            await governor.admit_many([work("replacement", supersedes="replace")], hold=True)
        else:
            async with database.transaction() as tx:
                ids.extend(
                    await governor.admit_many(
                        [work("replacement", supersedes="replace")], hold=True, transaction=tx
                    )
                )
                tx.after_commit(lambda: ordering.append("published"))

    async def following():
        async with database.transaction() as tx:
            ordering.append("next writer")
            next_writer.set()
            await tx.write("INSERT INTO kv(ns,k,v,updated_at) VALUES('commit-test','next','1',1)")

    monkeypatch.setattr(database, "_writer_call", pause)
    task = asyncio.create_task(admit())
    follower = None
    try:
        await asyncio.wait_for(reached.wait(), 5)
        follower = asyncio.create_task(following())
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done() and not next_writer.is_set()
        assert database._transaction_lock.locked() and queued(governor) == [old]
        assert len(await states(database)) == (2 if after_commit else 1)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await asyncio.wait_for(follower, 5)
        assert ordering == (["next writer"] if owned else ["published", "next writer"])
        rows = await states(database)
        assert rows == [(old, "superseded"), (old + 1, "held")]
        assert queued(governor) == [old + 1] and governor._held_ids == {old + 1}
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *([follower] if follower else []), return_exceptions=True)


@pytest.mark.parametrize("phase", ["begin", "body"])
async def test_repeated_cancellation_waits_for_rollback_without_publishing(
    durable, monkeypatch, phase
):
    database, governor, _ = durable
    old = await governor.admit(work("old", queue_key="replace"))
    reached, rollback, release, following = (asyncio.Event() for _ in range(4))
    original = database._writer_call

    async def pause(operation):
        if operation == database._rollback:
            rollback.set()
            await release.wait()
        result = await original(operation)
        if operation == database._begin and phase == "begin" and not reached.is_set():
            reached.set()
            await asyncio.Event().wait()
        return result

    async def admit():
        async with database.transaction() as tx:
            await governor.admit_many(
                [work("new", supersedes="replace")], hold=True, transaction=tx
            )
            reached.set()
            await asyncio.Event().wait()

    async def next_writer():
        async with database.transaction():
            following.set()

    monkeypatch.setattr(database, "_writer_call", pause)
    task = asyncio.create_task(admit())
    follower = None
    try:
        await asyncio.wait_for(reached.wait(), 5)
        task.cancel()
        await asyncio.wait_for(rollback.wait(), 5)
        follower = asyncio.create_task(next_writer())
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done() and not following.is_set()
        assert queued(governor) == [old]
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await asyncio.wait_for(follower, 5)
        assert await states(database) == [(old, "pending")]
        assert queued(governor) == [old] and not governor._held_ids
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *([follower] if follower else []), return_exceptions=True)


async def test_publications_are_synchronous_ordered_and_run_after_commit_with_writer_still_owned(
    durable, monkeypatch
):
    database, _, _ = durable
    events = []
    thread = threading.get_ident()
    commit = database._commit

    def committed():
        commit()
        events.append("commit")

    def publish():
        assert threading.get_ident() == thread
        assert database._transaction_lock.locked()
        assert not tx._active
        events.append("publish")

    monkeypatch.setattr(database, "_commit", committed)
    async with database.transaction() as tx:
        await tx.write("INSERT INTO kv(ns,k,v,updated_at) VALUES('commit-test','one','1',1)")
        tx.after_commit(publish)
        tx.after_commit(lambda: events.append("second"))
        assert not events
    assert events == ["commit", "publish", "second"]


@pytest.mark.parametrize("operation", ["read", "write", "admit", "register"])
async def test_closed_transactions_reject_operations_and_publication(durable, operation):
    database, governor, _ = durable
    async with database.transaction() as tx:
        pass
    with pytest.raises(StoreError, match="closed"):
        if operation == "read":
            await tx.read("SELECT 1")
        elif operation == "write":
            await tx.write("INSERT INTO kv(ns,k,v,updated_at) VALUES('bad','bad','1',1)")
        elif operation == "admit":
            await governor.admit_many([work("bad")], transaction=tx)
        else:
            tx.after_commit(lambda: None)
    assert not queued(governor) and not await states(database)


async def test_foreign_database_and_cross_task_transaction_use_are_rejected(durable, tmp_path):
    database, governor, _ = durable
    other = Database(tmp_path / "other.db")
    await other.open()
    try:
        async with other.transaction() as foreign:
            with pytest.raises(StoreError, match="different database"):
                await governor.admit_many([work("wrong store")], transaction=foreign)
            with pytest.raises(StoreError, match="different database"):
                await governor.outbox.admit_many(
                    [], queue_max_items=1, dedupe_window_s=1, transaction=foreign
                )
        async with database.transaction() as tx:
            with pytest.raises(StoreError, match="owning task"):
                await asyncio.create_task(governor.admit_many([work("wrong task")], transaction=tx))
        assert not await states(database) and not await states(other)
    finally:
        await other.close()


async def test_async_publication_is_rejected_before_commit(durable):
    database, _, _ = durable

    async def asynchronous():
        pass

    class AsyncPublication:
        async def __call__(self):
            pass

    async with database.transaction() as tx:
        for publication in (asynchronous, AsyncPublication(), None):
            with pytest.raises(TypeError, match="synchronous"):
                tx.after_commit(publication)


@pytest.mark.parametrize("inside_transaction", [False, True])
async def test_manually_constructed_transaction_cannot_borrow_the_writer(
    durable, inside_transaction
):
    database, governor, _ = durable

    async def refuse():
        forged = Transaction(database)
        with pytest.raises(StoreError, match="active owned writer"):
            await governor.admit_many([work("unowned")], transaction=forged)
        with pytest.raises(StoreError, match="active owned writer"):
            forged.after_commit(lambda: None)

    if inside_transaction:
        async with database.transaction():
            await refuse()
    else:
        await refuse()
    assert not queued(governor) and not await states(database)


@pytest.mark.parametrize("phase", ["write", "commit"])
async def test_single_write_settlement_also_retains_writer_across_repeated_cancellation(
    durable, monkeypatch, phase
):
    database, _, _ = durable
    reached, cleanup, release, following = (asyncio.Event() for _ in range(4))
    original_write, original_call = database._writer_write, database._writer_call

    async def pause_write(*args, **kwargs):
        result = await original_write(*args, **kwargs)
        if phase == "write" and not reached.is_set():
            reached.set()
            await asyncio.Event().wait()
        return result

    async def pause_call(operation):
        if (phase == "write" and operation == database._rollback) or (
            phase == "commit" and operation == database._commit
        ):
            reached.set()
            cleanup.set()
            await release.wait()
        return await original_call(operation)

    async def next_writer():
        async with database.transaction():
            following.set()

    monkeypatch.setattr(database, "_writer_write", pause_write)
    monkeypatch.setattr(database, "_writer_call", pause_call)
    task = asyncio.create_task(
        database.write("INSERT INTO kv(ns,k,v,updated_at) VALUES('commit-test','single','1',1)")
    )
    follower = None
    try:
        await asyncio.wait_for(reached.wait(), 5)
        task.cancel()
        await asyncio.wait_for(cleanup.wait(), 5)
        follower = asyncio.create_task(next_writer())
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done() and not following.is_set()
        assert database._transaction_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await asyncio.wait_for(follower, 5)
        assert bool(await database.read("SELECT 1 FROM kv WHERE ns='commit-test'")) == (
            phase == "commit"
        )
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *([follower] if follower else []), return_exceptions=True)


@pytest.mark.parametrize("partially_published", [False, True])
async def test_publication_failure_is_not_rollback_and_blocks_egress_until_quiesced_recovery(
    durable, monkeypatch, partially_published
):
    database, governor, radio = durable
    old = await governor.admit(work("old", queue_key="replace"))
    original = governor._publish_committed
    later_publications = []

    def fail(*args, **kwargs):
        if partially_published:
            original(*args, **kwargs)
        raise RuntimeError("publication failed")

    with monkeypatch.context() as patch:
        patch.setattr(governor, "_publish_committed", fail)
        with pytest.raises(PostCommitError, match="committed"):
            async with database.transaction() as tx:
                await tx.write(
                    "INSERT INTO kv(ns,k,v,updated_at) VALUES('commit-test','domain','1',1)"
                )
                ids = await governor.admit_many(
                    [work("new", supersedes="replace")], hold=True, transaction=tx
                )
                tx.after_commit(lambda: later_publications.append("still published"))
    assert later_publications == ["still published"]
    assert await database.read("SELECT 1 FROM kv WHERE ns='commit-test'")
    assert await states(database) == [(old, "superseded"), (ids[0], "held")]
    await radio.connect()
    with pytest.raises(StoreError, match="recover before sending"):
        await governor.tick()
    assert not radio.sent
    assert await governor.recover() == 1
    assert queued(governor) == ids
    assert (await governor.tick()).item_id == ids[0]


async def test_failed_recovery_keeps_egress_blocked(durable, monkeypatch):
    _, governor, radio = durable
    await governor.admit(work("kept"))

    async def fail(*args):
        raise RuntimeError("recovery failed")

    with monkeypatch.context() as patch:
        patch.setattr(governor.outbox, "recent_airtime", fail)
        with pytest.raises(RuntimeError, match="recovery failed"):
            await governor.recover()
    await radio.connect()
    with pytest.raises(StoreError, match="recover before sending"):
        await governor.tick()
    assert not radio.sent
    await governor.recover()
    assert await governor.tick() is not None


@pytest.mark.parametrize("hold", [False, True])
async def test_queue_limits_and_dedupe_include_unpublished_work_and_never_create_phantoms(
    durable, hold
):
    database, governor, _ = durable
    governor.config.queue_max_items = 2
    async with database.transaction() as tx:
        first = await governor.admit_many([work("one")], hold=hold, transaction=tx)
        duplicate = await governor.admit_many_result([work("one")], transaction=tx)
        assert duplicate.rejection_reason == "duplicate"
        second = await governor.admit_many([work("two")], transaction=tx)
        rejected = await governor.admit_many_result([work("three")], transaction=tx)
        assert rejected.rejection_reason == "queue_full"
        assert not queued(governor)
    assert queued(governor) == first + second
    assert governor.metrics.enqueued[TrafficClass.REPLY] == 2


@pytest.mark.parametrize("commit", [False, True])
async def test_an_already_selected_old_attempt_waits_for_transaction_outcome(durable, commit):
    database, governor, radio = durable
    old = await governor.admit(work("old", queue_key="replace"))
    await radio.connect()
    governor._next_outbox_sweep_at = float("inf")
    selected = asyncio.Event()
    original = governor.outbox.start_attempt

    async def attempting(*args, **kwargs):
        selected.set()
        return await original(*args, **kwargs)

    governor.outbox.start_attempt = attempting
    tick = None
    try:
        try:
            async with database.transaction() as tx:
                ids = await governor.admit_many(
                    [work("new", supersedes="replace")], hold=True, transaction=tx
                )
                tick = asyncio.create_task(governor.tick())
                await asyncio.wait_for(selected.wait(), 5)
                assert not radio.sent
                if not commit:
                    raise RuntimeError("rollback")
        except RuntimeError:
            pass
        sent = await asyncio.wait_for(tick, 5)
        if commit:
            assert sent is None and not radio.sent and queued(governor) == ids
        else:
            assert sent.item_id == old and len(radio.sent) == 1
            assert not queued(governor)
    finally:
        if tick is not None:
            if not tick.done():
                tick.cancel()
            await asyncio.gather(tick, return_exceptions=True)


@pytest.mark.parametrize("phase", ["telemetry", "attempt"])
async def test_publication_failure_while_tick_awaits_is_checked_before_radio_io(
    durable, monkeypatch, phase
):
    database, governor, radio = durable
    await governor.admit(work("old"))
    await radio.connect()
    governor._next_outbox_sweep_at = float("inf")
    reached, release = asyncio.Event(), asyncio.Event()
    original = radio.local_telemetry if phase == "telemetry" else governor.outbox.start_attempt

    async def pause(*args, **kwargs):
        result = await original(*args, **kwargs)
        reached.set()
        await release.wait()
        return result

    def fail(*args, **kwargs):
        raise RuntimeError("publication failed")

    monkeypatch.setattr(
        radio if phase == "telemetry" else governor.outbox,
        "local_telemetry" if phase == "telemetry" else "start_attempt",
        pause,
    )
    tick = asyncio.create_task(governor.tick())
    try:
        await asyncio.wait_for(reached.wait(), 5)
        with monkeypatch.context() as patch:
            patch.setattr(governor, "_publish_committed", fail)
            with pytest.raises(PostCommitError):
                await governor.admit(work("new"))
        release.set()
        with pytest.raises(StoreError, match="recover before sending"):
            await asyncio.wait_for(tick, 5)
        assert not radio.sent
        attempts = await database.read("SELECT state FROM outbound_attempt")
        assert len(attempts) == int(phase == "attempt")
    finally:
        release.set()
        if not tick.done():
            tick.cancel()
        await asyncio.gather(tick, return_exceptions=True)


async def test_bad_post_commit_result_or_callback_cancellation_is_explicit_and_other_consumers_run(
    durable,
):
    database, _, _ = durable
    later = []

    async def asynchronous():
        pytest.fail("post-commit async work must not execute")

    def cancelled():
        raise asyncio.CancelledError()

    with pytest.raises(PostCommitError, match="committed"):
        async with database.transaction() as tx:
            await tx.write("INSERT INTO kv(ns,k,v,updated_at) VALUES('commit-test','saved','1',1)")
            tx.after_commit(lambda: asynchronous())
            tx.after_commit(cancelled)
            tx.after_commit(lambda: 7)
            tx.after_commit(lambda: later.append("published"))
    assert later == ["published"]
    assert await database.read("SELECT 1 FROM kv WHERE ns='commit-test'")


async def prepare_checkin(database, governor):
    members = MemberRepo(database, governor.clock)
    for number in range(1, 3):
        member = await members.resolve(f"!{number:08x}")
        await members.claim_handle(member.mesh_id, f"person{number}")
    service = CheckinService(database, governor, governor.clock)
    event = await service.open_event("Flood", "all", "test:operator")
    return service, event


async def test_checkin_commit_cancellation_keeps_solicitations_and_held_work_for_recovery(
    durable, monkeypatch
):
    database, governor, _ = durable
    service, event = await prepare_checkin(database, governor)
    reached, release = asyncio.Event(), asyncio.Event()
    original = database._writer_call

    async def pause(operation):
        result = await original(operation)
        if operation == database._commit:
            reached.set()
            await release.wait()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(database, "_writer_call", pause)
        task = asyncio.create_task(service.solicit(event.id))
        try:
            await asyncio.wait_for(reached.wait(), 5)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done() and not queued(governor)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    rows = await database.read(
        "SELECT queue_item_id FROM checkin_solicitation ORDER BY queue_item_id"
    )
    ids = [row["queue_item_id"] for row in rows]
    assert len(ids) == 2 and queued(governor) == ids
    assert await states(database) == [(item_id, "held") for item_id in ids]
    assert not await service.solicitation_preview(event.id)
    assert await governor.recover() == 2
    assert not governor._held_ids


async def test_checkin_post_commit_publication_failure_does_not_erase_durable_domain_rows(
    durable, monkeypatch
):
    database, governor, _ = durable
    service, event = await prepare_checkin(database, governor)

    def fail(*args, **kwargs):
        raise RuntimeError("publication failed")

    with monkeypatch.context() as patch:
        patch.setattr(governor, "_publish_committed", fail)
        with pytest.raises(PostCommitError):
            await service.solicit(event.id)
    assert len(await database.read("SELECT * FROM checkin_solicitation")) == 2
    assert len(await states(database)) == 2 and not queued(governor)
    assert await governor.recover() == 2
    assert not await service.solicitation_preview(event.id)


async def test_publication_failure_is_visible_to_production_core_task_supervision(
    tmp_path, monkeypatch
):
    app = OutpostApp(Config.model_validate({"store": {"path": str(tmp_path / "app.db")}}))
    await app.database.open()

    def fail(*args, **kwargs):
        raise RuntimeError("publication failed")

    task = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(app.governor, "_publish_committed", fail)
            with pytest.raises(PostCommitError):
                await app.governor.admit(work("retained"))
        task = app._start_background_task("airtime-governor", app._governor_loop)
        reason = await asyncio.wait_for(app.wait_for_task_failure(), 5)
        assert "StoreError: outbox publication failed" in reason
        assert app.status()["tasks"]["airtime-governor"]["state"] == "failed"
        assert app.status()["tasks"]["airtime-governor"]["failure_domain"] == "core"
        await asyncio.gather(task, return_exceptions=True)
        assert await app.governor.recover() == 1
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await app.database.close()


@pytest.mark.parametrize("phase", ["uncommitted", "committed"])
async def test_test_owned_process_kill_recovers_only_committed_queue_and_domain_work(
    tmp_path, phase
):
    path = tmp_path / "killed.db"
    script = r"""
import asyncio, sys
from outpost.app import OutpostApp
from outpost.config import Config
from outpost.transport.governor import OutboundItem
from outpost.transport.models import TrafficClass

async def run():
    app = OutpostApp(Config.model_validate({"store": {"path": sys.argv[1]}}))
    await app.database.open()
    old = OutboundItem("old", "^all", 0, TrafficClass.REPLY, queue_key="replace")
    await app.governor.admit(old)
    original = app.database._writer_call
    async def pause(operation):
        result = await original(operation)
        if operation == app.database._commit:
            print("committed", flush=True)
            await asyncio.Event().wait()
        return result
    if sys.argv[2] == "committed":
        app.database._writer_call = pause
    async with app.database.transaction() as tx:
        await tx.write("INSERT INTO kv(ns,k,v,updated_at) VALUES('commit-test','domain','1',1)")
        await app.governor.admit_many(
            [OutboundItem("new", "^all", 0, TrafficClass.REPLY, supersedes="replace")],
            hold=True, transaction=tx,
        )
        if sys.argv[2] == "uncommitted":
            print("uncommitted", flush=True)
            await asyncio.Event().wait()
asyncio.run(run())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(path),
        phase,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert (await asyncio.wait_for(process.stdout.readline(), 30)).decode().strip() == phase
        process.kill()  # Only this test-owned process; its simulated radio never connects.
        await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode < 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    database, governor, radio = await durable_governor(path, VirtualClock())
    try:
        assert (await database.read("PRAGMA integrity_check"))[0][0] == "ok"
        assert bool(await database.read("SELECT 1 FROM kv WHERE ns='commit-test'")) == (
            phase == "committed"
        )
        assert await governor.recover() == 1
        assert governor.queued_items()[0].text == ("new" if phase == "committed" else "old")
        assert not radio.sent
    finally:
        await database.close()
