from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from outpost.config import Config, load_config

StepStatus = Literal["pending", "completed", "deferred"]
VALID_STATUSES: set[str] = {"pending", "completed", "deferred"}


class OnboardingError(ValueError):
    pass


@dataclass(frozen=True)
class SetupNeeds:
    internet: bool
    radio: bool
    restart: bool
    another_operator: bool


@dataclass(frozen=True)
class SetupStep:
    id: str
    title: str
    instructions: str
    verify: str
    needs: SetupNeeds
    optional: bool = False


STEPS = (
    SetupStep(
        "operator_credentials",
        "Operator credentials",
        "Use the one-time setup token, choose a permanent password, create named accounts, and "
        "enroll administrator MFA.",
        "Sign out, sign in with a named account, and confirm recovery codes are stored offline.",
        SetupNeeds(False, False, False, False),
    ),
    SetupStep(
        "identity_location",
        "Outpost identity, name, and location",
        "Set the Outpost name, radio short name, operator contact, timezone, units, "
        "and an approved location or explicitly defer location for privacy.",
        "Settings shows the intended identity and maps center on the approved location.",
        SetupNeeds(False, False, False, False),
    ),
    SetupStep(
        "radio_connection",
        "Radio connection",
        "Select serial, TCP, or BLE; use a stable serial path where possible; then start Outpost.",
        "Radio reports Connected and a direct-message PING receives a response.",
        SetupNeeds(False, True, True, False),
    ),
    SetupStep(
        "region_channel_safety",
        "Region and channel safety",
        "On the radio, confirm the legal region, modem preset, channel indices, keys, and MQTT "
        "policy. Outpost never creates or exports channel keys.",
        "Radio shows the intended region/preset with no missing policy channels; confirm keys out "
        "of band with the channel owner.",
        SetupNeeds(False, True, True, True),
    ),
    SetupStep(
        "maps_providers",
        "Maps and information providers",
        "Set location and a real provider contact, seed the bounded offline map, and review "
        "weather, CAP, earthquake, SAME, and AI module choices.",
        "Environment labels provider freshness and maps remain usable after WAN loss.",
        SetupNeeds(True, False, False, False),
    ),
    SetupStep(
        "backups",
        "Backups and recovery",
        "Create and validate a backup, copy it to encrypted off-device storage, and review "
        "rollback.",
        "A downloaded backup validates and an operator can identify the restore and rollback "
        "steps.",
        SetupNeeds(False, False, False, True),
    ),
    SetupStep(
        "federation",
        "Optional federation",
        "Leave federation disabled or pair one peer, compare its code out of band, and grant only "
        "the intended sharing policy.",
        "The peer is deliberately disabled/deferred or active with reviewed transports and policy.",
        SetupNeeds(False, True, False, True),
        optional=True,
    ),
    SetupStep(
        "wallboard",
        "Optional read-only wallboard",
        "Create a viewer account for a kiosk; never reuse an Administrator or Operator session.",
        "The wallboard can view ordinary status but cannot mutate state or open private exports.",
        SetupNeeds(False, False, False, False),
        optional=True,
    ),
)
STEP_BY_ID = {step.id: step for step in STEPS}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"format_version": 1, "steps": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OnboardingError("onboarding state is unreadable or invalid") from exc
    if not isinstance(value, dict) or value.get("format_version") != 1:
        raise OnboardingError("unsupported onboarding state format")
    steps = value.get("steps")
    if not isinstance(steps, dict):
        raise OnboardingError("onboarding step state is invalid")
    for step_id, entry in steps.items():
        if step_id not in STEP_BY_ID or not isinstance(entry, dict):
            raise OnboardingError("onboarding state contains an unknown step")
        if entry.get("status") not in VALID_STATUSES or not isinstance(
            entry.get("updated_at"), int
        ):
            raise OnboardingError(f"onboarding state for {step_id} is invalid")
    return value


def write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def record_step(path: Path, step_id: str, status: StepStatus, *, now: int | None = None) -> None:
    if step_id not in STEP_BY_ID:
        raise OnboardingError(f"unknown onboarding step: {step_id}")
    if status not in VALID_STATUSES:
        raise OnboardingError(f"invalid onboarding status: {status}")
    value = load_state(path)
    value["steps"][step_id] = {
        "status": status,
        "updated_at": int(time.time()) if now is None else now,
    }
    write_state(path, value)


def _credentials_complete(database_path: Path) -> bool:
    if not database_path.is_file():
        return False
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=2)
        try:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "web_account" not in tables:
                return False
            row = connection.execute(
                "SELECT 1 FROM web_account WHERE enabled=1 AND must_change=0 "
                "AND role IN ('administrator','operator') LIMIT 1"
            ).fetchone()
            return row is not None
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def checklist(config: Config, state_path: Path) -> list[dict[str, Any]]:
    state = load_state(state_path)
    stored = state["steps"]
    values: list[dict[str, Any]] = []
    for step in STEPS:
        entry = stored.get(step.id, {})
        status = entry.get("status", "pending")
        source = "recorded" if entry else "pending"
        if step.id == "operator_credentials" and _credentials_complete(Path(config.store.path)):
            status, source = "completed", "detected"
        values.append(
            {
                **asdict(step),
                "status": status,
                "status_source": source,
                "updated_at": entry.get("updated_at"),
            }
        )
    return values


def _print_checklist(values: list[dict[str, Any]]) -> None:
    completed = sum(value["status"] == "completed" for value in values)
    deferred = sum(value["status"] == "deferred" for value in values)
    print(f"Outpost first-run checklist: {completed}/{len(values)} complete, {deferred} deferred")
    for value in values:
        marker = {"completed": "x", "deferred": "-", "pending": " "}[value["status"]]
        needs = value["needs"]
        requirement_text = "; ".join(
            f"{name.replace('another_operator', 'another operator')}={'yes' if needed else 'no'}"
            for name, needed in needs.items()
        )
        optional = " · optional" if value["optional"] else ""
        print(f"\n[{marker}] {value['id']} — {value['title']}{optional}")
        print(f"    Needs: {requirement_text}")
        print(f"    Do: {value['instructions']}")
        print(f"    Verify: {value['verify']}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resume and record Outpost first-run setup")
    parser.add_argument("--config", type=Path, default=Path("/etc/outpost/config.yaml"))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "action",
        choices=("status", "complete", "defer", "reopen"),
        nargs="?",
        default="status",
    )
    parser.add_argument("step", choices=tuple(STEP_BY_ID), nargs="?")
    return parser


def main() -> int:
    parser = _parser()
    arguments = parser.parse_args()
    config = load_config(arguments.config)
    state_path = arguments.state or Path(config.store.path).parent / "onboarding.json"
    if arguments.action == "status":
        if arguments.step is not None:
            parser.error("status does not accept a step")
    else:
        if arguments.step is None:
            parser.error(f"{arguments.action} requires a step")
        status_by_action: dict[str, StepStatus] = {
            "complete": "completed",
            "defer": "deferred",
            "reopen": "pending",
        }
        status = status_by_action[arguments.action]
        try:
            record_step(state_path, arguments.step, status)
        except (OSError, OnboardingError) as exc:
            parser.error(str(exc))
    try:
        values = checklist(config, state_path)
    except (OSError, OnboardingError) as exc:
        parser.error(str(exc))
    if arguments.json:
        print(json.dumps({"steps": values}, indent=2, sort_keys=True))
    else:
        _print_checklist(values)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
