import io
import json
import urllib.error

import pytest

from outpost.clock import VirtualClock
from outpost.config import EnvConfig
from outpost.env import AstronomyService, FallbackWeatherProvider, WeatherService
from outpost.env.weather import _HTTP_CACHE, _request_json
from outpost.store import Database


class FakeProvider:
    name = "fake-weather"

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def fetch(self, lat: float, lon: float) -> dict[str, object]:
        self.calls += 1
        if self.fail:
            raise OSError("WAN down")
        return {
            "temperature_c": 21.5,
            "apparent_c": 20.8,
            "precipitation_mm": 0.2,
            "wind_kph": 12.0,
            "wind_direction": 245,
            "weather_code": 3,
            "observed_at": "2026-08-24T20:00",
        }

    async def forecast(self, lat: float, lon: float) -> dict[str, object]:
        self.calls += 1
        if self.fail:
            raise OSError("WAN down")
        return {
            "daily": [
                {
                    "name": "Today",
                    "start_time": "2026-08-24T06:00:00-04:00",
                    "high_c": 24.0,
                    "low_c": 15.0,
                    "precipitation_probability": 30,
                    "wind_kph": 14.0,
                    "wind_direction": 245,
                    "summary": "Partly cloudy",
                }
            ],
            "hourly": [
                {
                    "start_time": "2026-08-24T21:00:00-04:00",
                    "temperature_c": 20.0,
                    "precipitation_probability": 10,
                    "summary": "Clear",
                }
            ],
        }


class DownProvider:
    name = "nws"

    async def fetch(self, lat: float, lon: float) -> dict[str, object]:
        raise OSError("NWS unavailable")

    async def forecast(self, lat: float, lon: float) -> dict[str, object]:
        raise OSError("NWS unavailable")


@pytest.mark.asyncio
async def test_provider_conditional_request_reuses_body_on_304(monkeypatch) -> None:
    class Response:
        def __init__(self) -> None:
            self.headers = {
                "ETag": '"forecast-1"',
                "Last-Modified": "Mon, 24 Aug 2026 20:00:00 GMT",
            }

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit: int) -> bytes:
            return json.dumps({"value": 42}).encode()

    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        if len(requests) == 1:
            return Response()
        raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", {}, io.BytesIO())

    _HTTP_CACHE.clear()
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    config = EnvConfig(user_agent="Outpost test (operator: test@example.org)")
    url = "https://api.weather.gov/test"
    assert await _request_json(url, "api.weather.gov", config) == {"value": 42}
    assert await _request_json(url, "api.weather.gov", config) == {"value": 42}
    assert requests[1].get_header("If-none-match") == '"forecast-1"'
    assert requests[1].get_header("If-modified-since") == "Mon, 24 Aug 2026 20:00:00 GMT"


@pytest.mark.asyncio
async def test_weather_cache_age_labels_and_safe_wan_failure(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock, provider = VirtualClock(), FakeProvider()
    service = WeatherService(database, clock, EnvConfig(refresh_minutes=15), provider)

    fresh = await service.current(40.4406, -79.9959)
    assert fresh.temperature_c == 21.5 and not fresh.stale and provider.calls == 1
    assert (await service.current(40.4406, -79.9959)).age_seconds == 0
    assert provider.calls == 1

    clock.advance(16 * 60)
    provider.fail = True
    stale = await service.current(40.4406, -79.9959)
    assert stale.stale and stale.age_seconds == 16 * 60

    clock.advance(6 * 3600)
    with pytest.raises(RuntimeError, match="no safe cached"):
        await service.current(40.4406, -79.9959)
    await database.close()


@pytest.mark.asyncio
async def test_weather_falls_back_and_records_provider_health(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    fallback = FakeProvider()
    chain = FallbackWeatherProvider([DownProvider(), fallback])
    service = WeatherService(database, VirtualClock(), EnvConfig(), chain)

    value = await service.current(40.4406, -79.9959)

    assert value.provider == "fake-weather"
    assert chain.health["nws"]["status"] == "down"
    assert chain.health["fake-weather"]["status"] == "up"
    assert chain.health["nws"]["failures"] == 1
    await database.close()


@pytest.mark.asyncio
async def test_forecast_cache_fallback_and_safe_stale_result(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock, fallback = VirtualClock(), FakeProvider()
    chain = FallbackWeatherProvider([DownProvider(), fallback])
    service = WeatherService(database, clock, EnvConfig(refresh_minutes=15), chain)

    fresh = await service.forecast(40.4406, -79.9959)
    assert fresh.provider == "fake-weather"
    assert fresh.daily[0]["summary"] == "Partly cloudy"
    assert fallback.calls == 1
    assert (await service.forecast(40.4406, -79.9959)).age_seconds == 0
    assert fallback.calls == 1

    restarted_chain = FallbackWeatherProvider([DownProvider(), FakeProvider()])
    restarted = WeatherService(database, clock, EnvConfig(refresh_minutes=15), restarted_chain)
    await restarted.forecast(40.4406, -79.9959)
    assert restarted_chain.health["fake-weather"]["status"] == "cached"
    assert restarted_chain.health["nws"]["status"] == "standby"

    clock.advance(16 * 60)
    fallback.fail = True
    stale = await service.forecast(40.4406, -79.9959)
    assert stale.stale and stale.age_seconds == 16 * 60
    await database.close()


@pytest.mark.asyncio
async def test_full_wan_down_day_degrades_safely_and_sun_remains_available(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock, provider = VirtualClock(), FakeProvider()
    service = WeatherService(database, clock, EnvConfig(max_age_hours=6), provider)
    await service.current(40.4406, -79.9959)
    provider.fail = True

    for hour in range(1, 25):
        clock.advance(3600)
        if hour <= 6:
            value = await service.current(40.4406, -79.9959)
            assert value.stale and value.age_seconds == hour * 3600
        else:
            with pytest.raises(RuntimeError, match="no safe cached"):
                await service.current(40.4406, -79.9959)
        astronomy = AstronomyService(clock).current(40.4406, -79.9959, "America/New_York")
        assert astronomy.sunrise is not None and astronomy.sunset is not None
    await database.close()
