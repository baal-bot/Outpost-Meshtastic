"""Bounded passphrase envelope. No archive paths, compression or variable KDF cost."""

from __future__ import annotations

import hashlib
import json
import secrets
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from outpost import __version__

MAGIC = b"OUTPOST-RECOVERY\x00\x01"
MAX_DATABASE = 256 * 1024 * 1024
LIMITS = {
    "database": MAX_DATABASE,
    "config": 1024 * 1024,
    "intents": 1024 * 1024,
    "tls_certificate": 65536,
    "tls_private_key": 65536,
    "provider_credentials": 16384,
    "radio_profile": 2 * 1024 * 1024,
}
MAX_MANIFEST = 8192
MAX_PLAINTEXT = sum(LIMITS.values()) + MAX_MANIFEST + 4
HEADER_SIZE = len(MAGIC) + 16 + 12 + 8
MAX_BUNDLE = HEADER_SIZE + MAX_PLAINTEXT + 16


class RecoveryError(ValueError):
    """Safe, non-secret operational error suitable for the terminal."""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def strict_json(value: bytes) -> object:
    depth, quoted, escaped = 0, False, False
    if len(value) > 1024 * 1024:
        raise RecoveryError("Recovery metadata exceeds the supported bound")
    for byte in value:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > 16:
                raise RecoveryError("Recovery metadata is too deeply nested")
        elif byte in (93, 125):
            depth -= 1

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise RecoveryError("Duplicate recovery metadata field")
            result[key] = item
        return result

    def nonfinite(_value: str) -> object:
        raise RecoveryError("Invalid recovery metadata number")

    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise RecoveryError("Invalid recovery metadata") from error


def _key(passphrase: str, salt: bytes) -> bytes:
    password = passphrase.encode("utf-8")
    if not 16 <= len(password) <= 1024:
        raise RecoveryError("Passphrase must contain 16 to 1024 UTF-8 bytes")
    return Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(password)


def encrypt(plaintext: bytes, passphrase: str) -> bytes:
    if not 1 <= len(plaintext) <= MAX_PLAINTEXT:
        raise RecoveryError("Recovery content exceeds the supported bound")
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    header = MAGIC + salt + nonce + struct.pack(">Q", len(plaintext) + 16)
    return header + AESGCM(_key(passphrase, salt)).encrypt(nonce, plaintext, header)


def decrypt(bundle: bytes, passphrase: str) -> bytes:
    if not HEADER_SIZE + 16 < len(bundle) <= MAX_BUNDLE or not bundle.startswith(MAGIC):
        raise RecoveryError("Unsupported or truncated recovery bundle")
    header, ciphertext = bundle[:HEADER_SIZE], bundle[HEADER_SIZE:]
    if struct.unpack(">Q", header[-8:])[0] != len(ciphertext):
        raise RecoveryError("Recovery bundle length does not match its header")
    salt = header[len(MAGIC) : len(MAGIC) + 16]
    nonce = header[len(MAGIC) + 16 : -8]
    try:
        return AESGCM(_key(passphrase, salt)).decrypt(nonce, ciphertext, header)
    except InvalidTag as error:
        raise RecoveryError("Wrong passphrase or damaged recovery bundle") from error


def pack(components: dict[str, bytes], schema: str, created_at: int) -> bytes:
    if not {"database", "config", "intents"} <= components.keys() <= LIMITS.keys():
        raise RecoveryError("Unsupported recovery component selection")
    entries = []
    for name, value in sorted(components.items()):
        if not 0 < len(value) <= LIMITS[name]:
            raise RecoveryError("Recovery component exceeds the supported bound")
        entries.append(
            {"name": name, "size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
        )
    manifest = canonical(
        {"runtime": __version__, "schema": schema, "created_at": created_at, "components": entries}
    )
    if len(manifest) > MAX_MANIFEST:
        raise RecoveryError("Recovery manifest exceeds the supported bound")
    return (
        struct.pack(">I", len(manifest))
        + manifest
        + b"".join(components[str(entry["name"])] for entry in entries)
    )


def unpack(plaintext: bytes, schema: str) -> tuple[dict[str, bytes], dict[str, object]]:
    if not 5 <= len(plaintext) <= MAX_PLAINTEXT:
        raise RecoveryError("Invalid recovery content length")
    size = struct.unpack(">I", plaintext[:4])[0]
    if not 1 <= size <= MAX_MANIFEST or size + 4 >= len(plaintext):
        raise RecoveryError("Invalid recovery manifest length")
    manifest = strict_json(plaintext[4 : size + 4])
    if not isinstance(manifest, dict) or set(manifest) != {
        "runtime",
        "schema",
        "created_at",
        "components",
    }:
        raise RecoveryError("Invalid recovery manifest fields")
    if manifest["runtime"] != __version__ or manifest["schema"] != schema:
        raise RecoveryError("Bundle requires its original compatible Outpost runtime and schema")
    if type(manifest["created_at"]) is not int or not 0 <= manifest["created_at"] <= 253402300799:
        raise RecoveryError("Invalid recovery creation time")
    entries = manifest["components"]
    if not isinstance(entries, list) or not 3 <= len(entries) <= len(LIMITS):
        raise RecoveryError("Invalid recovery component count")
    offset = size + 4
    components = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "size", "sha256"}:
            raise RecoveryError("Invalid recovery component descriptor")
        name, length = entry["name"], entry["size"]
        if (
            not isinstance(name, str)
            or name not in LIMITS
            or name in components
            or type(length) is not int
            or not 0 < length <= LIMITS[name]
            or offset + length > len(plaintext)
        ):
            raise RecoveryError("Invalid recovery component name or length")
        value = plaintext[offset : offset + length]
        if hashlib.sha256(value).hexdigest() != entry["sha256"]:
            raise RecoveryError("Recovery component checksum failed")
        components[name] = value
        offset += length
    if offset != len(plaintext) or not {"database", "config", "intents"} <= components.keys():
        raise RecoveryError("Missing component or trailing recovery content")
    return components, manifest
