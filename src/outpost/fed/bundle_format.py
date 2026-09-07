"""Small signed, uncompressed physical-transfer files. Signatures are not secrecy."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from outpost.fed.framing import wire_int
from outpost.fed.incident_updates import IncidentUpdates
from outpost.fed.revisions import source_revision

MAX_FILE_BYTES = 192 * 1024
MAX_CORE_BYTES = 128 * 1024
MAX_ITEMS = 8
CONTEXT = b"outpost-physical-transfer-v1\x00"
FORMAT = "outpost-physical-transfer"
NODE = re.compile(r"^![0-9a-f]{8}$")
LABELS = frozenset({"author_label", "reporter_label", "raised_by"})
LOCATIONS = frozenset({"lat", "lon", "location_text"})
FIELDS = {
    "board": frozenset(
        "uid seq author_label origin_node body created_at edited_at thread_uid subject slug".split()
    ),
    "incidents": frozenset(
        (
            "uid type severity status title body lat lon location_text radius_m reporter_label "
            "origin_node created_at updated_at expires_at resolved_at resolution_note origin_uids"
        ).split()
    ),
    "alerts": frozenset(
        (
            "uid severity headline body source source_ref raised_by raised_at effective_at "
            "expires_at cancelled_at"
        ).split()
    ),
    "incident_updates": frozenset("uid incident_uid kind body author_label created_at".split()),
}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field in bundle")
        result[key] = value
    return result


def _json(raw: bytes) -> dict[str, Any]:
    try:
        result = json.loads(raw, object_pairs_hook=_pairs)
        # Bound nesting and reject non-finite numbers even inside unrecognized data.
        pending = [(result, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 10:
                raise ValueError("Bundle nesting is too deep")
            if isinstance(value, dict):
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
        canonical(result)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ValueError("Invalid bounded bundle JSON") from error
    if not isinstance(result, dict):
        raise ValueError("Bundle must be a JSON object")
    return result


def scope(*, public_labels: bool = False, precise_locations: bool = False) -> dict[str, Any]:
    if type(public_labels) is not bool or type(precise_locations) is not bool:
        raise ValueError("Bundle scope flags must be booleans")
    return {
        "public_records_only": True,
        "public_labels": public_labels,
        "precise_locations": precise_locations,
    }


def eligible(payload: dict[str, Any], policy: dict[str, Any]) -> bool:
    return (policy["public_labels"] or not any(payload.get(key) for key in LABELS)) and (
        policy["precise_locations"]
        or not any(payload.get(key) is not None and payload.get(key) != "" for key in LOCATIONS)
    )


def validate_item(item: Any, origin: str, policy: dict[str, Any]) -> None:
    if not isinstance(item, dict) or set(item) != {"stream", "uid", "epoch", "revision", "payload"}:
        raise ValueError("Unsupported bundle record fields")
    stream, uid, payload = item["stream"], item["uid"], item["payload"]
    if not isinstance(stream, str) or len(stream) > 80:
        raise ValueError("Invalid bundle stream")
    kind = "board" if stream.startswith("board:") else stream
    if kind not in FIELDS or not isinstance(payload, dict) or set(payload) != FIELDS[kind]:
        raise ValueError("Unsupported public record schema; private streams are never imported")
    if (
        not isinstance(uid, str)
        or not uid.startswith(origin + ":")
        or not 10 < len(uid) <= 160
        or payload.get("uid") != uid
    ):
        raise ValueError("Bundle record is not owned by its signing producer")
    if source_revision(item) is None or len(canonical(payload)) > 12_000:
        raise ValueError("Invalid or oversized producer revision")
    numeric = {
        "seq",
        "created_at",
        "edited_at",
        "updated_at",
        "expires_at",
        "resolved_at",
        "raised_at",
        "effective_at",
        "cancelled_at",
    }
    for key, value in payload.items():
        if key == "origin_uids":
            if (
                not isinstance(value, list)
                or len(value) > 64
                or any(not isinstance(uid, str) or not 1 <= len(uid) <= 160 for uid in value)
            ):
                raise ValueError("Invalid bounded incident provenance")
        elif key in numeric and value is not None:
            wire_int(value, key, maximum=253402300799)
        elif key in {"lat", "lon", "radius_m"} and value is not None:
            low, high = (
                (-90, 90) if key == "lat" else (-180, 180) if key == "lon" else (0, 2**63 - 1)
            )
            if type(value) not in {int, float} or not low <= value <= high:
                raise ValueError("Invalid numeric incident location")
        elif (
            key not in numeric | {"lat", "lon", "radius_m"}
            and value is not None
            and not isinstance(value, str)
        ):
            raise ValueError("Record text fields must be strings or null")
    if kind == "board" and (
        not isinstance(payload["thread_uid"], str)
        or not 1 <= len(payload["thread_uid"]) <= 160
        or type(payload["seq"]) is not int
        or payload["seq"] < 1
        or type(payload["created_at"]) is not int
    ):
        raise ValueError("Invalid board identity or sequence")
    if kind == "incidents":
        # Local import owns the same semantic validation; reject before showing
        # an apparently actionable preview, without creating another importer.
        from outpost.fed.sync import FederationSyncService

        try:
            FederationSyncService._incident_values(payload, revisioned=True)
        except (TypeError, OverflowError) as error:
            raise ValueError("Invalid incident values") from error
    if not eligible(payload, policy):
        raise ValueError("Record exceeds the explicitly approved confidentiality scope")
    if kind == "incident_updates":
        IncidentUpdates.validate(origin, uid, payload)
    if kind == "alerts" and (
        payload["severity"] not in {"caution", "urgent", "critical"}
        or type(payload["raised_at"]) is not int
    ):
        raise ValueError("Invalid alert severity or timestamp")


@dataclass(frozen=True)
class Bundle:
    core: dict[str, Any]
    public_key: bytes
    fingerprint: str
    digest: str


def encode(core: dict[str, Any], private: bytes, public: bytes) -> bytes:
    raw = canonical(core)
    if len(raw) > MAX_CORE_BYTES:
        raise ValueError("Bundle core is too large")
    signature = Ed25519PrivateKey.from_private_bytes(private).sign(CONTEXT + raw)
    result = canonical(
        {
            "format": FORMAT,
            "version": 1,
            "core": base64.b64encode(raw).decode(),
            "public_key": public.hex(),
            "signature": signature.hex(),
        }
    )
    # Use the same strict parser for our own output before it can leave the node.
    decode(result)
    return result


def decode(raw: bytes) -> Bundle:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_FILE_BYTES:
        raise ValueError("Bundle must be 1..196608 bytes")
    outer = _json(raw)
    if (
        set(outer) != {"format", "version", "core", "public_key", "signature"}
        or outer["format"] != FORMAT
        or type(outer["version"]) is not int
        or outer["version"] != 1
    ):
        raise ValueError("Unsupported bundle format/version")
    try:
        if not all(isinstance(outer[key], str) for key in ("core", "public_key", "signature")):
            raise ValueError("Invalid signature fields")
        if not re.fullmatch(r"[a-f0-9]{64}", outer["public_key"]) or not re.fullmatch(
            r"[a-f0-9]{128}", outer["signature"]
        ):
            raise ValueError("Invalid signature sizes")
        core_raw = base64.b64decode(outer["core"], validate=True)
        if len(core_raw) > MAX_CORE_BYTES:
            raise ValueError("Bundle core is too large")
        public = bytes.fromhex(outer["public_key"])
        Ed25519PublicKey.from_public_bytes(public).verify(
            bytes.fromhex(outer["signature"]), CONTEXT + core_raw
        )
    except (ValueError, InvalidSignature) as error:
        raise ValueError("Invalid bundle signature or encoding") from error
    core = _json(core_raw)
    if set(core) != {"origin", "destination", "created_at", "scope", "items"}:
        raise ValueError("Unsupported bundle core fields")
    for field in ("origin", "destination"):
        if not isinstance(core[field], str) or not NODE.fullmatch(core[field]):
            raise ValueError("Invalid bundle node identity")
    if core["origin"] == core["destination"]:
        raise ValueError("A bundle must target another Outpost")
    wire_int(core["created_at"], "bundle timestamp", maximum=253402300799)
    policy = core["scope"]
    if (
        not isinstance(policy, dict)
        or set(policy) != set(scope())
        or policy["public_records_only"] is not True
    ):
        raise ValueError("Unsupported bundle confidentiality scope")
    scope(public_labels=policy["public_labels"], precise_locations=policy["precise_locations"])
    if not isinstance(core["items"], list) or not 1 <= len(core["items"]) <= MAX_ITEMS:
        raise ValueError("A bundle must contain 1..8 public records")
    seen = set()
    for item in core["items"]:
        validate_item(item, core["origin"], policy)
        key = (item["stream"], item["uid"])
        if key in seen:
            raise ValueError("Duplicate bundle record")
        seen.add(key)
    # Semantic identity is independent of harmless outer JSON whitespace.
    return Bundle(core, public, digest(public), digest(CONTEXT + public + canonical(core)))
