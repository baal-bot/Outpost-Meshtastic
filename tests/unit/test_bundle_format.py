"""Untrusted file parser: deterministic bounded rejection before local mutations."""

import base64
import copy
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from outpost.fed import bundle_format as fmt

# The real file-transport parser, not a mock codec, is also a production ingress gate.
pytestmark = pytest.mark.production_wiring

PRIVATE = bytes(range(32))  # Synthetic fixture only, never an appliance key.
KEY = Ed25519PrivateKey.from_private_bytes(PRIVATE)
PUBLIC = KEY.public_key().public_bytes_raw()


def core():
    uid = "!00000001:synthetic"
    payload = {key: None for key in fmt.FIELDS["incidents"]}
    payload.update(
        uid=uid,
        type="hazard",
        severity="caution",
        status="open",
        title="Synthetic path",
        created_at=0,
        updated_at=0,
        origin_uids=[uid],
    )
    return {
        "origin": "!00000001",
        "destination": "!00000002",
        "created_at": 0,
        "scope": fmt.scope(),
        "items": [
            {
                "stream": "incidents",
                "uid": uid,
                "epoch": "a" * 32,
                "revision": 1,
                "payload": payload,
            }
        ],
    }


def unvalidated(core_raw):
    return fmt.canonical(
        {
            "format": fmt.FORMAT,
            "version": 1,
            "core": base64.b64encode(core_raw).decode(),
            "public_key": PUBLIC.hex(),
            "signature": KEY.sign(fmt.CONTEXT + core_raw).hex(),
        }
    )


def test_own_encode_exact_values_and_semantic_identity():
    value = core()
    value["items"][0]["payload"].update(
        body="<img src=x onerror=evil()> Ω\n", lat=40.01234567, lon=-79.01234567, radius_m=1.25
    )
    value["scope"] = fmt.scope(precise_locations=True)
    raw = fmt.encode(value, PRIVATE, PUBLIC)
    decoded = fmt.decode(raw)
    assert decoded.core == value and decoded.public_key == PUBLIC
    assert fmt.decode(json.dumps(json.loads(raw), indent=4).encode()).digest == decoded.digest
    assert decoded.fingerprint == fmt.digest(PUBLIC)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"x",
        b"[]",
        b"null",
        b"\xff",
        b"{}" * 100000,
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":-Infinity}',
        b'{"a":' * 20 + b"0" + b"}" * 20,
        b'{"a":' * 1500 + b"0" + b"}" * 1500,
    ],
)
def test_invalid_outer_json_is_bounded(raw):
    with pytest.raises(ValueError):
        fmt.decode(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("format", "other"),
        ("version", 2),
        ("version", True),
        ("public_key", "0" * 63),
        ("public_key", "z" * 64),
        ("public_key", 0),
        ("signature", "0" * 128),
        ("signature", "g" * 128),
        ("signature", []),
        ("core", "@@@@"),
        ("core", 0),
        ("extra", "unsupported"),
    ],
)
def test_invalid_signature_envelope(field, value):
    outer = json.loads(fmt.encode(core(), PRIVATE, PUBLIC))
    outer[field] = value
    with pytest.raises(ValueError):
        fmt.decode(fmt.canonical(outer))


@pytest.mark.parametrize(
    "path,value",
    [
        (("origin",), "!bad"),
        (("destination",), "!00000001"),
        (("destination",), []),
        (("created_at",), -1),
        (("created_at",), True),
        (("created_at",), 2**63),
        (("scope",), []),
        (("scope", "public_records_only"), False),
        (("scope", "public_labels"), 1),
        (("scope", "precise_locations"), "true"),
        (("scope", "private_mail"), True),
        (("items",), []),
        (("items",), {}),
        (("items", 0), []),
        (("items", 0, "stream"), "private_mail"),
        (("items", 0, "stream"), []),
        (("items", 0, "stream"), "x" * 81),
        (("items", 0, "uid"), "!00000002:foreign"),
        (("items", 0, "uid"), []),
        (("items", 0, "uid"), "!00000001:"),
        (("items", 0, "epoch"), "unknown"),
        (("items", 0, "revision"), True),
        (("items", 0, "revision"), 0),
        (("items", 0, "payload", "uid"), "other"),
        (("items", 0, "payload", "private_mail"), "excluded"),
        (("items", 0, "payload", "body"), []),
        (("items", 0, "payload", "body"), "x" * 12001),
        (("items", 0, "payload", "author_label"), "secret"),
        (("items", 0, "payload", "reporter_label"), "not approved"),
        (("items", 0, "payload", "lat"), "40"),
        (("items", 0, "payload", "lat"), True),
        (("items", 0, "payload", "lat"), 40),
        (("items", 0, "payload", "created_at"), None),
        (("items", 0, "payload", "created_at"), []),
        (("items", 0, "payload", "type"), "unknown"),
        (("items", 0, "payload", "origin_uids"), ["x"] * 65),
        (("items", 0, "payload", "origin_uids"), [False]),
    ],
)
def test_signed_malformed_content_is_not_trusted_as_a_supported_record(path, value):
    document = core()
    cursor = document
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    with pytest.raises(ValueError):
        fmt.decode(unvalidated(fmt.canonical(document)))


def test_duplicate_records_fields_core_size_and_nesting():
    document = core()
    document["items"] *= 2
    with pytest.raises(ValueError, match="Duplicate bundle record"):
        fmt.decode(unvalidated(fmt.canonical(document)))
    document["items"] *= 5
    with pytest.raises(ValueError, match="1..8"):
        fmt.decode(unvalidated(fmt.canonical(document)))
    for raw in (
        b"[]",
        b'{"a":1,"a":2}',
        b'{"a":' * 12 + b"0" + b"}" * 12,
        b" " * (fmt.MAX_CORE_BYTES + 1),
    ):
        with pytest.raises(ValueError):
            fmt.decode(unvalidated(raw))
    huge = copy.deepcopy(core())
    huge["items"][0]["payload"]["body"] = "a" * fmt.MAX_CORE_BYTES
    with pytest.raises(ValueError, match="too large"):
        fmt.encode(huge, PRIVATE, PUBLIC)
    with pytest.raises(ValueError):
        fmt.encode(core(), PRIVATE, b"\x00" * 32)
    with pytest.raises(ValueError):
        fmt.scope(public_labels="yes")
