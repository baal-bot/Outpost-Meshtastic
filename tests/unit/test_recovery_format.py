"""Actual bounded cryptographic envelope and untrusted manifest parser."""

import copy
import json
import struct

import pytest

from outpost import recovery_format as fmt

pytestmark = pytest.mark.production_wiring
PASSWORD = "synthetic passphrase for recovery"  # noqa: S105 - synthetic fixture only.


def packed():
    return fmt.pack({"database": b"db", "config": b"{}", "intents": b"[]"}, "schema", 1)


def repack(change):
    raw = packed()
    size = struct.unpack(">I", raw[:4])[0]
    manifest = json.loads(raw[4 : size + 4])
    change(manifest)
    header = fmt.canonical(manifest)
    return struct.pack(">I", len(header)) + header + raw[size + 4 :]


def test_encryption_randomizes_and_authenticates_every_header_field():
    plain = packed()
    first = fmt.encrypt(plain, PASSWORD)
    second = fmt.encrypt(plain, PASSWORD)
    assert first != second
    assert fmt.decrypt(first, PASSWORD) == plain
    assert plain not in first
    for offset in (len(fmt.MAGIC), len(fmt.MAGIC) + 16, fmt.HEADER_SIZE, len(first) - 1):
        bad = bytearray(first)
        bad[offset] ^= 1
        with pytest.raises(fmt.RecoveryError, match="Wrong passphrase or damaged"):
            fmt.decrypt(bytes(bad), PASSWORD)
    for bad in (b"", first[:-1], first + b"x", b"other" + first[5:]):
        with pytest.raises(fmt.RecoveryError):
            fmt.decrypt(bad, PASSWORD)
    assert fmt.unpack(plain, "schema")[0] == {"database": b"db", "config": b"{}", "intents": b"[]"}


@pytest.mark.parametrize("password", ["", "short", "x" * 1025])
def test_passphrase_bounds(password):
    with pytest.raises(fmt.RecoveryError, match="Passphrase"):
        fmt.encrypt(b"data", password)


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.update(extra=True),
        lambda m: m.update(runtime="other"),
        lambda m: m.update(schema="old"),
        lambda m: m.update(created_at=True),
        lambda m: m.update(created_at=-1),
        lambda m: m.update(created_at=253402300800),
        lambda m: m.update(components={}),
        lambda m: m.update(components=[]),
        lambda m: m["components"].append(copy.deepcopy(m["components"][0])),
        lambda m: m["components"][0].update(name="../config"),
        lambda m: m["components"][0].update(name=[]),
        lambda m: m["components"][0].update(size=True),
        lambda m: m["components"][0].update(size=-1),
        lambda m: m["components"][0].update(size=2**60),
        lambda m: m["components"][0].update(sha256="wrong"),
        lambda m: m["components"][0].update(extra="value"),
        lambda m: m["components"].__setitem__(0, []),
    ],
)
def test_rejects_malicious_manifest_before_extraction(change):
    with pytest.raises(fmt.RecoveryError):
        fmt.unpack(repack(change), "schema")


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"abc",
        b"\x00\x00\x00\x00x",
        b"\x7f\xff\xff\xffdata",
        struct.pack(">I", 2) + b"[]x",
        packed() + b"extra",
        packed()[:-1],
        struct.pack(">I", 20) + b'{"a":1,"a":2}' + b"x" * 20,
    ],
)
def test_invalid_lengths_and_structure(raw):
    with pytest.raises(fmt.RecoveryError):
        fmt.unpack(raw, "schema")


@pytest.mark.parametrize(
    "raw", [b"\xff", b"{", b'{"x":1,"x":2}', b'{"x":NaN}', b"[" * 2000 + b"]" * 2000]
)
def test_strict_metadata(raw):
    with pytest.raises(fmt.RecoveryError):
        fmt.strict_json(raw)


def test_component_and_ciphertext_bounds(monkeypatch):
    with pytest.raises(fmt.RecoveryError):
        fmt.pack({"database": b"db"}, "schema", 1)
    with pytest.raises(fmt.RecoveryError):
        fmt.pack({"database": b"", "config": b"{}", "intents": b"[]"}, "schema", 1)
    with pytest.raises(fmt.RecoveryError):
        fmt.encrypt(b"", PASSWORD)
    monkeypatch.setattr(fmt, "MAX_PLAINTEXT", 5)
    with pytest.raises(fmt.RecoveryError):
        fmt.encrypt(b"123456", PASSWORD)
    monkeypatch.setattr(fmt, "MAX_MANIFEST", 1)
    with pytest.raises(fmt.RecoveryError):
        packed()
