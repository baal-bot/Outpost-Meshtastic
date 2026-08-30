import json
import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.store import Database
from outpost.store.backups import BackupService
from outpost.store.maintenance import MaintenanceService
from outpost.store.members import MemberRepo
from outpost.watch.incidents import IncidentService
from outpost.web.api import create_web_app

pytestmark = pytest.mark.production_wiring


@pytest.mark.asyncio
async def test_maintenance_due_date_is_stable_across_dst_fall_back(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    config = Config.model_validate(
        {
            "node": {"timezone": "America/New_York"},
            "store": {"maintenance_hour": 1},
        }
    )
    clock = VirtualClock(epoch=datetime(2026, 11, 1, 4, 30, tzinfo=UTC))
    service = MaintenanceService(database, BackupService(database), clock, config)

    assert await service.due() is False  # 00:30 EDT
    clock.advance(3_600)
    assert await service.due() is True  # 01:30 EDT
    await database.write(
        "INSERT INTO runtime_setting(key,value,updated_at) VALUES(?,?,?)",
        ("maintenance.last_date", json.dumps("2026-11-01"), int(clock.now().timestamp())),
    )
    clock.advance(3_600)
    assert await service.due() is False  # 01:30 EST, same local date
    await database.close()


@pytest.mark.asyncio
async def test_maintenance_prunes_expired_data_preserves_pins_and_backs_up(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    clock.advance(8 * 3_600)
    now = int(clock.now().timestamp())
    old = now - 200 * 86_400
    old_clock = VirtualClock(epoch=datetime.fromtimestamp(old, UTC))
    board_id = (await database.read("SELECT id FROM board WHERE slug='gen'"))[0]["id"]
    await database.write(
        """
        INSERT INTO thread(uid,board_id,subject,origin_node,created_at,last_post_at,post_count)
        VALUES('old',?,'old','local',?,?,0)
        """,
        (board_id, old, old),
    )
    await database.write(
        """
        INSERT INTO thread(
          uid,board_id,subject,origin_node,created_at,last_post_at,post_count,pinned
        )
        VALUES('pinned',?,'pinned','local',?,?,0,1)
        """,
        (board_id, old, old),
    )
    await database.write(
        "INSERT INTO kv(ns,k,v,expires_at,updated_at) VALUES('test','old','x',?,?)",
        (old, old),
    )
    await database.write(
        """
        INSERT INTO message_log(direction,channel,portnum,is_direct,byte_len,created_at)
        VALUES('in',0,1,1,4,?)
        """,
        (old,),
    )
    await database.write(
        "INSERT INTO safety_floor_attempt(member_mesh_id,command,fingerprint,first_seen_at,"
        "last_seen_at,accepted_at) VALUES('!00000001','HELPME','old',?,?,?)",
        (old, old, old),
    )
    old_member = await MemberRepo(database, old_clock).resolve("!00000003")
    await database.write(
        "INSERT INTO member_position(member_id,lat,lon,received_at,expires_at) "
        "VALUES(?,40,-80,?,?)",
        (old_member.id, old, old),
    )
    await database.write(
        "INSERT INTO pending_incident_location(member_id,lat,lon,created_at,expires_at) "
        "VALUES(?,40,-80,?,?)",
        (old_member.id, old, old),
    )
    peer_id = await database.write(
        "INSERT INTO fed_peer(mesh_id,created_at) VALUES('!00000002',?)", (old,)
    )
    await database.write(
        "INSERT INTO fed_service_request(request_id,direction,peer_mesh_id,service,status,"
        "created_at,updated_at,expires_at) "
        "VALUES('old-service','in','!00000002','weather','complete',?,?,?)",
        (old, old, old),
    )
    await database.write(
        "INSERT INTO fed_service_usage(peer_id,window_start,requests) VALUES(?,?,1)",
        (peer_id, old),
    )
    await database.write(
        "INSERT INTO env_cache(cache_key,provider,payload,fetched_at,expires_at) "
        "VALUES('old-point','test','{}',?,?)",
        (old, old),
    )
    await database.write(
        "INSERT INTO cap_point_cache(cache_key,provider,query_lat,query_lon,status,result_json,"
        "fetched_at) VALUES('old-alerts','test',0,0,'empty','[]',?)",
        (old,),
    )
    recent_incident = now - 10 * 86_400
    reporter = await MemberRepo(database, old_clock).resolve("!00000004")
    old_incidents = IncidentService(database, old_clock)
    old_incident, _ = await old_incidents.create("Old incident on closed road", reporter)
    active_incident, _ = await old_incidents.create("Active incident at bridge", reporter)
    assert old_incident is not None and active_incident is not None
    await old_incidents.operator_patch(
        old_incident.id,
        status="resolved",
        severity=None,
        resolution="Resolved long ago",
        actor="operator",
    )
    recent_clock = VirtualClock(epoch=datetime.fromtimestamp(recent_incident, UTC))
    recent_incidents = IncidentService(database, recent_clock)
    recent, _ = await recent_incidents.create("Recent incident near shelter", reporter)
    assert recent is not None
    await recent_incidents.operator_patch(
        recent.id,
        status="resolved",
        severity=None,
        resolution="Recently resolved",
        actor="operator",
    )
    config = Config.model_validate(
        {"store": {"path": str(tmp_path / "outpost.db"), "maintenance_hour": 3}}
    )
    backups = BackupService(database)
    service = MaintenanceService(database, backups, clock, config)

    assert await service.due() is True
    result = await service.run()
    assert result.threads == 1 and result.messages == 1 and result.kv == 1
    assert result.member_positions == 1
    assert result.pending_positions == 1
    assert result.safety_floor == 1
    assert result.federation_services == 1
    assert result.federation_service_usage == 1
    assert result.environment_cache == 1
    assert result.alert_point_cache == 1
    assert result.removed["incidents"] == 1
    assert await database.read("SELECT 1 FROM safety_floor_attempt") == []
    assert await database.read("SELECT 1 FROM member_position") == []
    assert await database.read("SELECT 1 FROM pending_incident_location") == []
    assert await database.read("SELECT 1 FROM fed_service_request") == []
    assert await database.read("SELECT 1 FROM fed_service_usage") == []
    assert await database.read("SELECT 1 FROM env_cache") == []
    assert await database.read("SELECT 1 FROM cap_point_cache") == []
    retained_incidents = await database.read("SELECT title FROM incident ORDER BY title")
    assert [row["title"] for row in retained_incidents] == [
        "Active incident at bridge",
        "Recent incident near shelter",
    ]
    assert [row["uid"] for row in await database.read("SELECT uid FROM thread")] == ["pinned"]
    assert backups.list()
    assert await service.due() is False
    assert await database.read("SELECT 1 FROM audit_log WHERE action='maintenance.run'")
    await database.close()


@pytest.mark.asyncio
async def test_maintenance_bounds_situation_versions_but_keeps_latest_capability_anchor(
    tmp_path,
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    now = int(clock.now().timestamp())
    old = now - 31 * 86_400
    for capability, digest, created_at in (
        ("public", "public-old-one", old - 2),
        ("public", "public-old-two", old - 1),
        ("operator", "operator-latest", old),
        ("public", "public-latest", now),
    ):
        await database.write(
            "INSERT INTO situation_snapshot(capability,digest,facts_json,created_at) "
            "VALUES(?,?, '[]',?)",
            (capability, digest, created_at),
        )
    config = Config.model_validate(
        {"store": {"path": str(tmp_path / "outpost.db"), "backup": {"enabled": False}}}
    )
    service = MaintenanceService(database, BackupService(database), clock, config)

    result = await service.run()

    assert result.removed["situation_snapshots"] == 2
    retained = await database.read(
        "SELECT capability,digest FROM situation_snapshot ORDER BY capability"
    )
    assert [dict(row) for row in retained] == [
        {"capability": "operator", "digest": "operator-latest"},
        {"capability": "public", "digest": "public-latest"},
    ]
    await database.close()


@pytest.mark.asyncio
async def test_maintenance_preview_storage_health_and_bounded_cleanup(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    now = int(clock.now().timestamp())
    old = now - 400 * 86_400
    async with database.transaction() as transaction:
        for index in range(300):
            await transaction.write(
                "INSERT INTO web_login_attempt(source,successful,created_at) VALUES(?,?,?)",
                (f"source-{index}", 0, old),
            )
        await transaction.write(
            "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
            "VALUES('system','test','security.test','database',NULL,?)",
            (old,),
        )
    config = Config.model_validate(
        {
            "store": {
                "path": str(tmp_path / "outpost.db"),
                "maintenance_batch_rows": 25,
                "maintenance_max_rows": 250,
                "backup": {"enabled": False, "keep": 2},
            }
        }
    )
    service = MaintenanceService(database, BackupService(database), clock, config)

    preview = await service.preview()
    assert preview.total_rows == 300
    assert next(item for item in preview.rules if item.key == "web_login_attempts").rows == 300
    assert len(await database.read("SELECT 1 FROM web_login_attempt")) == 300

    health = await service.storage_report()
    assert health["database_bytes"] > 0
    assert health["wal_bytes"] >= 0 and health["backup_bytes"] == 0
    assert {item["key"] for item in health["domains"]} == {
        "system",
        "directory",
        "community",
        "watch",
        "environment",
        "ai",
        "federation",
    }
    audit_policy = next(item for item in health["policies"] if item["table"] == "audit_log")
    assert audit_policy["policy"] == "preserve" and audit_policy["protected"] is True

    result = await service.run(actor_kind="web", actor_ref="operator")
    assert result.removed["web_login_attempts"] == 250
    assert result.limited is True and result.batch_rows == 25
    assert len(await database.read("SELECT 1 FROM web_login_attempt")) == 50
    audits = await database.read("SELECT actor_kind,actor_ref FROM audit_log ORDER BY id")
    assert len(audits) == 2
    assert dict(audits[-1]) == {"actor_kind": "web", "actor_ref": "operator"}
    after = await service.storage_report()
    assert after["growth_since"] == now
    client = TestClient(
        create_web_app(
            lambda: {"radio": "up"},
            database=database,
            maintenance=service,
        )
    )
    assert client.get("/api/v1/maintenance/preview").status_code == 200
    denied = client.post("/api/v1/maintenance/run", json={"confirmation": "wrong"})
    assert denied.status_code == 422
    await database.close()


@pytest.mark.asyncio
async def test_storage_report_accounts_for_every_backup_kind_and_release(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    releases = tmp_path / "releases"
    (releases / "current").mkdir(parents=True)
    (releases / "current" / "runtime.bin").write_bytes(b"release-bytes")
    config = Config.model_validate(
        {
            "store": {
                "path": str(tmp_path / "outpost.db"),
                "releases_path": str(releases),
                "backup": {"enabled": False},
            }
        }
    )
    backups = BackupService(database, config.store.backup)
    backups.directory.mkdir()
    artifacts = {
        "outpost-test.db": b"scheduled",
        "pre-upgrade-test.db": b"upgrade",
        "pre-manual-rollback-test.db": b"rollback",
        "pre-upgrade-test.db-wal": b"auxiliary",
        "operator-note.txt": b"unmanaged",
    }
    for name, content in artifacts.items():
        (backups.directory / name).write_bytes(content)
    restore_jobs = backups.directory / "restore-jobs"
    restore_jobs.mkdir()
    (restore_jobs / "job.json").write_bytes(b"metadata")

    report = await MaintenanceService(database, backups, VirtualClock(), config).storage_report()

    assert report["backup_count"] == len(artifacts) + 1
    assert report["backup_bytes"] == sum(map(len, artifacts.values())) + len(b"metadata")
    assert set(report["backup_breakdown"]) == {
        "scheduled",
        "pre_upgrade",
        "pre_rollback",
        "auxiliary",
        "unmanaged",
        "restore_metadata",
    }
    assert report["release_count"] == 1
    assert report["release_bytes"] == len(b"release-bytes")
    await database.close()


@pytest.mark.asyncio
async def test_maintenance_redacts_ai_content_and_bounds_relay_history(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    now = int(clock.now().timestamp())
    member = await MemberRepo(database, clock).resolve("!00000005")

    async def add_interaction(question: str, age_days: int) -> None:
        await database.write(
            "INSERT INTO ai_interaction(member_id,channel,question,question_class,provider,"
            "model,answer,outcome,rated,created_at) VALUES(?,-1,?,'general','test','test',"
            "'private answer','grounded',1,?)",
            (member.id, question, now - age_days * 86_400),
        )

    await add_interaction("recent private question", 10)
    await add_interaction("elapsed private question", 60)
    await add_interaction("expired metrics", 200)
    peer_id = await database.write(
        "INSERT INTO fed_peer(mesh_id,created_at) VALUES('!00000006',?)", (now,)
    )
    old = now - 60 * 86_400
    await database.write(
        "INSERT INTO fed_relay_usage(peer_id,window_start,accepted) VALUES(?,?,1)",
        (peer_id, old),
    )
    relay_event_id = await database.write(
        "INSERT INTO fed_relay_event(peer_id,event_kind,created_at,actor) "
        "VALUES(?,'stored',?,'test')",
        (peer_id, old),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        await database.write("DELETE FROM fed_relay_event WHERE id=?", (relay_event_id,))
    await database.write(
        "INSERT INTO fed_relay_envelope(envelope_id,direction,origin_node,destination_node,"
        "scope,idempotency_key,created_at,expires_at,hop_limit,payload_cbor,payload_bytes,"
        "origin_public_key,origin_signature,route_json,state,stored_at,updated_at) "
        "VALUES('old-envelope','relay','!00000006','!00000007','opaque','old',?,?,2,"
        "X'01',1,?,?, '[]','delivered',?,?)",
        (old, old, bytes(32), bytes(64), old, old),
    )

    config = Config.model_validate(
        {"store": {"path": str(tmp_path / "outpost.db"), "backup": {"enabled": False}}}
    )
    service = MaintenanceService(database, BackupService(database), clock, config)
    assert await service.audit_table_policies() == []

    result = await service.run()

    assert result.failures == {}
    assert result.removed["ai_interactions"] == 1
    assert result.removed["ai_interaction_content"] == 1
    assert result.removed["federation_relay_usage"] == 1
    assert result.removed["federation_relay_events"] == 1
    assert result.removed["federation_relay_envelopes"] == 1
    interactions = await database.read(
        "SELECT member_id,question,answer,rated,content_purged_at FROM ai_interaction "
        "ORDER BY created_at"
    )
    assert len(interactions) == 2
    assert dict(interactions[0]) == {
        "member_id": None,
        "question": "",
        "answer": None,
        "rated": 1,
        "content_purged_at": now,
    }
    assert interactions[1]["question"] == "recent private question"
    assert await database.read("SELECT 1 FROM fed_relay_usage") == []
    assert await database.read("SELECT 1 FROM fed_relay_event") == []
    assert await database.read("SELECT 1 FROM fed_relay_envelope") == []
    assert (
        await database.read(
            "SELECT 1 FROM runtime_setting WHERE key='maintenance.allow_relay_event_delete'"
        )
        == []
    )
    await database.close()


@pytest.mark.asyncio
async def test_maintenance_deletes_service_incident_and_all_child_evidence(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    member = await MemberRepo(database, clock).resolve("!00000001")
    incidents = IncidentService(database, clock)
    incident, _ = await incidents.create("tree blocking road 40.0 -79.0", member)
    assert incident is not None
    await incidents.operator_update(incident.id, "update", "Crew dispatched")
    await incidents.operator_patch(
        incident.id,
        status="resolved",
        severity=None,
        resolution="Tree removed",
        actor="operator",
    )
    assert await database.read("SELECT 1 FROM incident_origin WHERE incident_id=?", (incident.id,))
    assert await database.read(
        "SELECT 1 FROM incident_provenance WHERE incident_id=?", (incident.id,)
    )
    provenance_id = (
        await database.read(
            "SELECT id FROM incident_provenance WHERE incident_id=? LIMIT 1", (incident.id,)
        )
    )[0]["id"]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        await database.write("DELETE FROM incident_provenance WHERE id=?", (provenance_id,))

    clock.advance(31 * 86_400)
    config = Config.model_validate(
        {"store": {"path": str(tmp_path / "outpost.db"), "backup": {"enabled": False}}}
    )
    service = MaintenanceService(database, BackupService(database), clock, config)
    result = await service.run()

    assert result.removed["incidents"] == 1 and result.failures == {}
    for table in ("incident", "incident_update", "incident_origin", "incident_provenance"):
        assert await database.read(f'SELECT 1 FROM "{table}"') == []  # noqa: S608
    assert await service.audit_cleanup_foreign_keys() == []
    await database.close()


@pytest.mark.asyncio
async def test_maintenance_deletes_merged_children_before_their_canonical_incident(
    tmp_path,
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    member = await MemberRepo(database, clock).resolve("!00000001")
    incidents = IncidentService(database, clock)
    target, _ = await incidents.create("road closure at bridge 40.0000 -79.0000", member)
    source, _ = await incidents.create(
        "road blocked at bridge 40.0005 -79.0005", member, force=True
    )
    assert target is not None and source is not None
    await incidents.merge(source.id, target.id, "operator")
    await incidents.operator_patch(
        target.id,
        status="resolved",
        severity=None,
        resolution="Road reopened",
        actor="operator",
    )
    clock.advance(31 * 86_400)
    config = Config.model_validate(
        {"store": {"path": str(tmp_path / "outpost.db"), "backup": {"enabled": False}}}
    )

    result = await MaintenanceService(database, BackupService(database), clock, config).run()

    assert result.removed["incidents"] == 2 and result.failures == {}
    assert await database.read("SELECT 1 FROM incident") == []
    assert await database.read("SELECT 1 FROM incident_match_decision") == []
    assert await database.read("SELECT 1 FROM incident_origin") == []
    assert await database.read("SELECT 1 FROM incident_provenance") == []
    await database.close()


@pytest.mark.asyncio
async def test_maintenance_isolates_failed_rule_and_bounds_backups(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    now = int(clock.now().timestamp())
    old = now - 200 * 86_400
    await database.write(
        "INSERT INTO incident(uid,local_ref,type,severity,status,title,reporter_label,"
        "origin_node,created_at,updated_at,resolved_at) "
        "VALUES('failed-rule',1,'hazard','info','resolved','Old incident','member',"
        "'local',?,?,?)",
        (old, old, old),
    )
    await database.write(
        "INSERT INTO message_log(direction,channel,portnum,is_direct,byte_len,created_at) "
        "VALUES('in',0,1,1,4,?)",
        (old,),
    )
    config = Config.model_validate(
        {
            "store": {
                "path": str(tmp_path / "outpost.db"),
                "backup": {"enabled": True, "keep": 2},
            }
        }
    )
    backups = BackupService(database)
    for _ in range(4):
        await backups.create()
    service = MaintenanceService(database, backups, clock, config)
    delete_batch = service._delete_batch

    async def fail_incidents(rule, limit):
        if rule.key == "incidents":
            raise RuntimeError("simulated incident constraint")
        return await delete_batch(rule, limit)

    monkeypatch.setattr(service, "_delete_batch", fail_incidents)
    result = await service.run()

    assert result.failures == {"incidents": "RuntimeError: simulated incident constraint"}
    assert result.removed["messages"] == 1
    assert await database.read("SELECT 1 FROM message_log") == []
    assert await database.read("SELECT 1 FROM incident")
    assert len(backups.list()) <= 2
    assert await service.due() is False
    assert (await service.health())["status"] == "degraded"
    assert await database.read(
        "SELECT 1 FROM audit_log WHERE action='maintenance.rule_failed' AND target='incidents'"
    )
    overview = TestClient(
        create_web_app(lambda: {"radio": "up"}, database=database, maintenance=service)
    ).get("/api/v1/dashboard/overview")
    assert overview.status_code == 200
    assert overview.json()["maintenance"]["failures"] == result.failures
    await database.close()


@pytest.mark.asyncio
async def test_failed_backup_leaves_no_partial_snapshot(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    backups = BackupService(database)

    async def fail_backup(destination):
        destination.write_bytes(b"partial")
        raise RuntimeError("simulated snapshot failure")

    monkeypatch.setattr(database, "backup", fail_backup)
    with pytest.raises(RuntimeError, match="snapshot failure"):
        await backups.create()
    assert list(backups.directory.glob("*")) == []
    await database.close()
