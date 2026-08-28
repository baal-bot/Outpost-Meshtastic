from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.fed import FederationPeerService
from outpost.store import Database
from outpost.web.api import create_web_app
from outpost.web.auth import WebAuthService


def test_radio_configuration_surfaces_policy_drift(tmp_path) -> None:
    app = OutpostApp(
        Config.model_validate(
            {
                "store": {"path": str(tmp_path / "outpost.db")},
                "channels": {
                    0: {"name": "public", "bbs": "read_only"},
                    2: {"name": "outpost", "bbs": "full", "ai": True},
                },
            }
        )
    )

    status = app._radio_configuration_context(
        {
            "available": True,
            "channels": [
                {"index": 0, "role": "PRIMARY"},
                {"index": 1, "role": "SECONDARY"},
                {"index": 2, "role": "DISABLED"},
            ],
            "warnings": ["Existing radio warning."],
        }
    )

    assert status["outpost_channel_policies"][0]["bbs"] == "read_only"
    assert status["outpost_channel_policies"][1]["ai"] is True
    assert status["warnings"] == [
        "Existing radio warning.",
        "Outpost policy references inactive radio slot(s): 2.",
        "Active radio slot(s) have no Outpost policy and reject commands: 1.",
    ]


@pytest.mark.asyncio
async def test_radio_config_mqtt_is_shared_with_federation_and_audited_safely(
    tmp_path,
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    state = {
        "available": True,
        "mqtt": {
            "available": True,
            "enabled": False,
            "address": "",
            "tls_enabled": True,
            "encryption_enabled": True,
            "root": "msh",
            "username_configured": False,
            "password_configured": False,
            "json_enabled": False,
            "proxy_to_client_enabled": False,
            "map_reporting_enabled": False,
            "channels": [
                {
                    "index": 0,
                    "name": "LongFast",
                    "uplink_enabled": False,
                    "downlink_enabled": False,
                }
            ],
        },
    }
    stored_credentials = {"username": "", "password": ""}

    async def mqtt_status():
        return deepcopy(state["mqtt"])

    async def configure_mqtt(**values):
        stored_credentials.update(
            {
                name: value
                for name, value in values.items()
                if name in stored_credentials and value is not None
            }
        )
        for name, value in values.items():
            if name in state["mqtt"] and value is not None:
                state["mqtt"][name] = value
        state["mqtt"]["username_configured"] = bool(stored_credentials["username"])
        state["mqtt"]["password_configured"] = bool(stored_credentials["password"])
        channel = state["mqtt"]["channels"][values["channel"]]
        channel["uplink_enabled"] = values["uplink_enabled"]
        channel["downlink_enabled"] = values["downlink_enabled"]
        return await mqtt_status()

    async def radio_status():
        result = deepcopy(state)
        result["mqtt"] = await mqtt_status()
        return result

    async def configure_radio(section, values):
        assert section == "mqtt"
        result = await configure_mqtt(**values)
        configured = deepcopy(state)
        configured["mqtt"] = result
        return configured

    client = TestClient(
        create_web_app(
            lambda: {"radio": "up"},
            database=database,
            federation=FederationPeerService(database, VirtualClock(), "!00000001"),
            federation_mqtt_status=mqtt_status,
            federation_mqtt_configure=configure_mqtt,
            radio_configuration_status=radio_status,
            radio_configuration_configure=configure_radio,
        )
    )

    updated = client.put(
        "/api/v1/radio/config",
        json={
            "mqtt": {
                "enabled": True,
                "address": "mqtt.example.test",
                "tls_enabled": True,
                "root": "community",
                "channel": 0,
                "uplink_enabled": True,
                "downlink_enabled": True,
                "username": "radio-user",
                "password": "never-audit-this",
                "json_enabled": True,
                "proxy_to_client_enabled": True,
                "map_reporting_enabled": False,
            }
        },
    )
    assert updated.status_code == 200
    assert updated.headers["cache-control"] == "no-store"
    assert "never-audit-this" not in updated.text
    federation_view = client.get("/api/v1/federation/mqtt")
    assert federation_view.status_code == 200, federation_view.text
    assert federation_view.json()["json_enabled"] is True

    limited = client.put(
        "/api/v1/federation/mqtt",
        json={
            "enabled": True,
            "address": "mqtt.changed.test",
            "tls_enabled": True,
            "root": "community",
            "channel": 0,
            "uplink_enabled": False,
            "downlink_enabled": True,
        },
    )
    assert limited.status_code == 200
    synced = client.get("/api/v1/radio/config").json()["mqtt"]
    assert synced["address"] == "mqtt.changed.test"
    assert synced["json_enabled"] is True
    assert stored_credentials == {
        "username": "radio-user",
        "password": "never-audit-this",
    }

    audit = await database.read(
        "SELECT action,target,detail FROM audit_log WHERE action='radio.config_update'"
    )
    assert len(audit) == 1
    assert audit[0]["target"] == "radio/mqtt"
    assert "password" not in audit[0]["detail"]
    assert "never-audit-this" not in audit[0]["detail"]

    invalid = client.put(
        "/api/v1/radio/config",
        json={
            "device": {
                "role": "ROUTER",
                "rebroadcast_mode": "ALL",
                "node_info_broadcast_secs": 10_800,
            }
        },
    )
    assert invalid.status_code == 422
    await database.close()


@pytest.mark.asyncio
async def test_radio_writes_require_recent_operator_confirmation(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    auth = WebAuthService(database, 12)
    writes: list[tuple[str, dict[str, object]]] = []

    async def configure(section, values):
        writes.append((section, values))
        return {"available": True}

    client = TestClient(
        create_web_app(
            lambda: {"radio": "up"},
            database=database,
            auth=auth,
            radio_configuration_configure=configure,
        )
    )
    setup = await auth.ensure_credential()
    assert setup is not None
    first = client.post("/api/v1/auth/login", json={"password": setup.path.read_text().strip()})
    csrf = first.json()["csrf_token"]
    password = "radio-operator-password-42"  # noqa: S105 - isolated test credential
    changed = client.post(
        "/api/v1/auth/password",
        headers={"x-csrf-token": csrf},
        json={"current_password": "", "new_password": password},
    )
    assert changed.status_code == 200
    login = client.post("/api/v1/auth/login", json={"password": password})
    csrf = login.json()["csrf_token"]
    await database.write("UPDATE web_session SET step_up_until=0")
    payload = {"identity": {"long_name": "Outpost", "short_name": "OUT"}}

    protected = client.put(
        "/api/v1/radio/config",
        headers={"x-csrf-token": csrf},
        json=payload,
    )
    assert protected.status_code == 428
    assert writes == []
    confirmed = client.post(
        "/api/v1/auth/step-up",
        headers={"x-csrf-token": csrf},
        json={"password": password},
    )
    assert confirmed.status_code == 200
    updated = client.put(
        "/api/v1/radio/config",
        headers={"x-csrf-token": csrf},
        json=payload,
    )
    assert updated.status_code == 200
    assert writes == [("identity", {"long_name": "Outpost", "short_name": "OUT"})]
    await database.close()


def test_radio_api_requires_matching_preflight_for_transactional_apply() -> None:
    calls: list[tuple[object, ...]] = []

    async def preflight(section, values):
        calls.append(("preflight", section, values))
        return {
            "id": "reviewed-change-123",
            "section": section,
            "state": "preflight",
            "diff": [{"field": "short_name", "from": "OLD", "to": "NEW"}],
            "impact": ["Nearby nodes see the new identity."],
        }

    async def apply(operation_id, section, values):
        calls.append(("apply", operation_id, section, values))
        return {
            "available": True,
            "operation": {"id": operation_id, "state": "verified"},
        }

    client = TestClient(
        create_web_app(
            lambda: {"radio": "up"},
            radio_configuration_preflight=preflight,
            radio_configuration_apply=apply,
        )
    )
    change = {"identity": {"long_name": "New Outpost", "short_name": "NEW"}}

    missing = client.put("/api/v1/radio/config", json=change)
    assert missing.status_code == 409
    assert "preflight" in missing.json()["error"]["message"]
    reviewed = client.post("/api/v1/radio/config/preflight", json=change)
    assert reviewed.status_code == 200
    applied = client.put(
        "/api/v1/radio/config",
        json={"preflight_id": reviewed.json()["id"], **change},
    )
    assert applied.status_code == 200
    assert applied.json()["operation"]["state"] == "verified"
    assert calls == [
        ("preflight", "identity", {"long_name": "New Outpost", "short_name": "NEW"}),
        (
            "apply",
            "reviewed-change-123",
            "identity",
            {"long_name": "New Outpost", "short_name": "NEW"},
        ),
    ]
