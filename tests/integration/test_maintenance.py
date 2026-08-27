import pytest
from fastapi.testclient import TestClient

from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.store import Database
from outpost.store.backups import BackupService
from outpost.store.maintenance import MaintenanceService
from outpost.web.api import create_web_app


@pytest.mark.asyncio
async def test_maintenance_prunes_expired_data_preserves_pins_and_backs_up(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    clock.advance(8 * 3_600)
    now = int(clock.now().timestamp())
    old = now - 200 * 86_400
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
    member_id = await database.write(
        "INSERT INTO member(mesh_id,mesh_num,first_seen,last_seen) VALUES('!00000003',3,?,?)",
        (old, old),
    )
    await database.write(
        "INSERT INTO member_position(member_id,lat,lon,received_at,expires_at) "
        "VALUES(?,40,-80,?,?)",
        (member_id, old, old),
    )
    await database.write(
        "INSERT INTO pending_incident_location(member_id,lat,lon,created_at,expires_at) "
        "VALUES(?,40,-80,?,?)",
        (member_id, old, old),
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
    assert await database.read("SELECT 1 FROM safety_floor_attempt") == []
    assert await database.read("SELECT 1 FROM member_position") == []
    assert await database.read("SELECT 1 FROM pending_incident_location") == []
    assert await database.read("SELECT 1 FROM fed_service_request") == []
    assert await database.read("SELECT 1 FROM fed_service_usage") == []
    assert await database.read("SELECT 1 FROM env_cache") == []
    assert await database.read("SELECT 1 FROM cap_point_cache") == []
    assert [row["uid"] for row in await database.read("SELECT uid FROM thread")] == ["pinned"]
    assert backups.list()
    assert await service.due() is False
    assert await database.read("SELECT 1 FROM audit_log WHERE action='maintenance.run'")
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
