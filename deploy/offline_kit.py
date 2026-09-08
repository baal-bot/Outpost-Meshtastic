#!/usr/bin/env python3
"""Prepare, verify and install an offline runtime into a fresh, inactive directory."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tomllib
import venv
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

HEX = re.compile(r"[0-9a-f]{64}")
MAX_FILE = 512 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_FILES = 512
MANIFEST = "KIT-MANIFEST.json"
PUBLIC_FILES = (
    "README.md",
    "pyproject.toml",
    "requirements.lock",
    "config/config.example.yaml",
    "config/intents.yaml",
    "deploy/outpost.service",
    "docs/ENCRYPTED-RECOVERY.md",
    "docs/NODE-LOSS-AND-MOBILITY.md",
    "docs/INSTALLATION.md",
    "docs/ONBOARDING.md",
    "docs/WEB-TRANSPORT.md",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def regular(path: Path, limit: int = MAX_FILE) -> bytes:
    with open(
        path, "rb", opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
    ) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"Not a bounded regular file: {path.name}")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"File grew beyond its limit: {path.name}")
    return data


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_pins(data: bytes) -> dict[str, str]:
    pins = {}
    for line in data.decode().splitlines():
        line = line.partition("#")[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.+_-]*)", line)
        if not match or normalized(match[1]) in pins:
            raise ValueError("Lock must contain unique, unconditional name==version pins")
        pins[normalized(match[1])] = match[2]
    if not pins or "outpost" in pins:
        raise ValueError("Runtime lock is empty or contains the application")
    return pins


def host() -> dict[str, Any]:
    if sys.platform != "linux" or sys.implementation.name != "cpython":
        raise ValueError("Offline kits require Linux CPython")
    if sys.version_info[:2] not in {(3, 12), (3, 13)}:
        raise ValueError("Python 3.12 or 3.13 is required")
    return {
        "python": list(sys.version_info[:2]),
        "platform": sysconfig.get_platform(),
        "libc": list(platform.libc_ver()),
    }


def run(args: list[str], *, cwd: Path) -> str:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIP_", "PYTHON", "OUTPOST__"))
    }
    environment["PIP_CONFIG_FILE"] = os.devnull
    result = subprocess.run(  # noqa: S603 - argument arrays, trusted local tools, no shell
        args, cwd=cwd, env=environment, text=True, capture_output=True, check=False, timeout=300
    )
    if result.returncode:
        raise ValueError(f"Local command failed: {result.stderr[-2000:] or result.stdout[-2000:]}")
    return result.stdout.strip()


def pip_install(python: Path, kit: Path, *, dry_run: bool = False) -> None:
    args = [
        str(python),
        "-I",
        "-m",
        "pip",
        "--isolated",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "install",
        "--no-index",
        "--no-deps",
        "--only-binary=:all:",
        "--require-hashes",
        "--requirement",
        "runtime-hashed.txt",
    ]
    if dry_run:
        args.extend(["--dry-run", "--ignore-installed"])
    run(args, cwd=kit)


def inventory(directory: Path) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    total = 0
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Kit contains a symlink")
        if path.is_dir():
            continue
        if path.name == MANIFEST and path.parent == directory:
            continue
        name = path.relative_to(directory).as_posix()
        if len(files) >= MAX_FILES:
            raise ValueError("Kit file inventory limit exceeded")
        data = regular(path)
        total += len(data)
        if total > MAX_TOTAL:
            raise ValueError("Kit byte limit exceeded")
        files[name] = {"sha256": digest(data), "bytes": len(data)}
    return files


def build(source: Path, wheels: Path, evidence: Path, output: Path) -> str:
    """Assemble only explicit public inputs; never query a live Outpost."""
    source, wheels, output = source.resolve(), wheels.resolve(), output.absolute()
    source_host = host()
    git = shutil.which("git")
    if git is None:
        raise ValueError("Building a kit requires git")
    commit = run([git, "rev-parse", "HEAD"], cwd=source)
    if run([git, "status", "--porcelain", "--untracked-files=no"], cwd=source):
        raise ValueError("Source checkout has tracked changes; use the reviewed clean revision")
    ci = json.loads(regular(evidence, 65536))
    if (
        not isinstance(ci, dict)
        or ci.get("commit") != commit
        or ci.get("status") != "completed"
        or ci.get("conclusion") != "success"
        or type(ci.get("run_id")) is not int
        or ci["run_id"] <= 0
        or ci.get("url")
        != f"https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/{ci['run_id']}"
    ):
        raise ValueError("Kit requires saved successful CI evidence for the exact source commit")
    project = tomllib.loads(regular(source / "pyproject.toml").decode())["project"]
    if project["name"] != "outpost":
        raise ValueError("Source project must be Outpost")
    expected = lock_pins(regular(source / "requirements.lock"))
    expected["outpost"] = project["version"]
    payload: dict[str, bytes] = {}
    packages: dict[str, dict[str, Any]] = {}
    source_names = run([git, "ls-files", "src/outpost"], cwd=source).splitlines()
    if not source_names:
        raise ValueError("Source package is missing")
    payload_bytes = 0
    for wheel in sorted(wheels.iterdir()):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.whl", wheel.name):
            raise ValueError("Wheelhouse must contain wheels only")
        data = regular(wheel, min(MAX_FILE, MAX_TOTAL - payload_bytes))
        payload_bytes += len(data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 20000 or sum(item.file_size for item in entries) > MAX_FILE:
                raise ValueError("Wheel expanded inventory exceeds limits")
            names = archive.namelist()
            metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(set(names)) != len(names) or len(metadata) != 1:
                raise ValueError("Wheel has duplicate entries or ambiguous metadata")
            message = BytesParser().parsebytes(archive.read(metadata[0]))
            name, version = normalized(str(message.get("Name", ""))), message.get("Version")
            if name in packages or expected.get(name) != version:
                raise ValueError("Wheel identity is duplicated, unlocked or has the wrong version")
            if name == "outpost":
                package_names = {
                    name for name in names if name.startswith("outpost/") and not name.endswith("/")
                }
                if package_names != {name.removeprefix("src/") for name in source_names}:
                    raise ValueError("Outpost wheel file inventory differs from the source commit")
                for source_name in source_names:
                    if archive.read(source_name.removeprefix("src/")) != regular(
                        source / source_name
                    ):
                        raise ValueError("Outpost wheel bytes differ from the source commit")
            packages[name] = {
                "version": version,
                "wheel": wheel.name,
                "license_expression": message.get("License-Expression"),
                "declared_license": str(message.get("License", ""))[:1000],
                "license_files": [item for item in names if ".dist-info/licenses/" in item],
            }
        payload["wheels/" + wheel.name] = data
    if set(packages) != set(expected):
        raise ValueError("Wheelhouse is missing one or more locked packages")
    for name in PUBLIC_FILES:
        payload["reference/" + name] = regular(source / name)
    payload["offline_kit.py"] = regular(Path(__file__))
    payload["OFFLINE-REPLACEMENT.md"] = regular(
        Path(__file__).parents[1] / "docs/OFFLINE-REPLACEMENT.md"
    )
    payload["ci-evidence.json"] = json.dumps(ci, sort_keys=True).encode() + b"\n"
    payload["runtime-hashed.txt"] = "".join(
        f"./{name} --hash=sha256:{digest(data)}\n"
        for name, data in sorted(payload.items())
        if name.startswith("wheels/")
    ).encode()
    schema = hashlib.sha256()
    migrations = sorted(
        (source / "src/outpost/store/migrations").glob("[0-9][0-9][0-9][0-9]_*.sql")
    )
    if not migrations:
        raise ValueError("Source migrations are missing")
    for path in migrations:
        schema.update(path.name.encode() + b"\x00" + regular(path) + b"\x00")
    if len(payload) > MAX_FILES or sum(map(len, payload.values())) > MAX_TOTAL:
        raise ValueError("Kit exceeds inventory limits")
    if run([git, "rev-parse", "HEAD"], cwd=source) != commit or run(
        [git, "status", "--porcelain", "--untracked-files=no"], cwd=source
    ):
        raise ValueError("Source changed during kit preparation")
    output.mkdir(mode=0o700)
    try:
        for name, data in payload.items():
            path = output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        pip_install(Path(sys.executable), output, dry_run=True)
        manifest = {
            "format": 1,
            "scope": "inactive_runtime_only",
            "source_commit": commit,
            "host": source_host,
            "packages": packages,
            "maximum_schema": max(int(path.name[:4]) for path in migrations),
            "schema_fingerprint": schema.hexdigest(),
            "files": inventory(output),
            "external_assets": [
                "verified boot media and OS packages",
                "powered permanent LAN and client access",
                "radio firmware and client installers",
                "maps and optional models/vendor runtimes",
                "separately protected encrypted recovery and key custody",
                "operator review of distribution rights and retained license notices",
            ],
        }
        raw = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
        (output / MANIFEST).write_bytes(raw)
    except BaseException:
        shutil.rmtree(output)
        raise
    return digest(raw)


def verify(directory: Path, expected_sha256: str) -> dict[str, Any]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Kit must be a regular directory")
    if not HEX.fullmatch(expected_sha256):
        raise ValueError("Use the independently recorded manifest SHA-256")
    raw = regular(directory / MANIFEST, 1024 * 1024)
    if digest(raw) != expected_sha256:
        raise ValueError("Kit manifest digest differs from the recorded copy")
    manifest = json.loads(raw)
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("format")) is not int
        or manifest["format"] != 1
        or manifest.get("scope") != "inactive_runtime_only"
    ):
        raise ValueError("Unsupported kit format or scope")
    files = manifest.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("Invalid kit inventory")
    for name in files:
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+/-]*", name)
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or PurePosixPath(name).is_absolute()
        ):
            raise ValueError("Invalid kit inventory path")
    if files != inventory(directory):
        raise ValueError("Kit checksum, size or file inventory mismatch")
    return manifest


def install(directory: Path, expected_sha256: str, destination: Path) -> None:
    directory, destination = directory.absolute(), destination.absolute()
    manifest = verify(directory, expected_sha256)
    if manifest["host"] != host():
        raise ValueError("Kit host differs: use the same Python minor, platform and libc")
    if destination.is_relative_to(directory):
        raise ValueError("Install outside the immutable kit directory")
    required = sum(item["bytes"] for item in manifest["files"].values()) * 4 + 256 * 1024 * 1024
    if shutil.disk_usage(destination.parent).free < required:
        raise ValueError("Insufficient free space for fresh runtime staging")
    destination.mkdir(mode=0o700)
    try:
        venv.EnvBuilder(with_pip=True).create(destination)
        python = destination / "bin/python"
        pip_install(python, directory)
        run([str(python), "-I", "-m", "pip", "--isolated", "check"], cwd=directory)
        installed = json.loads(
            run(
                [
                    str(python),
                    "-I",
                    "-c",
                    (
                        "import importlib.metadata,json;"
                        "print(json.dumps({d.metadata['Name']:d.version "
                        "for d in importlib.metadata.distributions()}))"
                    ),
                ],
                cwd=directory,
            )
        )
        actual = {normalized(name): version for name, version in installed.items()}
        expected = {name: package["version"] for name, package in manifest["packages"].items()}
        if {
            name: version for name, version in actual.items() if name not in {"pip", "setuptools"}
        } != expected:
            raise ValueError("Installed runtime differs from the kit package inventory")
        # Recheck the immutable copy before recording success, including the guide/tool.
        verify(directory, expected_sha256)
        (destination / "offline-kit.json").write_text(
            json.dumps(
                {
                    "manifest_sha256": expected_sha256,
                    "source_commit": manifest["source_commit"],
                    "schema_fingerprint": manifest["schema_fingerprint"],
                    "state": "inactive",
                },
                indent=2,
            )
            + "\n"
        )
    except BaseException:
        shutil.rmtree(destination)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("build")
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--wheelhouse", type=Path, required=True)
    prepare.add_argument("--ci-evidence", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    for command in ("verify", "install"):
        check = commands.add_parser(command)
        check.add_argument("--kit", type=Path, required=True)
        check.add_argument("--expected-sha256", required=True)
        if command == "install":
            check.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "build":
            print(build(args.source, args.wheelhouse, args.ci_evidence, args.output))
        elif args.command == "verify":
            value = verify(args.kit, args.expected_sha256)
            print(f"Verified {len(value['packages'])} packages; source {value['source_commit']}")
        else:
            install(args.kit, args.expected_sha256, args.destination)
            print("Offline runtime installed and verified; no service or radio was started")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        zipfile.BadZipFile,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"Offline kit failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
