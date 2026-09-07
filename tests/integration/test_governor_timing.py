from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from outpost.clock import VirtualClock
from outpost.config import AirtimeConfig, RadioPowerConfig
from outpost.store import Database
from outpost.store.database import Transaction
from outpost.store.outbox import OutboxStore
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.models import LinkState, Severity, TrafficClass
from outpost.transport.simulated import SimulatedRadioLink
from tests.support.application import production_governor

pytestmark = pytest.mark.production_wiring
PHASES = ("telemetry", "observer", "reservation", "writer", "guard")


@pytest.fixture
async def queue(tmp_path):
    database = Database(tmp_path / "timing.db")
    await database.open()
    clock = VirtualClock(epoch=datetime(2026, 1, 1, 12, tzinfo=UTC))
    radio = SimulatedRadioLink(clock)
    await radio.connect()
    governor = production_governor(database, clock, link=radio)
    try:
        yield database, governor, clock, radio
    finally:
        await database.close()


def delay_once(monkeypatch, governor, radio, phase, action):
    """Inject a clock/policy change on an actual awaited dispatch path."""
    done = False

    def change():
        nonlocal done
        if not done:
            done = True
            action()

    if phase == "guard":

        async def guard(transaction, work):
            await transaction.read("SELECT 1")
            change()
            return True

        governor.outbox.attempt_guards["timing-test"] = guard
    elif phase == "writer":
        original = Transaction.read

        async def read(self, sql, params=()):
            result = await original(self, sql, params)
            if sql == "SELECT * FROM outbound_work WHERE id=?":
                change()
            return result

        monkeypatch.setattr(Transaction, "read", read)
    elif phase == "observer":

        async def observe(level):
            await asyncio.sleep(0)
            change()

        governor.power_observer = observe
    else:
        owner = radio if phase == "telemetry" else governor.outbox
        name = "local_telemetry" if phase == "telemetry" else "start_attempt"
        original = getattr(owner, name)

        async def delayed(*args, **kwargs):
            await asyncio.sleep(0)
            change()
            return await original(*args, **kwargs)

        monkeypatch.setattr(owner, name, delayed)


def packet(phase, traffic_class=TrafficClass.REPLY):
    return OutboundItem(
        "timing probe",
        "^all",
        0,
        traffic_class,
        guard_kind="timing-test" if phase == "guard" else None,
    )


@pytest.mark.parametrize("phase", PHASES)
async def test_deadline_passed_during_await_cannot_start_radio_work(queue, monkeypatch, phase):
    database, governor, clock, radio = queue
    item = packet(phase)
    await governor.admit(item)
    delay_once(monkeypatch, governor, radio, phase, lambda: clock.advance(301))

    assert await governor.tick() is None
    assert not radio.sent
    assert not await database.read("SELECT * FROM outbound_attempt")
    row = (await database.read("SELECT state,attempts FROM outbound_work"))[0]
    assert tuple(row) == ("failed" if phase == "guard" else "expired", 0)
    assert not governor.queued_items()


@pytest.mark.parametrize("phase", PHASES)
async def test_quiet_boundary_defers_without_losing_work_or_blocking_replies(
    queue, monkeypatch, phase
):
    database, governor, clock, radio = queue
    clock.epoch = datetime(2026, 1, 1, 21, 59, 59, tzinfo=UTC)
    item = packet(phase, TrafficClass.FEDERATION)
    await governor.admit(item)
    delay_once(monkeypatch, governor, radio, phase, lambda: clock.advance(2))

    assert await governor.tick() is None
    assert not radio.sent
    assert governor.queued_items() == [item]
    assert tuple((await database.read("SELECT state,attempts FROM outbound_work"))[0]) == (
        "pending",
        0,
    )
    assert not await database.read("SELECT * FROM outbound_attempt")
    reply = OutboundItem("still available", "^all", 0, TrafficClass.REPLY)
    await governor.admit(reply)
    assert await governor.tick() is reply
    assert [sent.text for sent in radio.sent] == [reply.text]


@pytest.mark.parametrize("phase", PHASES)
async def test_attempt_timestamp_is_sampled_after_waits(queue, monkeypatch, phase):
    database, governor, clock, radio = queue
    item = packet(phase)
    await governor.admit(item)
    delay_once(monkeypatch, governor, radio, phase, lambda: clock.advance(17))

    assert await governor.tick() is item
    attempt = (await database.read("SELECT started_at,completed_at FROM outbound_attempt"))[0]
    assert tuple(attempt) == (clock.now().timestamp(), clock.now().timestamp())
    assert (await database.read("SELECT last_attempt_at FROM outbound_work"))[0][0] == (
        clock.now().timestamp()
    )


@pytest.mark.parametrize("outcome", ["sent", "failed", "cancelled"])
async def test_slow_radio_accounting_uses_completion_not_tick_start(queue, monkeypatch, outcome):
    database, governor, clock, radio = queue
    item = packet("reservation")
    await governor.admit(item)
    original = radio._send_text
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        if outcome == "failed":
            raise OSError("synthetic uncertain send")
        return await original(*args, **kwargs)

    monkeypatch.setattr(radio, "_send_text", slow)
    task = asyncio.create_task(governor.tick())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        clock.advance(120)
        if outcome == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            assert await task is (item if outcome == "sent" else None)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert len(governor.history) == 1
    assert governor.history[0][0] == clock.monotonic()
    assert governor.used_airtime == item.estimated_toa
    assert governor._next_tx_at >= clock.monotonic() + governor.config.min_gap_s
    attempt = (await database.read("SELECT * FROM outbound_attempt"))[0]
    if outcome == "cancelled":
        assert attempt["state"] == "started"  # Recovery conservatively settles uncertainty.
    else:
        assert attempt["completed_at"] == clock.now().timestamp()
        assert attempt["state"] == ("sent" if outcome == "sent" else "uncertain")
    if outcome == "failed":
        row = (await database.read("SELECT next_attempt_at FROM outbound_work"))[0]
        assert row[0] == clock.now().timestamp() + 5
        assert item.next_attempt_at == clock.monotonic() + 5
        assert await governor.tick() is None

    # Recovery never drops or double-charges this attempt, including a backward wall step.
    clock.epoch -= timedelta(hours=6)
    recovered = production_governor(database, clock, link=radio)
    await recovered.recover()
    assert recovered.used_airtime == pytest.approx(item.estimated_toa, abs=0.001)
    assert len(recovered.history) == 1


@pytest.mark.parametrize("hours", [-6, 6])
async def test_wall_step_during_reservation_does_not_extend_monotonic_deadline(
    queue, monkeypatch, hours
):
    database, governor, clock, radio = queue
    item = packet("reservation")
    await governor.admit(item)

    def advance():
        clock.advance(301)
        clock.epoch += timedelta(hours=hours)

    delay_once(monkeypatch, governor, radio, "writer", advance)
    assert await governor.tick() is None
    assert not radio.sent
    assert not await database.read("SELECT * FROM outbound_attempt")
    assert (await database.read("SELECT state FROM outbound_work"))[0][0] == "expired"


async def test_overlapping_ticks_do_not_cross_pacing_boundary(queue, monkeypatch):
    _, governor, clock, radio = queue
    first = OutboundItem("first", "^all", 0, TrafficClass.REPLY)
    second = OutboundItem("second", "^all", 0, TrafficClass.REPLY)
    await governor.admit_many([first, second])
    original = radio._send_text
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(radio, "_send_text", slow)
    one = asyncio.create_task(governor.tick())
    two = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        two = asyncio.create_task(governor.tick())
        await asyncio.sleep(0)
        clock.advance(120)
        release.set()
        assert await one is first
        assert await two is None
        assert [sent.text for sent in radio.sent] == ["first"]
    finally:
        release.set()
        tasks = [task for task in (one, two) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("delay", [17, 301])
async def test_actual_writer_contention_uses_post_acquisition_time(queue, monkeypatch, delay):
    database, governor, clock, radio = queue
    item = packet("reservation")
    await governor.admit(item)
    governor._next_outbox_sweep_at = float("inf")
    original = governor.outbox.start_attempt
    entered = asyncio.Event()

    async def selected(*args, **kwargs):
        entered.set()
        return await original(*args, **kwargs)

    monkeypatch.setattr(governor.outbox, "start_attempt", selected)
    task = None
    try:
        async with database.transaction():
            task = asyncio.create_task(governor.tick())
            await asyncio.wait_for(entered.wait(), 5)
            assert not task.done()
            clock.advance(delay)
        assert await task is (item if delay == 17 else None)
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    if delay == 17:
        assert (await database.read("SELECT started_at FROM outbound_attempt"))[0][0] == (
            clock.now().timestamp()
        )
    else:
        assert not radio.sent
        assert not await database.read("SELECT * FROM outbound_attempt")


@pytest.mark.parametrize(
    "gate", ["budget", "share", "quiet", "link", "power", "utilisation", "pacing", "profile"]
)
async def test_policy_changes_while_waiting_preserve_pending_work(queue, monkeypatch, gate):
    database, governor, clock, radio = queue
    item = packet("reservation", TrafficClass.DIGEST)
    await governor.admit(item)

    def change():
        if gate == "budget":
            governor.history.append((clock.monotonic(), 3600, TrafficClass.ALERT, Severity.INFO))
        elif gate == "share":
            governor.config.class_shares["digest"] = 0
        elif gate == "quiet":
            governor.config.quiet_hours.classes = ["digest"]
            governor.config.quiet_hours.start = "11:00"
            governor.config.quiet_hours.end = "13:00"
        elif gate == "link":
            radio._state = LinkState.DOWN
        elif gate == "power":
            governor.power_config = RadioPowerConfig(shed_discretionary=True)
            governor.battery_level = 1
        elif gate == "utilisation":
            governor.channel_utilisation = 100
        elif gate == "pacing":
            governor._next_tx_at = clock.monotonic() + 10
        else:
            governor.sync_radio_profile("SHORT_FAST", "US")

    delay_once(monkeypatch, governor, radio, "writer", change)
    assert await governor.tick() is None
    assert not radio.sent
    assert governor.queued_items() == [item]
    assert tuple((await database.read("SELECT state,attempts FROM outbound_work"))[0]) == (
        "pending",
        0,
    )
    assert not await database.read("SELECT * FROM outbound_attempt")


@pytest.mark.parametrize("hours", [-6, 6])
async def test_wall_step_alone_does_not_discard_live_monotonic_airtime(queue, monkeypatch, hours):
    _, governor, clock, radio = queue
    item = packet("reservation", TrafficClass.ALERT)
    await governor.admit(item)
    cost = governor.estimate_toa(item.payload_size)
    governor.history.append((clock.monotonic(), cost, TrafficClass.REPLY, Severity.INFO))

    def jump():
        clock.epoch += timedelta(hours=hours)

    delay_once(monkeypatch, governor, radio, "writer", jump)
    assert await governor.tick() is item
    assert governor.used_airtime == 2 * cost


async def test_expiry_after_invalid_payload_persistence_does_not_send_next_candidate(
    queue, monkeypatch
):
    database, governor, clock, radio = queue
    bad = OutboundItem("bad", "^all", 0, TrafficClass.ALERT)
    next_item = packet("reservation")
    await governor.admit_many([bad, next_item])
    bad.binary_payload = b"x" * 999
    original = governor.outbox.fail_unstarted

    async def delayed(*args, **kwargs):
        await original(*args, **kwargs)
        clock.advance(301)

    monkeypatch.setattr(governor.outbox, "fail_unstarted", delayed)
    assert await governor.tick() is None
    assert not radio.sent
    assert not governor.queued_items()
    assert [r[0] for r in await database.read("SELECT state FROM outbound_work ORDER BY id")] == [
        "failed",
        "expired",
    ]


@pytest.mark.parametrize("outcome", ["sent", "failed"])
async def test_long_radio_call_retains_cost_after_start_leaves_one_hour_window(
    queue, monkeypatch, outcome
):
    database, governor, clock, radio = queue
    item = OutboundItem("long call", "^all", 0, TrafficClass.ALERT)
    await governor.admit(item)
    original = radio._send_text

    async def delayed(*args, **kwargs):
        clock.advance(3601)
        if outcome == "failed":
            raise OSError("synthetic uncertain outcome")
        return await original(*args, **kwargs)

    monkeypatch.setattr(radio, "_send_text", delayed)
    assert await governor.tick() is (item if outcome == "sent" else None)
    recovered = production_governor(database, clock, link=radio)
    await recovered.recover()
    assert recovered.used_airtime == pytest.approx(item.estimated_toa, abs=0.001)
    clock.advance(3599)
    assert recovered.used_airtime > 0
    clock.advance(1)
    assert recovered.used_airtime == 0


async def test_failure_writer_wait_sets_future_retry_from_current_time(queue, monkeypatch):
    database, governor, clock, radio = queue
    item = packet("reservation")
    await governor.admit(item)

    async def fail(*args, **kwargs):
        clock.advance(10)
        raise OSError("synthetic uncertain outcome")

    original = Transaction.read

    async def read(self, sql, params=()):
        result = await original(self, sql, params)
        if sql.startswith("SELECT attempts,expires_at FROM outbound_work"):
            clock.advance(100)
        return result

    monkeypatch.setattr(radio, "_send_text", fail)
    monkeypatch.setattr(Transaction, "read", read)
    assert await governor.tick() is None
    row = (await database.read("SELECT next_attempt_at FROM outbound_work"))[0]
    assert row[0] == clock.now().timestamp() + 5
    assert item.next_attempt_at == clock.monotonic() + 5


async def test_volatile_queue_also_rechecks_expiry_after_telemetry(monkeypatch):
    clock = VirtualClock()
    radio = SimulatedRadioLink(clock)
    await radio.connect()
    governor = AirtimeGovernor(radio, AirtimeConfig(), clock)
    governor.enqueue(packet("telemetry"))
    delay_once(monkeypatch, governor, radio, "telemetry", lambda: clock.advance(301))
    assert await governor.tick() is None
    assert not radio.sent
    assert not governor.queued_items()


async def test_accounting_index_upgrades_existing_attempts_without_changing_them(tmp_path):
    path = tmp_path / "upgrade.db"
    database = Database(path)
    await database.open()
    try:
        clock = VirtualClock()
        radio = SimulatedRadioLink(clock)
        await radio.connect()
        governor = production_governor(database, clock, link=radio)
        await governor.admit(packet("reservation"))
        await governor.tick()
        before = [tuple(r) for r in await database.read("SELECT * FROM outbound_attempt")]
        await database.write("DROP INDEX idx_outbound_attempt_accounting")
        await database.write("DELETE FROM schema_version WHERE version=181")
    finally:
        await database.close()
    upgraded = Database(path)
    await upgraded.open()
    try:
        assert [tuple(r) for r in await upgraded.read("SELECT * FROM outbound_attempt")] == before
        plan = await upgraded.read(
            "EXPLAIN QUERY PLAN SELECT id FROM outbound_attempt "
            "WHERE state IN ('sent','uncertain') "
            "AND MAX(started_at,COALESCE(completed_at,started_at))>?",
            (0,),
        )
        assert any("SEARCH" in r[3] and "idx_outbound_attempt_accounting" in r[3] for r in plan)
        assert len(await OutboxStore(upgraded).recent_airtime(clock.now().timestamp())) == 1
    finally:
        await upgraded.close()


@pytest.mark.parametrize("multipart", [False, True])
async def test_recovery_preserves_dispatch_gap(queue, multipart):
    database, governor, clock, radio = queue
    governor.config.interpart_delay_s = 30
    item = OutboundItem("first", "^all", 0, TrafficClass.REPLY, multipart=multipart)
    next_item = OutboundItem("second", "^all", 0, TrafficClass.REPLY)
    await governor.admit_many([item, next_item])
    assert await governor.tick() is item
    recovered = production_governor(database, clock, link=radio, airtime=governor.config)
    await recovered.recover()
    assert await recovered.tick() is None
    assert len(radio.sent) == 1
    clock.advance(recovered._next_tx_at - clock.monotonic())
    assert (await recovered.tick()).item_id == next_item.item_id
    assert len(radio.sent) == 2


async def test_cancellation_during_expiry_sweep_does_not_break_the_queue(queue, monkeypatch):
    database, governor, clock, radio = queue
    first = OutboundItem("first", "^all", 0, TrafficClass.REPLY)
    second = OutboundItem("second", "^all", 0, TrafficClass.REPLY)
    await governor.admit_many([first, second])
    clock.advance(301)
    original = governor.outbox.expire

    async def expire(item_id, now):
        await original(item_id, now)
        if item_id == first.item_id:
            await governor.cancel_work(second.item_id)

    monkeypatch.setattr(governor.outbox, "expire", expire)
    assert await governor.tick() is None
    assert not radio.sent
    assert not governor.queued_items()
    assert [r[0] for r in await database.read("SELECT state FROM outbound_work ORDER BY id")] == [
        "expired",
        "cancelled",
    ]
