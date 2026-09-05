import asyncio
import copy
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.fed import MessageType
from outpost.fed.reconciliation import MAX_ROUNDS, Reconciliation
from outpost.fed.revisions import CAPABILITY, MODE, SCAN_LIMIT
from outpost.store import Database
from outpost.transport.models import InboundMessage
from outpost.transport.simulated import SimulatedRadioLink


@pytest.fixture
async def nodes(tmp_path):
    apps = []

    async def make(name="local", *, offset=0, budget=20, capture=True):
        clock = VirtualClock(epoch=datetime(2026, 1, 1, 12, tzinfo=UTC) + timedelta(seconds=offset))
        config = Config.model_validate(
            {
                "store": {"path": str(tmp_path / f"{name}.db")},
                "modules": {"fed": {"enabled": True}, "watch": {"enabled": True}},
                "fed": {"max_items_per_cycle": budget},
                # Isolate cursor skew from the deliberately wall-clock-based quiet
                # schedule; governor policy/quiet-hours coverage remains separate.
                "airtime": {"dedupe_window_s": 0, "quiet_hours": {"classes": []}},
            }
        )
        app = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock, node_id=f"!{name}"))
        await app.database.open()
        await app.radio.connect()
        app.federation.local_mesh_id = f"!{name}"
        app.federation_sync.local_mesh_id = f"!{name}"
        app.incidents.origin_node = f"!{name}"
        remote = "!remote" if name == "local" else "!local"
        peer = await app.federation.discover(remote, remote, 1, {CAPABILITY: MODE}, "radio")
        await app.database.write(
            "UPDATE fed_peer SET state='active',shared_secret=?,boards='[\"gen\"]',"
            "sync_incidents=1,relay_alerts=1,quota_items_per_hour=100,"
            "local_approved=1,remote_approved=1 WHERE id=?",
            (bytes(range(32)), peer.id),
        )
        await app.database.write("UPDATE board SET federated=1 WHERE slug='gen'")
        peer = await app.federation.by_mesh_id(remote)
        queued = []
        if capture:

            async def control(_peer, kind, value):
                queued.append((kind, copy.deepcopy(value)))
                return True

            app._queue_federation_control = control
        apps.append(app)
        return app, peer, queued

    yield make
    for app in reversed(apps):
        await app.ai_service.close()
        await app.database.close()


async def checkpoint(app, peer):
    rows = await app.database.read(
        "SELECT cursor FROM fed_cursor WHERE peer_id=? AND stream='_reconcile'", (peer.id,)
    )
    return json.loads(rows[0]["cursor"])


async def report(app, title="road sample obstruction"):
    incident, _ = await app.incidents.create(title, None, force=True)
    assert incident is not None
    return incident


async def transfer_request(source, source_peer, target, target_peer, kind, request):
    if kind is MessageType.SYNC_REQ:
        page = await source.federation_sync.revisions.page(source_peer, request)
        await target._handle_sync_manifest(source.radio.local_node_id, page)
        return page
    assert kind is MessageType.ITEM_REQ
    items = await source.federation_sync.revisions.export(source_peer, request)
    for item in items:
        await target.federation_reconciliation.receive(target_peer, item)
    return items


async def drain(source, source_peer, target, target_peer, queued):
    rounds = 0
    while queued:
        rounds += 1
        assert rounds <= 100, "reconciliation did not terminate"
        kind, request = queued.pop(0)
        await transfer_request(source, source_peer, target, target_peer, kind, request)


@pytest.mark.parametrize("offset", [-21600, 21600])
@pytest.mark.parametrize("step", [-21600, 21600])
async def test_clock_skew_and_step_do_not_skip_incident_updates(nodes, offset, step):
    source, source_peer, _ = await nodes("remote", offset=offset)
    target, target_peer, queued = await nodes()
    incident = await report(source)
    await target._federation_sync_once()
    first_request = queued[0][1]
    assert first_request["snapshot"] is None and first_request["after"] == 0
    await transfer_request(source, source_peer, target, target_peer, *queued.pop(0))
    assert (await checkpoint(target, target_peer))["page"] is not None
    source.clock.epoch += timedelta(seconds=step)
    target.clock.epoch -= timedelta(seconds=step)
    await drain(source, source_peer, target, target_peer, queued)
    state = await checkpoint(target, target_peer)
    assert state["status"] == "complete"
    inbox = (await target.database.read("SELECT id FROM fed_inbox_item"))[0]["id"]
    await target.federation_sync.import_inbox(inbox, "operator:test", 1)

    stamp = int(source.clock.now().timestamp())
    await source.database.write(
        "UPDATE incident SET title='Road corrected',updated_at=? WHERE id=?", (stamp, incident.id)
    )
    target.clock.advance(target.config.fed.sync_interval_minutes * 60)
    await target._federation_sync_once()
    assert queued[0][1]["after"] == state["snapshot"]
    assert queued[0][1]["snapshot"] is None
    await drain(source, source_peer, target, target_peer, queued)
    await target.federation_sync.import_inbox(inbox, "operator:test", 2)
    imported = (await target.database.read("SELECT uid,title,updated_at FROM incident"))[0]
    assert imported["title"] == "Road corrected" and imported["updated_at"] == stamp
    assert imported["uid"].startswith("!remote:")
    origin = (await target.database.read("SELECT source_revision FROM incident_origin"))[0]
    assert origin["source_revision"] > 0


async def test_page_waits_for_durable_receipts_and_restarts_without_skipping(nodes):
    source, sp, _ = await nodes("remote")
    target, tp, queued = await nodes(budget=10)
    for i in range(13):
        await report(source, f"road obstruction {i}")
    await target._federation_sync_once()
    kind, request = queued.pop(0)
    page = await transfer_request(source, sp, target, tp, kind, request)
    state = await checkpoint(target, tp)
    assert state["after"] == 0 and state["used"] == 8
    assert queued[0][0] is MessageType.ITEM_REQ
    # Duplicate manifest cannot use a second page budget or advance the cursor.
    await target._handle_sync_manifest("!remote", page)
    assert (await checkpoint(target, tp))["used"] == 8
    queued.clear()  # Simulated packet loss, including all fetches.
    await target.database.close()
    target.database = Database(target.database.path)
    await target.database.open()
    target.federation.database = target.database
    target.federation_sync.database = target.database
    target.federation_sync.revisions.database = target.database
    target.federation_reconciliation = Reconciliation(target)
    target.clock.epoch -= timedelta(hours=6)
    await target._federation_sync_once()
    assert queued[0][0] is MessageType.ITEM_REQ
    assert queued[0][1]["cycle"] == state["cycle"]
    await drain(source, sp, target, tp, queued)
    truncated = await checkpoint(target, tp)
    assert truncated["status"] == "truncated" and truncated["used"] == 10
    assert len(await target.database.read("SELECT id FROM fed_inbox_item")) == 10
    target.clock.advance(target.config.fed.sync_interval_minutes * 60)
    await target._federation_sync_once()
    assert queued[0][1]["after"] == truncated["after"]
    assert queued[0][1]["snapshot"] == truncated["snapshot"]
    assert queued[0][1]["cycle"] != state["cycle"]
    await target._handle_sync_manifest("!remote", page)  # Delayed prior-cycle page.
    assert (await checkpoint(target, tp))["used"] == 0
    await drain(source, sp, target, tp, queued)
    assert (await checkpoint(target, tp))["status"] == "complete"
    assert len(await target.database.read("SELECT id FROM fed_inbox_item")) == 13


async def test_concurrent_edits_move_to_next_watermark_and_newer_fetches_are_idempotent(nodes):
    source, sp, _ = await nodes("remote")
    target, tp, queued = await nodes()
    incidents = [await report(source, f"road obstruction {i}") for i in range(12)]
    await target._federation_sync_once()
    kind, request = queued.pop(0)
    page = await transfer_request(source, sp, target, tp, kind, request)
    # One advertised item and one not-yet-advertised item change under the cycle.
    for incident in (incidents[0], incidents[-1]):
        await source.database.write(
            "UPDATE incident SET title='Changed' WHERE id=?", (incident.id,)
        )
    await drain(source, sp, target, tp, queued)
    first = await checkpoint(target, tp)
    assert first["status"] == "complete"
    target.clock.advance(target.config.fed.sync_interval_minutes * 60)
    await target._federation_sync_once()
    await drain(source, sp, target, tp, queued)
    rows = await target.database.read("SELECT payload_json FROM fed_inbox_item")
    assert len(rows) == 12
    assert sum(json.loads(row["payload_json"])["title"] == "Changed" for row in rows) == 2
    await target._handle_sync_manifest("!remote", page)
    assert (await checkpoint(target, tp))["status"] == "complete"


async def test_older_equal_conflicting_and_legacy_payloads_cannot_replace_a_revision(nodes):
    source, sp, _ = await nodes("remote")
    target, tp, _ = await nodes()
    await report(source)
    page = await source.federation_sync.revisions.page(sp, {"cycle": "a" * 32})
    entry = page["items"][0]
    request = {**page, "items": [{"stream": entry["s"], "uid": entry["u"], "revision": entry["r"]}]}
    item = (await source.federation_sync.revisions.export(sp, request))[0]
    await target.federation_sync.quarantine(tp, item, 1)
    await target.federation_sync.import_inbox(1, "operator:test", 2)
    newer = copy.deepcopy(item)
    newer["revision"] += 2
    newer["payload"]["title"] = "Newer"
    newer["payload"]["updated_at"] -= 21600
    assert await target.federation_sync.quarantine(tp, newer, 3)
    assert not await target.federation_sync.quarantine(tp, item, 4)
    assert not await target.federation_sync.quarantine(tp, newer, 4)
    conflict = copy.deepcopy(newer)
    conflict["payload"]["title"] = "Conflicting"
    with pytest.raises(ValueError, match="same producer revision"):
        await target.federation_sync.quarantine(tp, conflict, 5)
    legacy = {key: value for key, value in newer.items() if key not in {"epoch", "revision"}}
    assert not await target.federation_sync.quarantine(tp, legacy, 6)
    legacy = copy.deepcopy(legacy)
    legacy["payload"]["title"] = "Old legacy body"
    with pytest.raises(ValueError, match="lineage"):
        await target.federation_sync.quarantine(tp, legacy, 6)
    await target.federation_sync.import_inbox(1, "operator:test", 7)
    assert (await target.database.read("SELECT title FROM incident"))[0]["title"] == "Newer"
    # Retention of reviewed bodies does not erase anti-rollback receipts.
    await target.database.write("DELETE FROM fed_inbox_item")
    assert not await target.federation_sync.quarantine(tp, item, 8)


@pytest.mark.parametrize("mutation", ["delete", "merge", "area", "disabled"])
async def test_item_leaving_export_scope_does_not_stall_or_leak_new_location(nodes, mutation):
    source, sp, _ = await nodes("remote")
    target, tp, queued = await nodes()
    incident = await report(source)
    await target._federation_sync_once()
    await transfer_request(source, sp, target, tp, *queued.pop(0))
    if mutation == "delete":
        await source.database.write("DELETE FROM incident WHERE id=?", (incident.id,))
    elif mutation == "merge":
        other = await report(source, "fire unrelated")
        await source.database.write(
            "UPDATE incident SET merged_into_id=? WHERE id=?", (other.id, incident.id)
        )
    elif mutation == "area":
        await source.database.write("UPDATE incident SET lat=30,lon=30 WHERE id=?", (incident.id,))
    else:
        source.config.modules.watch.enabled = False
        # Scope changes require a reset, not an unbounded retry of old requests.
        with pytest.raises(ValueError, match="scope"):
            await transfer_request(source, sp, target, tp, *queued.pop(0))
        return
    items = await transfer_request(source, sp, target, tp, *queued.pop(0))
    assert items[0]["unavailable"] is True and "payload" not in items[0]
    assert (await checkpoint(target, tp))["status"] == "complete"
    assert await target.database.read("SELECT id FROM fed_inbox_item") == []


async def test_empty_filtered_pages_are_bounded_and_use_an_index(nodes):
    source, sp, _ = await nodes("remote")
    for i in range(SCAN_LIMIT + 3):
        await source.database.write(
            "INSERT INTO fed_revision(stream,uid) VALUES('board:private',?)", (f"private-{i}",)
        )
    incident = await report(source)
    first = await source.federation_sync.revisions.page(sp, {"cycle": "a" * 32})
    assert first["items"] == [] and first["done"] is False
    assert first["next"] == SCAN_LIMIT
    second = await source.federation_sync.revisions.page(sp, {**first, "after": first["next"]})
    assert second["done"] is True and len(second["items"]) == 1
    assert second["items"][0]["u"].endswith(incident.uid)
    plan = await source.database.read(
        "EXPLAIN QUERY PLAN SELECT revision,stream,uid FROM fed_revision "
        "WHERE revision>? AND revision<=? ORDER BY revision LIMIT ?",
        (0, 100, 8),
    )
    assert any("INTEGER PRIMARY KEY" in row["detail"] for row in plan)
    assert not any("TEMP B-TREE" in row["detail"] for row in plan)


@pytest.mark.parametrize(
    "bad",
    [
        {"epoch": "bad"},
        {"next": -1},
        {"next": True},
        {"after": -1},
        {"items": {}},
        {"items": [{"s": "incidents", "u": "!remote:x", "r": True, "d": "a" * 16}]},
        {"items": [{"s": "board:private", "u": "!remote:x", "r": 1, "d": "a" * 16}]},
        {"items": [{"s": "incidents", "u": "", "r": 1, "d": "a" * 16}]},
        {"items": [{"s": "incidents", "u": "!remote:x", "r": 1, "d": "bad"}]},
        {"items": [{"s": "incidents", "u": "!remote:x", "r": 1, "d": "a" * 16}] * 9},
        {"done": "true"},
        {"done": False, "next": 0},
    ],
)
async def test_invalid_page_cannot_advance_checkpoint_or_expand_local_budget(nodes, bad):
    source, sp, _ = await nodes("remote")
    target, tp, queued = await nodes()
    await report(source)
    await target._federation_sync_once()
    before = await checkpoint(target, tp)
    page = await source.federation_sync.revisions.page(sp, queued.pop(0)[1])
    with pytest.raises(ValueError):
        await target._handle_sync_manifest("!remote", {**page, **bad, "remaining": 1000000})
    assert await checkpoint(target, tp) == before
    assert queued == []


async def test_scope_resets_consume_local_round_budget(nodes):
    target, tp, queued = await nodes(budget=10)
    await target._federation_sync_once()
    for i in range(MAX_ROUNDS):
        request = queued.pop(0)[1]
        await target._handle_sync_manifest(
            "!remote",
            {
                "mode": MODE,
                "cycle": request["cycle"],
                "epoch": "a" * 32,
                "scope": f"{i:016x}",
                "reset": True,
            },
        )
    state = await checkpoint(target, tp)
    assert state["status"] == "truncated" and state["rounds"] == MAX_ROUNDS
    assert queued == []
    await target._federation_sync_once()
    assert queued == []


async def test_new_lineage_blocks_and_unauthenticated_capability_loss_cannot_downgrade(nodes):
    source, sp, _ = await nodes("remote")
    target, tp, queued = await nodes()
    await report(source)
    await target._federation_sync_once()
    await drain(source, sp, target, tp, queued)
    old = await checkpoint(target, tp)
    await target.federation.discover("!remote", "Remote", 1, {}, "radio")
    target.clock.advance(target.config.fed.sync_interval_minutes * 60)
    await target._federation_sync_once()
    request = queued.pop(0)[1]
    assert request["mode"] == MODE and request["after"] == old["after"]
    await target._handle_sync_manifest(
        "!remote",
        {
            "mode": MODE,
            "cycle": request["cycle"],
            "epoch": "b" * 32,
            "scope": old["scope"],
            "reset": True,
        },
    )
    assert (await checkpoint(target, tp))["status"] == "blocked"
    target.clock.advance(86400)
    await target._federation_sync_once()
    assert queued == []


async def test_scope_change_during_fetch_returns_authenticated_reset_and_stops_old_requests(nodes):
    source, sp, source_queue = await nodes("remote")
    target, tp, queued = await nodes()
    await report(source)
    await target._federation_sync_once()
    await transfer_request(source, sp, target, tp, *queued.pop(0))
    kind, request = queued.pop(0)
    assert kind is MessageType.ITEM_REQ
    prior = await checkpoint(target, tp)
    source.config.modules.watch.enabled = False
    frames = source.federation_codec.encode(
        kind, {**request, "mesh_id": "!local"}, 1, bytes(range(32))
    )
    for i, frame in enumerate(frames, 1):
        await source._handle_federation_discovery(
            InboundMessage(i, "!local", "^all", 0, 260, False, None, frame, source.clock.now())
        )
    assert len(source_queue) == 1 and source_queue[0][0] is MessageType.SYNC_MANIFEST
    reset = source_queue[0][1]
    assert reset["reset"] is True
    await target._handle_sync_manifest("!remote", reset)
    replacement = await checkpoint(target, tp)
    assert replacement["cycle"] != prior["cycle"]
    assert replacement["used"] == prior["used"]
    assert replacement["rounds"] == prior["rounds"] + 1
    await drain(source, sp, target, tp, queued)
    assert (await checkpoint(target, tp))["status"] == "complete"
    assert await target.database.read("SELECT * FROM fed_inbox_item") == []


async def test_unupgraded_peer_keeps_explicit_legacy_wire_mode(nodes):
    source, sp, source_queue = await nodes("remote")
    target, tp, queued = await nodes()
    await target.federation.discover("!remote", "Legacy", 1, {}, "radio")
    await report(source)
    await target._federation_sync_once()
    kind, request = queued.pop(0)
    assert kind is MessageType.SYNC_REQ and "mode" not in request
    assert request["snapshot"] == int(target.clock.now().timestamp())
    frames = source.federation_codec.encode(
        kind, {**request, "mesh_id": "!local"}, 1, bytes(range(32))
    )
    for i, frame in enumerate(frames, 1):
        await source._handle_federation_discovery(
            InboundMessage(i, "!local", "^all", 0, 260, False, None, frame, source.clock.now())
        )
    assert len(source_queue) == 1
    page = source_queue[0][1]
    assert "mode" not in page and page["snapshot"] == request["snapshot"]
    assert set(page["items"][0]) == {"s", "u", "v", "d"}
    await target._handle_sync_manifest("!remote", page)
    assert (await checkpoint(target, tp)).get("mode") != MODE
    # This compatibility path intentionally does not claim the new clock-skew guarantee.


async def test_restored_older_producer_watermark_stops_for_recovery_review(nodes):
    source, sp, _ = await nodes("remote")
    target, tp, queued = await nodes()
    await report(source)
    await target._federation_sync_once()
    await drain(source, sp, target, tp, queued)
    # Model lost producer revision state after restoring an old full backup with
    # the same lineage. Do not reset or lower the receiver's receipt authority.
    await source.database.write("DELETE FROM fed_revision")
    await source.database.write("UPDATE sqlite_sequence SET seq=0 WHERE name='fed_revision'")
    target.clock.advance(target.config.fed.sync_interval_minutes * 60)
    await target._federation_sync_once()
    page = await transfer_request(source, sp, target, tp, *queued.pop(0))
    assert page["rollback"] is True
    state = await checkpoint(target, tp)
    assert state["status"] == "blocked" and "rollback" in state["reason"]
    assert queued == []


@pytest.mark.parametrize("boundary", [1, 2, 3, 4])
@pytest.mark.parametrize("cancel", [False, True])
async def test_revision_receipt_and_payload_roll_back_together(
    nodes, monkeypatch, boundary, cancel
):
    source, sp, _ = await nodes("remote")
    target, tp, _ = await nodes()
    await report(source)
    page = await source.federation_sync.revisions.page(sp, {"cycle": "a" * 32})
    entry = page["items"][0]
    item = (
        await source.federation_sync.revisions.export(
            sp,
            {**page, "items": [{"stream": entry["s"], "uid": entry["u"], "revision": entry["r"]}]},
        )
    )[0]
    real_write = target.database._writer_write
    writes = 0

    async def fail(sql, params=()):
        nonlocal writes
        result = await real_write(sql, params)
        writes += 1
        if writes == boundary:
            if cancel:
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            raise RuntimeError("injected revision receipt failure")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(target.database, "_writer_write", fail)
        task = asyncio.create_task(target.federation_sync.quarantine(tp, item, 1))
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await task
    assert await target.database.read("SELECT * FROM fed_inbox_item") == []
    assert await target.database.read("SELECT * FROM fed_revision_receipt") == []
    reopened = Database(target.database.path)
    await reopened.open()
    try:
        assert await reopened.read("SELECT * FROM fed_revision_receipt") == []
    finally:
        await reopened.close()
    assert await target.federation_sync.quarantine(tp, item, 2)


async def test_revision_migration_backfills_once_and_preserves_lineage_across_reopen(tmp_path):
    path = tmp_path / "pre-revisions.db"
    migrations = Path(__file__).parents[2] / "src/outpost/store/migrations"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute("PRAGMA journal_mode=WAL")
        for migration in sorted(migrations.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            version = int(migration.name[:4])
            if version > 174:
                break
            connection.executescript(migration.read_text())
            connection.execute(
                "INSERT INTO schema_version(version,applied_at) VALUES(?,1)", (version,)
            )
        connection.execute(
            "INSERT INTO incident(uid,local_ref,type,severity,title,reporter_label,origin_node,"
            "created_at,updated_at) "
            "VALUES('legacy',1,'road','caution','Legacy','Test','!local',1000000000,2000000000)"
        )
        connection.commit()
    finally:
        connection.close()
    database = Database(path)
    await database.open()
    try:
        head = dict((await database.read("SELECT * FROM fed_revision"))[0])
        assert head == {"revision": 1, "stream": "incidents", "uid": "legacy"}
        epoch = (await database.read("SELECT epoch FROM fed_revision_lineage"))[0]["epoch"]
        with pytest.raises(RuntimeError):
            async with database.transaction() as tx:
                await tx.write("UPDATE incident SET title='Rolled back',updated_at=10")
                assert (await tx.read("SELECT revision FROM fed_revision"))[0]["revision"] > 1
                raise RuntimeError("rollback")
        assert dict((await database.read("SELECT * FROM fed_revision"))[0]) == head
        await database.write("UPDATE incident SET title='Clock moved backwards',updated_at=10")
        revision = (await database.read("SELECT revision FROM fed_revision"))[0]["revision"]
        assert revision > 1
        await database.write("DELETE FROM incident")
        deleted = (await database.read("SELECT revision FROM fed_revision"))[0]["revision"]
        assert deleted > revision
    finally:
        await database.close()
    database = Database(path)
    await database.open()
    try:
        assert (await database.read("SELECT epoch FROM fed_revision_lineage"))[0]["epoch"] == epoch
        assert (await database.read("SELECT revision FROM fed_revision"))[0]["revision"] == deleted
        assert await database.read("SELECT * FROM incident") == []
    finally:
        await database.close()


async def test_post_edits_thread_subject_and_alert_cancellation_use_producer_revisions(nodes):
    source, sp, _ = await nodes("remote")
    target, tp, queued = await nodes()
    board = (await source.database.read("SELECT id FROM board WHERE slug='gen'"))[0]["id"]
    thread = await source.database.write(
        "INSERT INTO thread(uid,board_id,subject,origin_node,created_at,last_post_at) "
        "VALUES('thread',?,'Original subject','!remote',100000,100000)",
        (board,),
    )
    await source.database.write(
        "INSERT INTO post(uid,thread_id,seq,author_label,origin_node,body,created_at) "
        "VALUES('post',?,1,'Reporter','!remote','Original body',100000)",
        (thread,),
    )
    await source.database.write(
        "INSERT INTO alert(uid,severity,headline,source,channels,raised_by,raised_at) "
        "VALUES('alert','urgent','Original warning','operator','[]','operator',100000)"
    )

    async def sync_and_import():
        await target._federation_sync_once()
        await drain(source, sp, target, tp, queued)
        for row in await target.database.read(
            "SELECT id FROM fed_inbox_item WHERE state='pending'"
        ):
            await target.federation_sync.import_inbox(row["id"], "operator:test", 1)

    await sync_and_import()
    await source.database.write("UPDATE post SET body='Edited body',edited_at=100")
    await source.database.write("UPDATE thread SET subject='Corrected subject'")
    await source.database.write("UPDATE alert SET headline='Corrected warning',cancelled_at=101")
    target.clock.advance(target.config.fed.sync_interval_minutes * 60)
    await sync_and_import()
    assert (await target.database.read("SELECT body FROM post"))[0]["body"] == "Edited body"
    assert (await target.database.read("SELECT subject FROM thread"))[0][
        "subject"
    ] == "Corrected subject"
    assert (await target.database.read("SELECT cancelled_at FROM alert"))[0]["cancelled_at"] == 101
    assert (
        len(
            await target.database.read(
                "SELECT id FROM audit_log WHERE action='federation.revision_imported'"
            )
        )
        == 4
    )
    await source.database.write("DELETE FROM post")
    await source.database.write("DELETE FROM alert")
    target.clock.advance(target.config.fed.sync_interval_minutes * 60)
    await sync_and_import()
    assert (await checkpoint(target, tp))["status"] == "complete"


@pytest.mark.production_wiring
@pytest.mark.parametrize("offset", [-21600, 21600])
async def test_revision_protocol_crosses_real_framing_and_durable_outbox(nodes, offset):
    source, sp, _ = await nodes("remote", offset=offset, capture=False)
    target, tp, _ = await nodes(capture=False)
    await report(source)
    await report(target, "fire second community report")
    await source._federation_sync_once()
    await target._federation_sync_once()
    delivered = {id(source): 0, id(target): 0}
    frames = 0
    for _ in range(150):
        for sender, receiver in ((source, target), (target, source)):
            sender.clock.advance(3)
            await sender.governor.tick()
            while delivered[id(sender)] < len(sender.radio.sent):
                packet = sender.radio.sent[delivered[id(sender)]]
                delivered[id(sender)] += 1
                frames += 1
                assert packet.payload is not None and len(packet.payload) <= 188
                await receiver._handle_federation_discovery(
                    InboundMessage(
                        frames,
                        sender.radio.local_node_id,
                        "^all",
                        0,
                        receiver.config.radio.federation_portnum,
                        False,
                        None,
                        packet.payload,
                        receiver.clock.now(),
                    )
                )
        state = await checkpoint(target, tp)
        if state["status"] == "complete" and (await checkpoint(source, sp))["status"] == "complete":
            break
    assert state["status"] == "complete", (
        state,
        [
            dict(row)
            for row in await source.database.read("SELECT state,last_error FROM outbound_work")
        ],
        [
            dict(row)
            for row in await target.database.read("SELECT state,last_error FROM outbound_work")
        ],
    )
    assert frames > 3
    assert (
        len(await target.database.read("SELECT id FROM fed_inbox_item WHERE state='pending'")) == 1
    )
    assert len(await target.database.read("SELECT * FROM fed_revision_receipt")) == 1
    # Each side keeps its local report and quarantines the other; receipt is not approval.
    assert len(await target.database.read("SELECT * FROM incident")) == 1
    assert len(await source.database.read("SELECT * FROM fed_revision_receipt")) == 1
    assert (await checkpoint(source, sp))["status"] == "complete"
    assert (await target.database.read("SELECT COUNT(*) n FROM outbound_work WHERE state='sent'"))[
        0
    ]["n"] > 0
