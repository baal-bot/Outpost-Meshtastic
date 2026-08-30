from __future__ import annotations

import sys
from pathlib import Path
from typing import Self

import pytest
from fastapi.testclient import TestClient

from outpost.web.api import create_web_app
from outpost.web.tiles import find_tile, inspect_tile_pack
from tools import build_tile_pack
from tools.build_tile_pack import DEFAULT_TILES_PATH, configured_output, raster_extension

PNG_HEADER = b"\x89PNG\r\n\x1a\n"
JPEG_HEADER = b"\xff\xd8\xff\xe0legacy-jpeg"


def write_manifest(root: Path, value: str = '{"source":"test","tile_count":1}') -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(value, encoding="utf-8")


def test_tile_pack_status_distinguishes_missing_unreadable_and_legacy_jpeg(tmp_path: Path) -> None:
    missing = inspect_tile_pack(tmp_path / "missing")
    assert missing.state == "missing"

    unreadable_root = tmp_path / "unreadable"
    write_manifest(unreadable_root, "not-json")
    unreadable = inspect_tile_pack(unreadable_root)
    assert unreadable.state == "unreadable"
    assert "manifest is invalid" in unreadable.detail

    ready_root = tmp_path / "ready"
    write_manifest(ready_root)
    legacy_tile = ready_root / "1" / "0" / "0.png"
    legacy_tile.parent.mkdir(parents=True)
    legacy_tile.write_bytes(JPEG_HEADER)
    ready = inspect_tile_pack(ready_root)

    assert ready.state == "ready"
    assert ready.tile_extension == "jpg"
    assert ready.manifest is not None and ready.manifest["tile_extension"] == "jpg"
    assert find_tile(ready_root, 1, 0, 0, "jpg") == (legacy_tile, "image/jpeg")
    assert find_tile(ready_root, 1, 0, 0, "png") is None


def test_tile_http_status_and_extension_match_detected_raster(tmp_path: Path) -> None:
    tile_root = tmp_path / "tiles"
    write_manifest(tile_root)
    tile = tile_root / "1" / "0" / "0.png"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(JPEG_HEADER)
    client = TestClient(create_web_app(lambda: {"radio": "up"}, tile_path=tile_root))

    manifest = client.get("/tiles/manifest.json")
    assert manifest.status_code == 200
    assert manifest.headers["cache-control"] == "no-store"
    assert manifest.json()["status"] == "ready"
    assert manifest.json()["tile_extension"] == "jpg"
    image = client.get("/tiles/1/0/0.jpg")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"
    assert image.content == JPEG_HEADER
    assert client.get("/tiles/1/0/0.png").status_code == 404

    missing = TestClient(
        create_web_app(lambda: {"radio": "up"}, tile_path=tmp_path / "absent")
    ).get("/tiles/manifest.json")
    assert missing.status_code == 404 and missing.json()["status"] == "missing"

    broken_root = tmp_path / "broken"
    write_manifest(broken_root, "not-json")
    broken = TestClient(create_web_app(lambda: {"radio": "up"}, tile_path=broken_root)).get(
        "/tiles/manifest.json"
    )
    assert broken.status_code == 503 and broken.json()["status"] == "unreadable"


def test_tile_builder_uses_configured_absolute_default_and_detects_formats(tmp_path: Path) -> None:
    configured = tmp_path / "external-storage" / "tiles"

    assert configured_output(str(configured), None) == configured
    assert configured_output(None, None) == DEFAULT_TILES_PATH
    assert configured_output(None, tmp_path / "explicit") == tmp_path / "explicit"
    assert raster_extension(PNG_HEADER + b"payload") == "png"
    assert raster_extension(JPEG_HEADER) == "jpg"
    assert raster_extension(b"not-a-raster") is None

    with pytest.raises(ValueError, match="must be absolute"):
        configured_output("relative/tiles", None)


def test_tile_builder_writes_truthful_jpeg_extension_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "configured-tiles"
    config = tmp_path / "config.yaml"
    config.write_text(
        f"node:\n  location:\n    lat: 40\n    lon: -75\nstore:\n  tiles_path: {output}\n",
        encoding="utf-8",
    )

    class Response:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return JPEG_HEADER

    monkeypatch.setattr(
        build_tile_pack.urllib.request, "urlopen", lambda *_args, **_kwargs: Response()
    )
    monkeypatch.setattr(build_tile_pack.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_tile_pack.py",
            "--config",
            str(config),
            "--min-zoom",
            "0",
            "--max-zoom",
            "0",
        ],
    )

    build_tile_pack.main()

    assert (output / "0" / "0" / "0.jpg").read_bytes() == JPEG_HEADER
    assert not (output / "0" / "0" / "0.png").exists()
    assert '"tile_extension": "jpg"' in (output / "manifest.json").read_text()
