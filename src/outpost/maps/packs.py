from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from outpost.maps.regions import MAX_DOWNLOAD_BYTES, MAX_TILES, MapError, Region, tiles_for_region
from outpost.maps.vector import validate_tile

FORMAT = "outpost-vector-v1"
SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tiles (
  kind TEXT NOT NULL CHECK(kind IN ('overview','region')),
  z INTEGER NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL,
  data BLOB NOT NULL, sha256 TEXT NOT NULL,
  PRIMARY KEY(kind,z,x,y)
) WITHOUT ROWID;
"""


def read_json(path: Path, limit: int = 65536) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise MapError("Map metadata is not a bounded regular file.")
        value = json.loads(stream.read(limit + 1))
    if not isinstance(value, dict):
        raise MapError("Map metadata must be an object.")
    return value


def atomic_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    temporary = path.with_name(path.name + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "w") as stream:
        os.fchmod(stream.fileno(), mode)
        json.dump(value, stream, separators=(",", ":"), allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024**2):
            digest.update(chunk)
    return digest.hexdigest()


def open_pack(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * MAX_DOWNLOAD_BYTES:
        raise MapError("Map pack is missing, linked, or exceeds its size limit.")
    database = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=2)
    database.execute("PRAGMA query_only=ON")
    database.execute("PRAGMA trusted_schema=OFF")
    return database


def verify_pack(path: Path) -> dict[str, Any]:
    with closing(open_pack(path)) as database:
        objects = database.execute(
            "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if sorted(objects) != [("table", "metadata"), ("table", "tiles")]:
            raise MapError("Map pack has an unsupported database structure.")
        if database.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise MapError("Map pack database integrity failed.")
        meta_rows = database.execute("SELECT key,value FROM metadata LIMIT 9").fetchall()
        if len(meta_rows) != 1 or meta_rows[0][0] != "manifest" or len(meta_rows[0][1]) > 65536:
            raise MapError("Map pack metadata is invalid.")
        metadata = json.loads(meta_rows[0][1])
        if not isinstance(metadata, dict) or metadata.get("format") != FORMAT:
            raise MapError("Map pack format is unsupported.")
        if not str(metadata.get("source_version", "")).startswith("4."):
            raise MapError("Map pack needs an unsupported basemap style version.")
        if metadata.get("tile_compression") not in {1, 2}:
            raise MapError("Map pack compression is unsupported.")
        region_data = metadata.get("region")
        if region_data is not None and (
            not isinstance(region_data, dict)
            or not all(
                key in region_data for key in ("latitude", "longitude", "radius_km", "max_zoom")
            )
        ):
            raise MapError("Map region metadata is invalid.")
        region = (
            Region(
                **{k: region_data[k] for k in ("latitude", "longitude", "radius_km", "max_zoom")}
            )
            if region_data is not None
            else None
        )
        expected = set(tiles_for_region(region))
        metadata["region"] = region.document() if region else None
        count = database.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        if count != len(expected) or count > MAX_TILES:
            raise MapError("Map pack is incomplete or contains unexpected tiles.")
        overview_levels: set[int] = set()
        for kind, zoom, x, y, data, checksum in database.execute(
            "SELECT kind,z,x,y,data,sha256 FROM tiles"
        ):
            key = kind, zoom, x, y
            if key not in expected or not isinstance(data, bytes) or len(data) > 4 * 1024**2:
                raise MapError("Map pack contains an invalid tile.")
            expected.remove(key)
            if hashlib.sha256(data).hexdigest() != checksum:
                raise MapError("Map pack tile checksum failed.")
            if data:
                validate_tile(data, metadata["tile_compression"])
                if kind == "overview":
                    overview_levels.add(zoom)
        if overview_levels != set(range(7)):
            raise MapError("Map pack has no world overview data at one or more zooms.")
        if expected:
            raise MapError("Map pack coverage is incomplete.")
    return {
        **metadata,
        "tile_count": count,
        "bytes": path.stat().st_size,
        "sha256": file_hash(path),
    }


def publish_pack(root: Path, staged: Path, identifier: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise MapError("Invalid map pack identifier.")
    manifest = verify_pack(staged)
    destination = root / "packs" / f"{identifier}.sqlite"
    destination.parent.mkdir(mode=0o755, exist_ok=True)
    if destination.exists():
        raise MapError("This map pack already exists; create a new setup plan.")
    os.chmod(staged, 0o644)
    with staged.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(staged, destination)
    descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    info = destination.stat()
    manifest.update(
        version=3,
        pack_id=identifier,
        file=f"packs/{identifier}.sqlite",
        verified_at=int(time.time()),
        file_mtime_ns=info.st_mtime_ns,
        overview_max_zoom=6,
        attribution="© OpenStreetMap contributors · Protomaps",
        license="ODbL Produced Work",
    )
    # A previous raster/vector manifest and its files remain available for recovery.
    old = root / "manifest.json"
    if old.exists():
        try:
            previous = read_json(old)
        except (OSError, ValueError):
            previous = None  # A malformed old manifest must not prevent verified repair.
        if previous is not None:
            atomic_json(root / "previous-manifest.json", previous, 0o644)
    atomic_json(destination.with_suffix(".json"), manifest, 0o644)
    atomic_json(old, manifest, 0o644)
    return manifest


def active_manifest(root: Path, pack_id: str | None = None) -> dict[str, Any] | None:
    if pack_id is not None and not re.fullmatch(r"[0-9a-f]{32}", pack_id):
        return None
    path = root / "manifest.json" if pack_id is None else root / "packs" / f"{pack_id}.json"
    if not path.exists():
        return None
    value = read_json(path)
    if value.get("format") != FORMAT:
        return None
    identifier = value.get("pack_id", "")
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise MapError("Installed map selection is invalid.")
    file = root / "packs" / f"{identifier}.sqlite"
    info = file.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_size != value.get("bytes")
        or info.st_mtime_ns != value.get("file_mtime_ns")
    ):
        raise MapError("Installed map data changed; import or download a verified replacement.")
    if value.get("file") != f"packs/{identifier}.sqlite" or value.get("tile_compression") not in {
        1,
        2,
    }:
        raise MapError("Installed map metadata is invalid.")
    return value


def vector_tile(
    root: Path, kind: str, zoom: int, x: int, y: int, pack_id: str | None = None
) -> tuple[bytes, int] | None:
    if (
        kind not in {"overview", "region"}
        or not 0 <= zoom <= 15
        or not 0 <= x < 1 << zoom
        or not 0 <= y < 1 << zoom
    ):
        return None
    manifest = active_manifest(root, pack_id)
    if manifest is None:
        return None
    with closing(open_pack(root / manifest["file"])) as database:
        row = database.execute(
            "SELECT data,sha256 FROM tiles WHERE kind=? AND z=? AND x=? AND y=?", (kind, zoom, x, y)
        ).fetchone()
    if row is None:
        return None
    content, checksum = row
    if hashlib.sha256(content).hexdigest() != checksum:
        raise MapError("Installed map tile is corrupt; import or download a verified replacement.")
    return content, int(manifest["tile_compression"])
