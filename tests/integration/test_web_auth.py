import asyncio
import base64
import os
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from outpost.clock import SystemClock
from outpost.setup_token import _run as run_setup_token
from outpost.store import Database
from outpost.store.members import MemberRepo
from outpost.web.api import create_web_app
from outpost.web.auth import WebAuthService, _totp


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
    assert login.headers["cache-control"] == "no-store"
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
    unreviewed_update = client.patch(
        f"/api/v1/members/{member_id}",
        headers={"x-csrf-token": csrf},
        json={"trust": "trusted"},
    )
    assert unreviewed_update.status_code == 422
    update = client.patch(
        f"/api/v1/members/{member_id}",
        headers={"x-csrf-token": csrf},
        json={
            "trust": "trusted",
            "notes": "known neighbor",
            "reason": "Known neighbor verified",
        },
    )
    assert update.status_code == 422
    assert update.json()["error"]["code"] == "pki_required"
    await MemberRepo(database, SystemClock()).resolve(
        "!00000001", authenticated_pki_key=bytes(range(32))
    )
    pending = client.get(f"/api/v1/members/{member_id}").json()["member"]
    assert pending["pki_state"] == "pending"
    reviewed_key = client.post(
        f"/api/v1/members/{member_id}/pki",
        headers={"x-csrf-token": csrf},
        json={"action": "approve", "reason": "Fingerprint verified in person"},
    )
    assert reviewed_key.status_code == 200
    update = client.patch(
        f"/api/v1/members/{member_id}",
        headers={"x-csrf-token": csrf},
        json={
            "trust": "trusted",
            "notes": "known neighbor",
            "reason": "Known neighbor verified",
        },
    )
    assert update.status_code == 200
    detail = client.get(f"/api/v1/members/{member_id}")
    assert detail.status_code == 200
    assert detail.json()["trust_history"][0]["reason"] == "Known neighbor verified"
    exported = client.get(f"/api/v1/members/export?ids={member_id}")
    assert exported.status_code == 200
    assert "position_lat" not in exported.text
    audit = client.get("/api/v1/audit").json()["items"]
    assert {item["action"] for item in audit} >= {
        "member.update",
        "member.export",
        "member.pki.approve",
    }
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


@pytest.mark.asyncio
async def test_local_recovery_restores_bootstrap_admin_and_clears_mfa(tmp_path: Path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    auth = WebAuthService(database, 12)
    await auth.ensure_credential()
    await database.write(
        "UPDATE web_account SET role='operator',totp_secret='lost-secret',"
        "totp_pending_secret='pending-secret',totp_confirmed_at=1,"
        "recovery_code_hashes='[\"lost-code-hash\"]' WHERE id=1"
    )
    await database.write(
        "INSERT INTO web_session(token_hash,csrf_token,account_id,created_at,expires_at,"
        "last_seen_at) VALUES('stale','stale-csrf',1,1,9999999999,1)"
    )

    recovered = await auth.issue_setup_secret()
    account = (await database.read("SELECT * FROM web_account WHERE id=1"))[0]
    assert account["role"] == "administrator"
    assert account["totp_secret"] is None
    assert account["totp_pending_secret"] is None
    assert account["totp_confirmed_at"] is None
    assert account["recovery_code_hashes"] == "[]"
    assert await database.read("SELECT 1 FROM web_session") == []
    token = recovered.path.read_text().strip()
    assert await auth.login(token, "local-recovery", username="operator") is not None
    await database.close()


async def _permanent_operator(auth: WebAuthService, client: TestClient, password: str) -> str:
    setup = await auth.ensure_credential()
    assert setup is not None
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "operator", "password": setup.path.read_text().strip()},
    )
    csrf = login.json()["csrf_token"]
    changed = client.post(
        "/api/v1/auth/password",
        headers={"x-csrf-token": csrf},
        json={"current_password": "", "new_password": password},
    )
    assert changed.status_code == 200
    login = client.post("/api/v1/auth/login", json={"username": "operator", "password": password})
    assert login.status_code == 200
    return str(login.json()["csrf_token"])


@pytest.mark.asyncio
async def test_login_throttle_spans_sources_but_valid_credentials_bypass_hostile_lockout(
    tmp_path: Path,
) -> None:
    delays: list[float] = []

    async def record_delay(seconds: float) -> None:
        delays.append(seconds)

    database = Database(tmp_path / "outpost.db")
    await database.open()
    auth = WebAuthService(
        database,
        12,
        source_failure_limit=5,
        account_failure_limit=2,
        global_failure_limit=20,
        throttle_base_seconds=2,
        throttle_max_seconds=8,
        sleep=record_delay,
    )
    client = TestClient(create_web_app(lambda: {"radio": "up"}, database, auth))
    password = "operator-password-42"  # noqa: S105 - isolated test credential
    await _permanent_operator(auth, client, password)

    for username in ("operator", "unknown-user"):
        assert await auth.login("wrong-password", f"source-{username}-a", username=username) is None
        assert await auth.login("wrong-password", f"source-{username}-b", username=username) is None
    assert delays == [2, 2]

    known = client.post(
        "/api/v1/auth/login", json={"username": "operator", "password": "wrong-password"}
    )
    unknown = client.post(
        "/api/v1/auth/login", json={"username": "unknown-user", "password": "wrong-password"}
    )
    assert known.status_code == unknown.status_code == 401
    assert (
        known.json()
        == unknown.json()
        == {"error": {"code": "invalid_login", "message": "Invalid credentials."}}
    )
    assert delays[-2:] == [4, 4]

    legitimate = client.post(
        "/api/v1/auth/login", json={"username": "operator", "password": password}
    )
    assert legitimate.status_code == 200
    assert legitimate.json()["recent_failed_attempts"] == 3
    security = await auth.login_security_status()
    operator = next(
        item
        for item in security["identities"]
        if item["username"] == "operator"  # type: ignore[index]
    )
    assert operator["throttled"] is True
    assert operator["source_count"] == 3
    inventory = await auth.accounts(security)
    assert inventory[0]["login_throttled"] is True
    assert inventory[0]["failed_attempts_recent"] == 3
    assert (
        len(await database.read("SELECT 1 FROM audit_log WHERE action='auth.login_throttled'")) == 2
    )
    notices = await database.read(
        "SELECT state,operator_read_at FROM mail "
        "WHERE conversation_key LIKE 'system:auth-throttle:account:%'"
    )
    assert len(notices) == 2
    assert all(row["state"] == "failed" and row["operator_read_at"] is None for row in notices)
    await database.close()


@pytest.mark.asyncio
async def test_global_login_failure_cap_throttles_attempts_spread_across_names(
    tmp_path: Path,
) -> None:
    delays: list[float] = []

    async def record_delay(seconds: float) -> None:
        delays.append(seconds)

    database = Database(tmp_path / "outpost.db")
    await database.open()
    auth = WebAuthService(
        database,
        12,
        source_failure_limit=5,
        account_failure_limit=5,
        global_failure_limit=5,
        throttle_base_seconds=3,
        throttle_max_seconds=12,
        sleep=record_delay,
    )
    for index in range(5):
        assert (
            await auth.login("wrong-password", f"source-{index}", username=f"unknown-{index}")
            is None
        )
    assert delays == [3]
    security = await auth.login_security_status()
    assert security["total_failures"] == 5
    assert security["global_throttled"] is True
    assert len(await database.read("SELECT 1 FROM audit_log WHERE target='global'")) == 1
    notices = await database.read(
        "SELECT subject,state FROM mail WHERE conversation_key='system:auth-throttle:global'"
    )
    assert len(notices) == 1
    assert notices[0]["state"] == "failed"
    await database.close()


@pytest.mark.asyncio
async def test_named_roles_sessions_and_last_administrator_guard(tmp_path: Path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    auth = WebAuthService(database, 12)
    client = TestClient(create_web_app(lambda: {"radio": "up"}, database, auth))
    admin_password = "operator-password-42"  # noqa: S105 - isolated test credential
    csrf = await _permanent_operator(auth, client, admin_password)

    created = client.post(
        "/api/v1/auth/accounts",
        headers={"x-csrf-token": csrf},
        json={
            "username": "wallboard",
            "display_name": "Operations Display",
            "role": "viewer",
            "initial_password": "wallboard-initial-42",
        },
    )
    assert created.status_code == 200
    assert created.json()["role"] == "viewer" and created.json()["must_change"] is True
    account_id = created.json()["id"]

    only_admin = client.patch(
        "/api/v1/auth/accounts/1",
        headers={"x-csrf-token": csrf},
        json={"role": "operator"},
    )
    assert only_admin.status_code == 422
    assert "administrator" in only_admin.json()["error"]["message"]

    viewer_app = create_web_app(lambda: {"radio": "up"}, database, auth)
    viewer_app.add_api_route(
        "/api/v1/future-sensitive",
        lambda: {"secret": "a newly registered route must not inherit viewer access"},
        methods=["GET"],
    )
    viewer = TestClient(viewer_app)
    first = viewer.post(
        "/api/v1/auth/login",
        json={"username": "wallboard", "password": "wallboard-initial-42"},
    )
    assert first.status_code == 200 and first.json()["must_change"] is True
    changed = viewer.post(
        "/api/v1/auth/password",
        headers={"x-csrf-token": first.json()["csrf_token"]},
        json={"current_password": "", "new_password": "wallboard-permanent-42"},
    )
    assert changed.status_code == 200
    login = viewer.post(
        "/api/v1/auth/login",
        json={"username": "wallboard", "password": "wallboard-permanent-42"},
    )
    assert login.status_code == 200 and login.json()["role"] == "viewer"
    viewer_csrf = login.json()["csrf_token"]
    sensitive_routes = (
        "/api/v1/status",
        "/api/v1/dashboard/overview",
        "/api/v1/dashboard/poll",
        "/api/v1/members",
        "/api/v1/members/map",
        "/api/v1/members/1",
        "/api/v1/watch/map",
        "/api/v1/mesh/messages",
        "/api/v1/mail",
        "/api/v1/mail/",
        "/api/v1/config",
        "/api/v1/radio/config",
        "/api/v1/audit",
        "/api/v1/backups",
        "/api/v1/ai/status",
        "/api/v1/future-sensitive",
    )
    for route in sensitive_routes:
        response = viewer.get(route)
        assert response.status_code == 403, route
        assert response.json()["error"]["code"] == "read_only"
    wallboard = viewer.get("/api/v1/wallboard/summary")
    assert wallboard.status_code == 200
    assert wallboard.json()["privacy"]["mode"] == "aggregate"
    assert viewer.get("/api/v1/wallboard/summary/").status_code == 404
    assert viewer.get("/api/v1/auth/sessions").status_code == 200
    assert viewer.get("/api/v1/auth/accounts").status_code == 403
    public_or_self_service = {
        "/api/v1/health",
        "/api/v1/runtime",
        "/api/v1/diagnostics/status",
        "/api/v1/auth/setup",
        "/api/v1/auth/session",
        "/api/v1/auth/sessions",
        "/api/v1/wallboard/summary",
    }
    for route in viewer_app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set())
        if (
            not path.startswith("/api/v1/")
            or "GET" not in methods
            or path in public_or_self_service
            or path.startswith("/api/v1/recovery/restores/")
        ):
            continue
        concrete = re.sub(r"\{[^}]+\}", "1", path)
        for candidate in (concrete, f"{concrete}/"):
            gated = viewer.get(candidate)
            assert gated.status_code == 403, candidate
            assert gated.json()["error"]["code"] == "read_only", candidate
    denied = viewer.patch(
        "/api/v1/members/1",
        headers={"x-csrf-token": viewer_csrf},
        json={"notes": "should not write"},
    )
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "read_only"

    disabled = client.patch(
        f"/api/v1/auth/accounts/{account_id}",
        headers={"x-csrf-token": csrf},
        json={"enabled": False},
    )
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    assert viewer.get("/api/v1/wallboard/summary").status_code == 401
    await database.close()


@pytest.mark.asyncio
async def test_mesh_operators_are_inventoried_and_linked_to_named_web_accounts(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    auth = WebAuthService(database, 12)
    client = TestClient(create_web_app(lambda: {"radio": "up"}, database, auth))
    csrf = await _permanent_operator(auth, client, "operator-password-42")

    member = await MemberRepo(database, SystemClock()).resolve(
        "!00000666", authenticated_pki_key=bytes(range(32))
    )
    await database.write("UPDATE member SET handle='666' WHERE id=?", (member.id,))
    reviewed = client.post(
        f"/api/v1/members/{member.id}/pki",
        headers={"x-csrf-token": csrf},
        json={"action": "approve", "reason": "Operator verified this handheld"},
    )
    assert reviewed.status_code == 200
    promoted = client.patch(
        f"/api/v1/members/{member.id}",
        headers={"x-csrf-token": csrf},
        json={"trust": "operator", "reason": "Assigned as an Outpost operator radio"},
    )
    assert promoted.status_code == 200

    inventory = client.get("/api/v1/auth/accounts")
    assert inventory.status_code == 200
    radio = inventory.json()["operator_radios"][0]
    assert radio["mesh_id"] == "!00000666"
    assert radio["handle"] == "666"
    assert radio["account_id"] is None

    created = client.post(
        "/api/v1/auth/accounts",
        headers={"x-csrf-token": csrf},
        json={
            "username": "fieldlead",
            "display_name": "Field Lead",
            "role": "operator",
            "initial_password": "field-lead-password-42",
        },
    )
    assert created.status_code == 200
    account_id = created.json()["id"]
    linked = client.patch(
        f"/api/v1/auth/accounts/{account_id}/radio",
        headers={"x-csrf-token": csrf},
        json={"member_id": member.id},
    )
    assert linked.status_code == 200
    assert linked.json()["operator_radio"]["mesh_id"] == "!00000666"
    assert linked.json()["operator_radio"]["pki_state"] == "verified"

    already_owned = client.patch(
        "/api/v1/auth/accounts/1/radio",
        headers={"x-csrf-token": csrf},
        json={"member_id": member.id},
    )
    assert already_owned.status_code == 422
    assert "@fieldlead" in already_owned.json()["error"]["message"]

    await database.write("UPDATE member SET trust='guest' WHERE id=?", (member.id,))
    stale_link = client.get("/api/v1/auth/accounts").json()
    assert stale_link["operator_radios"][0]["trust"] == "guest"
    assert stale_link["operator_radios"][0]["account_id"] == account_id

    downgraded = client.patch(
        f"/api/v1/auth/accounts/{account_id}",
        headers={"x-csrf-token": csrf},
        json={"role": "viewer"},
    )
    assert downgraded.status_code == 200
    assert downgraded.json()["operator_radio"] is None
    after = client.get("/api/v1/auth/accounts").json()
    assert after["operator_radios"] == []
    actions = {row["action"] for row in await database.read("SELECT action FROM audit_log")}
    assert {"auth.operator_radio_link", "auth.account_update"} <= actions
    await database.close()


@pytest.mark.asyncio
async def test_wallboard_summary_omits_sensitive_fields_and_does_not_mutate(tmp_path: Path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    auth = WebAuthService(database, 12)
    operator = TestClient(create_web_app(lambda: {"radio": "up"}, database, auth))
    csrf = await _permanent_operator(auth, operator, "operator-password-42")
    created = operator.post(
        "/api/v1/auth/accounts",
        headers={"x-csrf-token": csrf},
        json={
            "username": "wallboard",
            "display_name": "Shared display",
            "role": "viewer",
            "initial_password": "wallboard-password-42",
        },
    )
    assert created.status_code == 200
    member_id = await database.write(
        "INSERT INTO member(mesh_id,mesh_num,handle,trust,first_seen,last_seen,notes) "
        "VALUES('!00abcdef',11259375,'private-handle','member',1,unixepoch(),"
        "'private operator note')"
    )
    await database.write(
        "INSERT INTO member_position(member_id,lat,lon,received_at,expires_at) "
        "VALUES(?,40.44061,-79.99591,unixepoch(),unixepoch()+3600)",
        (member_id,),
    )
    await database.write(
        "INSERT INTO message_log(direction,member_id,peer_mesh_id,channel,portnum,is_direct,"
        "packet_id,text,byte_len,created_at) VALUES('in',?,'!00abcdef',0,1,1,7,"
        "'private direct message',22,unixepoch())",
        (member_id,),
    )
    await database.write(
        "INSERT INTO mail(uid,from_id,from_label,to_label,subject,body,"
        "created_at,state,expires_at) "
        "VALUES('private:1',?,'private-handle','operator','private subject','private mail body',"
        "unixepoch(),'delivered',unixepoch()+3600)",
        (member_id,),
    )

    viewer = TestClient(create_web_app(lambda: {"radio": "up"}, database, auth))
    first = viewer.post(
        "/api/v1/auth/login",
        json={"username": "wallboard", "password": "wallboard-password-42"},
    )
    changed = viewer.post(
        "/api/v1/auth/password",
        headers={"x-csrf-token": first.json()["csrf_token"]},
        json={"current_password": "", "new_password": "wallboard-permanent-42"},
    )
    assert changed.status_code == 200
    assert (
        viewer.post(
            "/api/v1/auth/login",
            json={"username": "wallboard", "password": "wallboard-permanent-42"},
        ).status_code
        == 200
    )

    before = await database.read("SELECT operator_read_at,read_at FROM mail WHERE uid='private:1'")
    response = viewer.get("/api/v1/wallboard/summary")
    assert response.status_code == 200
    encoded = response.text
    for secret in (
        "!00abcdef",
        "private-handle",
        "private operator note",
        "private direct message",
        "private subject",
        "private mail body",
        "40.44061",
        "-79.99591",
    ):
        assert secret not in encoded
    assert response.json()["overview"]["members"]["members_total"] == 1
    after = await database.read("SELECT operator_read_at,read_at FROM mail WHERE uid='private:1'")
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    await database.close()


@pytest.mark.asyncio
async def test_totp_recovery_step_up_and_named_audit_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 2_000_000_000
    monkeypatch.setattr("outpost.web.auth.time.time", lambda: now)
    database = Database(tmp_path / "outpost.db")
    await database.open()
    auth = WebAuthService(database, 12)
    client = TestClient(create_web_app(lambda: {"radio": "up"}, database, auth))
    password = "operator-password-42"  # noqa: S105 - isolated test credential
    csrf = await _permanent_operator(auth, client, password)

    enrollment = client.post("/api/v1/auth/mfa/begin", headers={"x-csrf-token": csrf})
    assert enrollment.status_code == 200
    secret = enrollment.json()["secret"]
    confirmed = client.post(
        "/api/v1/auth/mfa/confirm",
        headers={"x-csrf-token": csrf},
        json={"code": _totp(secret, now)},
    )
    assert confirmed.status_code == 200
    recovery = confirmed.json()["recovery_codes"]
    assert len(recovery) == 8 and all(len(code) == 14 for code in recovery)
    stored = (
        await database.read(
            "SELECT totp_secret,recovery_code_hashes FROM web_account WHERE username='operator'"
        )
    )[0]
    assert recovery[0] not in stored["recovery_code_hashes"]

    client.post("/api/v1/auth/logout", headers={"x-csrf-token": csrf})
    challenge = client.post(
        "/api/v1/auth/login", json={"username": "operator", "password": password}
    )
    assert challenge.status_code == 202 and challenge.json()["mfa_required"] is True
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "operator", "password": password, "code": recovery[0]},
    )
    assert login.status_code == 200 and login.json()["mfa_enabled"] is True
    csrf = login.json()["csrf_token"]
    client.post("/api/v1/auth/logout", headers={"x-csrf-token": csrf})
    reused = client.post(
        "/api/v1/auth/login",
        json={"username": "operator", "password": password, "code": recovery[0]},
    )
    assert reused.status_code == 401
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "operator", "password": password, "code": _totp(secret, now)},
    )
    csrf = login.json()["csrf_token"]

    await database.write("UPDATE web_session SET step_up_until=?", (now - 1,))
    member_id = await database.write(
        "INSERT INTO member(mesh_id,mesh_num,first_seen,last_seen) VALUES('!00000099',153,1,1)"
    )
    protected = client.patch(
        f"/api/v1/members/{member_id}",
        headers={"x-csrf-token": csrf},
        json={"trust": "member", "reason": "Identity verified in person"},
    )
    assert protected.status_code == 428
    relay_key_recovery = client.patch(
        "/api/v1/federation/relay/origins/!aaaaaaaa",
        headers={"x-csrf-token": csrf},
        json={"state": "forget"},
    )
    assert relay_key_recovery.status_code == 428
    stepped = client.post(
        "/api/v1/auth/step-up",
        headers={"x-csrf-token": csrf},
        json={"password": password, "code": _totp(secret, now)},
    )
    assert stepped.status_code == 200 and stepped.json()["step_up_until"] == now + 600
    updated = client.patch(
        f"/api/v1/members/{member_id}",
        headers={"x-csrf-token": csrf},
        json={"trust": "member", "reason": "Identity verified in person"},
    )
    assert updated.status_code == 200
    audit = await database.read(
        "SELECT actor_ref FROM audit_log WHERE action='member.update' ORDER BY id DESC LIMIT 1"
    )
    assert audit[0]["actor_ref"] == "operator"
    concurrent = await asyncio.gather(
        auth.login(password, "race-a", username="operator", code=recovery[1]),
        auth.login(password, "race-b", username="operator", code=recovery[1]),
    )
    assert sum(result is not None for result in concurrent) == 1
    await database.close()


@pytest.mark.asyncio
async def test_totp_matches_rfc_vector() -> None:
    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    assert _totp(secret, 59, digits=8) == "94287082"


@pytest.mark.asyncio
async def test_audit_uses_named_web_account_not_shared_operator_label(tmp_path: Path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    auth = WebAuthService(database, 12)
    bootstrap_client = TestClient(create_web_app(lambda: {"radio": "up"}, database, auth))
    await _permanent_operator(auth, bootstrap_client, "operator-password-42")
    await auth.create_account(
        "alice",
        "Alice Rivera",
        "administrator",
        "alice-initial-password-42",
        "operator",
    )

    client = TestClient(create_web_app(lambda: {"radio": "up"}, database, auth))
    first = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "alice-initial-password-42"},
    )
    changed = client.post(
        "/api/v1/auth/password",
        headers={"x-csrf-token": first.json()["csrf_token"]},
        json={"current_password": "", "new_password": "alice-permanent-password-42"},
    )
    assert changed.status_code == 200
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "alice-permanent-password-42"},
    )
    member_id = await database.write(
        "INSERT INTO member(mesh_id,mesh_num,first_seen,last_seen) VALUES('!00000042',66,1,1)"
    )
    update = client.patch(
        f"/api/v1/members/{member_id}",
        headers={"x-csrf-token": login.json()["csrf_token"]},
        json={"trust": "member", "reason": "Verified by Alice"},
    )
    assert update.status_code == 200
    audit = await database.read(
        "SELECT actor_ref FROM audit_log WHERE action='member.update' ORDER BY id DESC LIMIT 1"
    )
    assert audit[0]["actor_ref"] == "alice"
    await database.close()


@pytest.mark.asyncio
async def test_single_password_database_migrates_with_session_and_audit_continuity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "outpost.db"
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
    connection.execute("PRAGMA journal_mode=WAL")
    migrations = Path(__file__).parents[2] / "src/outpost/store/migrations"
    for path in sorted(migrations.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        version = int(path.name[:4])
        if version >= 145:
            break
        connection.executescript(path.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_version(version,applied_at) VALUES(?,1)", (version,))
    connection.execute(
        "INSERT INTO web_credential(id,password_hash,must_change,created_at,changed_at) "
        "VALUES(1,'legacy-argon-hash',0,100,200)"
    )
    connection.execute(
        "INSERT INTO web_session(token_hash,csrf_token,created_at,expires_at,last_seen_at) "
        "VALUES('legacy-token','legacy-csrf',100,9999999999,150)"
    )
    connection.execute(
        "INSERT INTO audit_log(actor_kind,actor_ref,action,target,created_at) "
        "VALUES('web','operator','legacy.action','node',100)"
    )
    connection.commit()
    connection.close()

    database = Database(database_path)
    await database.open()
    account = (await database.read("SELECT * FROM web_account WHERE id=1"))[0]
    assert account["username"] == "operator"
    assert account["role"] == "administrator"
    assert account["password_hash"] == "legacy-argon-hash"  # noqa: S105
    migrated_session = (await database.read("SELECT * FROM web_session"))[0]
    assert migrated_session["account_id"] == 1
    audit = (await database.read("SELECT actor_ref FROM audit_log"))[0]
    assert audit["actor_ref"] == "operator"
    await database.close()
