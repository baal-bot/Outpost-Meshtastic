import pytest
from fastapi.testclient import TestClient

from outpost.clock import VirtualClock
from outpost.store import Database
from outpost.store.members import MemberRepo
from outpost.web.api import create_web_app


def test_health_is_minimal_and_status_has_detail() -> None:
    app = create_web_app(lambda: {"radio": "down", "queues": {"reply": 0}})
    client = TestClient(app)
    health = client.get("/api/v1/health")
    assert health.json() == {"status": "degraded", "version": "0.1.0"}
    assert "queues" not in health.json()
    assert client.get("/api/v1/status").json()["radio"] == "down"
    assert health.headers["x-frame-options"] == "DENY"

    failed_tasks = TestClient(create_web_app(lambda: {"radio": "up", "tasks_healthy": False})).get(
        "/api/v1/health"
    )
    assert failed_tasks.json() == {"status": "degraded", "version": "0.1.0"}


@pytest.mark.asyncio
async def test_read_only_bbs_api_is_paginated_and_never_exposes_channel_keys(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    await database.write("UPDATE channel_dir SET psk_b64='secret' WHERE name='public'")
    member = await MemberRepo(database, VirtualClock()).resolve("!00000001")
    await database.write("UPDATE member SET handle='dana',trust='member' WHERE id=?", (member.id,))
    await MemberRepo(database, VirtualClock()).resolve("!00000002")
    client = TestClient(create_web_app(lambda: {"radio": "up"}, database))

    await database.write(
        "INSERT INTO safety_floor_attempt(member_mesh_id,command,fingerprint,first_seen_at,"
        "last_seen_at,accepted_at,attempt_count,coalesced_count) "
        "VALUES('!00000001','HELPME','test',unixepoch(),unixepoch(),unixepoch(),3,2)"
    )
    safety = client.get("/api/v1/security/safety-floor").json()
    assert safety["summary"]["attempts"] == 3
    assert safety["summary"]["coalesced"] == 2
    assert safety["items"][0]["member_mesh_id"] == "!00000001"

    boards = client.get("/api/v1/boards?limit=2").json()
    assert len(boards["items"]) == 2 and boards["next_cursor"] == 2
    channels = client.get("/api/v1/channels").json()
    assert channels["items"] and "psk_b64" not in channels["items"][0]
    overview = client.get("/api/v1/dashboard/overview").json()
    assert overview["members"]["members_total"] == 1
    assert len(client.get("/api/v1/members?view=all").json()["items"]) == 2
    assert "text" not in str(overview["activity"])
    dashboard = client.get("/")
    assert dashboard.status_code == 200 and "AIRTIME · ROLLING HOUR" in dashboard.text
    assert "outpost.operator.authenticated" in client.get("/app.js").text
    assert "System capabilities" in dashboard.text
    assert client.get("/Figtree-Variable.ttf").status_code == 200
    operator = client.get("/operator.html")
    assert operator.status_code == 200 and "Members & moderation" in operator.text
    bbs = client.get("/bbs.html")
    assert bbs.status_code == 200 and "Boards & discussions" in bbs.text
    radio = client.get("/radio.html")
    assert radio.status_code == 200 and "Radio & traffic" in radio.text
    mail = client.get("/mail.html")
    assert mail.status_code == 200 and "Operator-readable plaintext" in mail.text
    backups = client.get("/backups.html")
    assert backups.status_code == 200 and "Backups contain sensitive location data" in backups.text
    navigation = client.get("/nav.js").text
    federation_script = client.get("/federation.js").text
    assert "LoRa observed" in federation_script and "MQTT observed" in federation_script
    assert "Radio + MQTT" in federation_script
    assert 'directory.insertAdjacentElement("afterend", panel)' in federation_script
    for label in (
        "Overview",
        "Members",
        "BBS",
        "Mail",
        "Radio",
        "Backups",
        "Activity",
        "System",
        "AI",
    ):
        assert label in navigation
    await database.close()
