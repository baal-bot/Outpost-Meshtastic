#!/usr/bin/env python3
"""Build a bounded local raster tile pack from the public-domain USGS basemap."""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.request
from pathlib import Path

import yaml

USGS_TOPO = (
    "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}"
)


def tile_xy(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    lat = max(-85.05112878, min(85.05112878, lat))
    scale = 2**zoom
    x = int((lon + 180) / 360 * scale)
    radians = math.radians(lat)
    y = int((1 - math.asinh(math.tan(radians)) / math.pi) / 2 * scale)
    return max(0, min(scale - 1, x)), max(0, min(scale - 1, y))


def bounds(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    lat_delta = radius_km / 111.32
    lon_delta = radius_km / (111.32 * max(0.05, math.cos(math.radians(lat))))
    return lat - lat_delta, lon - lon_delta, lat + lat_delta, lon + lon_delta


def planned_tiles(
    lat: float, lon: float, radius_km: float, min_zoom: int, max_zoom: int
) -> list[tuple[int, int, int]]:
    south, west, north, east = bounds(lat, lon, radius_km)
    result = []
    for zoom in range(min_zoom, max_zoom + 1):
        left, bottom = tile_xy(south, west, zoom)
        right, top = tile_xy(north, east, zoom)
        for x in range(min(left, right), max(left, right) + 1):
            for y in range(min(top, bottom), max(top, bottom) + 1):
                result.append((zoom, x, y))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--radius-km", type=float, default=20)
    parser.add_argument("--min-zoom", type=int, default=8)
    parser.add_argument("--max-zoom", type=int, default=14)
    parser.add_argument("--max-tiles", type=int, default=1000)
    parser.add_argument("--output", type=Path, default=Path(".data/tiles"))
    parser.add_argument("--url-template", default=USGS_TOPO)
    parser.add_argument("--source-name", default="USGS The National Map — USGSTopo")
    parser.add_argument(
        "--attribution",
        default=(
            "Map services and data available from U.S. Geological Survey, "
            "National Geospatial Program."
        ),
    )
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if args.config:
        data = yaml.safe_load(args.config.read_text()) or {}
        location = data.get("node", {}).get("location")
        if not location:
            parser.error(f"node.location is not configured in {args.config}")
        args.lat = location["lat"]
        args.lon = location["lon"]
    if args.lat is None or args.lon is None:
        parser.error("provide --config with node.location or both --lat and --lon")
    if not all(token in args.url_template for token in ("{z}", "{x}", "{y}")):
        parser.error("URL template must contain {z}, {x}, and {y}")
    if "tile.openstreetmap.org" in args.url_template:
        parser.error("tile.openstreetmap.org prohibits offline tile packs")
    if not (-90 <= args.lat <= 90 and -180 <= args.lon <= 180):
        parser.error("latitude or longitude is outside its valid range")
    if not (0 < args.radius_km <= 250):
        parser.error("radius must be greater than 0 and no more than 250 km")
    if not (0 <= args.min_zoom <= args.max_zoom <= 14):
        parser.error("zoom range must be between 0 and 14")
    tiles = planned_tiles(args.lat, args.lon, args.radius_km, args.min_zoom, args.max_zoom)
    if len(tiles) > args.max_tiles:
        parser.error(f"pack requires {len(tiles)} tiles; limit is {args.max_tiles}")
    args.output.mkdir(parents=True, exist_ok=True)
    downloaded = skipped = 0
    for index, (zoom, x, y) in enumerate(tiles, 1):
        destination = args.output / str(zoom) / str(x) / f"{y}.png"
        if destination.exists() and not args.replace:
            skipped += 1
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(  # noqa: S310
            args.url_template.format(z=zoom, x=x, y=y),
            headers={"User-Agent": "Outpost/0.1 offline tile pack (local operator install)"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            content = response.read()
        if not (content.startswith(b"\x89PNG") or content.startswith(b"\xff\xd8\xff")):
            raise RuntimeError(f"tile {zoom}/{x}/{y} was not a raster image")
        destination.write_bytes(content)
        downloaded += 1
        if index % 25 == 0:
            print(f"{index}/{len(tiles)} tiles processed", flush=True)
        time.sleep(0.03)
    south, west, north, east = bounds(args.lat, args.lon, args.radius_km)
    manifest = {
        "version": 1,
        "source": args.source_name,
        "attribution": args.attribution,
        "center": {"lat": args.lat, "lon": args.lon},
        "bounds": {"south": south, "west": west, "north": north, "east": east},
        "min_zoom": args.min_zoom,
        "max_zoom": args.max_zoom,
        "tile_count": len(tiles),
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Tile pack ready: {len(tiles)} total, {downloaded} downloaded, {skipped} retained")


if __name__ == "__main__":
    main()
