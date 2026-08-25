import json
import sys
from pathlib import Path

import shapefile

sys.path.insert(0, str(Path(__file__).parents[2] / "tools"))
from build_region_data import build  # noqa: E402


def _archive(source: Path, name: str, fields: list[tuple[str, str]], record: list[str]) -> None:
    import zipfile

    work = source / name
    writer = shapefile.Writer(str(work))
    for field, kind in fields:
        writer.field(field, kind)
    writer.poly([[[-80.1, 40.3], [-79.8, 40.3], [-79.8, 40.6], [-80.1, 40.3]]])
    writer.record(*record)
    writer.close()
    with zipfile.ZipFile(source / f"{name}.zip", "w") as archive:
        for path in source.glob(f"{name}.*"):
            if path.suffix != ".zip":
                archive.write(path, path.name)


def test_region_builder_filters_state_and_stays_under_budget(tmp_path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    _archive(
        source,
        "zones",
        [("STATE", "C"), ("ZONE", "C"), ("CWA", "C"), ("NAME", "C"), ("STATE_ZONE", "C")],
        ["PA", "001", "PBZ", "Test zone", "PA001"],
    )
    _archive(
        source,
        "counties",
        [("STATE", "C"), ("CWA", "C"), ("COUNTYNAME", "C"), ("FIPS", "C")],
        ["PA", "PBZ", "Test County", "42001"],
    )
    target = build("PA", tmp_path / "output", source)
    payload = json.loads(target.read_text())
    assert len(payload["features"]) == 2
    assert {item["properties"]["state"] for item in payload["features"]} == {"PA"}
    assert target.stat().st_size < 5_000_000


def test_bundled_pennsylvania_region_pack_is_valid_and_under_budget() -> None:
    target = Path(__file__).parents[2] / "data/regions/nws-boundaries-pa.geojson"
    payload = json.loads(target.read_text())
    assert payload["outpost_region"] == "PA"
    assert payload["features"]
    assert target.stat().st_size < 5_000_000
