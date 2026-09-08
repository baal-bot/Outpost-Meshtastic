"""Small local observations for SelfCheckService, never qualification exercises."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from outpost.maps.packs import FORMAT, active_manifest
from outpost.maps.regions import contains

MAX_MANIFEST_BYTES = 65_536
MIN_FREE_BYTES = 1_073_741_824
MIN_FREE_PERCENT = 5


def map_inventory(root: str, location: tuple[float, float] | None = None) -> dict[str, object]:
    """Read bounded metadata, not an unbounded tile walk or geographic proof."""
    try:
        descriptor = os.open(
            Path(root) / "manifest.json", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
        )
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_MANIFEST_BYTES:
                return {"state": "fail", "reason": "manifest_not_bounded_regular_file"}
            raw = source.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            return {"state": "fail", "reason": "manifest_too_large"}
        manifest = json.loads(raw)
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
    except FileNotFoundError:
        return {"state": "fail", "reason": "manifest_missing"}
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {"state": "fail", "reason": "manifest_unreadable"}
    if manifest.get("format") == FORMAT:
        try:
            selected = active_manifest(Path(root))
            regional = bool(selected and selected.get("region"))
            if regional and selected and location and not contains(selected["region"], *location):
                return {
                    "state": "fail",
                    "reason": "configured_location_outside_region",
                    "coverage_verified": False,
                    "world_overview": True,
                }
            return {
                "state": "pass" if regional else "unknown",
                "reason": "selected_region_verified" if regional else "overview_only",
                "coverage_verified": regional,
                "world_overview": True,
                "wan_disconnection_tested": False,
            }
        except (OSError, ValueError):
            return {"state": "fail", "reason": "installed_pack_changed"}
    count = manifest.get("tile_count")
    declared = count if type(count) is int and 0 <= count <= 10_000_000 else None
    return {
        "state": "fail" if declared == 0 else "unknown",
        "reason": "no_tiles_declared" if declared == 0 else "regional_coverage_not_verified",
        "manifest_present": True,
        "declared_tiles": declared,
        "coverage_verified": False,
    }


def storage_inventory(database_path: str) -> dict[str, object]:
    """Immediate free-space/inode headroom, not write endurance or outage duration."""
    try:
        space = os.statvfs(Path(database_path).parent)
    except OSError:
        return {"state": "unknown", "reason": "filesystem_counters_unavailable"}
    total, free = space.f_blocks * space.f_frsize, space.f_bavail * space.f_frsize
    if (
        total <= 0
        or not 0 <= free <= total
        or space.f_files <= 0
        or not 0 <= space.f_favail <= space.f_files
    ):
        return {"state": "unknown", "reason": "filesystem_counters_unavailable"}
    required = max(MIN_FREE_BYTES, total * MIN_FREE_PERCENT // 100)
    passed = free >= required and space.f_favail * 100 >= space.f_files * MIN_FREE_PERCENT
    return {
        "state": "pass" if passed else "fail",
        "reason": "immediate_headroom" if passed else "low_storage_headroom",
        "available_bytes": free,
        "required_bytes": required,
        "free_inode_percent": round(space.f_favail * 100 / space.f_files, 1),
        "minimum_percent": MIN_FREE_PERCENT,
        "outage_duration_verified": False,
    }
