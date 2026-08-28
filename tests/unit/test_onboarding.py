from __future__ import annotations

import json
import runpy
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

from outpost.config import load_config
from outpost.onboarding import OnboardingError, checklist, load_state, record_step

RENDER = runpy.run_path(
    str(Path(__file__).parents[2] / "deploy" / "render_avahi.py"), run_name="render_avahi"
)
render_avahi = cast(Callable[[str, int], str], RENDER["render"])


def _config():
    return load_config(Path(__file__).parents[2] / "config" / "config.example.yaml")


def test_checklist_is_complete_resumable_and_records_requirements(tmp_path: Path) -> None:
    state = tmp_path / "onboarding.json"
    config = _config()
    config.store.path = str(tmp_path / "missing-outpost.db")

    initial = checklist(config, state)
    assert [value["id"] for value in initial] == [
        "operator_credentials",
        "identity_location",
        "radio_connection",
        "region_channel_safety",
        "maps_providers",
        "backups",
        "federation",
        "wallboard",
    ]
    assert all(value["status"] == "pending" for value in initial)
    assert all(
        set(value["needs"]) == {"internet", "radio", "restart", "another_operator"}
        for value in initial
    )
    assert next(value for value in initial if value["id"] == "radio_connection")["needs"] == {
        "internet": False,
        "radio": True,
        "restart": True,
        "another_operator": False,
    }

    record_step(state, "identity_location", "completed", now=10)
    record_step(state, "federation", "deferred", now=11)
    resumed = {value["id"]: value for value in checklist(config, state)}
    assert resumed["identity_location"]["status"] == "completed"
    assert resumed["federation"]["status"] == "deferred"
    assert resumed["radio_connection"]["status"] == "pending"
    assert state.stat().st_mode & 0o777 == 0o600
    assert load_state(state)["steps"]["identity_location"]["updated_at"] == 10


def test_completed_operator_credentials_are_detected_without_storing_secrets(
    tmp_path: Path,
) -> None:
    config = _config()
    database = tmp_path / "outpost.db"
    config.store.path = str(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE web_account(enabled INTEGER,must_change INTEGER,role TEXT,"
            "password_hash TEXT)"
        )
        connection.execute(
            "INSERT INTO web_account VALUES(1,0,'administrator','$argon2id$secret-hash')"
        )
        connection.commit()

    values = {value["id"]: value for value in checklist(config, tmp_path / "state.json")}

    assert values["operator_credentials"]["status"] == "completed"
    assert values["operator_credentials"]["status_source"] == "detected"
    assert not (tmp_path / "state.json").exists()
    assert "secret-hash" not in json.dumps(values)


def test_onboarding_state_fails_closed_on_unknown_steps(tmp_path: Path) -> None:
    state = tmp_path / "onboarding.json"
    state.write_text(
        json.dumps(
            {
                "format_version": 1,
                "steps": {"invented": {"status": "completed", "updated_at": 1}},
            }
        )
    )

    with pytest.raises(OnboardingError, match="unknown step"):
        load_state(state)


def test_mdns_declaration_escapes_identity_and_advertises_dashboard() -> None:
    declaration = render_avahi("North & West <Outpost>", 8181)

    assert "North &amp; West &lt;Outpost&gt; on %h" in declaration
    assert "<type>_http._tcp</type>" in declaration
    assert "<port>8181</port>" in declaration
    assert "<txt-record>application=outpost</txt-record>" in declaration
    assert "<txt-record>transport=http</txt-record>" in declaration

    direct_tls = render_avahi("Secure Outpost", 8443, "direct_https", 443)
    assert "<type>_https._tcp</type>" in direct_tls
    assert "<port>8443</port>" in direct_tls
    proxy_tls = render_avahi("Proxy Outpost", 8080, "trusted_proxy", 443)
    assert "<type>_https._tcp</type>" in proxy_tls
    assert "<port>443</port>" in proxy_tls
