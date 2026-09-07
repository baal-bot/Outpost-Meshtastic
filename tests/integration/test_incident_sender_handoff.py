"""Atomic sender staging in temporary stores; no live radio or deployment."""

import asyncio
import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from outpost.fed.incident_events import content_digest
from outpost.fed.incident_handoff import MAX_PAGE
from outpost.fed.sync import FederationSyncService
from outpost.store import Database, Transaction
from outpost.store.backups import BackupService
from tests.integration.test_federation_incident_events import negotiate
from tests.integration.test_federation_incident_notes import exported, import_parent, source_note
from tests.integration.test_federation_revisions import nodes as nodes
from tests.integration.test_federation_revisions import report

pytestmark = pytest.mark.production_wiring


async def setup(nodes, *, notes=True):
    app, peer, _ = await nodes(notes=notes)
    peer = await negotiate(app, peer)
    return app, peer, app.federation_sync.incident_handoff


async def state(app):
    return {
        table: [dict(row) for row in await app.database.read(f"SELECT * FROM {table}")]  # noqa: S608 -- fixed table names
        for table in ("fed_incident_intent", "fed_incident_handoff", "incident_change_event")
    }


async def test_handoff_binds_versions_and_note_parent_without_consuming_or_sending(nodes):
    app, peer, handoff = await setup(nodes)
    incident, note = await source_note(app, peer)
    before = await app.incidents.change_events.pending()
    page = await handoff.stage(peer.id)
    assert page.scanned == page.staged == 2 and page.skipped == 0 and not page.remaining
    intents = await handoff.pending(peer.id)
    for item in intents:
        source = await exported(app, peer, item["stream"], item["uid"])
        assert (item["epoch"], item["revision"], item["digest"]) == (
            source["epoch"],
            source["revision"],
            content_digest(source["payload"]),
        )
        assert item["state"] == "pending"
    assert (
        next(i for i in intents if i["stream"] == "incident_updates")["parent_uid"]
        == note["payload"]["incident_uid"]
    )
    assert next(i for i in intents if i["stream"] == "incidents")["uid"] == incident.uid
    assert await app.incidents.change_events.pending() == before
    assert (await handoff.stage(peer.id)).scanned == 0
    assert not await app.database.read("SELECT * FROM outbound_work")
    assert not await app.database.read("SELECT * FROM fed_revision_receipt")
    assert not await app.database.read("SELECT * FROM alert")


async def test_supersession_retains_first_position_and_uses_revision_not_first_pending_cursor(
    nodes,
):
    app, peer, handoff = await setup(nodes)
    first, second = await report(app), await report(app, "fire second report")
    initial = await handoff.stage(peer.id)
    before = await handoff.pending(peer.id)
    for status in ("monitoring", "resolved", "open"):
        app.clock.epoch -= timedelta(seconds=100)
        await app.incidents.operator_patch(
            first.id, status=status, severity=None, resolution="Checked", actor="web:test"
        )
        assert (await handoff.pending(peer.id))[0]["state"] == "source_changed"
        result = await handoff.stage(peer.id)
        assert (
            result.scanned == result.staged == 1 and result.after_revision > initial.after_revision
        )
        pending = await handoff.pending(peer.id)
        assert [row["uid"] for row in pending] == [first.uid, second.uid]
        assert [row["first_revision"] for row in pending] == [
            row["first_revision"] for row in before
        ]
        assert pending[0]["revision"] > before[0]["revision"]


async def test_export_uses_only_the_callers_writer_for_parent_note_and_origins(nodes, monkeypatch):
    app, peer, handoff = await setup(nodes)
    await source_note(app, peer)

    async def forbidden(*args, **kwargs):
        pytest.fail("handoff escaped its writer transaction")

    monkeypatch.setattr(app.database, "read", forbidden)
    assert (await handoff.stage(peer.id)).staged == 2


async def test_policy_change_waiting_for_writer_is_observed_and_does_not_advance(nodes):
    app, peer, handoff = await setup(nodes)
    await report(app)
    async with app.database.transaction() as tx:
        task = asyncio.create_task(handoff.stage(peer.id))
        await asyncio.sleep(0)
        await tx.write("UPDATE fed_peer SET sync_incidents=0 WHERE id=?", (peer.id,))
    with pytest.raises(ValueError, match="policy"):
        await task
    assert not (await state(app))["fed_incident_handoff"]
    assert len(await app.incidents.change_events.pending()) == 1


async def test_scope_expansion_backfills_and_revocation_invalidates_old_intents(nodes):
    app, peer, handoff = await setup(nodes, notes=False)
    incident = await report(app)
    await app.incidents.operator_update(incident.id, "update", "Original plain note")
    assert (await handoff.stage(peer.id)).scanned == 2
    assert len(await handoff.pending(peer.id)) == 1
    caps = {**peer.capabilities, "incident_updates": 1}
    await app.database.write(
        "UPDATE fed_peer SET capabilities=? WHERE id=?", (json.dumps(caps), peer.id)
    )
    assert (await handoff.pending(peer.id))[0]["state"] == "policy_changed"
    result = await handoff.stage(peer.id)
    assert result.scope_reset and result.staged == 2
    await app.database.write("UPDATE fed_peer SET state='paused' WHERE id=?", (peer.id,))
    assert all(i["state"] == "policy_changed" for i in await handoff.pending(peer.id))
    with pytest.raises(ValueError, match="policy"):
        await handoff.stage(peer.id)


async def test_geographic_filter_expansion_and_parent_location_changes_are_not_skipped(nodes):
    app, peer, handoff = await setup(nodes)
    incident, _ = await source_note(app, peer)
    await app.database.write("UPDATE incident SET lat=10,lon=20 WHERE id=?", (incident.id,))
    assert (await handoff.stage(peer.id)).skipped == 2
    assert not await handoff.pending(peer.id)
    await app.database.write(
        "UPDATE fed_peer SET incident_lat=10,incident_lon=20 WHERE id=?", (peer.id,)
    )
    assert (await handoff.stage(peer.id)).staged == 2
    await app.database.write("UPDATE incident SET lat=-10,lon=-20 WHERE id=?", (incident.id,))
    assert all(i["state"] == "source_changed" for i in await handoff.pending(peer.id))
    assert (await handoff.stage(peer.id)).staged == 2
    assert all(i["state"] == "not_exportable" for i in await handoff.pending(peer.id))
    await app.database.write("UPDATE incident SET lat=10,lon=20 WHERE id=?", (incident.id,))
    await handoff.stage(peer.id)
    assert all(i["state"] == "pending" for i in await handoff.pending(peer.id))


async def test_foreign_and_self_prefixed_heads_cannot_republish_another_record(nodes):
    app, peer, handoff = await setup(nodes)
    remote, rp, _ = await nodes("remote", notes=True)
    foreign = await report(remote)
    await import_parent(remote, rp, app, peer, foreign)
    local = await report(app)
    await app.database.write(
        "INSERT INTO fed_revision(stream,uid) VALUES('incidents',?)", (f"!local:{local.uid}",)
    )
    page = await handoff.stage(peer.id)
    assert page.staged == 1 and page.skipped == 2
    assert [i["uid"] for i in await handoff.pending(peer.id)] == [local.uid]


async def test_deletion_invalidates_old_intent_without_inventing_a_withdrawal(nodes):
    app, peer, handoff = await setup(nodes)
    incident = await report(app)
    await handoff.stage(peer.id)
    await app.database.write("DELETE FROM incident WHERE id=?", (incident.id,))
    await handoff.stage(peer.id)
    intent = (await handoff.pending(peer.id))[0]
    assert intent["state"] == "not_exportable" and intent["digest"] is None
    assert len(await app.incidents.change_events.pending()) == 1
    assert not await app.database.read("SELECT * FROM outbound_work")


async def test_invalid_payload_is_visible_and_does_not_block_later_valid_work(nodes):
    app, peer, handoff = await setup(nodes)
    bad = await report(app)
    await app.database.write("UPDATE incident SET body=? WHERE id=?", ("x" * 12001, bad.id))
    good = await report(app, "fire neighboring report")
    await handoff.stage(peer.id)
    assert {i["uid"]: i["state"] for i in await handoff.pending(peer.id)} == {
        bad.uid: "invalid_payload",
        good.uid: "pending",
    }


@pytest.mark.parametrize("boundary", [1, 2, 3])
@pytest.mark.parametrize("cancel", [False, True])
async def test_fault_or_cancellation_rolls_back_intents_and_cursor_together(
    nodes, monkeypatch, boundary, cancel
):
    app, peer, handoff = await setup(nodes)
    await source_note(app, peer)
    before = await state(app)
    original = Transaction.write
    reached = asyncio.Event()
    writes = 0

    async def fail(tx, sql, params=()):
        nonlocal writes
        result = await original(tx, sql, params)
        writes += 1
        if writes == boundary:
            reached.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError("injected handoff fault")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Transaction, "write", fail)
        task = asyncio.create_task(handoff.stage(peer.id))
        await asyncio.wait_for(reached.wait(), 5)
        assert await state(app) == before  # other readers cannot see uncommitted staging
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await task
    assert await state(app) == before
    assert (await handoff.stage(peer.id)).staged == 2


@pytest.mark.parametrize(
    "damage",
    [
        "epoch",
        "sequence",
        "producer",
        "peer",
        "missing_head",
        "revision",
        "missing_lineage",
        "newer_intent",
        "intent_epoch",
    ],
)
async def test_lineage_corruption_or_newer_intent_never_advances_the_scan(nodes, damage):
    app, peer, handoff = await setup(nodes)
    incident = await report(app)
    await handoff.stage(peer.id)
    await app.database.write("UPDATE incident SET title='New revision' WHERE id=?", (incident.id,))
    if damage == "epoch":
        await app.database.write("UPDATE fed_revision_lineage SET epoch=?", ("b" * 32,))
    elif damage == "sequence":
        await app.database.write("UPDATE sqlite_sequence SET seq=0 WHERE name='fed_revision'")
    elif damage == "producer":
        app.federation_sync.local_mesh_id = "!different"
    elif damage == "peer":
        await app.database.write("UPDATE fed_peer SET mesh_id='!different' WHERE id=?", (peer.id,))
    elif damage == "missing_head":
        await app.database.write("DELETE FROM fed_revision WHERE uid=?", (incident.uid,))
    elif damage == "revision":
        await app.database.write(
            "UPDATE fed_revision SET revision=revision+100 WHERE uid=?", (incident.uid,)
        )
    elif damage == "missing_lineage":
        await app.database.write("DELETE FROM fed_revision_lineage")
    elif damage == "newer_intent":
        await app.database.write("UPDATE fed_incident_intent SET revision=revision+100")
    else:
        await app.database.write("UPDATE fed_incident_intent SET epoch=?", ("b" * 32,))
    before = await state(app)
    with pytest.raises(ValueError):
        await handoff.stage(peer.id)
    assert await state(app) == before
    if damage != "missing_lineage":
        pending = await handoff.pending(peer.id)
        assert pending[0]["state"] in {"lineage_blocked", "source_changed"}


async def test_each_peer_advances_independently_and_full_backup_preserves_work(nodes):
    app, peer, handoff = await setup(nodes)
    await source_note(app, peer)
    second = await app.federation.discover("!third", "Third", 1, peer.capabilities, "radio")
    await app.database.write(
        "UPDATE fed_peer SET state='active',sync_incidents=1 WHERE id=?", (second.id,)
    )
    first_page = await handoff.stage(peer.id, limit=1)
    assert first_page.remaining
    assert len(await handoff.pending(peer.id)) == 1
    assert not await handoff.pending(second.id)
    assert (await handoff.stage(second.id)).staged == 2
    before = await state(app)
    backup = await BackupService(app.database).create()
    for path in (app.database.path, backup):
        recovered = Database(path)
        await recovered.open()
        try:
            service = FederationSyncService(recovered, "!local").incident_handoff
            assert len(await service.pending(peer.id)) == 1
            assert len(await service.pending(second.id)) == 2
            assert (await service.stage(peer.id)).scanned == 1
            assert len(await service.pending(peer.id)) == 2
        finally:
            await recovered.close()
    assert (await state(app))["incident_change_event"] == before["incident_change_event"]


async def test_bounded_revision_seeks_and_intent_inspection_use_indexes(nodes, monkeypatch):
    app, peer, handoff = await setup(nodes)
    async with app.database.transaction() as tx:
        for i in range(MAX_PAGE + 7):
            await tx.write(
                "INSERT INTO incident(uid,local_ref,type,severity,title,reporter_label,"
                "origin_node,created_at,updated_at) "
                "VALUES(?,?,'road','caution','Test','Test','!local',1,1)",
                (f"synthetic-{i}", i + 1),
            )
    original = Transaction.read
    plans = []

    async def explain(tx, sql, params=()):
        if "INDEXED BY" in sql:
            plans.extend(await original(tx, "EXPLAIN QUERY PLAN " + sql, params))
        return await original(tx, sql, params)

    with monkeypatch.context() as patch:
        patch.setattr(Transaction, "read", explain)
        first = await handoff.stage(peer.id)
        second = await handoff.stage(peer.id)
        page = await handoff.pending(peer.id)
        tail = await handoff.pending(peer.id, after=page[-1]["first_revision"])
    assert first.scanned == 100 and first.remaining
    assert second.scanned == 7 and not second.remaining
    assert len(page) == 100 and len(tail) == 7
    details = " ".join(r["detail"] for r in plans)
    assert "idx_incident_change_revision (revision>?)" in details
    assert "idx_fed_incident_intent_pending (peer_id=? AND first_revision>?)" in details
    assert "SCAN" not in details and "TEMP B-TREE" not in details


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": 1.5}])
async def test_invalid_scan_and_inspection_bounds_are_rejected(nodes, kwargs):
    _, peer, handoff = await setup(nodes)
    for method in (handoff.stage, handoff.pending):
        with pytest.raises(ValueError):
            await method(peer.id, **kwargs)


async def test_no_identity_no_peer_and_disabled_modules_cannot_consume_work(nodes):
    app, peer, handoff = await setup(nodes)
    await report(app)
    with pytest.raises(ValueError, match="does not exist"):
        await handoff.stage(peer.id + 100)
    app.federation_sync.module_enabled = lambda _: False
    with pytest.raises(ValueError, match="policy"):
        await handoff.stage(peer.id)
    app.federation_sync.local_mesh_id = ""
    with pytest.raises(ValueError, match="radio identity"):
        await handoff.stage(peer.id)
    assert not (await state(app))["fed_incident_handoff"]
    assert len(await app.incidents.change_events.pending()) == 1


async def test_upgrade_preserves_source_heads_without_inventing_peer_progress(tmp_path):
    path = tmp_path / "upgrade.db"
    migrations = Path(__file__).parents[2] / "src/outpost/store/migrations"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute("PRAGMA journal_mode=WAL")
        for migration in sorted(migrations.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            version = int(migration.name[:4])
            if version > 178:
                break
            connection.executescript(migration.read_text())
            connection.execute(
                "INSERT INTO schema_version(version,applied_at) VALUES(?,1)", (version,)
            )
        connection.execute("INSERT INTO fed_revision(stream,uid) VALUES('incidents','deleted')")
        connection.commit()
        before = connection.execute("SELECT * FROM incident_change_event").fetchall()
    finally:
        connection.close()
    database = Database(path)
    await database.open()
    try:
        assert [
            tuple(r) for r in await database.read("SELECT * FROM incident_change_event")
        ] == before
        assert not await database.read("SELECT * FROM fed_incident_handoff")
        assert not await database.read("SELECT * FROM fed_incident_intent")
        assert len(await database.read("SELECT * FROM schema_version WHERE version=179")) == 1
    finally:
        await database.close()


@pytest.mark.parametrize("phase", ["committed", "uncommitted"])
async def test_killed_handoff_process_recovers_without_skipping_source_work(nodes, phase):
    app, peer, _ = await setup(nodes)
    await report(app)
    script = """
import asyncio, sys
from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.store import Transaction
from outpost.transport.simulated import SimulatedRadioLink

async def main():
    clock = VirtualClock()
    config = Config.model_validate({
        "store": {"path": sys.argv[1]},
        "modules": {"fed": {"enabled": True}, "watch": {"enabled": True}},
    })
    app = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock, node_id="!local"))
    await app.database.open()
    app.federation_sync.local_mesh_id = "!local"
    peer = await app.federation.by_mesh_id("!remote")
    if sys.argv[2] == "uncommitted":
        original = Transaction.write
        async def pause(tx, sql, params=()):
            result = await original(tx, sql, params)
            if sql.startswith("INSERT INTO fed_incident_handoff"):
                print("uncommitted", flush=True)
                await asyncio.Event().wait()
            return result
        Transaction.write = pause
    await app.federation_sync.incident_handoff.stage(peer.id)
    print("committed", flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(app.database.path),
        phase,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        marker = await asyncio.wait_for(process.stdout.readline(), 30)
        assert marker.decode().strip() == phase
        process.kill()  # Only this test-owned process; its simulated radio never connects.
        await asyncio.wait_for(process.wait(), 10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await process.communicate()
    assert process.returncode == -9
    reopened = Database(app.database.path)
    await reopened.open()
    try:
        assert (await reopened.read("PRAGMA integrity_check"))[0][0] == "ok"
        assert len(await reopened.read("SELECT * FROM fed_incident_intent")) == int(
            phase == "committed"
        )
        assert len(await reopened.read("SELECT * FROM fed_incident_handoff")) == int(
            phase == "committed"
        )
        assert len(await reopened.read("SELECT * FROM incident_change_event")) == 1
        handoff = FederationSyncService(reopened, "!local").incident_handoff
        assert (await handoff.stage(peer.id)).staged == int(phase == "uncommitted")
        assert len(await handoff.pending(peer.id)) == 1
        assert not await reopened.read("SELECT * FROM outbound_work")
    finally:
        await reopened.close()


async def test_concurrent_source_edit_remains_pending_after_handoff_commit(nodes, monkeypatch):
    app, peer, handoff = await setup(nodes)
    incident = await report(app)
    original = Transaction.write
    reached, release = asyncio.Event(), asyncio.Event()

    async def pause(tx, sql, params=()):
        result = await original(tx, sql, params)
        if sql.startswith("INSERT INTO fed_incident_handoff"):
            reached.set()
            await release.wait()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Transaction, "write", pause)
        staging = asyncio.create_task(handoff.stage(peer.id))
        await asyncio.wait_for(reached.wait(), 5)
        editing = asyncio.create_task(
            app.incidents.operator_patch(
                incident.id,
                status="resolved",
                severity=None,
                resolution="Checked",
                actor="web:test",
            )
        )
        await asyncio.sleep(0)
        assert not editing.done()
        release.set()
        await asyncio.gather(staging, editing)
    assert (await handoff.pending(peer.id))[0]["state"] == "source_changed"
    assert (await handoff.stage(peer.id)).staged == 1
    assert (await handoff.pending(peer.id))[0]["state"] == "pending"


@pytest.mark.parametrize("when", ["waiting", "exporting"])
async def test_producer_identity_changes_during_handoff_roll_back(nodes, monkeypatch, when):
    app, peer, handoff = await setup(nodes)
    await report(app)
    if when == "waiting":
        async with app.database.transaction():
            task = asyncio.create_task(handoff.stage(peer.id))
            await asyncio.sleep(0)
            app.federation_sync.local_mesh_id = "!changed"
    else:
        original = app.federation_sync.export_items

        async def change(*args, **kwargs):
            result = await original(*args, **kwargs)
            app.federation_sync.local_mesh_id = "!changed"
            return result

        monkeypatch.setattr(app.federation_sync, "export_items", change)
        task = asyncio.create_task(handoff.stage(peer.id))
    with pytest.raises(ValueError, match="identity changed"):
        await task
    assert not (await state(app))["fed_incident_intent"]
    assert not (await state(app))["fed_incident_handoff"]


async def test_conflicting_same_revision_content_is_not_silently_restaged(nodes, monkeypatch):
    app, peer, handoff = await setup(nodes)
    await report(app)
    await handoff.stage(peer.id)
    await app.database.write(
        "UPDATE fed_peer SET incident_lat=10,incident_lon=20 WHERE id=?", (peer.id,)
    )
    before = await state(app)
    original = app.federation_sync.export_items

    async def corrupt(*args, **kwargs):
        result = await original(*args, **kwargs)
        result[0]["payload"]["title"] = "Unversioned corrupt content"
        return result

    monkeypatch.setattr(app.federation_sync, "export_items", corrupt)
    with pytest.raises(ValueError, match="content conflicts"):
        await handoff.stage(peer.id)
    assert await state(app) == before


async def test_module_disable_during_export_rolls_back_page(nodes, monkeypatch):
    app, peer, handoff = await setup(nodes)
    await report(app)
    original = app.federation_sync.export_items

    async def disable(*args, **kwargs):
        result = await original(*args, **kwargs)
        app.federation_sync.module_enabled = lambda _: False
        return result

    monkeypatch.setattr(app.federation_sync, "export_items", disable)
    with pytest.raises(ValueError, match="module policy changed"):
        await handoff.stage(peer.id)
    assert not (await state(app))["fed_incident_intent"]
    assert not (await state(app))["fed_incident_handoff"]


@pytest.mark.parametrize("damage", ["missing", "epoch"])
async def test_damaged_checkpoint_is_not_treated_as_a_new_peer(nodes, damage):
    app, peer, handoff = await setup(nodes)
    await report(app)
    await handoff.stage(peer.id)
    if damage == "missing":
        await app.database.write("DELETE FROM fed_incident_handoff")
    else:
        await app.database.write("UPDATE fed_incident_handoff SET epoch=?", ("b" * 32,))
    before = await state(app)
    assert (await handoff.pending(peer.id))[0]["state"] == "lineage_blocked"
    with pytest.raises(ValueError):
        await handoff.stage(peer.id)
    assert await state(app) == before
