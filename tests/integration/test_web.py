import pytest
from fastapi.testclient import TestClient

from outpost.clock import VirtualClock
from outpost.store import Database
from outpost.store.members import MemberRepo
from outpost.web.api import create_web_app


def test_health_is_minimal_and_status_has_detail() -> None:
    app = create_web_app(
        lambda: {
            "radio": "down",
            "queues": {"reply": 0},
            "tasks_healthy": False,
            "tasks": {
                "radio-supervisor": {
                    "state": "running",
                    "started_at": 1,
                    "last_ok_at": 2,
                    "error": "must not leave loopback",
                }
            },
            "radio_config": {
                "region": "US",
                "preset": "LONG_FAST",
                "channels": [0, 2],
                "gps": {"lat": 40.0, "lon": -80.0},
            },
            "ai": {"provider": "hailo_vlm", "model": "Qwen3-VL-2B-Instruct"},
        }
    )
    client = TestClient(app)
    health = client.get("/api/v1/health")
    assert health.json() == {"status": "degraded", "version": "0.1.0"}
    assert "queues" not in health.json()
    assert client.get("/api/v1/status").json()["radio"] == "down"
    assert health.headers["x-frame-options"] == "DENY"
    for captive_path in ("/generate_204", "/hotspot-detect.html", "/ncsi.txt", "/connecttest.txt"):
        captive = client.get(captive_path, follow_redirects=False)
        assert captive.status_code == 307
        assert captive.headers["location"] == "/"
    assert client.get("/api/v1/diagnostics/status").status_code == 403

    local = TestClient(app, client=("127.0.0.1", 50000))
    diagnostics = local.get("/api/v1/diagnostics/status")
    assert diagnostics.status_code == 200
    assert diagnostics.json()["tasks"] == {
        "radio-supervisor": {
            "state": "running",
            "started_at": 1,
            "last_ok_at": 2,
        }
    }
    assert diagnostics.json()["radio_config"] == {
        "region": "US",
        "preset": "LONG_FAST",
        "channels": [0, 2],
    }
    assert "gps" not in str(diagnostics.json())
    assert "queues" not in diagnostics.json()

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
    assert operator.status_code == 200 and "Members & radio triage" in operator.text
    bbs = client.get("/bbs.html")
    assert bbs.status_code == 200 and "Boards & discussions" in bbs.text
    radio = client.get("/radio.html")
    assert radio.status_code == 200 and "Radio & traffic" in radio.text
    mail = client.get("/mail.html")
    assert mail.status_code == 200 and "Operator-readable plaintext" in mail.text
    backups = client.get("/backups.html")
    assert backups.status_code == 200 and "Backups contain sensitive location data" in backups.text
    ai = client.get("/ai.html")
    assert ai.status_code == 200 and "Test the guarded path" in ai.text
    navigation = client.get("/nav.js").text
    scheduler = client.get("/refresh-scheduler.js")
    assert scheduler.status_code == 200 and "visibilitychange" in scheduler.text
    federation_script = client.get("/federation.js").text
    assert "LoRa observed" in federation_script and "MQTT observed" in federation_script
    assert "Radio + MQTT" in federation_script
    assert "Peer-provided" in federation_script and "station observation" in federation_script
    assert 'directory.insertAdjacentElement("afterend", panel)' in federation_script
    watch_script = client.get("/watch.js").text
    assert "IDENTITY & RECONCILIATION" in watch_script
    assert "data-merge-target" in watch_script and "data-unmerge-source" in watch_script
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


@pytest.mark.asyncio
async def test_dashboard_poll_batches_status_and_revalidates_with_etag(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    peer_id = await database.write(
        "INSERT INTO fed_peer(mesh_id,node_name,state,created_at) "
        "VALUES('!00000002','Remote Outpost','active',1)"
    )
    for index, stream in enumerate(("board:gen", "incidents", "alerts"), start=1):
        await database.write(
            "INSERT INTO fed_inbox_item(peer_id,stream,uid,payload_json,digest,state,"
            "received_at) VALUES(?,?,?,?,?,'pending',1)",
            (peer_id, stream, f"remote:{index}", "{}", f"digest-{index}"),
        )
    await database.write(
        "INSERT INTO mail(uid,from_label,to_label,body,created_at,state,expires_at,"
        "conversation_key,mail_direction) VALUES('remote:mail','operator@REMOTE','operator',"
        "'Please review',1,'delivered',9999999999,'fed:remote:one','in')"
    )

    client = TestClient(create_web_app(lambda: {"radio": "up"}, database))
    response = client.get("/api/v1/dashboard/poll")
    assert response.status_code == 200
    assert response.json()["reviews"] == {
        "total": 3,
        "board": 1,
        "incidents": 1,
        "alerts": 1,
    }
    assert response.json()["mail"] == {"actionable": 1}
    assert response.json()["modules"]["items"]["bbs"]["enabled"] is True
    assert response.headers["cache-control"] == "private, max-age=0, must-revalidate"

    unchanged = client.get(
        "/api/v1/dashboard/poll",
        headers={"if-none-match": response.headers["etag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == response.headers["etag"]

    await database.close()
