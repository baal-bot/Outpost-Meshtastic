import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from outpost.bbs.admin import BBSAdmin
from outpost.bbs.service import BBSService
from outpost.clock import VirtualClock
from outpost.config import AirtimeConfig
from outpost.fed import FederationPeerService, FederationSyncService
from outpost.store import Database, Transaction
from outpost.store.members import MemberRepo
from outpost.transport.governor import AirtimeGovernor
from outpost.transport.simulated import SimulatedRadioLink
from outpost.watch import CheckinService


def inject_write_failure(monkeypatch: pytest.MonkeyPatch, failure_at: int) -> Any:
    original = Transaction.write
    calls = 0

    async def failing_write(transaction: Transaction, sql: str, params: Sequence[Any] = ()) -> int:
        nonlocal calls
        calls += 1
        if calls == failure_at:
            raise RuntimeError(f"injected write failure {failure_at}")
        return await original(transaction, sql, params)

    monkeypatch.setattr(Transaction, "write", failing_write)
    return original


@pytest.mark.asyncio
async def test_transaction_commits_or_rolls_back_as_one_unit(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    try:
        with pytest.raises(RuntimeError, match="rollback"):
            async with database.transaction() as transaction:
                await transaction.write(
                    "INSERT INTO kv(ns,k,v,updated_at) VALUES('test','one','1',1)"
                )
                await transaction.write(
                    "INSERT INTO kv(ns,k,v,updated_at) VALUES('test','two','2',1)"
                )
                assert await database.read("SELECT 1 FROM kv WHERE ns='test'") == []
                raise RuntimeError("rollback")
        assert await database.read("SELECT 1 FROM kv WHERE ns='test'") == []

        async with database.transaction() as transaction:
            await transaction.write("INSERT INTO kv(ns,k,v,updated_at) VALUES('test','one','1',1)")
            await transaction.write("INSERT INTO kv(ns,k,v,updated_at) VALUES('test','two','2',1)")
        assert len(await database.read("SELECT 1 FROM kv WHERE ns='test'")) == 2
    finally:
        await database.close()


@pytest.mark.parametrize("failure_at", range(1, 5))
@pytest.mark.asyncio
async def test_thread_and_opening_post_roll_back_at_every_write_boundary(
    tmp_path, monkeypatch, failure_at: int
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    member = await MemberRepo(database, VirtualClock()).resolve("!00000001")
    member = await MemberRepo(database, VirtualClock()).claim_handle(member.mesh_id, "dana")
    service = BBSService(database, VirtualClock(), "local")
    inject_write_failure(monkeypatch, failure_at)

    with pytest.raises(RuntimeError, match="injected write failure"):
        await service.create_thread("roads", "Atomic bridge report", member)

    assert await database.read("SELECT 1 FROM thread WHERE subject='Atomic bridge report'") == []
    assert await database.read("SELECT 1 FROM post WHERE body='Atomic bridge report'") == []
    await database.close()


@pytest.mark.parametrize("failure_at", range(1, 6))
@pytest.mark.asyncio
async def test_operator_thread_and_audit_roll_back_at_every_write_boundary(
    tmp_path, monkeypatch, failure_at: int
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    board_id = int((await database.read("SELECT id FROM board WHERE slug='gen'"))[0]["id"])
    admin = BBSAdmin(database, VirtualClock(), set())
    inject_write_failure(monkeypatch, failure_at)

    with pytest.raises(RuntimeError, match="injected write failure"):
        await admin.create_thread(board_id, "Atomic operator thread", "Opening post")

    assert await database.read("SELECT 1 FROM thread WHERE subject='Atomic operator thread'") == []
    assert await database.read("SELECT 1 FROM post WHERE body='Opening post'") == []
    assert await database.read("SELECT 1 FROM audit_log WHERE action='thread.create'") == []
    await database.close()


@pytest.mark.parametrize("failure_at", range(1, 4))
@pytest.mark.asyncio
async def test_reply_rolls_back_at_every_write_boundary(
    tmp_path, monkeypatch, failure_at: int
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    members = MemberRepo(database, VirtualClock())
    member = await members.resolve("!00000001")
    member = await members.claim_handle(member.mesh_id, "dana")
    service = BBSService(database, VirtualClock(), "local")
    thread = await service.create_thread("roads", "Opening report", member)
    inject_write_failure(monkeypatch, failure_at)

    with pytest.raises(RuntimeError, match="injected write failure"):
        await service.reply(thread.id, "Atomic reply", member)

    posts = await database.read("SELECT seq,body FROM post WHERE thread_id=?", (thread.id,))
    assert [dict(row) for row in posts] == [{"seq": 1, "body": "Opening report"}]
    count = (await database.read("SELECT post_count FROM thread WHERE id=?", (thread.id,)))[0]
    assert count["post_count"] == 1
    await database.close()


@pytest.mark.parametrize("failure_at", range(1, 5))
@pytest.mark.asyncio
async def test_operator_reply_and_audit_roll_back_at_every_write_boundary(
    tmp_path, monkeypatch, failure_at: int
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    board_id = int((await database.read("SELECT id FROM board WHERE slug='gen'"))[0]["id"])
    admin = BBSAdmin(database, VirtualClock(), set())
    thread_id = await admin.create_thread(board_id, "Opening", "Opening post")
    inject_write_failure(monkeypatch, failure_at)

    with pytest.raises(RuntimeError, match="injected write failure"):
        await admin.reply(thread_id, "Atomic operator reply")

    posts = await database.read("SELECT seq,body FROM post WHERE thread_id=?", (thread_id,))
    assert [dict(row) for row in posts] == [{"seq": 1, "body": "Opening post"}]
    assert not await database.read("SELECT 1 FROM audit_log WHERE action='post.create'")
    await database.close()


@pytest.mark.asyncio
async def test_concurrent_replies_allocate_unique_ordered_sequences(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    members = MemberRepo(database, VirtualClock())
    member = await members.resolve("!00000001")
    member = await members.claim_handle(member.mesh_id, "dana")
    service = BBSService(database, VirtualClock(), "local")
    thread = await service.create_thread("roads", "Opening report", member)

    replies = await asyncio.gather(
        *(service.reply(thread.id, f"Concurrent reply {number}", member) for number in range(20))
    )

    assert sorted(reply.seq for reply in replies) == list(range(2, 22))
    rows = await database.read("SELECT seq FROM post WHERE thread_id=? ORDER BY seq", (thread.id,))
    assert [row["seq"] for row in rows] == list(range(1, 22))
    thread_row = (await database.read("SELECT post_count FROM thread WHERE id=?", (thread.id,)))[0]
    assert thread_row["post_count"] == 21
    await database.close()


async def prepare_federated_item(database: Database) -> tuple[FederationSyncService, int]:
    peers = FederationPeerService(database, VirtualClock(), "!local")
    await peers.discover("!remote", "Remote", 1, {}, "radio")
    await database.write(
        "UPDATE fed_peer SET state='active',boards='[\"gen\"]' WHERE mesh_id='!remote'"
    )
    await database.write("UPDATE board SET federated=1 WHERE slug='gen'")
    peer = await peers.by_mesh_id("!remote")
    sync = FederationSyncService(database, "!local")
    await sync.quarantine(
        peer,
        {
            "stream": "board:gen",
            "uid": "!remote:post-1",
            "digest": "digest-1",
            "payload": {
                "thread_uid": "!remote:thread-1",
                "subject": "Remote thread",
                "author_label": "operator@remote",
                "body": "Remote opening",
                "created_at": 100,
            },
        },
        100,
    )
    item = (await database.read("SELECT id FROM fed_inbox_item WHERE uid='!remote:post-1'"))[0]
    return sync, int(item["id"])


@pytest.mark.parametrize("failure_at", range(1, 5))
@pytest.mark.asyncio
async def test_federation_import_and_inbox_state_roll_back_together(
    tmp_path, monkeypatch, failure_at: int
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    sync, item_id = await prepare_federated_item(database)
    inject_write_failure(monkeypatch, failure_at)

    with pytest.raises(RuntimeError, match="injected write failure"):
        await sync.import_inbox(item_id, "test", 101)

    assert await database.read("SELECT 1 FROM thread WHERE uid='!remote:thread-1'") == []
    state = (await database.read("SELECT state FROM fed_inbox_item WHERE id=?", (item_id,)))[0]
    assert state["state"] == "pending"
    await database.close()


@pytest.mark.parametrize("failure_at", range(1, 3))
@pytest.mark.asyncio
async def test_quarantine_and_receive_cursor_roll_back_together(
    tmp_path, monkeypatch, failure_at: int
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    peers = FederationPeerService(database, VirtualClock(), "!local")
    await peers.discover("!remote", "Remote", 1, {}, "radio")
    await database.write(
        "UPDATE fed_peer SET state='active',boards='[\"gen\"]' WHERE mesh_id='!remote'"
    )
    peer = await peers.by_mesh_id("!remote")
    sync = FederationSyncService(database, "!local")
    inject_write_failure(monkeypatch, failure_at)

    with pytest.raises(RuntimeError, match="injected write failure"):
        await sync.quarantine(
            peer,
            {
                "stream": "board:gen",
                "uid": "!remote:post-1",
                "digest": "digest-1",
                "payload": {"body": "test"},
            },
            100,
        )

    assert await database.read("SELECT 1 FROM fed_inbox_item") == []
    assert await database.read("SELECT 1 FROM fed_cursor WHERE direction='recv'") == []
    await database.close()


@pytest.mark.asyncio
async def test_federation_counters_are_atomic_under_concurrency(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    peers = FederationPeerService(database, VirtualClock(), "!local")
    await peers.discover("!remote", "Remote", 1, {}, "radio")
    await database.write("UPDATE fed_peer SET state='active' WHERE mesh_id='!remote'")

    counters = await asyncio.gather(*(peers.next_counter("!remote") for _ in range(20)))

    assert sorted(counters) == list(range(1, 21))
    await database.close()


@pytest.mark.parametrize("failure_at", range(1, 3))
@pytest.mark.asyncio
async def test_solicitation_rows_and_held_queue_roll_back_together(
    tmp_path, monkeypatch, failure_at: int
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    members = MemberRepo(database, clock)
    for number, handle in ((1, "dana"), (2, "ray")):
        member = await members.resolve(f"!{number:08x}")
        await members.claim_handle(member.mesh_id, handle)
    governor = AirtimeGovernor(SimulatedRadioLink(), AirtimeConfig(), clock)
    service = CheckinService(database, governor, clock)
    event = await service.open_event("Flood", "all", "operator")
    original = inject_write_failure(monkeypatch, failure_at)

    with pytest.raises(RuntimeError, match="injected write failure"):
        await service.solicit(event.id)

    assert await database.read("SELECT 1 FROM checkin_solicitation") == []
    assert governor.queued_items() == []
    monkeypatch.setattr(Transaction, "write", original)
    result = await service.solicit(event.id)
    assert result["recipient_count"] == 2
    assert len(governor.queued_items()) == 2
    await database.close()
