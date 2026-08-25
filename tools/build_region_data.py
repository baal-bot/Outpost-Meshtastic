#!/usr/bin/env python3
"""Download official NWS boundaries and emit a compact single-state GeoJSON pack."""

from __future__ import annotations

import argparse
import json
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import shapefile

SOURCES = {
    "zones": "https://www.weather.gov/source/gis/Shapefiles/WSOM/z_16ap26.zip",
    "counties": "https://www.weather.gov/source/gis/Shapefiles/County/c_16ap26.zip",
}


def download(url: str, target: Path) -> None:
    if urllib.parse.urlparse(url).hostname != "www.weather.gov":
        raise ValueError("region source must be hosted by www.weather.gov")
    request = urllib.request.Request(  # noqa: S310 - fixed NWS host validated above.
        url, headers={"User-Agent": "Outpost region builder"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        target.write_bytes(response.read(50_000_000))


def rounded_coordinates(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], (int, float)):
            return [round(float(number), 5) for number in value]
        return [rounded_coordinates(item) for item in value]
    return value


def features(archive: Path, state: str, kind: str) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix=f"outpost-{kind}-") as directory:
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(directory)
        shapefile_path = next(Path(directory).glob("*.shp"))
        reader = shapefile.Reader(shapefile_path)
        names = [field[0] for field in reader.fields[1:]]
        values = []
        for record in reader.iterShapeRecords():
            properties = dict(zip(names, record.record, strict=True))
            if str(properties.get("STATE", "")).upper() != state:
                continue
            keep = (
                ("STATE", "ZONE", "CWA", "NAME", "STATE_ZONE")
                if kind == "zones"
                else ("STATE", "CWA", "COUNTYNAME", "FIPS")
            )
            values.append(
                {
                    "type": "Feature",
                    "properties": {key.lower(): properties.get(key) for key in keep},
                    "geometry": {
                        "type": record.shape.__geo_interface__["type"],
                        "coordinates": rounded_coordinates(
                            record.shape.__geo_interface__["coordinates"]
                        ),
                    },
                }
            )
        return values


def build(state: str, output: Path, source_dir: Path | None = None) -> Path:
    state = state.upper()
    if len(state) != 2 or not state.isalpha():
        raise ValueError("state must be a two-letter postal abbreviation")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="outpost-region-") as temporary:
        work = Path(temporary)
        collections = {}
        for kind, url in SOURCES.items():
            archive = source_dir / f"{kind}.zip" if source_dir else work / f"{kind}.zip"
            if not archive.exists():
                download(url, archive)
            collections[kind] = features(archive, state, kind)
        payload = {
            "type": "FeatureCollection",
            "outpost_region": state,
            "source": "NOAA/National Weather Service GIS, valid 2026-04-16",
            "features": collections["zones"] + collections["counties"],
        }
        target = output / f"nws-boundaries-{state.lower()}.geojson"
        target.write_text(json.dumps(payload, separators=(",", ":")))
        if target.stat().st_size >= 5_000_000:
            raise RuntimeError(f"regional pack exceeds 5 MB: {target.stat().st_size} bytes")
        return target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, help="Two-letter postal abbreviation")
    parser.add_argument("--output", type=Path, default=Path("data/regions"))
    parser.add_argument("--source-dir", type=Path, help="Use local zones.zip/counties.zip")
    args = parser.parse_args()
    target = build(args.state, args.output, args.source_dir)
    print(f"{target} · {target.stat().st_size} bytes")


if __name__ == "__main__":
    main()
