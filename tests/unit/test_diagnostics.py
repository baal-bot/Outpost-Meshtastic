from __future__ import annotations

import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import outpost.diagnostics as diagnostics
from outpost.config import Config, load_config
from outpost.diagnostics import build_bundle, database_secrets, runtime_evidence


def test_diagnostic_bundle_redacts_bootstrap_password_session_and_csrf(
    tmp_path: Path,
) -> None:
    config = load_config(Path(__file__).parents[2] / "config" / "config.example.yaml")
    setup_token = "setup-token-value-123456"  # noqa: S105
    password = "operator-password-value"  # noqa: S105
    session = "browser-session-value"
    csrf = "csrf-value-123"
    bearer = "api-bearer-value"
    password_hash = "$argon2id$stored-password-hash"  # noqa: S105
    journal = "\n".join(
        (
            f"OUTPOST INITIAL OPERATOR PASSWORD: {setup_token}",
            f'{{"password":"{password}","csrf_token":"{csrf}"}}',
            f"Cookie: outpost_session={session}",
            f"Authorization: Bearer {bearer}",
            f"database password_hash={password_hash}",
            'provider error body="private member message with spaces"',
            "radio supervisor connected",
        )
    )
    output = build_bundle(
        tmp_path / "diagnostics.zip",
        config,
        journal,
        runtime={
            "platform": {"python": "3.13.7", "outpost": "0.1.0"},
            "database": {"schema": 147, "quick_check": "ok"},
            "storage": {"filesystem_free_bytes": 1024},
            "service": {"ActiveState": "active"},
            "live": {
                "reachable": True,
                "radio": "up",
                "tasks_healthy": True,
                "providers": {"nws": {"status": "up", "failures": 0}},
            },
            "self_check": {
                "status": "failed",
                "checks": [
                    {
                        "name": "responder_audience",
                        "detail": "password=must-not-leak",
                    }
                ],
            },
        },
        exact_values=(setup_token, password_hash),
    )

    assert output.stat().st_mode & 0o777 == 0o600
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"manifest.json", "recent-errors.log"}
        manifest = json.loads(archive.read("manifest.json"))
        contents = b"\n".join(archive.read(name) for name in archive.namelist()).decode()
    for value in (setup_token, password, session, csrf, bearer, password_hash):
        assert value not in contents
    assert config.node.operator_contact not in contents
    assert "private member message with spaces" not in contents
    assert "[REDACTED]" in contents
    assert "radio supervisor connected" in contents
    assert manifest["runtime"]["database"] == {"schema": 147, "quick_check": "ok"}
    assert manifest["runtime"]["live"]["radio"] == "up"
    assert manifest["runtime"]["self_check"]["status"] == "failed"
    assert "must-not-leak" not in contents


def test_full_journal_is_opt_in(tmp_path: Path) -> None:
    config = load_config(Path(__file__).parents[2] / "config" / "config.example.yaml")
    output = build_bundle(
        tmp_path / "diagnostics.zip",
        config,
        "recent warning\n",
        full_journal="ordinary service activity\n",
    )

    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "manifest.json",
            "recent-errors.log",
            "journal.log",
        }


def test_runtime_evidence_triggers_and_embeds_live_self_check(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    monkeypatch.setattr(
        diagnostics,
        "_run_live_self_check",
        lambda _config: calls.append("trigger") or {"reachable": True},
    )
    monkeypatch.setattr(
        diagnostics,
        "_live_status",
        lambda _config: (
            calls.append("status")
            or {
                "reachable": True,
                "readiness": {"status": "failed", "failed_checks": ["responder_audience"]},
            }
        ),
    )
    monkeypatch.setattr(diagnostics, "_service_status", lambda: {"available": False})

    evidence = runtime_evidence(config)

    assert calls == ["trigger", "status"]
    assert evidence["self_check"] == {
        "status": "failed",
        "failed_checks": ["responder_audience"],
    }


def test_database_secrets_returns_only_auth_redaction_values(tmp_path: Path) -> None:
    database = tmp_path / "outpost.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(
            "CREATE TABLE web_credential(password_hash TEXT);"
            "CREATE TABLE web_session(csrf_token TEXT);"
            "CREATE TABLE web_account(password_hash TEXT,totp_secret TEXT,"
            "totp_pending_secret TEXT,recovery_code_hashes TEXT);"
            "INSERT INTO web_credential VALUES('$argon2id$hash');"
            "INSERT INTO web_session VALUES('csrf-secret');"
            "INSERT INTO web_account VALUES('$argon2id$account','TOTPSECRET',"
            "'PENDINGSECRET','[\"recovery-hash\"]');"
            "CREATE TABLE mail(body TEXT);"
            "INSERT INTO mail VALUES('not-loaded-by-diagnostics');"
        )
    assert database_secrets(database) == {
        "$argon2id$hash",
        "$argon2id$account",
        "csrf-secret",
        "TOTPSECRET",
        "PENDINGSECRET",
        '["recovery-hash"]',
        "recovery-hash",
    }
