import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from outpost.setup_token import _run as run_setup_token
from outpost.store import Database
from outpost.web.api import create_web_app
from outpost.web.auth import WebAuthService


@pytest.mark.asyncio
async def test_password_session_csrf_and_forced_change(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    auth = WebAuthService(database, 12)
    setup = await auth.ensure_credential()
    assert setup is not None and setup.path == tmp_path / "setup-token"
    assert setup.path.stat().st_mode & 0o777 == 0o600
    assert setup.path.stat().st_uid == os.getuid()
    initial = setup.path.read_text().strip()
    assert initial and initial not in setup.path.name
    unchanged = await auth.ensure_credential()
    assert unchanged == setup and setup.path.read_text().strip() == initial
    client = TestClient(create_web_app(lambda: {"radio": "up"}, database, auth))

    assert client.get("/api/v1/health").status_code == 200
    setup_status = client.get("/api/v1/auth/setup")
    assert setup_status.status_code == 200
    assert setup_status.json() == {
        "required": True,
        "available": True,
        "expires_at": setup.expires_at,
    }
    assert client.get("/api/v1/status").status_code == 401
    login = client.post("/api/v1/auth/login", json={"password": initial})
    assert login.status_code == 200 and login.json()["must_change"] is True
    assert not setup.path.exists()
    assert (
        TestClient(create_web_app(lambda: {"radio": "up"}, database, auth))
        .post("/api/v1/auth/login", json={"password": initial})
        .status_code
        == 401
    )
    csrf = login.json()["csrf_token"]
    assert client.get("/api/v1/status").status_code == 403
    replacement = "new-operator-" + "password-42"
    no_csrf = client.post(
        "/api/v1/auth/password",
        json={"current_password": "", "new_password": replacement},
    )
    assert no_csrf.status_code == 403
    changed = client.post(
        "/api/v1/auth/password",
        headers={"x-csrf-token": csrf},
        json={"current_password": "", "new_password": replacement},
    )
    assert changed.status_code == 200 and changed.json()["reauthenticate"] is True
    assert await database.read("SELECT 1 FROM web_session") == []
    assert client.get("/api/v1/status").status_code == 401
    assert client.get("/api/v1/auth/setup").json()["required"] is False
    permanent_login = client.post("/api/v1/auth/login", json={"password": replacement})
    assert permanent_login.status_code == 200
    csrf = permanent_login.json()["csrf_token"]
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


@pytest.mark.asyncio
async def test_setup_secret_expiry_requires_local_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 2_000_000_000
    monkeypatch.setattr("outpost.web.auth.time.time", lambda: now)
    database = Database(tmp_path / "outpost.db")
    await database.open()
    auth = WebAuthService(database, 12, setup_ttl_seconds=60)
    setup = await auth.ensure_credential()
    assert setup is not None
    expired = setup.path.read_text().strip()

    monkeypatch.setattr("outpost.web.auth.time.time", lambda: now + 61)
    assert (await auth.setup_status())["available"] is False
    assert not setup.path.exists()
    assert await auth.login(expired, "test") is None
    assert await auth.ensure_credential() is None

    recovered = await auth.issue_setup_secret()
    replacement = recovered.path.read_text().strip()
    assert replacement != expired
    assert await auth.login(replacement, "test") is not None
    await database.close()


@pytest.mark.asyncio
async def test_local_setup_token_recovery_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "outpost.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"store:\n  path: {database_path}\n", encoding="utf-8")
    database = Database(database_path)
    await database.open()
    await database.close()

    assert await run_setup_token("reset", config_path) == 0
    assert "all dashboard sessions were invalidated" in capsys.readouterr().out
    token = (tmp_path / "setup-token").read_text().strip()
    assert await run_setup_token("show", config_path) == 0
    assert capsys.readouterr().out.strip() == token

    database = Database(database_path)
    await database.open()
    auth = WebAuthService(database, 12)
    assert await auth.login(token, "local-test") is not None
    await database.close()

    assert await run_setup_token("status", config_path) == 1
    assert "setup is incomplete" in capsys.readouterr().out
