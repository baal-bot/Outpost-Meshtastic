import pytest
from fastapi.testclient import TestClient

from outpost.store import Database
from outpost.web.api import create_web_app
from outpost.web.auth import WebAuthService


@pytest.mark.asyncio
async def test_password_session_csrf_and_forced_change(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    auth = WebAuthService(database, 12)
    initial = await auth.ensure_credential()
    assert initial is not None
    client = TestClient(create_web_app(lambda: {"radio": "up"}, database, auth))

    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/status").status_code == 401
    login = client.post("/api/v1/auth/login", json={"password": initial})
    assert login.status_code == 200 and login.json()["must_change"] is True
    csrf = login.json()["csrf_token"]
    assert client.get("/api/v1/status").status_code == 403
    replacement = "new-operator-" + "password-42"
    no_csrf = client.post(
        "/api/v1/auth/password",
        json={"current_password": initial, "new_password": replacement},
    )
    assert no_csrf.status_code == 403
    changed = client.post(
        "/api/v1/auth/password",
        headers={"x-csrf-token": csrf},
        json={"current_password": initial, "new_password": replacement},
    )
    assert changed.status_code == 200
    assert client.get("/api/v1/status").status_code == 200
    member_id = await database.write(
        "INSERT INTO member(mesh_id,mesh_num,first_seen,last_seen) VALUES('!00000001',1,1,1)"
    )
    approved = client.get("/api/v1/members").json()
    assert approved["items"] == [] and approved["discovered_count"] == 1
    members = client.get("/api/v1/members?view=all")
    assert members.status_code == 200 and members.json()["items"][0]["mesh_id"] == "!00000001"
    update = client.patch(
        f"/api/v1/members/{member_id}",
        headers={"x-csrf-token": csrf},
        json={"trust": "trusted", "notes": "known neighbor"},
    )
    assert update.status_code == 200
    audit = client.get("/api/v1/audit").json()["items"]
    assert audit[0]["action"] == "member.update"
    mail_id = await database.write(
        """
        INSERT INTO mail(
          uid,from_label,to_label,body,created_at,state,expires_at
        ) VALUES('local:1','dana','ray','private body',1,'queued',9999999999)
        """
    )
    summary = client.get("/api/v1/mail").json()["items"][0]
    assert "body" not in summary and summary["body_length"] == 12
    detail = client.get(f"/api/v1/mail/{mail_id}")
    assert detail.status_code == 200 and detail.json()["body"] == "private body"
    mail_audit = client.get("/api/v1/audit").json()["items"][0]
    assert mail_audit["action"] == "mail.view" and mail_audit["target"] == f"mail:{mail_id}"
    await database.close()
