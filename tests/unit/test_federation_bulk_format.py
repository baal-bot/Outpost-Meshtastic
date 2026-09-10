import base64
import copy
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from outpost.fed import bulk_format as fmt

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/bulk-v1.json").read_text())
SECRET = bytes.fromhex(FIXTURE["secret_hex"])
SOURCE, DESTINATION = FIXTURE["source"], FIXTURE["destination"]


def signed(outer, *, payload=None):
    outer = copy.deepcopy(outer)
    if payload is not None:
        raw = payload if isinstance(payload, bytes) else fmt.canonical(payload)
        outer["payload"] = base64.b64encode(raw).decode()
        outer["payload_digest"] = fmt.digest(raw)
    outer.pop("mac", None)
    outer["mac"] = hmac.new(
        fmt.authentication_key(SECRET, outer["source"], outer["destination"]),
        fmt.AUTH_CONTEXT + fmt.canonical(outer),
        hashlib.sha256,
    ).hexdigest()
    return fmt.canonical(outer)


@pytest.mark.parametrize("vector", FIXTURE["vectors"], ids=lambda vector: vector["operation"])
def test_independent_wire_vectors_and_immutable_payload_view(vector):
    assert fmt.generation(SECRET, SOURCE, DESTINATION) == FIXTURE["generation"]
    assert fmt.generation(SECRET, DESTINATION, SOURCE) == FIXTURE["generation"]
    assert fmt.authentication_key(SECRET, SOURCE, DESTINATION).hex() == FIXTURE["request_key_hex"]
    assert fmt.authentication_key(SECRET, DESTINATION, SOURCE).hex() == FIXTURE["response_key_hex"]
    raw = vector["request_utf8"].encode()
    assert (
        fmt.encode_request(
            SECRET,
            SOURCE,
            DESTINATION,
            vector["sequence"],
            vector["operation"],
            vector["request_payload"],
        )
        == raw
    )
    message = fmt.decode_request(raw, SECRET, SOURCE, DESTINATION)
    assert message.request_id == vector["request_id"]
    message.payload.clear()
    assert message.payload == vector["request_payload"]
    response = vector["response_utf8"].encode()
    assert fmt.encode_response(SECRET, message, vector["response_payload"]) == response
    assert fmt.decode_response(response, SECRET, message).payload == vector["response_payload"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("format", "outpost-physical-transfer"),
        ("version", True),
        ("version", 2),
        ("source", "!00000003"),
        ("destination", SOURCE),
        ("generation", "0" * 64),
        ("sequence", True),
        ("sequence", 0),
        ("sequence", 2**63),
        ("request_id", "0" * 32),
        ("request_digest", "0" * 64),
        ("operation", "radio"),
        ("kind", "response"),
        ("payload_digest", "0" * 64),
        ("payload", "!"),
        ("mac", "0" * 64),
        ("extra", "ignored?"),
    ],
)
def test_every_envelope_binding_is_enforced(field, value):
    outer = json.loads(FIXTURE["vectors"][0]["request_utf8"])
    outer[field] = value
    with pytest.raises(ValueError):
        fmt.decode_request(fmt.canonical(outer), SECRET, SOURCE, DESTINATION)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"cycle":1,"cycle":2}',
        b"[]",
        b"\xff",
        b'{"value":NaN}',
        b'{"value":1e999}',
        b'{"value":"\\ud800"}',
        b'{"x":' + b"[" * 1000 + b"0" + b"]" * 1000 + b"}",
        b'{"x":[' + b"0," * 5000 + b"0]}",
        b" " * (fmt.MAX_PAYLOAD_BYTES + 1),
    ],
)
def test_authenticated_payload_is_still_bounded_and_strict(payload):
    outer = json.loads(FIXTURE["vectors"][0]["request_utf8"])
    with pytest.raises(ValueError):
        fmt.decode_request(signed(outer, payload=payload), SECRET, SOURCE, DESTINATION)


@pytest.mark.parametrize("raw", [b"", b"\xff", b"{}", b"[]", b" " * (fmt.MAX_MESSAGE_BYTES + 1)])
def test_invalid_or_oversized_envelope(raw):
    with pytest.raises(ValueError):
        fmt.decode_request(raw, SECRET, SOURCE, DESTINATION)


def test_noncanonical_duplicate_and_trailing_envelopes_are_rejected():
    raw = FIXTURE["vectors"][0]["request_utf8"].encode()
    for invalid in (
        b" " + raw,
        raw + b"{}",
        b'{"version":1,' + raw[1:],
        raw.decode().encode("utf-16"),
    ):
        with pytest.raises(ValueError):
            fmt.decode_request(invalid, SECRET, SOURCE, DESTINATION)
    with pytest.raises(ValueError, match="generation"):
        fmt.decode_request(raw, b"z" * 32, SOURCE, DESTINATION)
    with pytest.raises(ValueError, match="peer"):
        fmt.decode_request(raw, SECRET, DESTINATION, SOURCE)


def test_resigned_request_id_and_operation_cannot_change_their_meaning():
    outer = json.loads(FIXTURE["vectors"][0]["request_utf8"])
    outer["sequence"] = 2
    with pytest.raises(ValueError, match="binding"):
        fmt.decode_request(signed(outer), SECRET, SOURCE, DESTINATION)
    outer["operation"] = "receipt"
    with pytest.raises(ValueError):
        fmt.decode_request(signed(outer), SECRET, SOURCE, DESTINATION)


@pytest.mark.parametrize(
    "stream", ["alerts", "mail", "members", "maps", "board:", "board:../private"]
)
def test_private_or_unsupported_streams_never_enter_bulk(stream):
    vector = FIXTURE["vectors"][1]
    body = copy.deepcopy(vector["request_payload"])
    body["items"][0]["stream"] = stream
    with pytest.raises(ValueError):
        fmt.encode_request(SECRET, SOURCE, DESTINATION, 1, "fetch", body)


def test_records_keep_producer_digest_and_page_limits():
    vector = FIXTURE["vectors"][1]
    request = fmt.decode_request(vector["request_utf8"].encode(), SECRET, SOURCE, DESTINATION)
    for change in ("digest", "producer", "oversize", "private_field", "duplicate", "count"):
        response = copy.deepcopy(vector["response_payload"])
        record = response["result"]["items"][0]
        if change == "digest":
            record["payload"]["body"] = "modified"
        elif change == "producer":
            record["uid"] = "!00000003:post-1"
        elif change == "oversize":
            record["payload"]["body"] = "x" * 12_000
            record["digest"] = fmt.digest(fmt.canonical(record["payload"]))[:16]
        elif change == "private_field":
            record["payload"]["secret"] = "private"  # noqa: S105 -- synthetic rejected data
        elif change == "duplicate":
            response["result"]["items"].append(copy.deepcopy(record))
        else:
            response["result"]["items"] *= 9
        with pytest.raises(ValueError):
            fmt.encode_response(SECRET, request, response)


@pytest.mark.parametrize(
    "status", ["busy", "denied", "storage_full", "unavailable", "reset", "rollback"]
)
def test_fixed_failure_results_and_exact_request_binding(status):
    vector = FIXTURE["vectors"][0]
    request = fmt.decode_request(vector["request_utf8"].encode(), SECRET, SOURCE, DESTINATION)
    result = {"epoch": "b" * 32, "scope": "c" * 16} if status in {"reset", "rollback"} else {}
    raw = fmt.encode_response(SECRET, request, {"status": status, "result": result})
    assert fmt.decode_response(raw, SECRET, request).payload["status"] == status
    other = fmt.decode_request(
        fmt.encode_request(SECRET, SOURCE, DESTINATION, 2, "manifest", request.payload),
        SECRET,
        SOURCE,
        DESTINATION,
    )
    with pytest.raises(ValueError, match="exact request"):
        fmt.decode_response(raw, SECRET, other)
    outer = json.loads(raw)
    outer["operation"] = "fetch"
    with pytest.raises(ValueError, match="exact request"):
        fmt.decode_response(signed(outer), SECRET, request)


@pytest.mark.parametrize(
    "field,value",
    [("cycle", "d" * 32), ("after", 1), ("snapshot", 0), ("next", 0), ("done", False)],
)
def test_manifest_response_cannot_skip_or_rebind_a_page(field, value):
    vector = FIXTURE["vectors"][0]
    request = fmt.decode_request(vector["request_utf8"].encode(), SECRET, SOURCE, DESTINATION)
    body = copy.deepcopy(vector["response_payload"])
    body["result"][field] = value
    # done=False with next=1 and snapshot=1 is legal: no unsupported completion claim.
    if field == "done":
        body["result"]["next"] = 0
    with pytest.raises(ValueError):
        fmt.encode_response(SECRET, request, body)


def test_response_records_and_receipts_bind_exact_requested_revisions():
    for vector in FIXTURE["vectors"][1:]:
        request = fmt.decode_request(vector["request_utf8"].encode(), SECRET, SOURCE, DESTINATION)
        body = copy.deepcopy(vector["response_payload"])
        if vector["operation"] == "fetch":
            body["result"]["items"][0]["revision"] += 1
        else:
            body["result"]["page_digest"] = "e" * 64
        with pytest.raises(ValueError):
            fmt.encode_response(SECRET, request, body)


@pytest.mark.parametrize("stream", ["incidents", "incident_updates"])
def test_supported_incident_and_plain_note_records_preserve_values(stream):
    uid = DESTINATION + ":record-1"
    if stream == "incidents":
        payload = {
            "uid": uid,
            "type": "other",
            "severity": "info",
            "status": "open",
            "title": "Synthetic incident",
            "body": "Synthetic text",
            "lat": 12.5,
            "lon": 34.25,
            "location_text": None,
            "radius_m": 100,
            "reporter_label": "Synthetic",
            "origin_node": DESTINATION,
            "created_at": 1,
            "updated_at": 1,
            "expires_at": None,
            "resolved_at": None,
            "resolution_note": None,
            "origin_uids": [],
        }
    else:
        payload = {
            "uid": uid,
            "incident_uid": DESTINATION + ":parent-1",
            "kind": "update",
            "body": "Synthetic update",
            "author_label": "Synthetic",
            "created_at": 1,
        }
    reference = {
        "stream": stream,
        "uid": uid,
        "revision": 1,
        "digest": fmt.digest(fmt.canonical(payload))[:16],
    }
    body = {"cycle": "a" * 32, "epoch": "b" * 32, "scope": "c" * 16, "items": [reference]}
    request = fmt.decode_request(
        fmt.encode_request(SECRET, SOURCE, DESTINATION, 1, "fetch", body),
        SECRET,
        SOURCE,
        DESTINATION,
    )
    result = {
        "status": "ok",
        "result": {
            "cycle": "a" * 32,
            "items": [{**reference, "epoch": "b" * 32, "payload": payload}],
        },
    }
    raw = fmt.encode_response(SECRET, request, result)
    assert fmt.decode_response(raw, SECRET, request).payload == result
    # Unavailable content is explicit and never masquerades as an empty stored payload.
    result["result"]["items"] = [{**reference, "epoch": "b" * 32, "unavailable": True}]
    assert (
        fmt.decode_response(fmt.encode_response(SECRET, request, result), SECRET, request).payload
        == result
    )
