import pytest

from outpost.clock import VirtualClock
from outpost.config import EnvConfig
from outpost.env import SeismicService
from outpost.store import Database


def quake(usgs_id: str, *, updated: int = 1_767_225_700_000, lon: float = -80.1) -> dict:
    return {
        "id": usgs_id,
        "properties": {
            "mag": 4.8,
            "place": "10 km SW of Pittsburgh, Pennsylvania",
            "time": 1_767_225_600_000,
            "updated": updated,
            "url": f"https://earthquake.usgs.gov/earthquakes/eventpage/{usgs_id}",
        },
        "geometry": {"type": "Point", "coordinates": [lon, 40.4, 8.2]},
    }


@pytest.mark.asyncio
async def test_seismic_filters_radius_deduplicates_and_tracks_updates(
    tmp_path, monkeypatch
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    payload = {"features": [quake("us-test"), quake("far-away", lon=-120.0)]}

    async def request(*args, **kwargs):
        return payload

    monkeypatch.setattr("outpost.env.seismic._request_json", request)
    service = SeismicService(database, VirtualClock(), EnvConfig(earthquake_radius_km=500))

    first = await service.poll(40.4406, -79.9959)
    assert first == {"seen": 2, "nearby": 1, "updated": 0, "review": 1}
    values = await service.list()
    assert len(values) == 1
    assert values[0]["review_state"] == "pending"
    assert values[0]["distance_km"] < 20
    assert 180 <= values[0]["bearing_deg"] <= 270

    second = await service.poll(40.4406, -79.9959)
    assert second["updated"] == 0
    payload["features"][0] = quake("us-test", updated=1_767_225_800_000)
    revised = await service.poll(40.4406, -79.9959)
    assert revised["updated"] == 1
    assert len(await service.list()) == 1
    await database.close()


@pytest.mark.asyncio
async def test_seismic_retains_durable_results_when_provider_fails(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = SeismicService(database, VirtualClock(), EnvConfig())

    async def working(*args, **kwargs):
        return {"features": [quake("us-cached")]}

    monkeypatch.setattr("outpost.env.seismic._request_json", working)
    await service.poll(40.4406, -79.9959)

    async def down(*args, **kwargs):
        raise OSError("WAN down")

    monkeypatch.setattr("outpost.env.seismic._request_json", down)
    with pytest.raises(OSError, match="WAN down"):
        await service.poll(40.4406, -79.9959)
    assert len(await service.list()) == 1
    assert service.health()["last_error"] == "WAN down"
    await database.close()


def test_seismic_distance_and_bearing_cardinals() -> None:
    north, north_bearing = SeismicService.distance_bearing(40, -80, 41, -80)
    east, east_bearing = SeismicService.distance_bearing(40, -80, 40, -79)
    assert 110 < north < 112 and north_bearing == 0
    assert 84 < east < 86 and 89 <= east_bearing <= 91
