from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from outpost import __version__
from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config, WebTransport
from outpost.router.models import DispatchTrace
from outpost.transport.models import InboundMessage
from outpost.transport.simulated import SentPacket, SimulatedRadioLink

REPLAY_FORMAT = "outpost-replay/v1"
MAX_REPLAY_RECORDS = 100_000
MAX_REPLAY_TEXT_BYTES = 16_384
MAX_REPLAY_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_UNIX_TIMESTAMP = 253_402_300_799
VALID_TRUST = frozenset({"blocked", "guest", "member", "trusted", "responder", "operator"})
VALID_PKI_STATES = frozenset({"unknown", "pending", "verified", "conflict"})
_MESSAGE_FIELDS = (
    "id",
    "peer_mesh_id",
    "channel",
    "portnum",
    "is_direct",
    "packet_id",
    "text",
    "payload",
    "rx_snr",
    "rx_rssi",
    "hops",
    "transport",
    "created_at",
    "to_mesh_id",
    "want_ack",
    "pki_encrypted",
    "pki_public_key",
    "no_reply",
    "request_id",
    "routing_error",
    "latitude",
    "longitude",
    "rx_time",
)
_FIDELITY_FIELDS = frozenset(_MESSAGE_FIELDS[7:])


class ReplayError(ValueError):
    pass


@dataclass(frozen=True)
class ReplaySelection:
    start_id: int | None = None
    end_id: int | None = None
    since: int | None = None
    until: int | None = None
    limit: int = 1_000

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_REPLAY_RECORDS:
            raise ReplayError(f"replay limit must be 1-{MAX_REPLAY_RECORDS}")
        for name, value in (
            ("start-id", self.start_id),
            ("end-id", self.end_id),
            ("since", self.since),
            ("until", self.until),
        ):
            if value is not None and value < 0:
                raise ReplayError(f"{name} must not be negative")
        if self.start_id is not None and self.end_id is not None and self.start_id > self.end_id:
            raise ReplayError("start-id must not exceed end-id")
        if self.since is not None and self.until is not None and self.since > self.until:
            raise ReplayError("since must not exceed until")


@dataclass(frozen=True)
class ReplayMember:
    mesh_id: str
    trust: str = "guest"
    handle: str | None = None
    public_key: bytes | None = None
    pki_state: str = "unknown"

    def __post_init__(self) -> None:
        if not _valid_mesh_id(self.mesh_id):
            raise ReplayError("replay member has an invalid mesh id")
        if self.trust not in VALID_TRUST:
            raise ReplayError(f"replay member has an invalid trust level: {self.trust}")
        if self.pki_state not in VALID_PKI_STATES:
            raise ReplayError(f"replay member has an invalid PKI state: {self.pki_state}")


@dataclass(frozen=True)
class ReplayRecord:
    source_id: int
    peer_mesh_id: str
    channel: int
    portnum: int
    is_direct: bool
    packet_id: int
    text: str | None
    payload: bytes | None
    rx_snr: float | None
    rx_rssi: int | None
    hops_away: int | None
    via_mqtt: bool
    created_at: int
    to_mesh_id: str | None = None
    want_ack: bool = False
    pki_encrypted: bool = False
    pki_public_key: bytes | None = None
    no_reply: bool = False
    request_id: int | None = None
    routing_error: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    rx_time: int | None = None

    def __post_init__(self) -> None:
        if self.source_id < 1:
            raise ReplayError("replay message source id must be positive")
        if not _valid_mesh_id(self.peer_mesh_id):
            raise ReplayError("replay message has an invalid sender mesh id")
        if self.to_mesh_id is not None and not _valid_destination(self.to_mesh_id):
            raise ReplayError("replay message has an invalid destination mesh id")
        if not 0 <= self.channel <= 7:
            raise ReplayError("replay message channel must be 0-7")
        if not 0 <= self.portnum <= 511:
            raise ReplayError("replay message portnum must be 0-511")
        if not 0 <= self.packet_id <= 0xFFFFFFFF:
            raise ReplayError("replay message packet id must be an unsigned 32-bit integer")
        if not 0 <= self.created_at <= MAX_UNIX_TIMESTAMP:
            raise ReplayError("replay message timestamp is outside the supported range")
        if self.rx_time is not None and not 0 <= self.rx_time <= MAX_UNIX_TIMESTAMP:
            raise ReplayError("replay packet receive time is outside the supported range")
        if self.request_id is not None and not 0 <= self.request_id <= 0xFFFFFFFF:
            raise ReplayError("replay request id must be an unsigned 32-bit integer")
        if self.latitude is not None and not -90 <= self.latitude <= 90:
            raise ReplayError("replay latitude must be between -90 and 90")
        if self.longitude is not None and not -180 <= self.longitude <= 180:
            raise ReplayError("replay longitude must be between -180 and 180")

    def message(self, local_node_id: str) -> InboundMessage:
        return InboundMessage(
            packet_id=self.packet_id,
            from_id=self.peer_mesh_id,
            to_id=self.to_mesh_id or local_node_id,
            channel=self.channel,
            portnum=self.portnum,
            is_direct=self.is_direct,
            text=self.text,
            payload=self.payload,
            rx_time=datetime.fromtimestamp(
                self.rx_time if self.rx_time is not None else self.created_at, UTC
            ),
            rx_snr=self.rx_snr,
            rx_rssi=self.rx_rssi,
            hops_away=self.hops_away,
            want_ack=self.want_ack,
            pki_encrypted=self.pki_encrypted,
            pki_public_key=self.pki_public_key,
            via_mqtt=self.via_mqtt,
            no_reply=self.no_reply,
            request_id=self.request_id,
            routing_error=self.routing_error,
            latitude=self.latitude,
            longitude=self.longitude,
        )


@dataclass(frozen=True)
class ReplayCorpus:
    records: tuple[ReplayRecord, ...]
    members: tuple[ReplayMember, ...] = ()
    source_kind: Literal["database", "bundle"] = "database"
    schema_version: int | None = None
    limitations: tuple[str, ...] = ()
    redacted: bool = False


def _read_only_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ReplayError(f"replay source does not exist: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _optional_blob(value: object) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    raise ReplayError("stored replay payload is not binary")


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray, int, float)):
        return int(value)
    raise ReplayError("stored replay value is not an integer")


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float)):
        return float(value)
    raise ReplayError("stored replay value is not numeric")


def _database_corpus(path: Path, selection: ReplaySelection) -> ReplayCorpus:
    connection = _read_only_connection(path)
    try:
        columns = _columns(connection, "message_log")
        required = {
            "id",
            "direction",
            "peer_mesh_id",
            "channel",
            "portnum",
            "is_direct",
            "packet_id",
            "text",
            "created_at",
        }
        missing_required = sorted(required - columns)
        if missing_required:
            raise ReplayError(
                "source message_log is missing required fields: " + ", ".join(missing_required)
            )
        selected = [field if field in columns else f"NULL AS {field}" for field in _MESSAGE_FIELDS]
        clauses = ["direction='in'", "peer_mesh_id IS NOT NULL"]
        params: list[object] = []
        for sql, value in (
            ("id>=?", selection.start_id),
            ("id<=?", selection.end_id),
            ("created_at>=?", selection.since),
            ("created_at<=?", selection.until),
        ):
            if value is not None:
                clauses.append(sql)
                params.append(value)
        query = (
            "SELECT * FROM (SELECT "  # noqa: S608 - fixed local field/predicate allowlists.
            + ",".join(selected)
            + " FROM message_log WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC,id DESC LIMIT ?) "
            "ORDER BY created_at,id"
        )
        rows = list(connection.execute(query, (*params, selection.limit)))
        records: list[ReplayRecord] = []
        for row in rows:
            peer_mesh_id = str(row["peer_mesh_id"])
            if not _valid_mesh_id(peer_mesh_id):
                raise ReplayError(f"message {row['id']} has an invalid sender mesh id")
            raw_text = row["text"]
            text = str(raw_text) if raw_text is not None else None
            if text is not None and len(text.encode()) > MAX_REPLAY_TEXT_BYTES:
                raise ReplayError(f"message {row['id']} exceeds the replay text limit")
            created_at = int(row["created_at"])
            records.append(
                ReplayRecord(
                    source_id=int(row["id"]),
                    peer_mesh_id=peer_mesh_id,
                    channel=int(row["channel"]),
                    portnum=int(row["portnum"]),
                    is_direct=bool(row["is_direct"]),
                    packet_id=int(row["packet_id"] or row["id"]),
                    text=text,
                    payload=_optional_blob(row["payload"]),
                    rx_snr=_optional_float(row["rx_snr"]),
                    rx_rssi=_optional_int(row["rx_rssi"]),
                    hops_away=_optional_int(row["hops"]),
                    via_mqtt=str(row["transport"] or "").lower() == "mqtt",
                    created_at=created_at,
                    to_mesh_id=str(row["to_mesh_id"]) if row["to_mesh_id"] else None,
                    want_ack=bool(row["want_ack"]),
                    pki_encrypted=bool(row["pki_encrypted"]),
                    pki_public_key=_optional_blob(row["pki_public_key"]),
                    no_reply=bool(row["no_reply"]),
                    request_id=_optional_int(row["request_id"]),
                    routing_error=(
                        str(row["routing_error"]) if row["routing_error"] is not None else None
                    ),
                    latitude=_optional_float(row["latitude"]),
                    longitude=_optional_float(row["longitude"]),
                    rx_time=_optional_int(row["rx_time"]),
                )
            )

        members: list[ReplayMember] = []
        member_columns = _columns(connection, "member")
        peer_ids = sorted({record.peer_mesh_id for record in records})
        if peer_ids and {"mesh_id", "trust"}.issubset(member_columns):
            placeholders = ",".join("?" for _ in peer_ids)
            member_select = [
                field if field in member_columns else f"NULL AS {field}"
                for field in ("mesh_id", "trust", "handle", "public_key", "pki_state")
            ]
            member_rows = connection.execute(
                f"SELECT {','.join(member_select)} FROM member "  # noqa: S608
                f"WHERE mesh_id IN ({placeholders}) ORDER BY mesh_id",  # noqa: S608
                peer_ids,
            )
            members = [
                ReplayMember(
                    mesh_id=str(row["mesh_id"]),
                    trust=str(row["trust"] or "guest"),
                    handle=str(row["handle"]) if row["handle"] else None,
                    public_key=_optional_blob(row["public_key"]),
                    pki_state=str(row["pki_state"] or "unknown"),
                )
                for row in member_rows
            ]
        schema_rows = list(connection.execute("SELECT MAX(version) FROM schema_version"))
        schema_version = int(schema_rows[0][0]) if schema_rows and schema_rows[0][0] else None
        missing_fidelity = sorted(_FIDELITY_FIELDS - columns)
        limitations = []
        if missing_fidelity:
            limitations.append(
                "The source predates replay-fidelity fields: " + ", ".join(missing_fidelity) + "."
            )
        if any(record.pki_encrypted and record.pki_public_key is None for record in records):
            limitations.append("At least one encrypted packet lacks its authenticated public key.")
        if any(record.text is None and record.payload is None for record in records):
            limitations.append("At least one non-text packet lacks a retained binary payload.")
        if any(record.portnum == 5 and record.request_id is not None for record in records):
            limitations.append(
                "Routing acknowledgements start without their historical outbound "
                "correlation state."
            )
        return ReplayCorpus(
            records=tuple(records),
            members=tuple(members),
            schema_version=schema_version,
            limitations=tuple(limitations),
        )
    except sqlite3.DatabaseError as error:
        raise ReplayError(f"cannot read replay source: {error}") from error
    finally:
        connection.close()


def _valid_mesh_id(value: str) -> bool:
    if len(value) != 9 or not value.startswith("!"):
        return False
    try:
        int(value[1:], 16)
    except ValueError:
        return False
    return True


def _valid_destination(value: str) -> bool:
    return value == "^all" or _valid_mesh_id(value)


def _decoded_blob(value: object, field: str) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReplayError(f"{field} must be base64 text or null")
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise ReplayError(f"{field} is not valid base64") from error
    if len(decoded) > 1_024:
        raise ReplayError(f"{field} exceeds the replay payload limit")
    return decoded


def _boolean(value: object, field: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ReplayError(f"{field} must be true or false")
    return value


def _bundle_corpus(path: Path, selection: ReplaySelection) -> ReplayCorpus:
    try:
        if path.stat().st_size > MAX_REPLAY_BUNDLE_BYTES:
            raise ReplayError(
                f"replay bundle exceeds the {MAX_REPLAY_BUNDLE_BYTES // (1024 * 1024)} MiB limit"
            )
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ReplayError(f"cannot read replay bundle: {error}") from error
    if not isinstance(value, dict) or value.get("format") != REPLAY_FORMAT:
        raise ReplayError(f"replay bundle must use {REPLAY_FORMAT}")
    raw_records = value.get("messages")
    raw_members = value.get("members", [])
    if not isinstance(raw_records, list) or not isinstance(raw_members, list):
        raise ReplayError("replay bundle messages and members must be arrays")
    if len(raw_records) > MAX_REPLAY_RECORDS:
        raise ReplayError("replay bundle contains too many messages")
    if len(raw_members) > MAX_REPLAY_RECORDS:
        raise ReplayError("replay bundle contains too many members")
    records: list[ReplayRecord] = []
    source_ids: set[int] = set()
    for index, raw in enumerate(raw_records, 1):
        if not isinstance(raw, dict):
            raise ReplayError(f"bundle message {index} must be an object")
        try:
            source_id = int(raw["source_id"])
            peer_mesh_id = str(raw["peer_mesh_id"])
            created_at = int(raw["created_at"])
            text_value = raw.get("text")
            text = str(text_value) if text_value is not None else None
            if not _valid_mesh_id(peer_mesh_id):
                raise ReplayError(f"bundle message {index} has an invalid sender mesh id")
            if text is not None and len(text.encode()) > MAX_REPLAY_TEXT_BYTES:
                raise ReplayError(f"bundle message {index} exceeds the replay text limit")
            record = ReplayRecord(
                source_id=source_id,
                peer_mesh_id=peer_mesh_id,
                channel=int(raw.get("channel", 0)),
                portnum=int(raw.get("portnum", 1)),
                is_direct=_boolean(raw.get("is_direct"), "is_direct", default=True),
                packet_id=int(raw.get("packet_id", source_id)),
                text=text,
                payload=_decoded_blob(raw.get("payload"), "payload"),
                rx_snr=_optional_float(raw.get("rx_snr")),
                rx_rssi=_optional_int(raw.get("rx_rssi")),
                hops_away=_optional_int(raw.get("hops_away")),
                via_mqtt=_boolean(raw.get("via_mqtt"), "via_mqtt"),
                created_at=created_at,
                to_mesh_id=str(raw["to_mesh_id"]) if raw.get("to_mesh_id") else None,
                want_ack=_boolean(raw.get("want_ack"), "want_ack"),
                pki_encrypted=_boolean(raw.get("pki_encrypted"), "pki_encrypted"),
                pki_public_key=_decoded_blob(raw.get("pki_public_key"), "pki_public_key"),
                no_reply=_boolean(raw.get("no_reply"), "no_reply"),
                request_id=_optional_int(raw.get("request_id")),
                routing_error=str(raw["routing_error"]) if raw.get("routing_error") else None,
                latitude=_optional_float(raw.get("latitude")),
                longitude=_optional_float(raw.get("longitude")),
                rx_time=_optional_int(raw.get("rx_time")),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, ReplayError):
                raise
            raise ReplayError(f"bundle message {index} is invalid: {error}") from error
        if source_id in source_ids:
            raise ReplayError(f"bundle message {index} repeats source id {source_id}")
        source_ids.add(source_id)
        if selection.start_id is not None and source_id < selection.start_id:
            continue
        if selection.end_id is not None and source_id > selection.end_id:
            continue
        if selection.since is not None and created_at < selection.since:
            continue
        if selection.until is not None and created_at > selection.until:
            continue
        records.append(record)
    records = sorted(records, key=lambda item: (item.created_at, item.source_id))[
        -selection.limit :
    ]
    members: list[ReplayMember] = []
    member_ids: set[str] = set()
    for index, raw in enumerate(raw_members, 1):
        if not isinstance(raw, dict):
            raise ReplayError(f"bundle member {index} must be an object")
        mesh_id = str(raw.get("mesh_id", ""))
        if not _valid_mesh_id(mesh_id):
            raise ReplayError(f"bundle member {index} has an invalid mesh id")
        if mesh_id in member_ids:
            raise ReplayError(f"bundle member {index} repeats mesh id {mesh_id}")
        member_ids.add(mesh_id)
        members.append(
            ReplayMember(
                mesh_id=mesh_id,
                trust=str(raw.get("trust", "guest")),
                handle=str(raw["handle"]) if raw.get("handle") else None,
                public_key=_decoded_blob(raw.get("public_key"), "member.public_key"),
                pki_state=str(raw.get("pki_state", "unknown")),
            )
        )
    metadata = value.get("metadata", {})
    limitations = value.get("limitations", [])
    if not isinstance(metadata, dict) or not isinstance(limitations, list):
        raise ReplayError("replay bundle metadata or limitations are invalid")
    return ReplayCorpus(
        records=tuple(records),
        members=tuple(members),
        source_kind="bundle",
        schema_version=_optional_int(metadata.get("source_schema")),
        limitations=tuple(str(item) for item in limitations),
        redacted=bool(metadata.get("redacted", False)),
    )


def load_corpus(path: Path, selection: ReplaySelection | None = None) -> ReplayCorpus:
    chosen = selection or ReplaySelection()
    if path.suffix.lower() == ".json":
        return _bundle_corpus(path, chosen)
    return _database_corpus(path, chosen)


def _alias_map(values: set[str]) -> dict[str, str]:
    salt = secrets.token_bytes(32)
    aliases: dict[str, str] = {}
    used: set[str] = set()
    for value in sorted(values):
        counter = 0
        while True:
            digest = hashlib.sha256(salt + value.encode() + counter.to_bytes(2, "big")).hexdigest()
            alias = f"!{digest[:8]}"
            if alias not in used:
                aliases[value] = alias
                used.add(alias)
                break
            counter += 1
    return aliases


def _coarsen(
    latitude: float | None, longitude: float | None, meters: int
) -> tuple[float | None, float | None]:
    if latitude is None or longitude is None:
        return latitude, longitude
    lat_step = meters / 111_320
    lon_scale = max(0.1, abs(math.cos(math.radians(latitude))))
    lon_step = meters / (111_320 * lon_scale)
    return round(round(latitude / lat_step) * lat_step, 6), round(
        round(longitude / lon_step) * lon_step, 6
    )


def redacted_bundle(
    corpus: ReplayCorpus,
    *,
    strip_bodies: bool = False,
    coarsen_meters: int = 1_000,
) -> dict[str, object]:
    if coarsen_meters < 100:
        raise ReplayError("export position coarsening must be at least 100 metres")
    identifiers = {record.peer_mesh_id for record in corpus.records}
    identifiers.update(
        record.to_mesh_id
        for record in corpus.records
        if record.to_mesh_id is not None and _valid_mesh_id(record.to_mesh_id)
    )
    aliases = _alias_map(identifiers)
    member_aliases = {member.mesh_id: index for index, member in enumerate(corpus.members, 1)}
    messages: list[dict[str, object]] = []
    for record in corpus.records:
        latitude, longitude = _coarsen(record.latitude, record.longitude, coarsen_meters)
        messages.append(
            {
                "source_id": record.source_id,
                "peer_mesh_id": aliases[record.peer_mesh_id],
                "to_mesh_id": aliases.get(record.to_mesh_id or "", record.to_mesh_id),
                "channel": record.channel,
                "portnum": record.portnum,
                "is_direct": record.is_direct,
                "packet_id": record.packet_id,
                "text": None if strip_bodies else record.text,
                "payload": None,
                "rx_snr": record.rx_snr,
                "rx_rssi": record.rx_rssi,
                "hops_away": record.hops_away,
                "via_mqtt": record.via_mqtt,
                "created_at": record.created_at,
                "want_ack": record.want_ack,
                "pki_encrypted": record.pki_encrypted,
                "pki_public_key": None,
                "no_reply": record.no_reply,
                "request_id": record.request_id,
                "routing_error": record.routing_error,
                "latitude": latitude,
                "longitude": longitude,
                "rx_time": record.rx_time,
            }
        )
    members = [
        {
            "mesh_id": aliases[member.mesh_id],
            "trust": member.trust,
            "handle": f"member-{member_aliases[member.mesh_id]:04d}" if member.handle else None,
            "public_key": None,
            "pki_state": "unknown" if member.public_key is not None else member.pki_state,
        }
        for member in corpus.members
        if member.mesh_id in aliases
    ]
    limitations = list(corpus.limitations)
    limitations.append("Member and destination node identifiers were pseudonymised.")
    limitations.append(f"Positions were coarsened to approximately {coarsen_meters} metres.")
    limitations.append("Binary payloads and authenticated PKI public keys were stripped.")
    if strip_bodies:
        limitations.append("Message bodies were stripped; text commands cannot be reproduced.")
    return {
        "format": REPLAY_FORMAT,
        "metadata": {
            "redacted": True,
            "source_schema": corpus.schema_version,
            "message_count": len(messages),
            "first_source_id": messages[0]["source_id"] if messages else None,
            "last_source_id": messages[-1]["source_id"] if messages else None,
            "coarsen_meters": coarsen_meters,
            "bodies_stripped": strip_bodies,
        },
        "limitations": sorted(set(limitations)),
        "members": members,
        "messages": messages,
    }


def write_private_json(path: Path, value: object, *, overwrite: bool = False) -> None:
    destination = path.expanduser().resolve(strict=False)
    if destination.exists() and not overwrite:
        raise ReplayError(f"destination exists; use --force to replace it: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _isolated_config(
    base: Config, scratch_path: Path, allow_providers: bool
) -> tuple[Config, list[str]]:
    limitations: list[str] = []
    modules = base.modules
    ai = base.ai
    if not allow_providers:
        if modules.env.enabled:
            modules = modules.model_copy(
                update={"env": modules.env.model_copy(update={"enabled": False})}
            )
            limitations.append(
                "Environment-provider commands were disabled to prevent replay network access."
            )
        if modules.ai.enabled and ai.provider != "null":
            ai = ai.model_copy(update={"provider": "null"})
            limitations.append(
                "AI inference was replaced with the null provider to prevent replay data egress."
            )
    store = base.store.model_copy(update={"path": str(scratch_path)})
    web = base.web.model_copy(update={"transport": WebTransport()})
    return (
        base.model_copy(update={"store": store, "web": web, "modules": modules, "ai": ai}),
        limitations,
    )


def _choose_local_node(corpus: ReplayCorpus) -> str:
    occupied = {record.peer_mesh_id for record in corpus.records}
    for number in range(0xFFFFFFFE, 0xFFFF0000, -1):
        candidate = f"!{number:08x}"
        if candidate not in occupied:
            return candidate
    raise ReplayError("could not allocate a simulated local node id")


class ReplayHarness:
    def __init__(
        self,
        base_config: Config,
        corpus: ReplayCorpus,
        scratch_path: Path,
        *,
        mode: Literal["replay", "drill"] = "replay",
        preset: str = "LONG_FAST",
        region: str = "US",
        allow_providers: bool = False,
    ) -> None:
        epoch = datetime.fromtimestamp(
            corpus.records[0].created_at if corpus.records else int(time.time()), UTC
        )
        self.clock = VirtualClock(epoch=epoch)
        self.corpus = corpus
        self.allow_providers = allow_providers
        config_value = base_config.model_dump(mode="json")
        self.config_fingerprint = hashlib.sha256(
            json.dumps(config_value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.scratch_path = scratch_path.expanduser().resolve(strict=False)
        scratch_artifacts = (
            self.scratch_path,
            Path(f"{self.scratch_path}-wal"),
            Path(f"{self.scratch_path}-shm"),
        )
        existing = next((path for path in scratch_artifacts if path.exists()), None)
        if existing is not None:
            raise ReplayError(f"scratch database artifact already exists: {existing}")
        config, limitations = _isolated_config(base_config, self.scratch_path, allow_providers)
        self.limitations = [
            *corpus.limitations,
            *limitations,
            "Replay starts from clean domain state seeded only with retained member "
            "identity and trust.",
        ]
        if any(record.portnum == 5 and record.request_id is not None for record in corpus.records):
            self.limitations.append(
                "Routing acknowledgements start without their historical outbound "
                "correlation state."
            )
        node_id = _choose_local_node(corpus)
        channels = frozenset({0, *(record.channel for record in corpus.records)})
        self.radio = SimulatedRadioLink(
            self.clock,
            node_id=node_id,
            region=region,
            preset=preset,
            channels=channels,
        )
        self.app = OutpostApp(
            config,
            clock=self.clock,
            radio=self.radio,
            runtime_mode=mode,
            runtime_source="recorded mesh traffic",
        )
        intent_state = self.app.router.intents.status()
        if intent_state.get("state") not in {"ready", "empty"}:
            self.limitations.append(
                "The configured tolerant-intent map was unavailable; "
                "built-in intents remain active."
            )
        self._inbound = self.radio.inbound()
        self._prepared = False
        self._results: list[dict[str, object]] = []

    @property
    def processed_count(self) -> int:
        return len(self._results)

    async def prepare(self) -> None:
        if self._prepared:
            return
        self.scratch_path.parent.mkdir(parents=True, exist_ok=True)
        await self.app.database.open()
        try:
            await self.radio.connect()
            self.app.inbound_pipeline.local_node_id = self.radio.local_node_id
            self.app.incidents.origin_node = self.radio.local_node_id
            self.app.federation.local_mesh_id = self.radio.local_node_id
            self.app.federation_sync.local_mesh_id = self.radio.local_node_id
            await self.app.radio_power.restore()
            await self.app.radio_configuration.initialize()
            await self.app.governor.recover()
            await self.app.runtime_settings.load()
            await self._seed_members()
            now = int(self.clock.now().timestamp())
            self.app._task_health["replay-driver"] = {
                "state": "running",
                "failure_domain": "core",
                "required": True,
                "started_at": now,
                "last_started_at": now,
                "last_ok_at": now,
                "stopped_at": None,
                "error": None,
                "degraded_reason": None,
                "degradation_count": 0,
                "failure_count": 0,
                "consecutive_failures": 0,
                "restart_count": 0,
                "last_error": None,
                "last_error_at": None,
                "next_retry_at": None,
                "circuit_open": False,
            }
            self._prepared = True
        except BaseException:
            await self.app.database.close()
            raise

    async def _seed_members(self) -> None:
        for member in self.corpus.members:
            await self.app.router.members.resolve(member.mesh_id)
            pki_state = member.pki_state if member.public_key is not None else "unknown"
            await self.app.database.write(
                "UPDATE member SET handle=?,trust=?,public_key=?,pki_state=?,"
                "directory_state='active',reviewed_at=? WHERE mesh_id=?",
                (
                    member.handle,
                    member.trust,
                    member.public_key,
                    pki_state,
                    int(self.clock.now().timestamp()),
                    member.mesh_id,
                ),
            )

    async def close(self) -> None:
        if not self._prepared:
            return
        await self.app.shutdown()
        self._prepared = False

    def _advance_to(self, created_at: int) -> None:
        target = max(0.0, created_at - self.clock.epoch.timestamp())
        if target > self.clock.value:
            self.clock.advance(target - self.clock.value)

    async def _drain_outbound(self) -> None:
        idle_ticks = 0
        while sum(self.app.governor.queue_depths().values()) and idle_ticks < 7_200:
            item = await self.app.governor.tick()
            if item is None:
                idle_ticks += 1
                self.clock.advance(1)
            else:
                idle_ticks = 0

    @staticmethod
    def _sent_packet(packet: SentPacket) -> dict[str, object]:
        return {
            "kind": "data" if packet.payload is not None else "text",
            "destination": packet.dest,
            "channel": packet.channel,
            "want_ack": packet.want_ack,
            "text": packet.text,
            "payload_bytes": len(packet.payload) if packet.payload is not None else 0,
            "payload_sha256": (
                hashlib.sha256(packet.payload).hexdigest() if packet.payload is not None else None
            ),
        }

    async def process(self, record: ReplayRecord) -> dict[str, object]:
        if not self._prepared:
            raise RuntimeError("replay harness is not prepared")
        self._advance_to(record.created_at)
        before_sent = len(self.radio.sent)
        await self.radio.inject(record.message(self.radio.local_node_id))
        raw = await anext(self._inbound)
        message = self.app.inbound_pipeline.process(raw)
        if message is None:
            result: dict[str, object] = {
                "sequence": len(self._results) + 1,
                "source_id": record.source_id,
                "source_at": record.created_at,
                "packet": self._packet_summary(record),
                "command": {"input": None, "resolved": None, "resolution": None},
                "trust": {"level": None, "decision": "pipeline_dropped"},
                "response": {
                    "kind": "none",
                    "text": None,
                    "airtime_class": None,
                    "parts": 0,
                    "admission": None,
                    "drop_reason": "duplicate or self packet",
                },
                "transmissions": [],
            }
            self._results.append(result)
            return result
        log_id = await self.app.message_log.record_inbound(message)
        trace = DispatchTrace()
        handled = await self.app._handle_inbound_safely(message, log_id, trace)
        await self._drain_outbound()
        inbound_rows = await self.app.database.read(
            "SELECT outcome,drop_reason FROM message_log WHERE id=?", (log_id,)
        )
        inbound_outcome = dict(inbound_rows[0]) if inbound_rows else {}
        drop_reason = trace.admission if trace.admission not in {None, "admitted"} else None
        if not handled:
            drop_reason = str(inbound_outcome.get("drop_reason") or "handler failure")
        result = {
            "sequence": len(self._results) + 1,
            "source_id": record.source_id,
            "source_at": record.created_at,
            "packet": self._packet_summary(record),
            "command": {
                "input": trace.input_command,
                "resolved": trace.resolved_command,
                "resolution": trace.resolution,
            },
            "trust": {"level": trace.member_trust, "decision": trace.decision},
            "response": {
                "kind": trace.response_kind,
                "text": trace.response_text,
                "airtime_class": trace.airtime_class,
                "parts": trace.outbound_parts,
                "admission": trace.admission,
                "drop_reason": drop_reason,
            },
            "transmissions": [
                self._sent_packet(packet) for packet in self.radio.sent[before_sent:]
            ],
        }
        self.app._task_progress("replay-driver")
        self._results.append(result)
        return result

    @staticmethod
    def _packet_summary(record: ReplayRecord) -> dict[str, object]:
        packet_class = (
            "position"
            if record.latitude is not None and record.longitude is not None
            else "routing_ack"
            if record.portnum == 5 and record.request_id is not None
            else "text"
            if record.text is not None
            else "binary"
        )
        return {
            "class": packet_class,
            "channel": record.channel,
            "portnum": record.portnum,
            "direct": record.is_direct,
            "via_mqtt": record.via_mqtt,
        }

    async def run(self) -> dict[str, object]:
        await self.prepare()
        for record in self.corpus.records:
            await self.process(record)
        return self.report()

    def report(self) -> dict[str, object]:
        decisions: dict[str, int] = {}
        transmissions = 0
        for result in self._results:
            trust = result["trust"]
            assert isinstance(trust, dict)
            decision = str(trust["decision"])
            decisions[decision] = decisions.get(decision, 0) + 1
            sent = result["transmissions"]
            assert isinstance(sent, list)
            transmissions += len(sent)
        return {
            "format": "outpost-replay-result/v1",
            "engine": {
                "version": __version__,
                "config_sha256": self.config_fingerprint,
            },
            "source": {
                "kind": self.corpus.source_kind,
                "schema": self.corpus.schema_version,
                "redacted": self.corpus.redacted,
                "messages": len(self.corpus.records),
                "first_id": self.corpus.records[0].source_id if self.corpus.records else None,
                "last_id": self.corpus.records[-1].source_id if self.corpus.records else None,
            },
            "simulation": {
                "clock": "virtual",
                "radio": "simulated",
                "store": "scratch",
                "node_id": self.radio.local_node_id,
                "region": self.radio.snapshot.region,
                "preset": self.radio.snapshot.preset,
                "provider_access": self.allow_providers,
            },
            "limitations": sorted(set(self.limitations)),
            "summary": {
                "processed": len(self._results),
                "transmissions": transmissions,
                "decisions": dict(sorted(decisions.items())),
                "queued_after_replay": sum(self.app.governor.queue_depths().values()),
            },
            "messages": list(self._results),
        }


async def provision_drill_operator(app: OutpostApp) -> str:
    """Create one random, permanent credential inside an already-isolated scratch store."""
    password = secrets.token_urlsafe(18)
    password_hash = app.web_auth.hasher.hash(password)
    now = int(time.time())
    async with app.database.transaction() as transaction:
        await transaction.write(
            "INSERT INTO web_credential(id,password_hash,must_change,created_at,changed_at) "
            "VALUES(1,?,0,?,?)",
            (password_hash, now, now),
        )
        await transaction.write(
            "INSERT INTO web_account(id,username,display_name,role,password_hash,must_change,"
            "enabled,created_at,changed_at,created_by) "
            "VALUES(1,'drill','Drill Operator','administrator',?,0,1,?,?,"
            "'replay-harness')",
            (password_hash, now, now),
        )
    return password
