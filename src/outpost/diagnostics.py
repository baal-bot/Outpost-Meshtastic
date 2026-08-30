from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sqlite3
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from outpost import __version__
from outpost.config import Config, load_config

REDACTED = "[REDACTED]"
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:password(?:_hash)?|csrf(?:_token)?|api[_-]?key|secret|setup[_-]?token)"
    r"\b[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,\"'}]+)"
)
CONTENT_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:body|payload|text|question|content)\b[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,\"'}]+)"
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
        CONTENT_ASSIGNMENT,
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
        "ai": {
            "enabled": config.modules.ai.enabled,
            "provider": config.ai.provider,
            "model": config.ai.model,
        },
        "web": {
            "bind": config.web.bind,
            "port": config.web.port,
            "transport_mode": config.web.transport.mode,
        },
        "modules": config.modules.model_dump(),
    }


def _database_status(database_path: Path) -> dict[str, object]:
    result: dict[str, object] = {
        "exists": database_path.is_file(),
        "bytes": database_path.stat().st_size if database_path.is_file() else 0,
        "schema": None,
        "quick_check": "not_available",
    }
    if not database_path.is_file():
        return result
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=2)
        try:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "schema_version" in tables:
                row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
                result["schema"] = int(row[0]) if row and row[0] is not None else 0
            check = connection.execute("PRAGMA quick_check(1)").fetchone()
            result["quick_check"] = str(check[0]) if check else "no_result"
        finally:
            connection.close()
    except sqlite3.Error:
        result["quick_check"] = "unavailable"
    return result


def _directory_bytes(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    try:
        for item in path.iterdir():
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
    except OSError:
        return total
    return total


def _storage_status(database_path: Path) -> dict[str, int | str]:
    state_directory = database_path.parent
    probe = state_directory
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return {"status": "unavailable"}
    return {
        "status": "available",
        "filesystem_total_bytes": usage.total,
        "filesystem_used_bytes": usage.used,
        "filesystem_free_bytes": usage.free,
        "database_bytes": database_path.stat().st_size if database_path.is_file() else 0,
        "backup_bytes": _directory_bytes(state_directory / "backups"),
    }


def _service_status() -> dict[str, object]:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return {"available": False}
    properties = (
        "ActiveState,SubState,Result,NRestarts,MainPID,WatchdogTimestampMonotonic,"
        "ExecMainStartTimestamp"
    )
    try:
        result = subprocess.run(  # noqa: S603
            [systemctl, "show", "outpost.service", f"--property={properties}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}
    if result.returncode != 0:
        return {"available": False}
    allowed = {
        "ActiveState",
        "SubState",
        "Result",
        "NRestarts",
        "MainPID",
        "WatchdogTimestampMonotonic",
        "ExecMainStartTimestamp",
    }
    values = {
        key: value
        for line in result.stdout.splitlines()
        for key, separator, value in (line.partition("="),)
        if separator and key in allowed
    }
    return {"available": True, **values}


def _live_request(config: Config, path: str, *, method: str = "GET") -> dict[str, Any]:
    direct_https = config.web.transport.mode == "direct_https"
    scheme = "https" if direct_https else "http"
    url = f"{scheme}://127.0.0.1:{config.web.port}{path}"
    request = urllib.request.Request(url, method=method)  # noqa: S310 - fixed HTTP(S) loopback URL.
    try:
        if direct_https:
            context = ssl._create_unverified_context()  # noqa: SLF001, S323
            opened = urllib.request.urlopen(request, timeout=3, context=context)  # noqa: S310
        else:
            opened = urllib.request.urlopen(request, timeout=3)  # noqa: S310
        with opened as response:
            content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            return {"reachable": False, "reason": "response_too_large"}
        value = json.loads(content)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return {"reachable": False, "reason": "unavailable"}
    if not isinstance(value, dict):
        return {"reachable": False, "reason": "invalid_response"}
    return {"reachable": True, **value}


def _live_status(config: Config) -> dict[str, Any]:
    return _live_request(config, "/api/v1/diagnostics/status")


def _run_live_self_check(config: Config) -> dict[str, Any]:
    return _live_request(config, "/api/v1/diagnostics/readiness", method="POST")


def runtime_evidence(config: Config) -> dict[str, object]:
    try:
        os_release = platform.freedesktop_os_release().get("PRETTY_NAME", platform.system())
    except OSError:
        os_release = platform.platform()
    database_path = Path(config.store.path)
    trigger = _run_live_self_check(config)
    live = _live_status(config)
    readiness = live.get("readiness") if live.get("reachable") is True else None
    if not isinstance(readiness, dict):
        readiness = {
            "status": "unavailable",
            "trigger_reachable": trigger.get("reachable") is True,
        }
    return {
        "platform": {
            "operating_system": os_release,
            "machine": platform.machine(),
            "python": platform.python_version(),
            "outpost": __version__,
        },
        "database": _database_status(database_path),
        "storage": _storage_status(database_path),
        "service": _service_status(),
        "live": live,
        "self_check": readiness,
    }


def database_secrets(database_path: Path) -> set[str]:
    if not database_path.is_file():
        return set()
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return set()
    values: set[str] = set()
    try:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
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
        if "web_account" in tables:
            for row in connection.execute(
                "SELECT password_hash,totp_secret,totp_pending_secret,recovery_code_hashes "
                "FROM web_account"
            ):
                values.update(str(value) for value in row if value)
                try:
                    values.update(str(value) for value in json.loads(str(row[3] or "[]")))
                except (json.JSONDecodeError, TypeError):
                    pass
    except sqlite3.Error:
        pass
    finally:
        connection.close()
    return values


def build_bundle(
    output: Path,
    config: Config,
    recent_errors: str,
    *,
    runtime: dict[str, object] | None = None,
    full_journal: str | None = None,
    exact_values: Iterable[str] = (),
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    summary = diagnostic_summary(config)
    summary["runtime"] = runtime or {"collection": "not_requested"}
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            manifest = json.dumps(summary, indent=2, sort_keys=True) + "\n"
            archive.writestr("manifest.json", redact_text(manifest, exact_values))
            archive.writestr("recent-errors.log", redact_text(recent_errors, exact_values))
            if full_journal is not None:
                archive.writestr("journal.log", redact_text(full_journal, exact_values))
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def _journal(*, lines: int, priority: str | None = None) -> str:
    command = shutil.which("journalctl")
    if command is None:
        return "journalctl unavailable\n"
    arguments = [command, "-u", "outpost", "-n", str(lines), "--no-pager", "-o", "short-iso"]
    if priority is not None:
        arguments.extend(("--priority", priority))
    try:
        result = subprocess.run(  # noqa: S603
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "journalctl failed or timed out\n"
    return result.stdout + result.stderr


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a credential-redacted support bundle.")
    parser.add_argument("--config", type=Path, default=Path("/etc/outpost/config.yaml"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(f"outpost-diagnostics-{int(time.time())}.zip"),
    )
    parser.add_argument(
        "--include-journal",
        action="store_true",
        help="include the broader 500-line service journal; review it before sharing",
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
    output = build_bundle(
        args.output,
        config,
        _journal(lines=200, priority="warning"),
        runtime=runtime_evidence(config),
        full_journal=_journal(lines=500) if args.include_journal else None,
        exact_values=exact_values,
    )
    print(f"Wrote redacted diagnostics: {output}")


if __name__ == "__main__":
    main()
