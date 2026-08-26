from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import zipfile
from collections.abc import Iterable
from pathlib import Path

from outpost import __version__
from outpost.config import Config, load_config

REDACTED = "[REDACTED]"
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:password(?:_hash)?|csrf(?:_token)?|api[_-]?key|secret|setup[_-]?token)"
    r"\b[\"']?\s*[:=]\s*[\"']?)([^\s,\"'}]+)"
)
SESSION_COOKIE = re.compile(r"(?i)(\boutpost_session=)([^;\s]+)")
AUTHORIZATION = re.compile(r"(?i)(\bauthorization\s*[:=]\s*bearer\s+)([^\s,]+)")
LEGACY_INITIAL_PASSWORD = re.compile(r"(?i)(OUTPOST INITIAL OPERATOR PASSWORD:\s*)(\S+)")


def redact_text(text: str, exact_values: Iterable[str] = ()) -> str:
    redacted = text
    for value in sorted({item for item in exact_values if item}, key=len, reverse=True):
        redacted = redacted.replace(value, REDACTED)
    for pattern in (
        SENSITIVE_ASSIGNMENT,
        SESSION_COOKIE,
        AUTHORIZATION,
        LEGACY_INITIAL_PASSWORD,
    ):
        redacted = pattern.sub(rf"\1{REDACTED}", redacted)
    return redacted


def diagnostic_summary(config: Config) -> dict[str, object]:
    return {
        "product": "Outpost",
        "version": __version__,
        "generated_at": int(time.time()),
        "node": {
            "name": config.node.name,
            "short_name": config.node.short_name,
            "timezone": config.node.timezone,
            "locale": config.node.locale,
            "units": config.node.units,
            "location_configured": config.node.location is not None,
        },
        "radio": {"transport": config.radio.transport},
        "modules": config.modules.model_dump(),
    }


def database_secrets(database_path: Path) -> set[str]:
    if not database_path.is_file():
        return set()
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        values: set[str] = set()
        if "web_session" in tables:
            values.update(
                str(row[0])
                for row in connection.execute("SELECT csrf_token FROM web_session")
                if row[0]
            )
        if "web_credential" in tables:
            values.update(
                str(row[0])
                for row in connection.execute("SELECT password_hash FROM web_credential")
                if row[0]
            )
        return values
    finally:
        connection.close()


def build_bundle(
    output: Path,
    config: Config,
    journal: str,
    *,
    exact_values: Iterable[str] = (),
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    summary = diagnostic_summary(config)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(summary, indent=2, sort_keys=True) + "\n")
            archive.writestr("journal.log", redact_text(journal, exact_values))
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def _journal() -> str:
    command = shutil.which("journalctl")
    if command is None:
        return "journalctl unavailable\n"
    result = subprocess.run(  # noqa: S603
        [command, "-u", "outpost", "-n", "500", "--no-pager", "-o", "short-iso"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a credential-redacted support bundle.")
    parser.add_argument("--config", type=Path, default=Path("/etc/outpost/config.yaml"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(f"outpost-diagnostics-{int(time.time())}.zip"),
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    config = load_config(args.config)
    database_path = Path(config.store.path)
    exact_values = database_secrets(database_path)
    setup_path = database_path.parent / "setup-token"
    try:
        exact_values.add(setup_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, PermissionError):
        pass
    output = build_bundle(args.output, config, _journal(), exact_values=exact_values)
    print(f"Wrote redacted diagnostics: {output}")


if __name__ == "__main__":
    main()
