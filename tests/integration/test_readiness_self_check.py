from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.router.intents import IntentResolver
from outpost.self_check import CHECK_NAMES, SelfCheckService
from outpost.store import Database
from outpost.store.backups import BackupService
from outpost.store.members import MemberRepo
from outpost.web.api import create_web_app

pytestmark = pytest.mark.production_wiring


def readiness_config(tmp_path: Path) -> Config:
    intents = tmp_path / "intents.yaml"
    intents.write_text("[]\n", encoding="utf-8")
    return Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "router": {"intents_file": str(intents)},
        }
    )


@pytest.mark.asyncio
async def test_self_check_persists_failures_recovers_and_detects_delivery_history(
    tmp_path: Path,
) -> None:
    config = readiness_config(tmp_path)
    database = Database(config.store.path)
    await database.open()
    clock = VirtualClock()
    service = SelfCheckService(
        database,
        config,
        clock,
        BackupService(database),
        IntentResolver(config.router.intents_file),
    )

    failed = await service.run("startup")
    assert failed["status"] == "failed"
    assert failed["safety_failures"] == 2
    assert set(failed["failed_checks"]) == {
        "responder_audience",
        "escalation_audiences",
        "maintenance_freshness",
    }
    assert {item["name"] for item in failed["checks"]} == CHECK_NAMES
    assert all(item["impact"] and item["remediation"] for item in failed["checks"])
    inbox = await database.read(
        "SELECT conversation_key,state FROM mail WHERE conversation_key LIKE 'system:self-check:%'"
    )
    assert {row["conversation_key"] for row in inbox} == {
        "system:self-check:responder_audience",
        "system:self-check:escalation_audiences",
    }
    assert {row["state"] for row in inbox} == {"failed"}

    member = await MemberRepo(database, clock).resolve("!00000001")
    await database.write(
        "UPDATE member SET trust='responder',directory_state='active' WHERE id=?",
        (member.id,),
    )
    now = int(clock.now().timestamp())
    await database.write(
        "INSERT INTO runtime_setting(key,value,updated_at) VALUES('maintenance.last_date',?,?)",
        (json.dumps(clock.now().date().isoformat()), now),
    )

    ready = await service.run("dashboard:web:operator")
    assert ready["status"] == "ready"
    assert ready["safety_failures"] == 0
    persisted = await database.read(
        "SELECT value FROM runtime_setting WHERE key='readiness.self_check'"
    )
    assert json.loads(persisted[0]["value"])["status"] == "ready"
    inbox = await database.read(
        "SELECT state,operator_read_at FROM mail WHERE conversation_key LIKE 'system:self-check:%'"
    )
    assert all(row["state"] == "delivered" and row["operator_read_at"] == now for row in inbox)

    await database.write(
        "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
        "VALUES('system','delivery','safety.delivery.zero','alert:9:stage:2','{}',?)",
        (now,),
    )
    delivery_failure = await service.run("maintenance")
    assert delivery_failure["status"] == "failed"
    assert delivery_failure["safety_failures"] == 1
    assert delivery_failure["failed_checks"] == ["alert_delivery_history"]
    metrics = generate_latest().decode()
    assert (
        'outpost_self_check_state{check="alert_delivery_history",severity="safety"} 0.0' in metrics
    )
    await database.close()


@pytest.mark.asyncio
async def test_self_check_api_is_operator_visible_and_diagnostics_trigger_is_loopback_only(
    tmp_path: Path,
) -> None:
    config = readiness_config(tmp_path)
    database = Database(config.store.path)
    await database.open()
    service = SelfCheckService(
        database,
        config,
        VirtualClock(),
        BackupService(database),
        IntentResolver(config.router.intents_file),
    )
    app = create_web_app(
        lambda: {"radio": "up"},
        database=database,
        self_check=service,
    )
    external = TestClient(app)
    assert external.post("/api/v1/diagnostics/readiness").status_code == 403

    local = TestClient(app, client=("127.0.0.1", 50000))
    run = local.post("/api/v1/diagnostics/readiness")
    assert run.status_code == 200
    assert run.json()["trigger"] == "diagnostics-cli"
    assert local.get("/api/v1/diagnostics/status").json()["readiness"]["status"] == "failed"
    assert external.get("/api/v1/readiness").json()["checks"]
    assert external.get("/api/v1/dashboard/poll").json()["readiness"]["safety_failures"] == 2
    assert external.get("/api/v1/dashboard/overview").json()["readiness"]["status"] == "failed"
    await database.close()


@pytest.mark.asyncio
async def test_configuration_and_inventory_checks_report_specific_operator_evidence(
    tmp_path: Path,
) -> None:
    intents = tmp_path / "broken-intents.yaml"
    intents.write_text("not: a-list\n", encoding="utf-8")
    config = Config.model_validate(
        {
            "node": {"timezone": "Not/A_Real_Zone"},
            "store": {
                "path": str(tmp_path / "outpost.db"),
                "backup": {"keep": 1},
            },
            "router": {"intents_file": str(intents)},
            "web": {"bind": "127.0.0.1", "auth": {"mode": "none"}},
        }
    )
    database = Database(config.store.path)
    await database.open()
    clock = VirtualClock()
    responder = await MemberRepo(database, clock).resolve("!00000001")
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (responder.id,))
    now = int(clock.now().timestamp())
    await database.write(
        "INSERT INTO runtime_setting(key,value,updated_at) VALUES('maintenance.last_date',?,?)",
        (json.dumps(clock.now().date().isoformat()), now),
    )
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    for name in ("outpost-20260101T000000Z.db", "outpost-20260102T000000Z.db"):
        (backup_directory / name).write_bytes(b"backup")

    service = SelfCheckService(
        database,
        config,
        clock,
        BackupService(database),
        IntentResolver(config.router.intents_file),
    )
    report = await service.run("test")
    checks = {item["name"]: item for item in report["checks"]}
    assert report["status"] == "degraded"
    assert report["safety_failures"] == 0
    assert checks["backup_rotation"]["evidence"] == {"file_count": 2, "keep": 1}
    assert checks["intent_map"]["evidence"]["error"].startswith("TypeError:")
    assert checks["configured_keys_effective"]["evidence"]["ineffective_keys"] == ["web.auth.mode"]
    assert checks["timezone"]["evidence"] == {"timezone": "Not/A_Real_Zone"}
    await database.close()
