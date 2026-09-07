"""Operator-owned encrypted export and fresh-directory offline recovery.

No server, radio, shell, migration, external URL, or full environment capture is
needed for export/verification/restore. Only `serve` starts the fenced workbench.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import warnings
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote

import yaml
from cryptography import x509
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_private_key,
)
from pydantic import ValidationError

from outpost.audit import write_recovery_audit
from outpost.config import AIProviderEndpoint, Config, load_config
from outpost.recovery_format import (
    LIMITS,
    MAX_BUNDLE,
    RecoveryError,
    canonical,
    decrypt,
    encrypt,
    pack,
    strict_json,
    unpack,
)
from outpost.store.recovery_snapshot import open_image, schema_fingerprint, snapshot

FILES = {
    "database": "outpost.db",
    "config": "effective-config.json",
    "intents": "intents.yaml",
    "tls_certificate": "tls-certificate.pem",
    "tls_private_key": "tls-private-key.pem",
    "provider_credentials": "provider-credentials.json",
    "radio_profile": "radio-profile.bin",
}


def read_regular(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
            raise RecoveryError("Input must be a bounded regular file, not a link or device")
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or len(data) != before.st_size:
            raise RecoveryError("Input changed during capture; freeze configuration and retry")
        return data


def _config(value: bytes) -> Config:
    try:
        return Config.model_validate(strict_json(value))
    except ValidationError as error:
        # Pydantic errors may include submitted credentials: never print them.
        raise RecoveryError("Recovery configuration is incompatible") from error


def _credential_name(config: Config) -> str:
    endpoint = getattr(config.ai, config.ai.provider, None)
    return endpoint.api_key_env if isinstance(endpoint, AIProviderEndpoint) else ""


def capture_material(
    config: Config, *, include_provider_key: bool = False, radio_profile: Path | None = None
) -> dict[str, bytes]:
    result = {
        "config": canonical(config.model_dump(mode="json")),
        "intents": read_regular(Path(config.router.intents_file), LIMITS["intents"]),
    }
    if config.web.transport.mode == "direct_https":
        certificate, key = (
            config.web.transport.certificate_file,
            config.web.transport.private_key_file,
        )
        assert certificate is not None and key is not None
        result["tls_certificate"] = read_regular(certificate, LIMITS["tls_certificate"])
        result["tls_private_key"] = read_regular(key, LIMITS["tls_private_key"])
    if include_provider_key:
        name = _credential_name(config)
        value = os.getenv(name) if name else None
        if not value:
            raise RecoveryError("Selected provider credential is not configured or available")
        result["provider_credentials"] = canonical({name: value})
    if radio_profile is not None:
        result["radio_profile"] = read_regular(radio_profile, LIMITS["radio_profile"])
    return result


def validate_support(components: dict[str, bytes], config: Config) -> None:
    try:
        intents = yaml.safe_load(components["intents"])
        if not isinstance(intents, list) or len(intents) > 256:
            raise RecoveryError("Recovery intents must be a bounded list")
        for entry in intents:
            if not isinstance(entry, dict) or not {"pattern", "command"} <= entry.keys():
                raise RecoveryError("Invalid recovery intent entry")
            for name in ("pattern", "command"):
                if not isinstance(entry[name], str) or not 0 < len(entry[name].strip()) <= 1024:
                    raise RecoveryError("Invalid recovery intent text")
            re.compile(entry["pattern"])
        if config.web.transport.mode == "direct_https":
            certificate = x509.load_pem_x509_certificate(components["tls_certificate"])
            private = load_pem_private_key(components["tls_private_key"], password=None)
            if certificate.public_key().public_bytes(
                Encoding.DER, PublicFormat.SubjectPublicKeyInfo
            ) != private.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo):
                raise RecoveryError("Recovery TLS key does not match its certificate")
        elif {"tls_certificate", "tls_private_key"} & components.keys():
            raise RecoveryError("Unexpected TLS material for this recovery configuration")
    except (ValueError, TypeError, yaml.YAMLError, RecursionError, re.error) as error:
        raise RecoveryError("Recovery support material is incompatible or invalid") from error


def snapshot_file(path: Path) -> bytes:
    # Reject links/devices before SQLite opens its read-only WAL-aware source.
    # The enclosing directory must be controlled by this operator, as for config.
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RecoveryError("Source database must be a regular file")
    connection = sqlite3.connect(f"file:{quote(str(path.absolute()))}?mode=ro", uri=True, timeout=5)
    try:
        return snapshot(connection)
    finally:
        connection.close()


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def publish_bundle(output: Path, value: bytes) -> None:
    if output.exists() or output.is_symlink():
        raise RecoveryError("Export destination already exists; choose a new filename")
    if shutil.disk_usage(output.parent).free < len(value) + 16 * 1024 * 1024:
        raise RecoveryError("Insufficient free space for the encrypted export")
    descriptor, temporary = tempfile.mkstemp(prefix=".outpost-encrypted-", dir=output.parent)
    partial = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link publication is atomic and NEVER replaces an existing file.
        os.link(partial, output, follow_symlinks=False)
        _sync_directory(output.parent)
    finally:
        partial.unlink(missing_ok=True)


def export(
    config: Config,
    output: Path,
    passphrase: str,
    *,
    include_provider_key: bool = False,
    radio_profile: Path | None = None,
) -> dict[str, object]:
    material = capture_material(
        config, include_provider_key=include_provider_key, radio_profile=radio_profile
    )
    validate_support(material, config)
    material["database"] = snapshot_file(Path(config.store.path))
    value = encrypt(pack(material, schema_fingerprint(), int(time.time())), passphrase)
    publish_bundle(output, value)
    # Report only non-secret transport evidence. Identity and component metadata
    # stay encrypted and are available only after intentional local decryption.
    return {"sha256": hashlib.sha256(value).hexdigest(), "encrypted_bytes": len(value)}


def verified_components(bundle: bytes, passphrase: str) -> tuple[dict[str, bytes], Config]:
    components, _manifest = unpack(decrypt(bundle, passphrase), schema_fingerprint())
    config = _config(components["config"])
    if (
        config.web.transport.mode == "direct_https"
        and not {"tls_certificate", "tls_private_key"} <= components.keys()
    ):
        raise RecoveryError("Required configured TLS material is missing")
    if "provider_credentials" in components:
        credentials = strict_json(components["provider_credentials"])
        name = _credential_name(config)
        if (
            not name
            or not isinstance(credentials, dict)
            or set(credentials) != {name}
            or not isinstance(credentials[name], str)
            or not credentials[name]
        ):
            raise RecoveryError("Recovery provider credential selection is incompatible")
    validate_support(components, config)
    memory = open_image(components["database"])
    try:
        if (
            memory.execute(
                "SELECT 1 FROM web_account WHERE enabled=1 "
                "AND role IN ('administrator','operator') "
                "AND bootstrap_expires_at IS NULL AND bootstrap_consumed_at IS NULL LIMIT 1"
            ).fetchone()
            is None
        ):
            raise RecoveryError(
                "Recovery requires an enabled named operator account from the source"
            )
    finally:
        memory.close()
    return components, config


def verify(bundle: bytes, passphrase: str) -> dict[str, object]:
    components, config = verified_components(bundle, passphrase)
    return {
        "verified": True,
        "sha256": hashlib.sha256(bundle).hexdigest(),
        "components": sorted(components),
        "database_bytes": len(components["database"]),
        "provider_credential_required_but_not_included": bool(
            _credential_name(config) and "provider_credentials" not in components
        ),
        "reactivation": "requires_identity_and_queue_review",
    }


def restore(bundle: bytes, passphrase: str, target: Path) -> dict[str, object]:
    if not target.is_absolute() or target.exists() or target.is_symlink():
        raise RecoveryError(
            "Restore requires a fresh absolute directory; existing nodes are untouched"
        )
    if target.parent.resolve(strict=True) != target.parent:
        raise RecoveryError("Restore parent must be a real directory without symlink components")
    components, config = verified_components(bundle, passphrase)
    digest = hashlib.sha256(bundle).hexdigest()
    now = int(time.time())
    memory = open_image(components["database"])
    try:
        with memory:
            memory.execute("DELETE FROM web_session")
            memory.execute("UPDATE web_account SET totp_pending_secret=NULL")
            memory.execute("DELETE FROM recovery_fence")
            memory.execute(
                "INSERT INTO recovery_fence VALUES(1,?,?,'review_required')", (digest, now)
            )
            write_recovery_audit(memory, digest=digest, created_at=now)
        components["database"] = memory.serialize()
    finally:
        memory.close()
    candidate = config.model_dump(mode="json")
    candidate["store"].update(
        {
            "path": str(target / "outpost.db"),
            "tiles_path": str(target / "tiles"),
            "releases_path": str(target / "releases"),
        }
    )
    candidate["router"]["intents_file"] = str(target / "intents.yaml")
    candidate["web"].update({"bind": "127.0.0.1", "transport": {"mode": "trusted_http"}})
    runtime = _config(canonical(candidate))
    files = {FILES[name]: value for name, value in components.items()}
    files["config.json"] = canonical(runtime.model_dump(mode="json"))
    files["RECOVERY.json"] = canonical({"bundle_digest": digest, "state": "review_required"})
    if shutil.disk_usage(target.parent).free < sum(map(len, files.values())) * 2 + 16 * 1024 * 1024:
        raise RecoveryError("Insufficient free space for a verified restore")
    target.mkdir(mode=0o700)  # Exclusive reservation; never merge with or replace anything.
    created: list[Path] = []
    try:
        for name, value in files.items():
            path = target / name
            created.append(path)
            _write(path, value)
        # Convert only this fresh recovery image to the application's WAL format.
        connection = sqlite3.connect(target / "outpost.db")
        try:
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise RecoveryError("Restore destination does not support WAL")
        finally:
            connection.close()
        descriptor = os.open(target / "outpost.db", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # The final marker certifies validation/publication, not radio readiness.
        created.append(target / "READY")
        _write(target / "READY", digest.encode())
        _sync_directory(target)
        _sync_directory(target.parent)
    except BaseException:
        # Only our fixed files in our newly reserved directory, never a recursive
        # deletion. SIGKILL leaves a 0700 incomplete target for operator inspection.
        for path in reversed(created):
            path.unlink(missing_ok=True)
        target.rmdir()
        raise
    return {"restored": True, "bundle_digest": digest, "state": "review_required"}


def workbench_config(target: Path) -> Config:
    digest = read_regular(target / "READY", 64).decode("ascii")
    metadata = strict_json(read_regular(target / "RECOVERY.json", 1024))
    if metadata != {"bundle_digest": digest, "state": "review_required"}:
        raise RecoveryError("Incomplete recovery publication")
    config = _config(read_regular(target / "config.json", LIMITS["config"]))
    if Path(config.store.path) != target / "outpost.db" or config.web.bind != "127.0.0.1":
        raise RecoveryError("Workbench configuration must target its own local restored store")
    connection = sqlite3.connect(f"file:{quote(config.store.path)}?mode=ro", uri=True)
    try:
        if connection.execute("SELECT bundle_digest FROM recovery_fence WHERE id=1").fetchall() != [
            (digest,)
        ]:
            raise RecoveryError("Restored identity fence is missing or changed")
    finally:
        connection.close()
    return config


def _passphrase(prompt: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except getpass.GetPassWarning as error:
        raise RecoveryError(
            "Cannot disable terminal echo; use a private interactive terminal"
        ) from error


class RecoveryParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # An accidental --passphrase argument must not be echoed in diagnostics.
        self.print_usage(sys.stderr)
        self.exit(
            2, "Invalid recovery arguments; use --help. Enter passphrases only at the prompt.\n"
        )


def main() -> int:
    parser = RecoveryParser(
        description="Encrypted offline recovery; never overwrite an active node"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("export")
    create.add_argument("--config", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--include-provider-key", action="store_true")
    create.add_argument("--radio-profile", type=Path)
    for name in ("verify", "restore"):
        command = commands.add_parser(name)
        command.add_argument("bundle", type=Path)
        if name == "restore":
            command.add_argument("target", type=Path)
    serve = commands.add_parser("serve")
    serve.add_argument("target", type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.command == "serve":
            from outpost.__main__ import main as run_app

            run_app(workbench_config(arguments.target))
            return 0
        if not sys.stdin.isatty():
            raise RecoveryError(
                "Use an interactive terminal; passphrases are never "
                "command-line or environment inputs"
            )
        passphrase = _passphrase("Recovery passphrase: ")
        if arguments.command == "export":
            if _passphrase("Confirm recovery passphrase: ") != passphrase:
                raise RecoveryError("Passphrase confirmation did not match")
            if not arguments.config.is_file():
                raise RecoveryError("Explicit source configuration is required")
            result = export(
                load_config(arguments.config),
                arguments.output,
                passphrase,
                include_provider_key=arguments.include_provider_key,
                radio_profile=arguments.radio_profile,
            )
        else:
            bundle = read_regular(arguments.bundle, MAX_BUNDLE)
            result = (
                verify(bundle, passphrase)
                if arguments.command == "verify"
                else restore(bundle, passphrase, arguments.target)
            )
        print(canonical(result).decode())
        return 0
    except (RecoveryError, OSError, sqlite3.Error, ValueError, MemoryError) as error:
        detail = (
            str(error)
            if isinstance(error, RecoveryError)
            else "Recovery failed; check local files, permissions and compatible runtime"
        )
        print(detail, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print(
            "Recovery interrupted; retain the encrypted original and inspect any incomplete target",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
