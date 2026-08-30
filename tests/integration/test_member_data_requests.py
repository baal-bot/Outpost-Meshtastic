from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from outpost.app import OutpostApp
from outpost.config import Config
from outpost.render.renderer import render_response
from outpost.store.backups import BackupService
from outpost.store.maintenance import MaintenanceService
from outpost.transport.chunker import chunk_text
from outpost.transport.models import InboundMessage
from outpost.web.api import create_web_app
from outpost.web.member_triage import MemberTriageService

pytestmark = pytest.mark.production_wiring


def command(packet_id: int, text: str, *, key: bytes | None = None) -> InboundMessage:
    return InboundMessage(
        packet_id=packet_id,
        from_id="!00000001",
        to_id="!699c2f30",
        channel=0,
        portnum=1,
        is_direct=True,
        text=text,
        payload=None,
        rx_time=datetime.now(UTC),
        pki_encrypted=key is not None,
        pki_public_key=key,
    )


@pytest.mark.asyncio
async def test_member_data_summary_verified_requests_and_operator_pseudonymization(
    tmp_path,
) -> None:
    config = Config.model_validate(
        {
            "store": {
                "path": str(tmp_path / "outpost.db"),
                "retention": {
                    "posts_days": 45,
                    "mail_days": 60,
                    "member_positions_hours": 24,
                    "message_log_days": 30,
                    "message_log_max_rows": 10_000,
                    "incident_history_days": 30,
                    "watch_history_days": 365,
                    "outbound_history_days": 30,
                },
            },
            "modules": {"watch": {"enabled": True}},
        }
    )
    app = OutpostApp(config)
    await app.database.open()
    key = bytes(range(32))
    try:
        named = await app.router.dispatch(command(1, "NAME alex", key=key))
        assert named.kind == "ack"
        member = await app.router.members.by_handle("alex")
        assert member is not None
        await MemberTriageService(app.database).review_pki(
            member.id, "approve", "Fingerprint verified with member"
        )
        now = int(app.clock.now().timestamp())
        await app.database.write(
            "INSERT INTO member_position(member_id,lat,lon,received_at,expires_at) "
            "VALUES(?,40.4,-80.0,?,?)",
            (member.id, now, now + 3_600),
        )
        await app.database.write(
            "INSERT INTO pending_incident_location(member_id,lat,lon,created_at,expires_at) "
            "VALUES(?,40.4,-80.0,?,?)",
            (member.id, now, now + 600),
        )
        await app.database.write(
            "INSERT INTO message_log(direction,member_id,peer_mesh_id,channel,portnum,is_direct,"
            "text,payload,byte_len,pki_public_key,latitude,longitude,created_at) "
            "VALUES('in',?,?,0,1,1,'private packet',x'0102',14,?,40.4,-80.0,?)",
            (member.id, member.mesh_id, key, now),
        )
        post_response = await app.router.dispatch(
            command(2, "POST gen A public note from this member")
        )
        assert post_response.kind == "ack"
        await app.database.write(
            "INSERT INTO mail(uid,from_id,from_label,to_label,body,created_at,state,expires_at) "
            "VALUES('member-private',?,'alex','operator','private mail',?,'delivered',?)",
            (member.id, now, now + 86_400),
        )
        await app.database.write(
            "INSERT INTO checkin(member_id,status,note,lat,lon,created_at) "
            "VALUES(?,'ok','safe at home',40.4,-80.0,?)",
            (member.id, now),
        )
        incident, _ = await app.incidents.create("Tree down at the member location", member)
        assert incident is not None
        await app.database.write(
            "INSERT INTO ai_interaction(member_id,channel,question,question_class,provider,model,"
            "answer,outcome,created_at) VALUES(?,0,'private question','general','null','none',"
            "'private answer','ok',?)",
            (member.id, now),
        )

        summary_response = await app.router.dispatch(command(3, "MYDATA"))
        summary = render_response(summary_response)
        assert "position 1" in summary
        assert "messages 1" in summary
        assert "mail 1" in summary
        assert "posts 1" in summary
        assert "Welfare 1" in summary
        assert "incidents/updates 1" in summary
        assert "AI 1" in summary
        summary_parts = chunk_text(summary, max_parts=summary_response.max_parts or 1)
        assert len(summary_parts) <= 3
        assert all("…MORE" not in part for part in summary_parts)

        denied = await app.router.dispatch(command(4, "FORGETPOS CONFIRM"))
        assert denied.kind == "error"
        assert "Verified action denied" in render_response(denied)
        deleted = await app.router.dispatch(command(5, "FORGETPOS CONFIRM", key=key))
        assert "Exact position deleted" in render_response(deleted)
        assert (
            await app.database.read("SELECT 1 FROM member_position WHERE member_id=?", (member.id,))
            == []
        )
        assert (
            await app.database.read(
                "SELECT 1 FROM pending_incident_location WHERE member_id=?", (member.id,)
            )
            == []
        )
        position_audit = (
            await app.database.read(
                "SELECT actor_kind,actor_ref FROM audit_log "
                "WHERE action='member.position_delete' ORDER BY id DESC LIMIT 1"
            )
        )[0]
        assert dict(position_audit) == {"actor_kind": "mesh", "actor_ref": member.mesh_id}

        await app.database.write(
            "INSERT INTO member_position(member_id,lat,lon,received_at,expires_at) "
            "VALUES(?,40.4,-80.0,?,?)",
            (member.id, now, now + 3_600),
        )
        requested = await app.router.dispatch(command(6, "REMOVEME", key=key))
        assert "sent for operator review" in render_response(requested)
        duplicate = await app.router.dispatch(command(7, "REMOVEME", key=key))
        assert "already awaiting" in render_response(duplicate)
        assert (
            await app.database.read(
                "SELECT COUNT(*) count FROM member_data_request WHERE state='pending'"
            )
        )[0]["count"] == 1

        client = TestClient(
            create_web_app(
                lambda: {"radio": "up"},
                database=app.database,
                member_data=app.member_data,
            )
        )
        public_policy = client.get("/api/v1/privacy/retention")
        assert public_policy.status_code == 200
        policy = public_policy.json()
        assert policy["generated_from"].startswith("validated store.retention")
        assert policy["categories"][0]["window"] == "up to 24 hours"
        assert "45 days" in next(
            item["window"] for item in policy["categories"] if item["key"] == "posts"
        )
        queue = client.get("/api/v1/member-data-requests?state=all").json()
        assert queue["pending"] == 1
        request = queue["items"][0]
        assert request["member_label"] == "alex"
        poll = client.get("/api/v1/dashboard/poll").json()
        assert poll["reviews"]["data_requests"] == 1
        inbox = client.get("/api/v1/mail/conversations").json()
        assert inbox["total"] >= 1
        assert request["conversation_key"] in {item["conversation_key"] for item in inbox["items"]}
        assert (
            client.get(f"/api/v1/mail/conversations/{request['conversation_key']}").status_code
            == 200
        )
        assert client.get("/api/v1/dashboard/poll").json()["mail"]["actionable"] == 1

        approved = client.post(
            f"/api/v1/member-data-requests/{request['id']}/review",
            json={"action": "approve", "reason": "Identity verified and policy explained"},
        )
        assert approved.status_code == 200
        assert approved.json()["state"] == "approved"
        assert client.get("/api/v1/dashboard/poll").json()["reviews"]["data_requests"] == 0
        assert client.get("/api/v1/dashboard/poll").json()["mail"]["actionable"] == 0

        retained = (
            await app.database.read(
                "SELECT mesh_id,handle,long_name,public_key,pki_state,trust,prefs,notes,"
                "directory_state FROM member WHERE id=?",
                (member.id,),
            )
        )[0]
        assert retained["mesh_id"] != member.mesh_id
        assert retained["mesh_id"].startswith("!") and len(retained["mesh_id"]) == 9
        assert retained["handle"] is None and retained["public_key"] is None
        assert retained["pki_state"] == "unknown" and retained["trust"] == "guest"
        assert retained["prefs"] == '{"position":"off"}'
        assert retained["directory_state"] == "ignored"
        assert (
            await app.database.read("SELECT 1 FROM member_position WHERE member_id=?", (member.id,))
            == []
        )
        packet = (
            await app.database.read(
                "SELECT text,payload,pki_public_key,latitude,longitude,peer_mesh_id "
                "FROM message_log WHERE member_id=?",
                (member.id,),
            )
        )[0]
        assert all(packet[key] is None for key in ("text", "payload", "pki_public_key"))
        assert packet["latitude"] is None and packet["longitude"] is None
        assert packet["peer_mesh_id"] == retained["mesh_id"]
        ai = (await app.database.read("SELECT * FROM ai_interaction"))[0]
        assert ai["member_id"] is None and ai["question"] == "" and ai["answer"] is None
        checkin = (await app.database.read("SELECT lat,lon FROM checkin"))[0]
        assert checkin["lat"] is None and checkin["lon"] is None
        assert (await app.database.read("SELECT author_label FROM post"))[0][
            "author_label"
        ].startswith("former-")
        retained_incident = (
            await app.database.read(
                "SELECT reporter_label,lat,lon FROM incident WHERE id=?", (incident.id,)
            )
        )[0]
        assert retained_incident["reporter_label"].startswith("former-")
        assert retained_incident["lat"] == pytest.approx(40.4)
        assert retained_incident["lon"] == pytest.approx(-80.0)
        assert await app.database.read(
            "SELECT 1 FROM member_pki_event WHERE member_id=?", (member.id,)
        )
        assert await app.database.read(
            "SELECT 1 FROM audit_log WHERE action='member.data_removal_approved'"
        )
        assert "data-review-request" in client.get("/mail.js").text
        assert "max-height: min(62vh, 620px)" in client.get("/member-data.css").text
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_pending_removal_request_survives_maintenance_until_rejected(tmp_path) -> None:
    config = Config.model_validate(
        {
            "store": {
                "path": str(tmp_path / "outpost.db"),
                "backup": {"enabled": False},
            }
        }
    )
    app = OutpostApp(config)
    await app.database.open()
    try:
        member = await app.router.members.resolve("!00000002")
        request, created = await app.member_data.request_removal(member)
        assert created is True
        await app.database.write(
            "UPDATE mail SET created_at=0,expires_at=0 WHERE conversation_key=?",
            (request["conversation_key"],),
        )
        maintenance = MaintenanceService(
            app.database, BackupService(app.database), app.clock, config
        )

        pending_run = await maintenance.run()
        assert pending_run.mail == 0
        assert await app.database.read(
            "SELECT 1 FROM mail WHERE conversation_key=?", (request["conversation_key"],)
        )

        client = TestClient(
            create_web_app(
                lambda: {"radio": "up"},
                database=app.database,
                member_data=app.member_data,
            )
        )
        rejected = client.post(
            f"/api/v1/member-data-requests/{request['id']}/review",
            json={"action": "reject", "reason": "Request withdrawn by verified member"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["state"] == "rejected"

        reviewed_run = await maintenance.run()
        assert reviewed_run.mail == 1
        assert (
            await app.database.read(
                "SELECT 1 FROM mail WHERE conversation_key=?", (request["conversation_key"],)
            )
            == []
        )
        assert await app.database.read(
            "SELECT 1 FROM audit_log WHERE action='member.data_removal_rejected'"
        )
    finally:
        await app.database.close()
