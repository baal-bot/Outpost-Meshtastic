"""Static boot-schema evidence, never an alternate interpreter or deployment probe."""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import shutil
import subprocess
import time
from pathlib import Path

CURRENT = Path("/opt/outpost/current")
PACKAGE = Path(__file__).parent
UNIT_PROPERTIES = (
    "LoadState",
    "UnitFileState",
    "ActiveState",
    "MainPID",
    "NeedDaemonReload",
    "ExecStart",
    "Environment",
    "EnvironmentFiles",
    "PassEnvironment",
    "UnsetEnvironment",
    "DropInPaths",
)
PROBE_SECONDS = 3.0
MAX_OUTPUT = 32_768
MAX_ENTRIES = 1_024


class Unavailable(Exception):
    """Only fixed reason codes, never paths, environment, or subprocess output."""


def _unit_properties() -> dict[str, str]:
    command = shutil.which("systemctl")
    if command is None:
        raise Unavailable("unit_unavailable")
    # Drain a bounded pipe, with a deadline, rather than buffering arbitrary unit
    # strings (which can contain secrets) or allowing a stalled bus to hold startup.
    with subprocess.Popen(  # noqa: S603 - fixed read-only command, never unit-provided arguments.
        [command, "show", "outpost.service", "--all", "--property=" + ",".join(UNIT_PROPERTIES)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        assert process.stdout is not None
        output = bytearray()
        deadline = time.monotonic() + PROBE_SECONDS
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise Unavailable("unit_timeout")
                    chunk = os.read(
                        process.stdout.fileno(), min(4096, MAX_OUTPUT + 1 - len(output))
                    )
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > MAX_OUTPUT:
                        raise Unavailable("unit_output_limit")
            if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
                raise Unavailable("unit_unavailable")
        except subprocess.TimeoutExpired:
            raise Unavailable("unit_timeout") from None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    properties: dict[str, str] = {}
    for line in output.decode("utf-8", errors="strict").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in UNIT_PROPERTIES or key in properties:
            raise Unavailable("unit_metadata_invalid")
        properties[key] = value
    # systemctl elides empty string-array properties even with --all on some
    # supported hosts; any nonempty override is rejected below.
    for key in ("EnvironmentFiles", "PassEnvironment", "UnsetEnvironment"):
        properties.setdefault(key, "")
    if set(properties) != set(UNIT_PROPERTIES):
        raise Unavailable("unit_metadata_invalid")
    return properties


def _entries(directory: Path) -> list[Path]:
    entries: list[Path] = []
    with os.scandir(directory) as iterator:
        for entry in iterator:
            if len(entries) >= MAX_ENTRIES:
                raise Unavailable("package_entry_limit")
            entries.append(Path(entry.path))
    return entries


def _schema_cap(package: Path) -> int:
    versions = []
    for path in _entries(package / "store" / "migrations"):
        if re.fullmatch(r"[0-9]{4}_[A-Za-z0-9_]+\.sql", path.name):
            if path.is_symlink() or not path.is_file() or not os.access(path, os.R_OK):
                raise Unavailable("package_unreadable")
            versions.append(int(path.name[:4]))
    if not versions or 0 not in versions or len(set(versions)) != len(versions):
        raise Unavailable("package_metadata_invalid")
    return max(versions)


def _boot_package(release: Path) -> Path:
    # Resolve only for its minor version: the interpreter's parent is normally
    # /usr/bin, NOT the venv whose package must be inspected.
    executable = release / "bin" / "python"
    if not os.access(executable, os.X_OK):
        raise Unavailable("boot_interpreter_unavailable")
    version = re.fullmatch(r"python3\.(12|13)", executable.resolve(strict=True).name)
    if version is None:
        raise Unavailable("boot_interpreter_ambiguous")
    packages = [
        path / "site-packages" / "outpost"
        for path in _entries(release / "lib")
        if re.fullmatch(r"python3\.(12|13)", path.name)
        and (path / "site-packages" / "outpost").is_dir()
    ]
    if len(packages) != 1 or packages[0].resolve().is_relative_to(release) is False:
        raise Unavailable("boot_package_ambiguous")
    if packages[0].parents[1].name != version.group():
        raise Unavailable("boot_interpreter_package_mismatch")
    return packages[0]


def inspect_boot_schema(database_schema: object) -> dict[str, object]:
    """Compare the selected boot package with *this* database, without opening it.

    ``inspector_schema_cap`` describes source on disk, not a remotely queried
    server or proof of loaded bytecode. Location IDs are opaque path hashes, not
    artifact checksums. Even a compatible result cannot certify a successful boot.
    """
    evidence: dict[str, object] = {
        "state": "unknown",
        "reason": "not_inspected",
        "database_schema": database_schema if type(database_schema) is int else None,
        "inspector_schema_cap": None,
        "boot_schema_cap": None,
        "source_relation": "unknown",
    }

    def result(state: str, reason: str) -> dict[str, object]:
        return {**evidence, "state": state, "reason": reason}

    try:
        evidence["inspector_schema_cap"] = _schema_cap(PACKAGE)
        properties = _unit_properties()
        if properties["LoadState"] != "loaded":
            return result("unknown", "unit_not_loaded")
        if properties["UnitFileState"] != "enabled":
            return result("failed", "unit_not_enabled")
        if properties["NeedDaemonReload"] != "no":
            return result("unknown", "unit_reload_pending")
        executable = str(CURRENT / "bin" / "python")
        launch = re.escape(f"{{ path={executable} ; argv[]={executable} -m outpost ; ")
        if not re.fullmatch(launch + r"ignore_errors=no ; [^{}]*\}", properties["ExecStart"]):
            return result("unknown", "unsupported_launch")
        if (
            properties["Environment"] != "OUTPOST_CONFIG=/etc/outpost/config.yaml"
            or properties["EnvironmentFiles"]
            or properties["PassEnvironment"]
            or properties["UnsetEnvironment"]
            or properties["DropInPaths"]
        ):
            return result("unknown", "custom_unit_environment")
        if not CURRENT.is_symlink():
            return result("unknown", "boot_selection_unavailable")
        release = CURRENT.resolve(strict=True)
        package = _boot_package(release)
        cap = _schema_cap(package)
        evidence.update(
            boot_schema_cap=cap,
            boot_location_id=hashlib.sha256(str(release).encode()).hexdigest()[:16],
            source_relation="same_location"
            if PACKAGE.resolve() == package.resolve()
            else "different_location",
        )
        # A switched symlink or loaded unit invalidates this observation. Do not
        # combine old package capacity with a new selection and call it ready.
        if CURRENT.resolve(strict=True) != release or _unit_properties() != properties:
            return result("unknown", "selection_changed")
        if type(database_schema) is not int or not 0 <= database_schema <= 9999:
            return result("unknown", "database_schema_unavailable")
        if database_schema > cap:
            return result("incompatible", "database_newer_than_boot")
        if properties["ActiveState"] == "failed":
            return result("failed", "selected_service_failed")
        # Type=notify remains activating until this process finishes startup,
        # including its self-check. This is a static schema pass, not READY=1.
        own_startup = (
            properties["ActiveState"] == "activating"
            and properties["MainPID"] == str(os.getpid())
            and evidence["source_relation"] == "same_location"
        )
        if properties["ActiveState"] != "active" and not own_startup:
            return result("unknown", "selected_service_not_active")
        return result("compatible", "schema_capacity_sufficient")
    except Unavailable as error:
        return result("unknown", str(error))
    except (OSError, ValueError, RuntimeError):
        return result("unknown", "inspection_unavailable")


def describe_boot_schema(evidence: dict[str, object]) -> str:
    state = evidence["state"]
    reason = evidence["reason"]
    if state == "incompatible":
        return (
            f"Current database schema {evidence['database_schema']} exceeds selected boot "
            f"capacity {evidence['boot_schema_cap']}. "
            "A healthy developer process does not fix boot."
        )
    if state == "compatible":
        return (
            f"Selected boot capacity {evidence['boot_schema_cap']} supports current schema "
            f"{evidence['database_schema']}; source: {evidence['source_relation']}. "
            "Static check only, not proof of reboot recovery."
        )
    return f"Boot compatibility {state}: {reason}. Recovery readiness is not established."
