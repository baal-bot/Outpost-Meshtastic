from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any


class ReleaseVerificationError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_checksums(directory: Path) -> dict[str, str]:
    checksum_file = directory / "SHA256SUMS"
    entries: dict[str, str] = {}
    for line in checksum_file.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]*)", line)
        if match is None:
            raise ReleaseVerificationError("SHA256SUMS contains an invalid entry")
        digest, name = match.groups()
        if name in entries:
            raise ReleaseVerificationError(f"SHA256SUMS contains a duplicate: {name}")
        entries[name] = digest
    if not entries:
        raise ReleaseVerificationError("SHA256SUMS is empty")
    return entries


def inspect_wheel(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ReleaseVerificationError("wheel must contain exactly one METADATA file")
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    name, version = metadata.get("Name"), metadata.get("Version")
    if not name or not version:
        raise ReleaseVerificationError("wheel METADATA is missing identity")
    return name, version


def verify_release(directory: Path, expected_tag: str) -> dict[str, Any]:
    entries = load_checksums(directory)
    expected_names = {path.name for path in directory.iterdir() if path.name != "SHA256SUMS"}
    if set(entries) != expected_names:
        missing = sorted(expected_names - set(entries))
        extra = sorted(set(entries) - expected_names)
        raise ReleaseVerificationError(
            f"checksum inventory mismatch; missing={missing or 'none'} extra={extra or 'none'}"
        )
    for name, expected in entries.items():
        if sha256_file(directory / name) != expected:
            raise ReleaseVerificationError(f"checksum mismatch: {name}")

    metadata_path = directory / "RELEASE-METADATA.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ReleaseVerificationError("release metadata is missing or invalid") from exc
    if not isinstance(metadata, dict) or metadata.get("format_version") != 1:
        raise ReleaseVerificationError("unsupported release metadata format")
    package, source = metadata.get("package"), metadata.get("source")
    database, support = metadata.get("database"), metadata.get("support")
    if not all(isinstance(item, dict) for item in (package, source, database, support)):
        raise ReleaseVerificationError("release metadata sections are invalid")
    if source.get("tag") != expected_tag or not re.fullmatch(
        r"[0-9a-f]{40}", str(source.get("commit", ""))
    ):
        raise ReleaseVerificationError("release source tag or commit is invalid")
    wheel_name = str(package.get("wheel", ""))
    if wheel_name not in entries or not wheel_name.endswith(".whl"):
        raise ReleaseVerificationError("release metadata names an invalid wheel")
    required_names = {wheel_name, "RELEASE-METADATA.json", "outpost.spdx.json"}
    if set(entries) != required_names:
        raise ReleaseVerificationError("release contains an unexpected artifact set")
    wheel = directory / wheel_name
    if package.get("sha256") != entries[wheel_name]:
        raise ReleaseVerificationError("release metadata wheel digest does not match SHA256SUMS")
    name, version = inspect_wheel(wheel)
    if package.get("name") != name or package.get("version") != version:
        raise ReleaseVerificationError("release metadata does not match wheel identity")
    if expected_tag != f"v{version}":
        raise ReleaseVerificationError("release tag does not match wheel version")
    if not isinstance(database.get("maximum_schema"), int) or database["maximum_schema"] < 0:
        raise ReleaseVerificationError("release metadata database schema is invalid")
    if not str(support.get("python", "")).startswith(">="):
        raise ReleaseVerificationError("release metadata Python support is invalid")
    systems = support.get("operating_systems")
    if not isinstance(systems, list) or not systems:
        raise ReleaseVerificationError("release metadata OS support is missing")
    try:
        sbom = json.loads((directory / "outpost.spdx.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ReleaseVerificationError("release SBOM is missing or invalid") from exc
    if not isinstance(sbom, dict) or not str(sbom.get("spdxVersion", "")).startswith("SPDX-"):
        raise ReleaseVerificationError("release SBOM is not SPDX JSON")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify downloaded Outpost release artifacts")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--print-commit", action="store_true")
    arguments = parser.parse_args()
    try:
        metadata = verify_release(arguments.directory, arguments.tag)
    except (OSError, zipfile.BadZipFile, ReleaseVerificationError) as exc:
        print(f"release verification failed: {exc}", file=sys.stderr)
        return 1
    if arguments.print_commit:
        print(metadata["source"]["commit"])
    else:
        print(
            f"verified {metadata['source']['tag']} ({metadata['source']['commit']}) "
            f"package {metadata['package']['name']} {metadata['package']['version']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
