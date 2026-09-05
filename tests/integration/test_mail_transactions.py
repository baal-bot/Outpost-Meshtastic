import asyncio
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from outpost.bbs.mail import MailService
from outpost.clock import VirtualClock
from outpost.store import Database
from outpost.store.members import MemberRepo
from outpost.web.operator_inbox import OperatorInboxService


async def participants(database: Database):
    clock = VirtualClock()
    members = MemberRepo(database, clock)
    dana = await members.resolve("!00000001")
    dana = await members.claim_handle(dana.mesh_id, "dana")
    ray = await members.resolve("!00000002")
    ray = await members.claim_handle(ray.mesh_id, "ray")
    return clock, members, dana, ray


async def test_concurrent_sends_and_replies_keep_unique_ids_and_conversations(tmp_path) -> None:
    database = Database(tmp_path / "mail.db")
    await database.open()
    try:
        clock, members, dana, ray = await participants(database)
        services = [MailService(database, members, clock, "local") for _ in range(2)]
        opening = await services[0].send(dana, "ray", "Opening", subject="Coordination")
        results = await asyncio.gather(
            *(services[i % 2].send(dana, "ray", f"Independent message {i}") for i in range(12)),
            *(
                services[i % 2].reply(ray, opening, "dana", f"Threaded reply {i}")
                for i in range(12)
            ),
            return_exceptions=True,
        )
        assert not [value for value in results if isinstance(value, BaseException)], results
        rows = await database.read("SELECT * FROM mail ORDER BY id")
        assert len(rows) == len({row["uid"] for row in rows}) == 25
        assert len(set(results[:12])) == 12
        for row in rows:
            assert row["uid"] == f"local:{row['id']}"
            if row["in_reply_to"] is None:
                assert row["conversation_key"] == f"local:{row['uid']}"
                assert row["from_id"] == dana.id and row["to_id"] == ray.id
            else:
                assert row["in_reply_to"] == opening
                assert row["conversation_key"] == rows[0]["conversation_key"]
                assert row["subject"] == "Coordination"
                assert row["participant_handle"] == "ray"
                assert row["operator_actor"] == "member:@ray"
                assert row["from_id"] == ray.id and row["to_id"] == dana.id
        detail = await OperatorInboxService(database).open(rows[0]["conversation_key"])
        assert detail is not None and len(detail["messages"]) == 13
    finally:
        await database.close()


@pytest.mark.parametrize("boundary", [1, 2])
@pytest.mark.parametrize("cancellation", [False, True])
@pytest.mark.parametrize("reply", [False, True])
async def test_interrupted_send_or_reply_is_atomic_and_does_not_poison_future_mail(
    tmp_path, monkeypatch, boundary: int, cancellation: bool, reply: bool
) -> None:
    database = Database(tmp_path / "mail.db")
    await database.open()
    try:
        clock, members, dana, ray = await participants(database)
        service = MailService(database, members, clock, "local")
        opening = await service.send(dana, "ray", "Opening")
        before = [dict(row) for row in await database.read("SELECT * FROM mail ORDER BY id")]
        original = database._writer_write
        calls = 0

        async def interrupted(sql: str, params: Sequence[Any] = ()) -> int:
            nonlocal calls
            result = await original(sql, params)
            calls += 1
            if calls == boundary:
                if cancellation:
                    task = asyncio.current_task()
                    assert task is not None
                    task.cancel()
                    await asyncio.sleep(0)
                raise RuntimeError("injected mail interruption")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(database, "_writer_write", interrupted)
            work = (
                service.reply(ray, opening, "dana", "Interrupted")
                if reply
                else service.send(dana, "ray", "Interrupted")
            )
            error = asyncio.CancelledError if cancellation else RuntimeError
            with pytest.raises(error):
                await asyncio.create_task(work)
        assert calls == boundary
        assert [
            dict(row) for row in await database.read("SELECT * FROM mail ORDER BY id")
        ] == before
        await service.send(dana, "ray", "After interruption")
    finally:
        await database.close()
    reopened = Database(database.path)
    await reopened.open()
    try:
        service = MailService(reopened, MemberRepo(reopened, clock), clock, "local")
        await service.send(dana, "ray", "After reopen")
        await service.reply(ray, opening, "dana", "Reply after reopen")
        rows = await reopened.read("SELECT * FROM mail ORDER BY id")
        assert [row["body"] for row in rows] == [
            "Opening",
            "After interruption",
            "After reopen",
            "Reply after reopen",
        ]
        assert all(row["uid"] == f"local:{row['id']}" for row in rows)
        assert rows[-1]["in_reply_to"] == opening
        assert rows[-1]["conversation_key"] == rows[0]["conversation_key"]
    finally:
        await reopened.close()


async def test_pending_mail_is_never_visible_to_other_readers(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "mail.db")
    await database.open()
    paused, release = asyncio.Event(), asyncio.Event()
    task = None
    try:
        clock, members, dana, _ = await participants(database)
        original = database._writer_write
        calls = 0

        async def pause_before_final_uid(sql: str, params: Sequence[Any] = ()) -> int:
            nonlocal calls
            calls += 1
            if calls == 2:
                paused.set()
                await release.wait()
            return await original(sql, params)

        monkeypatch.setattr(database, "_writer_write", pause_before_final_uid)
        task = asyncio.create_task(
            MailService(database, members, clock, "local").send(
                dana, "ray", "Private until committed"
            )
        )
        await asyncio.wait_for(paused.wait(), timeout=5)
        assert await database.read("SELECT * FROM mail") == []
        release.set()
        mail_id = await task
        row = (await database.read("SELECT uid,conversation_key FROM mail"))[0]
        assert dict(row) == {
            "uid": f"local:{mail_id}",
            "conversation_key": f"local:local:{mail_id}",
        }
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await database.close()


@pytest.mark.parametrize("conversation_key", [None, "legacy:pending", "local:existing-thread"])
async def test_legacy_pending_mail_migration_preserves_content_routes_and_reply_links(
    tmp_path, conversation_key: str | None
) -> None:
    path = tmp_path / "legacy.db"
    migrations = Path(__file__).parents[2] / "src/outpost/store/migrations"
    # Start from the real pre-fix schema, not a fixture that already ran the repair.
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute("PRAGMA journal_mode=WAL")
        for migration in sorted(migrations.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            version = int(migration.name[:4])
            if version > 172:
                break
            connection.executescript(migration.read_text())
            connection.execute(
                "INSERT INTO schema_version(version,applied_at) VALUES(?,1)", (version,)
            )
        connection.execute(
            "INSERT INTO mail(id,uid,from_label,to_label,subject,body,created_at,state,"
            "expires_at,conversation_key,participant_handle,operator_actor) "
            "VALUES(1,'pending','dana','ray','Supply request','Keep this content',1,'queued',"
            "9999999999,?,'ray','member:@dana')",
            (conversation_key,),
        )
        connection.execute(
            "INSERT INTO mail(id,uid,from_label,to_label,body,created_at,state,expires_at,"
            "conversation_key,in_reply_to,reply_peer_mesh_id,federation_conversation_id) "
            "VALUES(2,'fed:existing','operator@REMOTE','operator','Existing reply',2,'delivered',"
            "9999999999,?,1,'!aaaaaaaa','remote-conversation')",
            (conversation_key,),
        )
        connection.commit()
        connection.row_factory = sqlite3.Row
        before = [dict(row) for row in connection.execute("SELECT * FROM mail ORDER BY id")]
    finally:
        connection.close()
    database = Database(path)
    await database.open()
    try:
        rows = [dict(row) for row in await database.read("SELECT * FROM mail ORDER BY id")]
        recovered_uid = rows[0]["uid"]
        assert recovered_uid.startswith("recovered:mail:")
        before[0]["uid"] = recovered_uid
        before[0]["conversation_key"] = conversation_key or f"local:{recovered_uid}"
        assert rows == before
        # Reapplying the data repair and reopening must not change recovered identity.
        async with database.transaction() as transaction:
            for statement in (migrations / "0173_recover_pending_mail.sql").read_text().split(";"):
                if statement.strip():
                    await transaction.write(statement)
        assert [dict(row) for row in await database.read("SELECT * FROM mail ORDER BY id")] == rows
        clock, members, dana, _ = await participants(database)
        service = MailService(database, members, clock, "local")
        await service.send(dana, "newperson", "New mail after migration")
        ray = await members.by_handle("ray")
        assert ray is not None and await service.bind_handle(ray) == 1
        await service.reply(ray, 1, "dana", "Reply to recovered mail")
        detail = await OperatorInboxService(database).open(before[0]["conversation_key"])
        assert detail is not None
        assert any(message["body"] == "Keep this content" for message in detail["messages"])
        reply = (await database.read("SELECT * FROM mail WHERE body='Reply to recovered mail'"))[0]
        assert reply["conversation_key"] == before[0]["conversation_key"]
        assert reply["in_reply_to"] == 1
    finally:
        await database.close()
    reopened = Database(path)
    await reopened.open()
    try:
        assert (await reopened.read("SELECT uid FROM mail WHERE id=1"))[0]["uid"] == recovered_uid
        assert (await reopened.read("PRAGMA integrity_check"))[0][0] == "ok"
        assert await reopened.read("PRAGMA foreign_key_check") == []
    finally:
        await reopened.close()
