"""Versioned, bounded IP messages. Authentication does not grant import authority."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from outpost.fed import bundle_format
from outpost.fed.framing import wire_int

MAX_MESSAGE_BYTES = 192 * 1024
MAX_PAYLOAD_BYTES = 128 * 1024
MAX_RECORD_BYTES = 12_000
MAX_ITEMS = 8
MAX_SEQUENCE = 2**63 - 1
FORMAT = "outpost-bulk"
SALT = b"outpost-bulk-v1\x00"
AUTH_CONTEXT = b"outpost-bulk-v1-message\x00"
GENERATION_CONTEXT = b"outpost-bulk-v1-generation\x00"
REQUEST_CONTEXT = b"outpost-bulk-v1-request-id\x00"
OPERATIONS = frozenset({"manifest", "fetch", "receipt"})
TRANSIENT_STATUSES = frozenset({"busy", "storage_full", "unavailable"})
NODE = re.compile(r"![0-9a-f]{8}")


class BulkFormatError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BulkFormatError("duplicate JSON field")
        result[key] = value
    return result


def _json(raw: bytes, limit: int) -> dict[str, Any]:
    if type(raw) is not bytes or not 0 < len(raw) <= limit:
        raise BulkFormatError("bulk message exceeds byte limit")
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        pending = [(result, 0)]
        count = 0
        while pending:
            value, depth = pending.pop()
            count += 1
            if depth > 12 or count > 4096:
                raise BulkFormatError("bulk JSON structure exceeds limit")
            if isinstance(value, dict):
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
            elif isinstance(value, float) and not math.isfinite(value):
                raise BulkFormatError("non-finite bulk number")
            elif isinstance(value, str):
                value.encode("utf-8")
    except (UnicodeError, ValueError, RecursionError) as error:
        raise BulkFormatError("invalid bounded bulk JSON") from error
    if not isinstance(result, dict):
        raise BulkFormatError("bulk JSON must be an object")
    return result


def _fields(value: Any, fields: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise BulkFormatError("unsupported bulk fields")
    return value


def _hex(value: Any, length: int) -> str:
    if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
        raise BulkFormatError("invalid bulk digest or identity token")
    return value


def node(value: str) -> str:
    if not isinstance(value, str) or NODE.fullmatch(value) is None:
        raise BulkFormatError("bulk requires a lowercase mesh node ID")
    return value


def _identities(secret: bytes, source: str, destination: str) -> None:
    node(source)
    node(destination)
    if source == destination or type(secret) is not bytes or len(secret) != 32:
        raise BulkFormatError("bulk requires distinct peers and a 32-byte pairing secret")


def generation(secret: bytes, source: str, destination: str) -> str:
    _identities(secret, source, destination)
    return hmac.new(
        secret, GENERATION_CONTEXT + canonical(sorted((source, destination))), hashlib.sha256
    ).hexdigest()


def authentication_key(secret: bytes, source: str, destination: str) -> bytes:
    _identities(secret, source, destination)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=SALT,
        info=b"authentication\x00" + canonical([source, destination]),
    ).derive(secret)


def request_id(secret: bytes, source: str, destination: str, sequence: int) -> str:
    wire_int(sequence, "IP sequence", minimum=1, maximum=MAX_SEQUENCE)
    return hmac.new(
        authentication_key(secret, source, destination),
        REQUEST_CONTEXT + sequence.to_bytes(8, "big"),
        hashlib.sha256,
    ).hexdigest()[:32]


def _reference(value: Any, producer: str, extra: str = "") -> dict[str, Any]:
    item = _fields(value, "stream uid revision digest " + extra)
    stream, uid = item["stream"], item["uid"]
    if not isinstance(stream, str) or not (
        stream in {"incidents", "incident_updates"}
        or re.fullmatch(r"board:[a-z0-9][a-z0-9_-]{0,63}", stream)
    ):
        raise BulkFormatError("stream is outside bulk v1")
    if not isinstance(uid, str) or not uid.startswith(producer + ":") or not 10 < len(uid) <= 160:
        raise BulkFormatError("record must belong to its original producer")
    wire_int(item["revision"], "record revision", minimum=1)
    _hex(item["digest"], 16)
    if "epoch" in item:
        _hex(item["epoch"], 32)
    return item


def _items(value: Any, *, allow_empty: bool = False) -> list[Any]:
    if not isinstance(value, list) or not (0 if allow_empty else 1) <= len(value) <= MAX_ITEMS:
        raise BulkFormatError("bulk page must contain at most eight records")
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise BulkFormatError("invalid bulk record")
        key = (item.get("stream"), item.get("uid"))
        if not all(isinstance(part, str) for part in key) or key in seen:
            raise BulkFormatError("duplicate or invalid bulk record identity")
        seen.add(key)
    return value


def _record(item: Any, producer: str) -> None:
    if isinstance(item, dict) and "unavailable" in item:
        _reference(item, producer, "epoch unavailable")
        if item["unavailable"] is not True:
            raise BulkFormatError("invalid unavailable record")
        return
    _reference(item, producer, "epoch payload")
    public = {key: value for key, value in item.items() if key != "digest"}
    bundle_format.validate_item(
        public, producer, bundle_format.scope(public_labels=True, precise_locations=True)
    )
    if len(canonical(item)) > MAX_RECORD_BYTES:
        raise BulkFormatError("bulk record exceeds byte limit")
    if item["digest"] != digest(canonical(item["payload"]))[:16]:
        raise BulkFormatError("record digest does not match producer content")


def validate_payload(operation: str, kind: str, body: dict[str, Any], producer: str) -> None:
    if operation not in OPERATIONS or kind not in {"request", "response"}:
        raise BulkFormatError("unsupported bulk operation")
    if kind == "response":
        _fields(body, "status result")
        status, body = body["status"], body["result"]
        if status in {"busy", "denied", "storage_full", "unavailable"}:
            _fields(body, "")
            return
        if status in {"reset", "rollback"}:
            _fields(body, "epoch scope")
            _hex(body["epoch"], 32)
            _hex(body["scope"], 16)
            return
        if status != "ok":
            raise BulkFormatError("unsupported bulk response status")
    if operation == "manifest":
        if kind == "request":
            _fields(body, "cycle epoch scope after snapshot limit")
            for field, size in (("epoch", 32), ("scope", 16)):
                if body[field] is not None:
                    _hex(body[field], size)
            wire_int(body["limit"], "page limit", minimum=1, maximum=MAX_ITEMS)
        else:
            _fields(body, "cycle epoch scope after snapshot next done items")
            _hex(body["epoch"], 32)
            _hex(body["scope"], 16)
            wire_int(body["next"], "next cursor")
            if type(body["done"]) is not bool:
                raise BulkFormatError("invalid completed page flag")
            for item in _items(body["items"], allow_empty=True):
                _reference(item, producer)
            if not body["after"] <= body["next"] <= body["snapshot"]:
                raise BulkFormatError("invalid manifest cursor order")
        _hex(body["cycle"], 32)
        wire_int(body["after"], "after cursor")
        if body["snapshot"] is not None:
            wire_int(body["snapshot"], "snapshot")
            if body["snapshot"] < body["after"]:
                raise BulkFormatError("cursor exceeds snapshot")
    elif operation == "fetch":
        _fields(body, "cycle epoch scope items" if kind == "request" else "cycle items")
        _hex(body["cycle"], 32)
        if kind == "request":
            _hex(body["epoch"], 32)
            _hex(body["scope"], 16)
        for item in _items(body["items"]):
            if kind == "request":
                _reference(item, producer)
            else:
                _record(item, producer)
    else:
        _fields(body, "cycle page_digest items" if kind == "request" else "cycle page_digest")
        _hex(body["cycle"], 32)
        _hex(body["page_digest"], 64)
        if kind == "request":
            for item in _items(body["items"]):
                _reference(item, producer, "epoch state")
                if item["state"] not in {"stored", "duplicate"}:
                    raise BulkFormatError("receipt is not committed storage")


@dataclass(frozen=True)
class BulkMessage:
    raw: bytes
    kind: str
    operation: str
    source: str
    destination: str
    generation: str
    sequence: int
    request_id: str
    request_digest: str | None
    payload_bytes: bytes

    @property
    def payload(self) -> dict[str, Any]:
        # Callers cannot mutate the authenticated representation through this view.
        return _json(self.payload_bytes, MAX_PAYLOAD_BYTES)


def _decode(raw: bytes, secret: bytes, source: str, destination: str) -> BulkMessage:
    outer = _json(raw, MAX_MESSAGE_BYTES)
    _fields(
        outer,
        "format version kind operation source destination generation sequence "
        "request_id request_digest payload_digest payload mac",
    )
    if outer["format"] != FORMAT or type(outer["version"]) is not int or outer["version"] != 1:
        raise BulkFormatError("unsupported bulk format/version")
    if outer["source"] != source or outer["destination"] != destination:
        raise BulkFormatError("wrong bulk peer or direction")
    if outer["generation"] != generation(secret, source, destination):
        raise BulkFormatError("wrong pairing generation")
    wire_int(outer["sequence"], "IP sequence", minimum=1, maximum=MAX_SEQUENCE)
    _hex(outer["request_id"], 32)
    _hex(outer["payload_digest"], 64)
    _hex(outer["mac"], 64)
    if outer["request_digest"] is not None:
        _hex(outer["request_digest"], 64)
    if canonical(outer) != raw:
        raise BulkFormatError("noncanonical bulk envelope")
    signed = {key: value for key, value in outer.items() if key != "mac"}
    expected = hmac.new(
        authentication_key(secret, source, destination),
        AUTH_CONTEXT + canonical(signed),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(outer["mac"], expected):
        raise BulkFormatError("bulk authentication failed")
    try:
        if not isinstance(outer["payload"], str):
            raise ValueError("payload must be base64")
        payload = base64.b64decode(outer["payload"], validate=True)
        if base64.b64encode(payload).decode() != outer["payload"]:
            raise ValueError("noncanonical base64")
    except ValueError as error:
        raise BulkFormatError("invalid bulk payload encoding") from error
    if digest(payload) != outer["payload_digest"]:
        raise BulkFormatError("bulk payload digest mismatch")
    body = _json(payload, MAX_PAYLOAD_BYTES)
    producer = destination if outer["kind"] == "request" else source
    try:
        validate_payload(outer["operation"], outer["kind"], body, producer)
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        raise BulkFormatError("invalid bulk operation payload") from error
    return BulkMessage(
        raw,
        outer["kind"],
        outer["operation"],
        source,
        destination,
        outer["generation"],
        outer["sequence"],
        outer["request_id"],
        outer["request_digest"],
        payload,
    )


def decode_request(raw: bytes, secret: bytes, source: str, destination: str) -> BulkMessage:
    message = _decode(raw, secret, source, destination)
    if (
        message.kind != "request"
        or message.request_digest is not None
        or message.request_id != request_id(secret, source, destination, message.sequence)
    ):
        raise BulkFormatError("invalid bulk request binding")
    return message


def decode_response(raw: bytes, secret: bytes, request: BulkMessage) -> BulkMessage:
    request = decode_request(request.raw, secret, request.source, request.destination)
    message = _decode(raw, secret, request.destination, request.source)
    if message.kind != "response" or (
        message.operation,
        message.sequence,
        message.request_id,
        message.request_digest,
    ) != (request.operation, request.sequence, request.request_id, digest(request.raw)):
        raise BulkFormatError("response does not bind the exact request")
    result = message.payload
    if result["status"] == "ok":
        result, wanted = result["result"], request.payload
        if result["cycle"] != wanted["cycle"]:
            raise BulkFormatError("response cycle does not match request")
        if request.operation == "receipt" and result["page_digest"] != wanted["page_digest"]:
            raise BulkFormatError("response does not bind the stored page")
        if request.operation == "fetch":
            references = {
                (item["stream"], item["uid"], item["revision"], item["digest"])
                for item in wanted["items"]
            }
            actual = {
                (item["stream"], item["uid"], item["revision"], item["digest"])
                for item in result["items"]
            }
            if actual != references or any(
                item["epoch"] != wanted["epoch"] for item in result["items"]
            ):
                raise BulkFormatError("response does not match requested revisions")
        if request.operation == "manifest":
            if result["after"] != wanted["after"] or len(result["items"]) > wanted["limit"]:
                raise BulkFormatError("response does not match requested page")
            for field in ("epoch", "scope", "snapshot"):
                if wanted[field] is not None and result[field] != wanted[field]:
                    raise BulkFormatError("response changed the requested snapshot or scope")
            revisions = [item["revision"] for item in result["items"]]
            if revisions != sorted(set(revisions)) or any(
                not result["after"] < revision <= result["next"] for revision in revisions
            ):
                raise BulkFormatError("response revisions are outside the requested page")
            if (result["done"] and result["next"] != result["snapshot"]) or (
                not result["done"] and result["next"] <= result["after"]
            ):
                raise BulkFormatError("response cannot advance this page")
    return message


def _encode(
    secret: bytes,
    source: str,
    destination: str,
    sequence: int,
    operation: str,
    payload: dict[str, Any],
    request: BulkMessage | None = None,
) -> bytes:
    encoded = canonical(payload)
    _json(encoded, MAX_PAYLOAD_BYTES)
    outer = {
        "format": FORMAT,
        "version": 1,
        "kind": "response" if request else "request",
        "operation": operation,
        "source": source,
        "destination": destination,
        "generation": generation(secret, source, destination),
        "sequence": sequence,
        "request_id": request.request_id
        if request
        else request_id(secret, source, destination, sequence),
        "request_digest": digest(request.raw) if request else None,
        "payload_digest": digest(encoded),
        "payload": base64.b64encode(encoded).decode(),
    }
    outer["mac"] = hmac.new(
        authentication_key(secret, source, destination),
        AUTH_CONTEXT + canonical(outer),
        hashlib.sha256,
    ).hexdigest()
    raw = canonical(outer)
    if request:
        decode_response(raw, secret, request)
    else:
        decode_request(raw, secret, source, destination)
    return raw


def encode_request(
    secret: bytes,
    source: str,
    destination: str,
    sequence: int,
    operation: str,
    payload: dict[str, Any],
) -> bytes:
    return _encode(secret, source, destination, sequence, operation, payload)


def encode_response(secret: bytes, request: BulkMessage, payload: dict[str, Any]) -> bytes:
    request = decode_request(request.raw, secret, request.source, request.destination)
    return _encode(
        secret,
        request.destination,
        request.source,
        request.sequence,
        request.operation,
        payload,
        request,
    )
