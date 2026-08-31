from __future__ import annotations

import base64
import re
import secrets
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

from outpost.config import ChannelConfig

if TYPE_CHECKING:
    from outpost.config import Config


OUTPOST_PROFILE_VERSION = 1
OUTPOST_CHANNEL_NAMES = ("public", "outpost", "watch")
CHANNEL_BINDINGS_SETTING = "radio.outpost_channel_bindings"


@dataclass(frozen=True)
class StandardChannel:
    name: str
    psk_base64: str

    @property
    def psk(self) -> bytes:
        return base64.b64decode(self.psk_base64, validate=True)


# These are interoperability keys, not authentication secrets. They are intentionally
# common to every Outpost v1 installation so separately administered nodes can meet on air.
OUTPOST_CHANNEL_PROFILE = {
    "public": StandardChannel("public", "VSZiALtrapuZLiO/fTH8Yn7HJ1VDOVd3KBfX1vwNgG4="),
    "outpost": StandardChannel("outpost", "tgqEVrUA460UvY5Olnio0BUSARs2UFadQDlKE4YWoqU="),
    "watch": StandardChannel("watch", "Q0Y3uM3l38YIAaTabAo8MBgJJ6HANWiBQnYy8jxF5ok="),
}

_DEFAULT_POLICIES = {
    "public": ChannelConfig(
        name="public", ai=False, bbs="read_only", alerts=True, accept_reports=True
    ),
    "outpost": ChannelConfig(name="outpost", ai=True, bbs="full", alerts=True, accept_reports=True),
    "watch": ChannelConfig(name="watch", ai=False, bbs="none", alerts=True, accept_reports=True),
}
_MESH_ID = re.compile(r"^![0-9a-fA-F]{8}$")


def mesh_id_suffix(mesh_id: str | None) -> str | None:
    value = str(mesh_id or "").strip()
    return value[-4:].lower() if _MESH_ID.fullmatch(value) else None


def matching_profile_channel(name: str, psk: bytes) -> str | None:
    normalized = str(name).strip().lower()
    profile = OUTPOST_CHANNEL_PROFILE.get(normalized)
    if profile is None or not secrets.compare_digest(bytes(psk), profile.psk):
        return None
    return normalized


def outpost_display_name(base_name: str, mesh_id: str | None, *, max_bytes: int = 40) -> str:
    """Return a radio-distinct display name without changing the stored human name."""
    base = " ".join(str(base_name).split()) or "Outpost"
    suffix = mesh_id_suffix(mesh_id)
    if suffix is None or base.lower().endswith(f" {suffix}"):
        return _truncate_utf8(base, max_bytes)
    reserved = len(suffix.encode()) + 1
    trimmed = _truncate_utf8(base, max(1, max_bytes - reserved)).rstrip()
    return f"{trimmed} {suffix}"


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode()
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def configured_channel_bindings(config: Config) -> dict[str, int]:
    bindings: dict[str, int] = {}
    for index, policy in config.channels.items():
        name = policy.name.strip().lower()
        if name in OUTPOST_CHANNEL_PROFILE and name not in bindings:
            bindings[name] = int(index)
    return bindings


def channel_slot(config: Config, name: str, fallback: int) -> int:
    return configured_channel_bindings(config).get(name.strip().lower(), fallback)


def apply_channel_bindings(config: Config, bindings: dict[str, int]) -> None:
    """Move semantic Outpost policies to verified radio slots in memory."""
    normalized = {str(name).lower(): int(index) for name, index in bindings.items()}
    if set(normalized) != set(OUTPOST_CHANNEL_NAMES):
        raise ValueError("Outpost channel bindings must include public, outpost, and watch")
    if len(set(normalized.values())) != len(normalized):
        raise ValueError("Outpost channel bindings must use distinct radio slots")
    if any(index < 0 or index > 7 for index in normalized.values()):
        raise ValueError("Outpost channel bindings must use radio slots 0 through 7")

    previous = configured_channel_bindings(config)
    policies = {
        name: deepcopy(
            next(
                (
                    policy
                    for policy in config.channels.values()
                    if policy.name.strip().lower() == name
                ),
                _DEFAULT_POLICIES[name],
            )
        )
        for name in OUTPOST_CHANNEL_NAMES
    }
    retained = {
        int(index): deepcopy(policy)
        for index, policy in config.channels.items()
        if policy.name.strip().lower() not in OUTPOST_CHANNEL_PROFILE
    }
    collisions = sorted(set(retained) & set(normalized.values()))
    if collisions:
        slots = ", ".join(str(index) for index in collisions)
        raise ValueError(f"selected slot(s) already have custom Outpost policy: {slots}")
    config.channels = {
        **retained,
        **{index: policies[name] for name, index in normalized.items()},
    }

    slot_changes = {
        old: normalized[name] for name, old in previous.items() if old != normalized[name]
    }
    if not slot_changes:
        return
    escalation = config.watch.escalation.model_dump(mode="python")
    for policy in escalation.values():
        for stage in policy["stages"]:
            stage["channels"] = list(
                dict.fromkeys(slot_changes.get(channel, channel) for channel in stage["channels"])
            )
    config.watch.escalation = type(config.watch.escalation).model_validate(escalation)
