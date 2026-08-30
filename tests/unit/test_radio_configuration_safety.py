from __future__ import annotations

import asyncio
import base64
import json
from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.radio_configuration import (
    RadioConfigurationError,
    RadioConfigurationManager,
    _redacted_change,
)
from outpost.store import Database
from outpost.transport.radio_frequency import frequency_plan, regional_duty_cycle_percent
from outpost.web.api import create_web_app


def radio_state() -> dict[str, Any]:
    return {
        "available": True,
        "identity": {"long_name": "Outpost", "short_name": "OUT"},
        "device": {
            "role": "CLIENT",
            "rebroadcast_mode": "ALL",
            "node_info_broadcast_secs": 10_800,
        },
        "lora": {
            "region": "US",
            "modem_preset": "LONG_FAST",
            "frequency_slot": 0,
            "hop_limit": 3,
            "tx_power": 0,
            "tx_enabled": True,
        },
        "position": {
            "fixed_position": False,
            "gps_mode": "NOT_PRESENT",
            "smart_broadcast": True,
            "broadcast_secs": 0,
            "latitude": 40.44,
            "longitude": -79.99,
            "altitude": 366,
        },
        "channels": [
            {
                "index": 0,
                "role": "PRIMARY",
                "name": "LongFast",
                "psk": "default",
                "uplink_enabled": False,
                "downlink_enabled": False,
                "position_precision": 0,
                "muted": False,
            }
        ],
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
            "channels": [],
        },
    }


class FakeRadio:
    def __init__(self, behavior: str = "success") -> None:
        self.state = radio_state()
        self.behavior = behavior
        self.refreshes = 0
        self.restores = 0
        self.generated_psk = base64.b64encode(bytes.fromhex("d7" * 32)).decode()
        self.configure_started = asyncio.Event()
        self.release_configure = asyncio.Event()
        self.release_configure.set()

    async def refresh_configuration(self) -> dict[str, Any]:
        self.refreshes += 1
        if self.behavior in {"disconnect", "timeout"} and self.refreshes > 1:
            if self.behavior == "timeout":
                raise TimeoutError("simulated reconnect timeout")
            raise ConnectionError("simulated reboot disconnect")
        return deepcopy(self.state)

    async def capture_configuration(self, section: str) -> dict[str, Any]:
        return {"state": deepcopy(self.state), "section": section}

    async def configure(self, section: str, values: dict[str, Any]) -> dict[str, Any]:
        self.configure_started.set()
        await self.release_configure.wait()
        if self.behavior == "reject":
            raise ValueError("simulated firmware rejection")
        if self.behavior == "partial":
            self.state["mqtt"]["enabled"] = values["enabled"]
            raise OSError("simulated second-write failure")
        if self.behavior != "mismatch":
            if section == "identity":
                self.state["identity"].update(values)
            elif section == "lora":
                self.state["lora"].update(values)
            elif section == "mqtt":
                for key, value in values.items():
                    if key in self.state["mqtt"] and value is not None:
                        self.state["mqtt"][key] = value
                channel = self.state["channels"][values["channel"]]
                channel["uplink_enabled"] = values["uplink_enabled"]
                channel["downlink_enabled"] = values["downlink_enabled"]
            elif section == "channel":
                channel = self.state["channels"][values["index"]]
                for key in (
                    "role",
                    "name",
                    "uplink_enabled",
                    "downlink_enabled",
                    "position_precision",
                    "muted",
                ):
                    channel[key] = values[key]
                channel["psk"] = "AES-256"
                result = deepcopy(self.state)
                result["generated_psk"] = self.generated_psk
                return result
        return deepcopy(self.state)

    async def restore_configuration(
        self, section: str, snapshot: dict[str, Any], *, channel_index: int | None = None
    ) -> None:
        self.restores += 1
        if self.behavior == "disconnect":
            raise ConnectionError("radio remains unreachable")
        self.state = deepcopy(snapshot["state"])


async def manager(
    tmp_path, behavior: str = "success"
) -> tuple[RadioConfigurationManager, FakeRadio, Database]:
    database = Database(tmp_path / f"{behavior}.db")
    await database.open()
    radio = FakeRadio(behavior)
    config = Config.model_validate({"store": {"path": str(database.path)}})
    service = RadioConfigurationManager(database, radio, VirtualClock(), config)
    return service, radio, database


def test_frequency_slot_is_bounded_and_automatic_slot_is_explained() -> None:
    automatic = frequency_plan("US", "LONG_FAST", 0, "LongFast")
    explicit = frequency_plan("US", "LONG_FAST", 20, "LongFast")

    assert automatic["effective_slot"] == 20
    assert automatic["frequency_mhz"] == 906.875
    assert "stable hash" in str(automatic["explanation"])
    assert explicit["frequency_mhz"] == 906.875
    with pytest.raises(ValueError, match="1-104"):
        frequency_plan("US", "LONG_FAST", 105, "LongFast")
    with pytest.raises(ValueError, match="not valid"):
        frequency_plan("EU_866", "LONG_FAST", 1, "LongFast")


def test_regional_duty_cycle_matches_firmware_profiles() -> None:
    assert regional_duty_cycle_percent("US") == 100
    assert regional_duty_cycle_percent("EU_868") == 10
    assert regional_duty_cycle_percent("EU_866") == 2.5
    assert regional_duty_cycle_percent("UA_868") == 1
    assert regional_duty_cycle_percent("future_region") is None


@pytest.mark.parametrize("field", ("psk", "password"))
def test_radio_change_redaction_covers_secret_fields(field: str) -> None:
    assert _redacted_change({field: "private-value", "safe": "retained"}) == {
        field: "replacement provided",
        "safe": "retained",
    }
    assert _redacted_change({field: ""}) == {field: "clear requested"}
    assert _redacted_change({field: None}) == {field: None}


def generated_channel_change() -> dict[str, object]:
    return {
        "channel": {
            "index": 0,
            "role": "PRIMARY",
            "name": "Outpost",
            "psk": None,
            "generate_psk": True,
            "uplink_enabled": False,
            "downlink_enabled": False,
            "position_precision": 0,
            "muted": False,
        }
    }


async def persisted_radio_configuration(database: Database) -> str:
    operation = await database.read(
        "SELECT key,value,updated_at FROM runtime_setting WHERE key='radio.configuration.operation'"
    )
    audit = await database.read(
        "SELECT actor_kind,actor_ref,action,target,detail,created_at,outcome FROM audit_log"
    )
    return json.dumps(
        {
            "operation": [dict(row) for row in operation],
            "audit": [dict(row) for row in audit],
        },
        sort_keys=True,
    )


@pytest.mark.asyncio
async def test_generated_channel_psk_is_returned_once_and_never_persisted(tmp_path) -> None:
    service, radio, database = await manager(tmp_path)
    client = TestClient(
        create_web_app(
            lambda: {"radio": "up"},
            database,
            radio_configuration_preflight=service.preflight,
            radio_configuration_apply=service.apply,
        )
    )
    change = generated_channel_change()

    reviewed = client.post("/api/v1/radio/config/preflight", json=change)
    assert reviewed.status_code == 200
    assert radio.generated_psk not in reviewed.text
    assert radio.generated_psk not in await persisted_radio_configuration(database)

    applied = client.put(
        "/api/v1/radio/config",
        json={"preflight_id": reviewed.json()["id"], **change},
    )
    assert applied.status_code == 200
    assert applied.json()["generated_psk"] == radio.generated_psk
    assert radio.generated_psk not in await persisted_radio_configuration(database)
    await database.close()


@pytest.mark.asyncio
async def test_generated_channel_psk_is_not_persisted_when_reconnect_fails(tmp_path) -> None:
    service, radio, database = await manager(tmp_path, "timeout")
    client = TestClient(
        create_web_app(
            lambda: {"radio": "up"},
            database,
            radio_configuration_preflight=service.preflight,
            radio_configuration_apply=service.apply,
        )
    )
    change = generated_channel_change()

    reviewed = client.post("/api/v1/radio/config/preflight", json=change)
    assert reviewed.status_code == 200
    assert radio.generated_psk not in await persisted_radio_configuration(database)
    failed = client.put(
        "/api/v1/radio/config",
        json={"preflight_id": reviewed.json()["id"], **change},
    )

    assert failed.status_code == 409
    assert radio.generated_psk not in failed.text
    assert radio.generated_psk not in await persisted_radio_configuration(database)
    await database.close()


@pytest.mark.asyncio
async def test_preflight_diff_and_successful_reboot_readback_are_durable(tmp_path) -> None:
    service, radio, database = await manager(tmp_path)
    values = {
        "region": "US",
        "modem_preset": "LONG_FAST",
        "frequency_slot": 20,
        "hop_limit": 3,
        "tx_power": 0,
        "tx_enabled": True,
    }

    preflight = await service.preflight("lora", values)
    assert preflight["state"] == "preflight"
    assert preflight["frequency"]["frequency_mhz"] == 906.875
    assert preflight["diff"] == [{"field": "frequency_slot", "from": 0, "to": 20}]
    result = await service.apply(preflight["id"], "lora", values)

    assert result["operation"]["state"] == "verified"
    assert result["operation"]["rollback"] == "not_needed"
    assert radio.refreshes == 2
    stored = await database.read(
        "SELECT value FROM runtime_setting WHERE key='radio.configuration.operation'"
    )
    assert '"state": "verified"' in stored[0]["value"]
    await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("behavior", "section", "values", "rollback"),
    [
        (
            "reject",
            "identity",
            {"long_name": "Relief", "short_name": "RLF"},
            "restored_pre_change_state",
        ),
        (
            "mismatch",
            "identity",
            {"long_name": "Relief", "short_name": "RLF"},
            "restored_pre_change_state",
        ),
        (
            "timeout",
            "identity",
            {"long_name": "Relief", "short_name": "RLF"},
            "failed_or_radio_unreachable",
        ),
        (
            "disconnect",
            "identity",
            {"long_name": "Relief", "short_name": "RLF"},
            "failed_or_radio_unreachable",
        ),
        (
            "partial",
            "mqtt",
            {
                "enabled": True,
                "address": "mqtt.example.test",
                "tls_enabled": True,
                "root": "msh",
                "channel": 0,
                "uplink_enabled": True,
                "downlink_enabled": True,
                "username": "operator",
                "password": "do-not-store",
                "json_enabled": False,
                "proxy_to_client_enabled": False,
                "map_reporting_enabled": False,
            },
            "restored_pre_change_state",
        ),
    ],
)
async def test_failed_writes_rollback_or_give_manual_recovery(
    tmp_path, behavior, section, values, rollback
) -> None:
    service, radio, database = await manager(tmp_path, behavior)
    preflight = await service.preflight(section, values)

    with pytest.raises(RadioConfigurationError) as failure:
        await service.apply(preflight["id"], section, values)

    operation = failure.value.operation
    assert operation["state"] == "failed"
    assert operation["rollback"] == rollback
    assert "USB or Bluetooth" in operation["recovery"]
    if behavior == "mismatch":
        assert {item["field"] for item in operation["mismatches"]} == {
            "long_name",
            "short_name",
        }
    persisted = await database.read(
        "SELECT value FROM runtime_setting WHERE key='radio.configuration.operation'"
    )
    audit = await database.read("SELECT detail FROM audit_log ORDER BY id")
    combined = str([row["value"] for row in persisted] + [row["detail"] for row in audit])
    assert "do-not-store" not in combined
    assert radio.state["mqtt"]["enabled"] is False
    await database.close()


@pytest.mark.asyncio
async def test_apply_is_bound_to_the_exact_reviewed_values(tmp_path) -> None:
    service, radio, database = await manager(tmp_path)
    reviewed = {"long_name": "Relief", "short_name": "RLF"}
    preflight = await service.preflight("identity", reviewed)

    with pytest.raises(RadioConfigurationError, match="does not match"):
        await service.apply(
            preflight["id"],
            "identity",
            {"long_name": "Different", "short_name": "DIFF"},
        )

    assert radio.configure_started.is_set() is False
    await database.close()


@pytest.mark.asyncio
async def test_concurrent_apply_is_rejected(tmp_path) -> None:
    service, radio, database = await manager(tmp_path)
    values = {"long_name": "Relief", "short_name": "RLF"}
    preflight = await service.preflight("identity", values)
    radio.release_configure.clear()
    applying = asyncio.create_task(service.apply(preflight["id"], "identity", values))
    await radio.configure_started.wait()

    with pytest.raises(RadioConfigurationError, match="in progress"):
        await service.apply(preflight["id"], "identity", values)

    radio.release_configure.set()
    await applying
    await database.close()


@pytest.mark.asyncio
async def test_restart_marks_interrupted_operation_failed(tmp_path) -> None:
    service, _, database = await manager(tmp_path)
    interrupted = {
        "id": "interrupted-operation",
        "section": "lora",
        "state": "verifying",
        "created_at": 1,
        "updated_at": 1,
    }
    await database.write(
        "INSERT INTO runtime_setting(key,value,updated_at) VALUES(?,?,?)",
        ("radio.configuration.operation", __import__("json").dumps(interrupted), 1),
    )

    await service.initialize()

    assert service.operation()["state"] == "failed"
    assert "restarted" in service.operation()["error"]
    await database.close()
