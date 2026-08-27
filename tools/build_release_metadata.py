from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
import zipfile
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path
from typing import Any


class ReleaseMetadataError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_wheel(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        metadata_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_files) != 1:
            raise ReleaseMetadataError("wheel must contain exactly one METADATA file")
        metadata = BytesParser().parsebytes(archive.read(metadata_files[0]))
    name, version = metadata.get("Name"), metadata.get("Version")
    if not name or not version:
        raise ReleaseMetadataError("wheel METADATA is missing Name or Version")
    return name, version


def build_metadata(
    wheel: Path,
    root: Path,
    *,
    commit: str,
    tag: str,
    built_at: str | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseMetadataError("commit must be a full 40-character Git SHA")
    name, version = inspect_wheel(wheel)
    if tag != f"v{version}":
        raise ReleaseMetadataError(f"release tag {tag!r} does not match wheel version {version!r}")
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if project["name"] != name or project["version"] != version:
        raise ReleaseMetadataError("wheel identity does not match pyproject.toml")
    migrations = sorted((root / "src/outpost/store/migrations").glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not migrations:
        raise ReleaseMetadataError("no database migrations were found")
    maximum_schema = max(int(path.name[:4]) for path in migrations)
    capabilities = root / "docs/capabilities.toml"
    return {
        "format_version": 1,
        "package": {
            "name": name,
            "version": version,
            "wheel": wheel.name,
            "sha256": sha256_file(wheel),
        },
        "source": {"commit": commit, "tag": tag},
        "database": {"maximum_schema": maximum_schema},
        "support": {
            "python": str(project["requires-python"]),
            "operating_systems": [
                "64-bit Raspberry Pi OS Trixie",
                "Ubuntu 24.04 CI",
            ],
        },
        "capabilities": {
            "manifest": "docs/capabilities.toml",
            "sha256": sha256_file(capabilities),
        },
        "build": {
            "workflow": ".github/workflows/release.yml",
            "built_at": built_at or datetime.now(UTC).replace(microsecond=0).isoformat(),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build machine-readable Outpost release metadata")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        value = build_metadata(
            arguments.wheel,
            Path.cwd(),
            commit=arguments.commit,
            tag=arguments.tag,
        )
    except (
        OSError,
        KeyError,
        tomllib.TOMLDecodeError,
        zipfile.BadZipFile,
        ReleaseMetadataError,
    ) as exc:
        parser.error(str(exc))
    arguments.output.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
