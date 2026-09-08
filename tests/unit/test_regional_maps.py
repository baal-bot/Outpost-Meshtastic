import gzip
import sqlite3
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from outpost.maps import setup
from outpost.maps.packs import active_manifest, vector_tile, verify_pack
from outpost.maps.pmtiles import Archive, RangeSource, directory, expand, tile_id
from outpost.maps.regions import (
    MAX_DOWNLOAD_BYTES,
    MapError,
    Region,
    position_suggestion,
    tiles_for_region,
)
from outpost.maps.setup import MapSetupService
from outpost.readiness_probes import map_inventory
from outpost.transport.models import LinkState
from outpost.transport.radio_link import MeshtasticRadioLink
from outpost.web.api import create_web_app
from tests.support.maps import URL, pmtiles, source_factory
from tests.support.maps import vector_tile as mvt


def wait(service):
    deadline = time.monotonic() + 10
    while service.status()["busy"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not service.status()["busy"]
    return service.status()["job"]


@pytest.fixture
def service(tmp_path, monkeypatch):
    factory = source_factory()
    monkeypatch.setattr(setup, "RangeSource", factory)
    result = MapSetupService(tmp_path)
    monkeypatch.setattr(result, "_source", factory)
    yield result
    result.close()


def install(service, region=None):
    job = service.plan(region, 64 * 1024**2)
    assert wait(service)["state"] == "planned"
    service.download(job["id"])
    assert wait(service)["state"] == "complete"
    return active_manifest(service.root)


def test_worldwide_planning_wraps_date_line_and_rejects_unbounded_work():
    tiles = tiles_for_region(Region(-16.5, 179.99, 20, 14))
    assert len(tiles_for_region(None)) == 5461
    assert ("region", 14, 0, 0) not in tiles  # latitude still bounds the work
    xs = {x for kind, z, x, y in tiles if kind == "region" and z == 14}
    assert 0 in xs and 16383 in xs
    assert len(xs) < 30
    for point in [(0, 0), (-1.2864, 36.8172), (35.7, 139.7), (51.5, -0.1)]:
        assert len(tiles_for_region(Region(*point))) < 60000
    with pytest.raises(MapError):
        Region(85, 0, 100)
    with pytest.raises(MapError):
        tiles_for_region(Region(80, 0, 100, 15))
    with pytest.raises(MapError):
        Region(0, 0, float("nan"))


def test_position_requires_actual_recent_local_fix():
    raw = {"latitude": 0, "longitude": 0, "source": "LOC_INTERNAL", "timestamp": 990}
    assert position_suggestion(raw, 1000)["available"]
    for change in [
        {"source": "LOC_MANUAL"},
        {"timestamp": 0},
        {"timestamp": 1001},
        {"timestamp": 1},
        {"latitude": float("nan")},
        {"longitude": True},
    ]:
        assert not position_suggestion({**raw, **change}, 1000)["available"]
    assert position_suggestion({}, 1000, (10, 20))["source"] == "configured"
    radio = object.__new__(MeshtasticRadioLink)
    radio._state = LinkState.UP
    radio._local_id = "!00000001"
    radio._interface = SimpleNamespace(
        nodes={
            "!00000001": {
                "position": {"latitudeI": 0, "longitudeI": 0, "locationSource": 2, "timestamp": 990}
            },
            "!00000002": {"position": {"latitude": 50, "longitude": 40}},
        }
    )
    assert position_suggestion(radio.local_position(), 1000)["available"]
    radio._state = LinkState.DOWN
    assert radio.local_position() == {}


def test_pmtiles_hilbert_and_interoperable_directory():
    assert [tile_id(1, x, y) for x, y in [(0, 0), (0, 1), (1, 1), (1, 0)]] == [1, 2, 3, 4]
    data = pmtiles()
    archive = Archive(lambda start, length: data[start : start + length])
    offset, length = archive.locate(tile_id(14, 1000, 1000))
    assert expand(data[offset : offset + length], 2) == mvt()
    with pytest.raises(MapError):
        directory(b"\x80" * 12, 1)
    with pytest.raises(MapError):
        expand(gzip.compress(b"x" * 10000), 2, 1024)


@pytest.mark.parametrize("mode", ["whole", "wrong_range", "changed", "redirect"])
def test_remote_reader_never_accepts_whole_file_or_changed_source(mode):
    def respond(request):
        status = 200 if mode == "whole" else 302 if mode == "redirect" else 206
        return httpx.Response(
            status,
            content=b"abc",
            headers={
                "Content-Range": "bytes 1-3/100" if mode == "wrong_range" else "bytes 0-2/100",
                "ETag": '"changed"' if mode == "changed" else '"original"',
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        source = RangeSource(URL, budget=100, etag='"original"', client=client)
        with pytest.raises(MapError):
            source.read(0, 3)
        assert source.received == 0
    with pytest.raises(MapError):
        RangeSource("http://127.0.0.1/private", budget=100)


def test_install_serves_verified_tiles_and_preserves_selection_on_bad_import(service):
    manifest = install(service, Region(0, 179.99, 1, 7))
    assert map_inventory(str(service.root))["state"] == "pass"
    assert map_inventory(str(service.root), (0, 179.99))["state"] == "pass"
    assert (
        map_inventory(str(service.root), (51.5, 0))["reason"]
        == "configured_location_outside_region"
    )
    assert verify_pack(service.root / manifest["file"])["tile_count"] == manifest["tile_count"]
    app = create_web_app(lambda: {}, tile_path=service.root, map_setup=service)
    client = TestClient(app)
    assert client.get("/tiles/manifest.json").json()["format"] == "outpost-vector-v1"
    public = client.get("/tiles/manifest.json").json()
    assert set(public["region"]) == {"bounds", "max_zoom"}
    assert public["region"]["bounds"] != manifest["region"]["bounds"]
    assert not {"file", "sha256", "id", "source_etag"} & public.keys()
    private = client.get("/api/v1/maps").json()["installed"]
    assert private["region"]["longitude"] == 179.99
    tile = client.get(f"/tiles/vector/{manifest['pack_id']}/overview/0/0/0.pbf")
    assert tile.status_code == 200 and tile.content == mvt()
    assert client.get("/api/v1/maps").json()["installed"]["pack_id"] == manifest["pack_id"]
    bad = service.root / "bad.sqlite"
    bad.write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        service.import_pack(bad)
    assert active_manifest(service.root)["pack_id"] == manifest["pack_id"]
    # Open tabs retain access to their previous immutable pack after replacement.
    install(service)
    assert client.get(f"/tiles/vector/{manifest['pack_id']}/overview/0/0/0.pbf").status_code == 200
    assert map_inventory(str(service.root))["reason"] == "overview_only"


def test_interrupted_download_resumes_without_selecting_partial_pack(service, monkeypatch):
    old = install(service)
    job = service.plan(Region(0, 0, 1, 8), 64 * 1024**2)
    wait(service)
    source = source_factory()()
    original = source.read
    data_offset = Archive(original).data_offset

    def interrupted(offset, length):
        if offset >= data_offset:
            service.cancel.set()
            raise MapError("paused")
        return original(offset, length)

    source.read = interrupted
    monkeypatch.setattr(setup, "RangeSource", lambda *a, **kw: source)
    service.download(job["id"])
    assert wait(service)["state"] == "paused"
    assert active_manifest(service.root)["pack_id"] == old["pack_id"]
    monkeypatch.setattr(setup, "RangeSource", source_factory())
    service.download(job["id"])
    assert wait(service)["state"] == "complete"
    assert active_manifest(service.root)["pack_id"] == job["id"]


def test_import_rejects_missing_or_corrupt_tiles_and_detects_installed_changes(service):
    manifest = install(service)
    pack = service.root / manifest["file"]
    with sqlite3.connect(pack) as database:
        database.execute("DELETE FROM tiles WHERE z=0")
    with pytest.raises(MapError):
        verify_pack(pack)
    assert map_inventory(str(service.root))["state"] == "fail"
    with pytest.raises(MapError):
        vector_tile(service.root, "overview", 1, 0, 0)


def test_setup_limit_and_process_lock(service):
    with pytest.raises(MapError):
        service.plan(None, MAX_DOWNLOAD_BYTES + 1)
    other = MapSetupService(service.root)
    service._claim()
    try:
        with pytest.raises(MapError, match="Another"):
            other.plan(None, 64 * 1024**2)
    finally:
        service._release()
        other.close()
    client = TestClient(create_web_app(lambda: {}, tile_path=service.root, map_setup=service))
    assert client.post("/api/v1/maps/plan", json={"budget_bytes": 120 * 1024**3}).status_code == 422
    assert client.post("/api/v1/maps/plan", json={"planet": True}).status_code == 422
    assert (
        client.post(
            "/api/v1/maps/plan", json={"region": {"latitude": False, "longitude": 0}}
        ).status_code
        == 422
    )


def test_vendor_runtime_is_complete_and_matches_pinned_hashes():
    import hashlib
    import json
    from pathlib import Path

    root = Path("src/outpost/web/static/vendor/maps")
    manifest = json.loads((root / "manifest.json").read_text())
    expected = manifest["files"]
    assert set(expected) == {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    for name, digest in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest


def test_valid_checksum_does_not_accept_malformed_vector_data(service):
    import hashlib

    from outpost.maps.vector import validate_tile

    with pytest.raises(MapError):
        validate_tile(b"not a vector tile", 1)
    manifest = install(service)
    pack = service.root / manifest["file"]
    corrupt = gzip.compress(b"malformed")
    with sqlite3.connect(pack) as database:
        database.execute(
            "UPDATE tiles SET data=?,sha256=? WHERE z=0",
            (corrupt, hashlib.sha256(corrupt).hexdigest()),
        )
    with pytest.raises(MapError):
        verify_pack(pack)


def test_verified_replacement_repairs_a_corrupt_selection_manifest(service):
    manifest = install(service)
    pack = service.root / manifest["file"]
    (service.root / "manifest.json").write_text("broken selection")
    replacement = service.import_pack(pack)
    assert active_manifest(service.root)["pack_id"] == replacement["pack_id"]
    assert replacement["pack_id"] != manifest["pack_id"]
