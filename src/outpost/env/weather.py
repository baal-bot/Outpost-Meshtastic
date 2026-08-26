from __future__ import annotations

import asyncio
import gzip
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Protocol, cast

from outpost.clock import Clock
from outpost.config import EnvConfig
from outpost.store import Database

OPEN_METEO_HOST = "api.open-meteo.com"
NWS_HOST = "api.weather.gov"
_HTTP_CACHE: dict[str, tuple[dict[str, Any], str | None, str | None]] = {}
ENV_CACHE_MAX = 1_000
HTTP_CACHE_MAX = 1_000


async def _request_json(url: str, host: str, config: EnvConfig) -> dict[str, Any]:
    if urllib.parse.urlparse(url).hostname != host:
        raise ValueError("weather provider host is not allowed")

    def request() -> dict[str, Any]:
        headers = {
            "User-Agent": config.user_agent,
            "Accept": "application/geo+json",
            "Accept-Encoding": "gzip",
        }
        cached = _HTTP_CACHE.get(url)
        if cached:
            if cached[1]:
                headers["If-None-Match"] = cached[1]
            if cached[2]:
                headers["If-Modified-Since"] = cached[2]
        try:
            with urllib.request.urlopen(  # noqa: S310 - caller supplies a fixed, checked host.
                urllib.request.Request(url, headers=headers),  # noqa: S310 - checked HTTPS host.
                timeout=config.request_timeout_s,
            ) as response:
                payload = response.read(1_000_000)
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    payload = gzip.decompress(payload)
                value = cast(dict[str, Any], json.loads(payload))
                _HTTP_CACHE[url] = (
                    value,
                    response.headers.get("ETag"),
                    response.headers.get("Last-Modified"),
                )
                while len(_HTTP_CACHE) > HTTP_CACHE_MAX:
                    _HTTP_CACHE.pop(next(iter(_HTTP_CACHE)))
                return value
        except urllib.error.HTTPError as error:
            if error.code == 304 and cached:
                return cached[0]
            raise

    return await asyncio.to_thread(request)


@dataclass(frozen=True)
class WeatherSnapshot:
    provider: str
    temperature_c: float
    apparent_c: float
    precipitation_mm: float
    wind_kph: float
    wind_direction: int
    weather_code: int
    observed_at: str
    fetched_at: int
    age_seconds: int = 0
    stale: bool = False

    def json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ForecastSnapshot:
    provider: str
    daily: list[dict[str, Any]]
    hourly: list[dict[str, Any]]
    fetched_at: int
    age_seconds: int = 0
    stale: bool = False

    def json(self) -> dict[str, Any]:
        return asdict(self)


class WeatherProvider(Protocol):
    name: str

    async def fetch(self, lat: float, lon: float) -> dict[str, Any]: ...

    async def forecast(self, lat: float, lon: float) -> dict[str, Any]: ...


class OpenMeteoProvider:
    name = "open-meteo"

    def __init__(self, config: EnvConfig) -> None:
        self.config = config

    async def fetch(self, lat: float, lon: float) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {
                "latitude": lat,
                "longitude": lon,
                "current": (
                    "temperature_2m,apparent_temperature,precipitation,weather_code,"
                    "wind_speed_10m,wind_direction_10m"
                ),
                "temperature_unit": "celsius",
                "wind_speed_unit": "kmh",
                "timezone": "auto",
            }
        )
        url = f"https://{OPEN_METEO_HOST}/v1/forecast?{query}"
        value = await _request_json(url, OPEN_METEO_HOST, self.config)
        current = value["current"]
        return {
            "temperature_c": float(current["temperature_2m"]),
            "apparent_c": float(current["apparent_temperature"]),
            "precipitation_mm": float(current["precipitation"]),
            "wind_kph": float(current["wind_speed_10m"]),
            "wind_direction": int(current["wind_direction_10m"]),
            "weather_code": int(current["weather_code"]),
            "observed_at": str(current["time"]),
        }

    async def forecast(self, lat: float, lon: float) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {
                "latitude": lat,
                "longitude": lon,
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max,wind_speed_10m_max,"
                    "wind_direction_10m_dominant"
                ),
                "hourly": "temperature_2m,precipitation_probability,weather_code",
                "temperature_unit": "celsius",
                "wind_speed_unit": "kmh",
                "forecast_days": 7,
                "timezone": "auto",
            }
        )
        value = await _request_json(
            f"https://{OPEN_METEO_HOST}/v1/forecast?{query}", OPEN_METEO_HOST, self.config
        )
        daily = value["daily"]
        hourly = value["hourly"]
        summaries = {
            0: "Clear",
            1: "Mostly clear",
            2: "Partly cloudy",
            3: "Cloudy",
            45: "Fog",
            48: "Icy fog",
            51: "Light drizzle",
            53: "Drizzle",
            55: "Heavy drizzle",
            61: "Light rain",
            63: "Rain",
            65: "Heavy rain",
            71: "Light snow",
            73: "Snow",
            75: "Heavy snow",
            80: "Rain showers",
            81: "Rain showers",
            82: "Heavy showers",
            85: "Snow showers",
            95: "Thunderstorms",
            96: "Storms with hail",
            99: "Storms with hail",
        }
        days = [
            {
                "name": str(day),
                "start_time": str(day),
                "high_c": float(daily["temperature_2m_max"][index]),
                "low_c": float(daily["temperature_2m_min"][index]),
                "precipitation_probability": int(
                    daily["precipitation_probability_max"][index] or 0
                ),
                "wind_kph": float(daily["wind_speed_10m_max"][index]),
                "wind_direction": int(daily["wind_direction_10m_dominant"][index]),
                "summary": summaries.get(int(daily["weather_code"][index]), "Mixed conditions"),
            }
            for index, day in enumerate(daily["time"])
        ]
        hours = [
            {
                "start_time": str(stamp),
                "temperature_c": float(hourly["temperature_2m"][index]),
                "precipitation_probability": int(hourly["precipitation_probability"][index] or 0),
                "summary": summaries.get(int(hourly["weather_code"][index]), "Mixed"),
            }
            for index, stamp in enumerate(hourly["time"][:24])
        ]
        return {"daily": days, "hourly": hours}


class NWSProvider:
    name = "nws"

    def __init__(self, config: EnvConfig) -> None:
        self.config = config
        self._forecast_urls: dict[tuple[float, float], dict[str, str]] = {}

    async def _urls(self, lat: float, lon: float) -> dict[str, str]:
        key = (round(lat, 4), round(lon, 4))
        urls = self._forecast_urls.get(key)
        if urls is None:
            point = await _request_json(
                f"https://{NWS_HOST}/points/{lat:.4f},{lon:.4f}", NWS_HOST, self.config
            )
            properties = point["properties"]
            urls = {
                "hourly": str(properties["forecastHourly"]),
                "daily": str(properties["forecast"]),
            }
            if any(urllib.parse.urlparse(url).hostname != NWS_HOST for url in urls.values()):
                raise ValueError("NWS returned a forecast host outside the allowlist")
            self._forecast_urls[key] = urls
            while len(self._forecast_urls) > HTTP_CACHE_MAX:
                self._forecast_urls.pop(next(iter(self._forecast_urls)))
        return urls

    @staticmethod
    def _direction(value: str) -> int:
        points = {
            name: index * 22.5
            for index, name in enumerate(
                (
                    "N",
                    "NNE",
                    "NE",
                    "ENE",
                    "E",
                    "ESE",
                    "SE",
                    "SSE",
                    "S",
                    "SSW",
                    "SW",
                    "WSW",
                    "W",
                    "WNW",
                    "NW",
                    "NNW",
                )
            )
        }
        return round(points.get(value.upper(), 0))

    async def fetch(self, lat: float, lon: float) -> dict[str, Any]:
        urls = await self._urls(lat, lon)
        forecast = await _request_json(urls["hourly"], NWS_HOST, self.config)
        period = forecast["properties"]["periods"][0]
        temperature = float(period["temperature"])
        if str(period.get("temperatureUnit", "F")).upper() == "F":
            temperature = (temperature - 32) * 5 / 9
        wind_match = re.search(r"\d+(?:\.\d+)?", str(period.get("windSpeed", "0")))
        wind_mph = float(wind_match.group()) if wind_match else 0.0
        return {
            "temperature_c": temperature,
            "apparent_c": temperature,
            "precipitation_mm": 0.0,
            "wind_kph": wind_mph * 1.609344,
            "wind_direction": self._direction(str(period.get("windDirection", "N"))),
            "weather_code": 0,
            "observed_at": str(period["startTime"]),
        }

    async def forecast(self, lat: float, lon: float) -> dict[str, Any]:
        urls = await self._urls(lat, lon)
        daily_value, hourly_value = await asyncio.gather(
            _request_json(urls["daily"], NWS_HOST, self.config),
            _request_json(urls["hourly"], NWS_HOST, self.config),
        )

        def temperature_c(period: dict[str, Any]) -> float:
            value = float(period["temperature"])
            return (
                (value - 32) * 5 / 9
                if str(period.get("temperatureUnit", "F")).upper() == "F"
                else value
            )

        nws_periods = daily_value["properties"]["periods"]
        days: list[dict[str, Any]] = []
        if nws_periods and not nws_periods[0].get("isDaytime", True):
            tonight = nws_periods[0]
            tonight_temperature = temperature_c(tonight)
            days.append(
                {
                    "name": str(tonight["name"]),
                    "start_time": str(tonight["startTime"]),
                    "high_c": tonight_temperature,
                    "low_c": tonight_temperature,
                    "precipitation_probability": int(
                        (tonight.get("probabilityOfPrecipitation") or {}).get("value") or 0
                    ),
                    "wind_kph": 0.0,
                    "wind_direction": self._direction(str(tonight.get("windDirection", "N"))),
                    "summary": str(tonight.get("shortForecast", "Forecast unavailable")),
                }
            )
        for period in nws_periods:
            if not period.get("isDaytime", False):
                continue
            following = next(
                (
                    item
                    for item in nws_periods
                    if not item.get("isDaytime", True) and item["startTime"] >= period["startTime"]
                ),
                period,
            )
            wind_match = re.search(r"\d+(?:\.\d+)?", str(period.get("windSpeed", "0")))
            days.append(
                {
                    "name": str(period["name"]),
                    "start_time": str(period["startTime"]),
                    "high_c": temperature_c(period),
                    "low_c": temperature_c(following),
                    "precipitation_probability": int(
                        (period.get("probabilityOfPrecipitation") or {}).get("value") or 0
                    ),
                    "wind_kph": (float(wind_match.group()) if wind_match else 0.0) * 1.609344,
                    "wind_direction": self._direction(str(period.get("windDirection", "N"))),
                    "summary": str(period.get("shortForecast", "Forecast unavailable")),
                }
            )
        hours = [
            {
                "start_time": str(period["startTime"]),
                "temperature_c": temperature_c(period),
                "precipitation_probability": int(
                    (period.get("probabilityOfPrecipitation") or {}).get("value") or 0
                ),
                "summary": str(period.get("shortForecast", "Mixed")),
            }
            for period in hourly_value["properties"]["periods"][:24]
        ]
        return {"daily": days[:7], "hourly": hours}


class FallbackWeatherProvider:
    name = "weather-chain"

    def __init__(self, providers: list[WeatherProvider]) -> None:
        self.providers = providers
        self.last_provider = providers[0].name
        self.health: dict[str, dict[str, Any]] = {
            provider.name: {"status": "standby", "failures": 0, "last_error": None}
            for provider in providers
        }

    def mark_cached(self, provider_name: str) -> None:
        """Report cached startup state without claiming a live probe succeeded."""
        if provider_name in self.health:
            self.last_provider = provider_name
            self.health[provider_name].update({"status": "cached", "last_error": None})

    async def fetch(self, lat: float, lon: float) -> dict[str, Any]:
        return await self._call("fetch", lat, lon)

    async def forecast(self, lat: float, lon: float) -> dict[str, Any]:
        return await self._call("forecast", lat, lon)

    async def _call(self, method: str, lat: float, lon: float) -> dict[str, Any]:
        last_error: Exception | None = None
        for provider in self.providers:
            try:
                function = cast(Any, getattr(provider, method))
                value = cast(dict[str, Any], await function(lat, lon))
            except (OSError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as error:
                last_error = error
                state = self.health[provider.name]
                state.update(
                    {
                        "status": "down",
                        "failures": int(state["failures"]) + 1,
                        "last_error": str(error)[:160],
                    }
                )
                continue
            self.last_provider = provider.name
            self.health[provider.name].update({"status": "up", "last_error": None})
            return value
        raise OSError(str(last_error or "all weather providers failed"))


class WeatherService:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        config: EnvConfig,
        provider: WeatherProvider,
    ) -> None:
        self.database, self.clock, self.config, self.provider = database, clock, config, provider

    async def _cached(self, key: str) -> tuple[dict[str, Any], str, int, int] | None:
        rows = await self.database.read(
            "SELECT payload,provider,fetched_at,expires_at FROM env_cache WHERE cache_key=?", (key,)
        )
        if not rows:
            return None
        return (
            json.loads(rows[0]["payload"]),
            str(rows[0]["provider"]),
            int(rows[0]["fetched_at"]),
            int(rows[0]["expires_at"]),
        )

    def _mark_cached_provider(self, provider_name: str) -> None:
        marker = getattr(self.provider, "mark_cached", None)
        if marker is not None:
            marker(provider_name)

    async def current(self, lat: float, lon: float, *, refresh: bool = False) -> WeatherSnapshot:
        key = f"weather:{lat:.4f},{lon:.4f}"
        now = int(self.clock.now().timestamp())
        cached = await self._cached(key)
        if cached and not refresh and cached[3] > now:
            self._mark_cached_provider(cached[1])
            return self._snapshot(cached[0], cached[1], cached[2], now)
        try:
            payload = await self.provider.fetch(lat, lon)
        except (OSError, TimeoutError, ValueError, KeyError, json.JSONDecodeError):
            if cached and now - cached[2] <= self.config.max_age_hours * 3600:
                self._mark_cached_provider(cached[1])
                return self._snapshot(cached[0], cached[1], cached[2], now, stale=True)
            raise RuntimeError("Weather unavailable; no safe cached forecast.") from None
        await self.database.write(
            """INSERT INTO env_cache(cache_key,provider,payload,fetched_at,expires_at)
               VALUES(?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET
               provider=excluded.provider,payload=excluded.payload,
               fetched_at=excluded.fetched_at,expires_at=excluded.expires_at""",
            (
                key,
                getattr(self.provider, "last_provider", self.provider.name),
                json.dumps(payload, separators=(",", ":")),
                now,
                now + self.config.refresh_minutes * 60,
            ),
        )
        await self.database.write(
            "DELETE FROM env_cache WHERE cache_key IN (SELECT cache_key FROM env_cache "
            "ORDER BY fetched_at DESC LIMIT -1 OFFSET ?)",
            (ENV_CACHE_MAX,),
        )
        return self._snapshot(
            payload, getattr(self.provider, "last_provider", self.provider.name), now, now
        )

    async def forecast(self, lat: float, lon: float, *, refresh: bool = False) -> ForecastSnapshot:
        key = f"forecast:{lat:.4f},{lon:.4f}"
        now = int(self.clock.now().timestamp())
        cached = await self._cached(key)
        if cached and not refresh and cached[3] > now:
            self._mark_cached_provider(cached[1])
            return self._forecast_snapshot(cached[0], cached[1], cached[2], now)
        try:
            payload = await self.provider.forecast(lat, lon)
        except (OSError, TimeoutError, ValueError, KeyError, AttributeError, json.JSONDecodeError):
            if cached and now - cached[2] <= self.config.max_age_hours * 3600:
                self._mark_cached_provider(cached[1])
                return self._forecast_snapshot(cached[0], cached[1], cached[2], now, stale=True)
            raise RuntimeError("Forecast unavailable; no safe cached forecast.") from None
        provider = getattr(self.provider, "last_provider", self.provider.name)
        await self.database.write(
            """INSERT INTO env_cache(cache_key,provider,payload,fetched_at,expires_at)
               VALUES(?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET
               provider=excluded.provider,payload=excluded.payload,
               fetched_at=excluded.fetched_at,expires_at=excluded.expires_at""",
            (
                key,
                provider,
                json.dumps(payload, separators=(",", ":")),
                now,
                now + self.config.refresh_minutes * 60,
            ),
        )
        await self.database.write(
            "DELETE FROM env_cache WHERE cache_key IN (SELECT cache_key FROM env_cache "
            "ORDER BY fetched_at DESC LIMIT -1 OFFSET ?)",
            (ENV_CACHE_MAX,),
        )
        return self._forecast_snapshot(payload, provider, now, now)

    @staticmethod
    def _forecast_snapshot(
        payload: dict[str, Any], provider: str, fetched_at: int, now: int, *, stale: bool = False
    ) -> ForecastSnapshot:
        return ForecastSnapshot(
            provider,
            payload["daily"],
            payload["hourly"],
            fetched_at,
            max(0, now - fetched_at),
            stale,
        )

    def _snapshot(
        self,
        payload: dict[str, Any],
        provider: str,
        fetched_at: int,
        now: int,
        *,
        stale: bool = False,
    ) -> WeatherSnapshot:
        return WeatherSnapshot(
            provider=provider,
            fetched_at=fetched_at,
            age_seconds=max(0, now - fetched_at),
            stale=stale,
            **payload,
        )

    def provider_health(self) -> dict[str, dict[str, Any]]:
        return getattr(
            self.provider,
            "health",
            {self.provider.name: {"status": "unknown", "failures": 0, "last_error": None}},
        )
