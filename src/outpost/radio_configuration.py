from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import secrets
from copy import deepcopy
from typing import Any, cast

from outpost.clock import Clock
from outpost.config import Config
from outpost.operator_context import current_actor_ref
from outpost.store import Database
from outpost.transport.radio_frequency import frequency_plan

_SETTING_KEY = "radio.configuration.operation"
_ACTIVE_STATES = {"preflight", "applying", "reconnecting", "verifying"}
_SECRET_FIELDS = {"password", "psk"}


class RadioConfigurationError(ValueError):
    def __init__(self, message: str, operation: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.operation = operation


def _redacted_change(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: ("replacement provided" if value else "clear requested")
        if key in _SECRET_FIELDS and value is not None
        else value
        for key, value in values.items()
    }


def _channel(status: dict[str, Any], index: int) -> dict[str, Any]:
    for channel in status.get("channels", []):
        if int(channel.get("index", -1)) == index:
            return dict(channel)
    raise ValueError(f"radio channel slot {index} is unavailable")


def _section_snapshot(
    status: dict[str, Any], section: str, values: dict[str, Any]
) -> dict[str, Any]:
    if section == "channel":
        return _channel(status, int(values["index"]))
    if section == "mqtt":
        mqtt = {key: deepcopy(value) for key, value in status["mqtt"].items() if key != "channels"}
        mqtt["channel"] = _channel(status, int(values["channel"]))
        return mqtt
    return deepcopy(status[section])


def _psk_kind(value: str) -> str:
    try:
        size = len(base64.b64decode(value, validate=True))
    except (binascii.Error, ValueError) as error:
        raise ValueError("channel key must be valid base64") from error
    return {1: "default", 16: "AES-128", 32: "AES-256"}.get(size, "invalid")


def _desired_snapshot(
    before: dict[str, Any], section: str, values: dict[str, Any]
) -> dict[str, Any]:
    desired = deepcopy(before)
    if section == "channel":
        for key in (
            "role",
            "name",
            "uplink_enabled",
            "downlink_enabled",
            "position_precision",
            "muted",
        ):
            desired[key] = values[key]
        if values.get("generate_psk"):
            desired["psk"] = "AES-256"
        elif values.get("psk") is not None:
            desired["psk"] = _psk_kind(str(values["psk"]))
        return desired
    if section == "mqtt":
        for key in (
            "enabled",
            "address",
            "tls_enabled",
            "root",
            "json_enabled",
            "proxy_to_client_enabled",
            "map_reporting_enabled",
        ):
            if values.get(key) is not None:
                desired[key] = values[key]
        desired["encryption_enabled"] = True
        if values.get("username") is not None:
            desired["username_configured"] = bool(values["username"])
        if values.get("password") is not None:
            desired["password_configured"] = bool(values["password"])
        desired["channel"]["uplink_enabled"] = values["uplink_enabled"]
        desired["channel"]["downlink_enabled"] = values["downlink_enabled"]
        return desired
    for key, value in values.items():
        if value is not None:
            desired[key] = value
    return desired


def _diff(before: dict[str, Any], desired: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []

    def walk(left: dict[str, Any], right: dict[str, Any], prefix: str = "") -> None:
        for key in sorted(right):
            path = f"{prefix}.{key}" if prefix else key
            old, new = left.get(key), right[key]
            if isinstance(old, dict) and isinstance(new, dict):
                walk(old, new, path)
            elif old != new:
                changes.append({"field": path, "from": old, "to": new})

    walk(before, desired)
    return changes


def _review_diff(
    before: dict[str, Any], desired: dict[str, Any], section: str, values: dict[str, Any]
) -> list[dict[str, Any]]:
    changes = _diff(before, desired)
    if section == "channel" and (values.get("psk") or values.get("generate_psk")):
        changes = [entry for entry in changes if entry["field"] != "psk"]
        changes.append(
            {
                "field": "psk",
                "from": before.get("psk", "configured"),
                "to": f"replacement provided ({desired['psk']})",
            }
        )
    if section == "mqtt":
        for secret in ("username", "password"):
            if values.get(secret) is None:
                continue
            configured_field = f"{secret}_configured"
            changes = [entry for entry in changes if entry["field"] != configured_field]
            changes.append(
                {
                    "field": secret,
                    "from": "configured" if before.get(configured_field) else "not configured",
                    "to": "replacement provided" if values[secret] else "cleared",
                }
            )
    return sorted(changes, key=lambda entry: str(entry["field"]))


def _equal(field: str, expected: object, actual: object) -> bool:
    if field in {"latitude", "longitude"} and expected is not None and actual is not None:
        return abs(float(str(expected)) - float(str(actual))) <= 0.00001
    return expected == actual


def _mismatches(
    desired: dict[str, Any], actual: dict[str, Any], prefix: str = ""
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key, expected in desired.items():
        field = f"{prefix}.{key}" if prefix else key
        observed = actual.get(key)
        if isinstance(expected, dict) and isinstance(observed, dict):
            result.extend(_mismatches(expected, observed, field))
        elif not _equal(key, expected, observed):
            result.append({"field": field, "expected": expected, "observed": observed})
    return result


class RadioConfigurationManager:
    """Transaction-like orchestration for configuration written to a Meshtastic node."""

    def __init__(self, database: Database, radio: Any, clock: Clock, config: Config) -> None:
        self.database = database
        self.radio = radio
        self.clock = clock
        self.config = config
        self._operation_lock = asyncio.Lock()
        self._signing_key = secrets.token_bytes(32)
        self._preflights: dict[str, bytes] = {}
        self._operation: dict[str, Any] | None = None

    def _now(self) -> int:
        return int(self.clock.now().timestamp())

    @staticmethod
    def _payload(section: str, values: dict[str, Any]) -> bytes:
        return json.dumps(
            {"section": section, "values": values}, sort_keys=True, separators=(",", ":")
        ).encode()

    def _signature(self, section: str, values: dict[str, Any]) -> bytes:
        return hmac.new(self._signing_key, self._payload(section, values), hashlib.sha256).digest()

    async def initialize(self) -> None:
        rows = await self.database.read(
            "SELECT value FROM runtime_setting WHERE key=?", (_SETTING_KEY,)
        )
        if not rows:
            return
        try:
            operation = json.loads(str(rows[0]["value"]))
        except (json.JSONDecodeError, TypeError):
            return
        if operation.get("state") in _ACTIVE_STATES:
            operation.update(
                {
                    "state": "failed",
                    "updated_at": self._now(),
                    "error": "Outpost restarted before this radio change was verified.",
                    "recovery": self._manual_recovery(str(operation.get("section", "settings"))),
                    "rollback": "not_attempted_after_restart",
                }
            )
            await self._save(operation)
        else:
            self._operation = operation

    async def _save(self, operation: dict[str, Any]) -> None:
        self._operation = deepcopy(operation)
        await self.database.write(
            """
            INSERT INTO runtime_setting(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (_SETTING_KEY, json.dumps(operation, sort_keys=True), self._now()),
        )

    async def _transition(self, state: str, **fields: Any) -> None:
        if self._operation is None:
            raise RuntimeError("radio configuration operation is unavailable")
        self._operation.update({"state": state, "updated_at": self._now(), **fields})
        await self._save(self._operation)

    async def _audit(self, action: str, outcome: str, operation: dict[str, Any]) -> None:
        detail = {
            "operation_id": operation["id"],
            "section": operation["section"],
            "state": operation["state"],
            "fields": [entry["field"] for entry in operation.get("diff", [])],
            "rollback": operation.get("rollback"),
            "mismatches": operation.get("mismatches", []),
        }
        await self.database.write(
            """
            INSERT INTO audit_log(
                actor_kind,actor_ref,action,target,detail,created_at,outcome
            ) VALUES('web',?,?,?,?,?,?)
            """,
            (
                current_actor_ref(),
                action,
                f"radio/{operation['section']}",
                json.dumps(detail, sort_keys=True),
                self._now(),
                outcome,
            ),
        )

    def operation(self) -> dict[str, Any] | None:
        return deepcopy(self._operation)

    def _validate_dependencies(
        self, status: dict[str, Any], section: str, values: dict[str, Any]
    ) -> list[str]:
        impacts: list[str] = []
        if section == "channel":
            index = int(values["index"])
            current = _channel(status, index)
            if values["role"] == "DISABLED" and index in self.config.channels:
                raise ValueError(
                    "This channel is required by Outpost policy; change Outpost policy "
                    "before disabling it"
                )
            if values["role"] == "DISABLED" and (
                current["uplink_enabled"] or current["downlink_enabled"]
            ):
                raise ValueError("disable this channel's MQTT uplink/downlink before the channel")
            if values.get("psk") or values.get("generate_psk"):
                impacts.append("Nodes without the replacement channel key will lose access.")
            if index == 0 and status["lora"]["frequency_slot"] == 0:
                current_name = str(current.get("name", ""))
                if str(values["name"]) != current_name:
                    impacts.append(
                        "Changing the primary channel name may change the automatic RF slot."
                    )
        elif section == "lora":
            if not values["tx_enabled"]:
                raise ValueError("Outpost-connected radios must keep LoRa transmission enabled")
            impacts.append("The radio will reboot and may move to a different RF network.")
        elif section == "mqtt":
            selected = _channel(status, int(values["channel"]))
            if selected["role"] == "DISABLED":
                raise ValueError("MQTT requires an active radio channel")
            dependency = (
                self.config.modules.fed.enabled
                and self.config.fed.mqtt.enabled
                and self.config.fed.mqtt.use_radio_module
            )
            if dependency and not values["enabled"]:
                raise ValueError(
                    "Federation is configured to use the radio MQTT module; change its "
                    "transport policy before disabling MQTT"
                )
            if values["enabled"] and not (values["uplink_enabled"] or values["downlink_enabled"]):
                impacts.append("MQTT is enabled but the selected channel will exchange no traffic.")
            impacts.append("Federation's shared live MQTT view will change with this setting.")
        elif section == "position" and values["fixed_position"]:
            impacts.append("The fixed position may be shared at each channel's precision.")
        elif section == "identity":
            impacts.append("Nearby nodes will see the new radio identity.")
        elif section == "device":
            impacts.append("Device behavior changes while the serial client role is preserved.")
        return impacts

    @staticmethod
    def _manual_recovery(section: str) -> str:
        suffix = (
            " Channel keys and MQTT credentials must be restored from the operator's "
            "secret store because Outpost never persists them."
            if section in {"channel", "mqtt"}
            else ""
        )
        return (
            "Connect directly to the Outpost radio over USB or Bluetooth with a Meshtastic "
            f"client, restore the pre-change {section} values shown here, then reconnect "
            f"the Outpost service.{suffix}"
        )

    async def preflight(self, section: str, values: dict[str, Any]) -> dict[str, Any]:
        if self._operation_lock.locked():
            raise RadioConfigurationError("another radio configuration operation is in progress")
        async with self._operation_lock:
            status = await self.radio.refresh_configuration()
            if not status.get("available"):
                raise ConnectionError("radio configuration is unavailable")
            impacts = self._validate_dependencies(status, section, values)
            before = _section_snapshot(status, section, values)
            desired = _desired_snapshot(before, section, values)
            changes = _review_diff(before, desired, section, values)
            if not changes:
                raise ValueError("reviewed values already match the fresh radio state")
            operation_id = secrets.token_urlsafe(18)
            frequency: dict[str, object] | None = None
            if section == "lora":
                primary = _channel(status, 0)
                frequency = frequency_plan(
                    str(values["region"]),
                    str(values["modem_preset"]),
                    int(values["frequency_slot"]),
                    str(primary.get("name", "")),
                )
                impacts.append(
                    f"Effective center frequency: {frequency['frequency_mhz']:.6f} MHz "
                    f"(slot {frequency['effective_slot']} of {frequency['slot_count']})."
                )
            operation = {
                "id": operation_id,
                "section": section,
                "state": "preflight",
                "created_at": self._now(),
                "updated_at": self._now(),
                "expires_at": self._now() + 600,
                "change": _redacted_change(values),
                "before": before,
                "diff": changes,
                "impact": impacts,
                "frequency": frequency,
                "recovery": self._manual_recovery(section),
            }
            self._preflights = {operation_id: self._signature(section, values)}
            await self._save(operation)
            await self._audit("radio.config_preflight", "success", operation)
            return deepcopy(operation)

    async def apply(
        self, operation_id: str, section: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        signature = self._preflights.get(operation_id)
        if signature is None or not hmac.compare_digest(
            signature, self._signature(section, values)
        ):
            raise RadioConfigurationError(
                "preflight is missing, expired, or does not match this exact change"
            )
        if self._operation is None or self._operation.get("id") != operation_id:
            raise RadioConfigurationError("a newer radio preflight replaced this one")
        if self._now() > int(self._operation["expires_at"]):
            raise RadioConfigurationError("radio preflight expired; review fresh state again")
        if self._operation_lock.locked():
            raise RadioConfigurationError("another radio configuration operation is in progress")

        async with self._operation_lock:
            rollback_image: dict[str, Any] | None = None
            wrote = False
            readback_mismatches: list[dict[str, Any]] = []
            try:
                await self._transition("applying")
                rollback_image = await self.radio.capture_configuration(section)
                # A raised SDK call can still mean the first half of a multi-write
                # operation reached firmware, so every attempted call requires rollback.
                wrote = True
                write_result = await self.radio.configure(section, values)
                await self._transition("reconnecting")
                fresh = await self.radio.refresh_configuration()
                await self._transition("verifying")
                actual = _section_snapshot(fresh, section, values)
                before = dict(self._operation["before"])
                desired = _desired_snapshot(before, section, values)
                if section == "lora":
                    frequency = self._operation.get("frequency") or {}
                    if int(values["frequency_slot"]) == 0 and actual.get(
                        "frequency_slot"
                    ) == frequency.get("effective_slot"):
                        # Firmware persists the hash-resolved one-based slot while still
                        # treating it as the automatic/default selection.
                        desired["frequency_slot"] = actual["frequency_slot"]
                    if int(values["tx_power"]) == 0 and int(actual.get("tx_power", -1)) >= 0:
                        # Zero asks firmware to choose the legal regional power. Current
                        # firmware may read the chosen dBm value back instead of zero.
                        desired["tx_power"] = actual["tx_power"]
                readback_mismatches = _mismatches(desired, actual)
                if hasattr(self.radio, "verify_configuration_secrets"):
                    rejected_secrets = await self.radio.verify_configuration_secrets(
                        section, values, write_result.get("generated_psk")
                    )
                    readback_mismatches.extend(
                        {
                            "field": field,
                            "expected": "replacement provided",
                            "observed": "rejected or changed by firmware",
                        }
                        for field in rejected_secrets
                    )
                if readback_mismatches:
                    fields = ", ".join(item["field"] for item in readback_mismatches)
                    raise RadioConfigurationError(
                        f"radio rejected or changed reviewed field(s): {fields}"
                    )
                await self._transition(
                    "verified", verified_at=self._now(), mismatches=[], rollback="not_needed"
                )
                await self._audit("radio.config_update", "success", self._operation)
                self._preflights.pop(operation_id, None)
                result = cast(dict[str, Any], deepcopy(fresh))
                result["operation"] = self.operation()
                if write_result.get("generated_psk"):
                    result["generated_psk"] = write_result["generated_psk"]
                return result
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._transition(
                        "failed",
                        error="Radio configuration request was interrupted before verification.",
                        rollback="not_attempted_after_interruption",
                        recovery=self._manual_recovery(section),
                    )
                )
                self._preflights.pop(operation_id, None)
                raise
            except Exception as error:
                rollback = "not_attempted"
                if rollback_image is not None:
                    try:
                        await self.radio.restore_configuration(
                            section,
                            rollback_image,
                            channel_index=(
                                int(values.get("index", values.get("channel", -1)))
                                if section in {"channel", "mqtt"}
                                else None
                            ),
                        )
                        if wrote:
                            rollback_fresh = await self.radio.refresh_configuration()
                            rollback_actual = _section_snapshot(rollback_fresh, section, values)
                            rollback_expected = dict(self._operation["before"])
                            rollback_mismatches = _mismatches(rollback_expected, rollback_actual)
                            if rollback_mismatches:
                                raise RadioConfigurationError(
                                    "rollback readback did not match pre-change state"
                                )
                        rollback = "restored_pre_change_state"
                    except Exception:
                        rollback = "failed_or_radio_unreachable"
                message = " ".join(str(error).split())[:240] or type(error).__name__
                await self._transition(
                    "failed",
                    error=message,
                    rollback=rollback,
                    mismatches=readback_mismatches,
                    recovery=self._manual_recovery(section),
                )
                await self._audit("radio.config_update", "failure", self._operation)
                self._preflights.pop(operation_id, None)
                raise RadioConfigurationError(message, self.operation()) from error
