from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

TilePackState = Literal["ready", "missing", "unreadable"]
_MEDIA_TYPES = {"jpg": "image/jpeg", "png": "image/png"}
_PHYSICAL_EXTENSIONS = ("jpg", "jpeg", "png")


@dataclass(frozen=True)
class TilePackStatus:
    root: Path
    state: TilePackState
    detail: str
    manifest: dict[str, Any] | None = None
    tile_extension: str | None = None


def absolute_tile_root(value: str | Path) -> Path:
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise ValueError("offline tile path must be absolute")
    return root.resolve(strict=False)


def raster_extension(path: Path) -> str | None:
    with path.open("rb") as source:
        header = source.read(8)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    return None


def inspect_tile_pack(value: str | Path) -> TilePackStatus:
    root = absolute_tile_root(value)
    try:
        root_mode = root.stat().st_mode
    except FileNotFoundError:
        return TilePackStatus(root, "missing", "directory does not exist")
    except OSError as error:
        return TilePackStatus(root, "unreadable", f"directory cannot be read: {error.strerror}")
    if not stat.S_ISDIR(root_mode):
        return TilePackStatus(root, "unreadable", "configured path is not a directory")

    manifest_path = root / "manifest.json"
    try:
        manifest_mode = manifest_path.stat().st_mode
    except FileNotFoundError:
        return TilePackStatus(root, "missing", "manifest.json is not installed")
    except OSError as error:
        return TilePackStatus(root, "unreadable", f"manifest cannot be read: {error.strerror}")
    if not stat.S_ISREG(manifest_mode):
        return TilePackStatus(root, "unreadable", "manifest.json is not a regular file")

    try:
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return TilePackStatus(root, "unreadable", f"manifest is invalid: {type(error).__name__}")
    if not isinstance(manifest_value, dict):
        return TilePackStatus(root, "unreadable", "manifest root is not an object")

    requested = str(manifest_value.get("tile_extension") or "").lower().removeprefix(".")
    extensions = tuple(
        dict.fromkeys((requested, *_PHYSICAL_EXTENSIONS)) if requested else _PHYSICAL_EXTENSIONS
    )
    try:
        candidates = (
            candidate
            for extension in extensions
            for candidate in root.glob(f"*/*/*.{extension}")
            if candidate.is_file()
        )
        for candidate in candidates:
            detected = raster_extension(candidate)
            if detected is None:
                continue
            manifest = dict(manifest_value)
            manifest["tile_extension"] = detected
            return TilePackStatus(
                root,
                "ready",
                f"usable {detected.upper()} tile pack found",
                manifest,
                detected,
            )
    except OSError as error:
        return TilePackStatus(root, "unreadable", f"tile data cannot be read: {error.strerror}")
    return TilePackStatus(root, "unreadable", "manifest exists but no readable raster tile exists")


def find_tile(
    root: Path, zoom: int, x: int, y: int, requested_extension: str
) -> tuple[Path, str] | None:
    requested = requested_extension.lower().removeprefix(".")
    if requested not in _MEDIA_TYPES:
        return None
    for physical_extension in dict.fromkeys((requested, *_PHYSICAL_EXTENSIONS)):
        candidate = root / str(zoom) / str(x) / f"{y}.{physical_extension}"
        try:
            if not candidate.is_file() or raster_extension(candidate) != requested:
                continue
        except OSError:
            continue
        return candidate, _MEDIA_TYPES[requested]
    return None
