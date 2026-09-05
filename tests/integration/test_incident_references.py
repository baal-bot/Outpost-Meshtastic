import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.fed import FederationSyncService
from outpost.render.renderer import render_response
from outpost.router.models import ResponseKind
from outpost.store import Database
from outpost.store.backups import BackupService
from outpost.store.maintenance import MaintenanceService
from outpost.store.members import MemberRepo
from outpost.transport.chunker import truncate_utf8
from outpost.transport.simulated import SimulatedRadioLink
from outpost.watch.incidents import IncidentService
from tests.integration.test_incident_reconciliation import active_incident_peer, import_remote
from tests.integration.test_safety_commands import inbound


@pytest.mark.parametrize("status", ["resolved", "false_alarm", "expired"])
@pytest.mark.parametrize("purge", [False, True])
async def test_terminal_references_survive_restart_and_are_never_reassigned(
    tmp_path, status: str, purge: bool
) -> None:
    database = Database(tmp_path / "references.db")
    await database.open()
    clock = VirtualClock()
    try:
        service = IncidentService(database, clock)
        original, _ = await service.create("road original obstruction", None)
        assert original is not None
        await service.operator_patch(
            original.id, status=status, severity=None, resolution="Done", actor="operator:test"
        )
        if purge:
            await database.write("DELETE FROM incident WHERE id=?", (original.id,))
    finally:
        await database.close()
    reopened = Database(database.path)
    await reopened.open()
    try:
        service = IncidentService(reopened, clock)
        member = await MemberRepo(reopened, clock).resolve("!00000001")
        fresh, _ = await service.create("fire unrelated new event", None)
        assert fresh is not None and fresh.local_ref > original.local_ref
        prior = await service.by_ref(original.local_ref)
        assert prior is None if purge else prior is not None and prior.uid == original.uid
        for kind in ("confirm", "dispute"):
            with pytest.raises(ValueError, match="No active incident"):
                await service.react(original.local_ref, member, kind)
        new = await service.by_ref(fresh.local_ref)
        assert new is not None and new.confirm_count == new.dispute_count == 0
        assert await service.updates(new.id) == []
    finally:
        await reopened.close()


@pytest.mark.production_wiring
@pytest.mark.parametrize("purge", [False, True])
async def test_partition_delayed_commands_cannot_target_a_new_incident(tmp_path, purge) -> None:
    clock = VirtualClock()
    config = Config.model_validate(
        {"store": {"path": str(tmp_path / "app.db")}, "modules": {"watch": {"enabled": True}}}
    )
    app = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock))
    await app.database.open()
    try:
        for member_id in (1, 2, 3, 10):
            await app.router.members.resolve(f"!{member_id:08x}")
        old, _ = await app.incidents.create("road original event", None)
        assert old is not None
        await app.incidents.operator_patch(
            old.id, status="resolved", severity=None, resolution="Clear", actor="operator:test"
        )
        if purge:
            await app.database.write("DELETE FROM incident WHERE id=?", (old.id,))
        new, _ = await app.incidents.create("fire unrelated event", None)
        assert new is not None
        # These packets were composed before the old incident resolved, then delayed.
        for i, command in enumerate(("INC", "CONFIRM", "DISPUTE"), 1):
            response = await app.router.dispatch(
                inbound(i, f"{command} {old.local_ref}", f"!{i:08x}")
            )
            if command == "INC" and not purge:
                assert "road original event" in render_response(response)
                assert "resolved" in render_response(response)
            else:
                assert response.kind == ResponseKind.ERROR
                assert (
                    "No active incident." in render_response(response)
                    if command != "INC"
                    else "No incident." in render_response(response)
                )
            assert "fire unrelated event" not in render_response(response)
        response = await app.router.dispatch(inbound(10, f"CONFIRM {new.local_ref}", "!00000010"))
        assert response.kind == ResponseKind.ACK, render_response(response)
        text = render_response(response)
        assert new.title in text and new.origin_node in text
        assert len(text.encode()) <= 200
        current = await app.incidents.by_id(new.id)
        assert current is not None and current.confirm_count == 1 and current.dispute_count == 0
    finally:
        await app.ai_service.close()
        await app.database.close()


async def test_import_merge_unmerge_and_reimport_keep_reference_identity(tmp_path) -> None:
    database = Database(tmp_path / "federated.db")
    await database.open()
    try:
        clock = VirtualClock(epoch=datetime.fromtimestamp(10, UTC))
        service = IncidentService(database, clock, "!local")
        original, _ = await service.create("road first local report", None)
        assert original is not None
        await service.operator_patch(
            original.id, status="resolved", severity=None, resolution="Clear", actor="operator:test"
        )
        peer = await active_incident_peer(database)
        sync = FederationSyncService(database, "!local")
        await import_remote(database, sync, peer, version=100, digest="first", now=101)
        imported = (await service.list())[0]
        assert imported.local_ref > original.local_ref
        local, _ = await service.create(
            "road blocked near bridge again 40.0 -79.0", None, force=True
        )
        assert local is not None and local.local_ref > imported.local_ref
        await service.merge(imported.id, local.id, "test:merge")
        canonical = await service.by_ref(imported.local_ref)
        assert canonical is not None and canonical.uid == local.uid
        await service.unmerge(imported.id, "test:unmerge")
        restored = await service.by_ref(imported.local_ref)
        assert restored is not None and restored.uid == imported.uid
        await database.write("DELETE FROM incident WHERE id=?", (imported.id,))
        assert await service.by_ref(imported.local_ref) is None
        await import_remote(database, sync, peer, version=200, digest="reimport", now=201)
        reimported = await service.by_ref(imported.local_ref)
        assert reimported is not None and reimported.uid == imported.uid
        rows = await database.read(
            "SELECT local_ref,incident_uid FROM incident_reference ORDER BY local_ref"
        )
        assert [tuple(row) for row in rows] == [
            (original.local_ref, original.uid),
            (imported.local_ref, imported.uid),
            (local.local_ref, local.uid),
        ]
    finally:
        await database.close()


@pytest.mark.production_wiring
async def test_maintenance_removes_content_but_preserves_retired_references(tmp_path) -> None:
    database = Database(tmp_path / "retention.db")
    await database.open()
    try:
        clock = VirtualClock()
        incidents = IncidentService(database, clock)
        original, _ = await incidents.create("road old report", None)
        assert original is not None
        await incidents.operator_patch(
            original.id, status="resolved", severity=None, resolution="Clear", actor="operator:test"
        )
        clock.advance(200 * 86_400)
        config = Config.model_validate(
            {"store": {"path": str(database.path), "backup": {"enabled": False}}}
        )
        maintenance = MaintenanceService(database, BackupService(database), clock, config)
        await maintenance.run()
        assert await incidents.by_id(original.id) is None
        fresh, _ = await incidents.create("fire later report", None)
        assert fresh is not None and fresh.local_ref > original.local_ref
        rows = await database.read(
            "SELECT * FROM incident_reference WHERE local_ref=?", (original.local_ref,)
        )
        assert len(rows) == 1 and rows[0]["incident_uid"] == original.uid
    finally:
        await database.close()


async def test_migration_retires_ambiguous_legacy_numbers_without_changing_incident_identity(
    tmp_path,
) -> None:
    path = tmp_path / "legacy.db"
    migrations = Path(__file__).parents[2] / "src/outpost/store/migrations"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute("PRAGMA journal_mode=WAL")
        for migration in sorted(migrations.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            version = int(migration.name[:4])
            if version > 173:
                break
            connection.executescript(migration.read_text())
            connection.execute(
                "INSERT INTO schema_version(version,applied_at) VALUES(?,1)", (version,)
            )
        for uid, ref, status in (
            ("old", 1, "resolved"),
            ("reused", 1, "open"),
            ("unique", 9, "resolved"),
        ):
            connection.execute(
                "INSERT INTO incident(uid,local_ref,type,severity,status,title,body,reporter_label,"
                "origin_node,created_at,updated_at) "
                "VALUES(?,?,'road','caution',?,?,?,'reporter','!local',1,1)",
                (uid, ref, status, uid, uid + " body"),
            )
        connection.commit()
        connection.row_factory = sqlite3.Row
        before = [dict(row) for row in connection.execute("SELECT * FROM incident ORDER BY id")]
    finally:
        connection.close()
    database = Database(path)
    await database.open()
    try:
        service = IncidentService(database, VirtualClock())
        assert await service.by_ref(1) is None
        rows = [dict(row) for row in await database.read("SELECT * FROM incident ORDER BY id")]
        assert [row["local_ref"] for row in rows] == [10, 11, 9]
        before[0]["local_ref"], before[1]["local_ref"] = 10, 11
        assert rows == before
        retired = (
            await database.read("SELECT incident_uid FROM incident_reference WHERE local_ref=1")
        )[0]
        assert retired["incident_uid"] is None
        fresh, _ = await service.create("fire after migration", None)
        assert fresh is not None and fresh.local_ref == 12
        with pytest.raises(sqlite3.IntegrityError, match="reference"):
            await database.write("UPDATE incident SET local_ref=1 WHERE id=?", (fresh.id,))
        with pytest.raises(sqlite3.IntegrityError, match="reference"):
            await database.write(
                "INSERT INTO incident(uid,local_ref,type,severity,title,reporter_label,origin_node,"
                "created_at,updated_at) "
                "VALUES('unrelated',1,'fire','urgent','Bad reuse','operator','local',1,1)"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            await database.write("DELETE FROM incident_reference WHERE local_ref=1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            await database.write(
                "UPDATE incident_reference SET incident_uid='new' WHERE local_ref=1"
            )
    finally:
        await database.close()
    reopened = Database(path)
    await reopened.open()
    try:
        assert await IncidentService(reopened, VirtualClock()).by_ref(1) is None
        assert (await reopened.read("PRAGMA integrity_check"))[0][0] == "ok"
        assert await reopened.read("PRAGMA foreign_key_check") == []
    finally:
        await reopened.close()


@pytest.mark.production_wiring
@pytest.mark.parametrize("command", ["CONFIRM", "DISPUTE"])
async def test_reaction_identity_context_stays_within_radio_byte_budget(tmp_path, command) -> None:
    clock = VirtualClock()
    config = Config.model_validate(
        {"store": {"path": str(tmp_path / "app.db")}, "modules": {"watch": {"enabled": True}}}
    )
    app = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock))
    await app.database.open()
    try:
        await app.router.members.resolve("!00000001")
        await app.database.write(
            "INSERT INTO incident_reference(local_ref,incident_uid) VALUES(1000000000,'reserved')"
        )
        report, _ = await app.incidents.create("road " + "🔥" * 50, None)
        assert report is not None and report.local_ref == 1_000_000_001
        result = await app.router.dispatch(inbound(1, f"{command} {report.local_ref}", "!00000001"))
        text = render_response(result)
        assert result.kind == ResponseKind.ACK, text
        assert truncate_utf8(report.title, 48) in text
        assert report.origin_node in text
        assert len(text.encode()) <= 200
    finally:
        await app.ai_service.close()
        await app.database.close()
