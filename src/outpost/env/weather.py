from __future__ import annotations

import asyncio
import gzip
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from outpost.clock import Clock
from outpost.config import EnvConfig
from outpost.store import Database

OPEN_METEO_HOST = "api.open-meteo.com"
NWS_HOST = "api.weather.gov"
_HTTP_CACHE: dict[str, tuple[dict[str, Any], str | None, str | None]] = {}
ENV_CACHE_MAX = 1_000
HTTP_CACHE_MAX = 1_000


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    parsed = _optional_float(value)
    return round(parsed) if parsed is not None else None


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
    source_kind: str
    temperature_c: float | None
    apparent_c: float | None
    precipitation_mm: float | None
    wind_kph: float | None
    wind_direction: int | None
    weather_code: int | None
    observed_at: str
    fetched_at: int
    summary: str | None = None
    source_detail: str | None = None
    valid_age_seconds: int | None = None
    age_seconds: int = 0
    stale: bool = False

    def json(self) -> dict[str, Any]:
        value = asdict(self)
        value["valid_at"] = self.observed_at
        units = {
            "temperature_c": "°C",
            "apparent_c": "°C",
            "precipitation_mm": "mm",
            "wind_kph": "km/h",
            "wind_direction": "degrees",
            "weather_code": "WMO code",
        }
        value["measurements"] = {
            name: {
                "value": getattr(self, name),
                "unit": unit,
                "available": getattr(self, name) is not None,
                "unavailable": getattr(self, name) is None,
                "provider": self.provider,
                "source_kind": self.source_kind,
                "valid_at": self.observed_at,
                "age_seconds": self.valid_age_seconds,
                "cached": self.stale,
            }
            for name, unit in units.items()
        }
        return value


@dataclass(frozen=True)
class ForecastSnapshot:
    provider: str
    daily: list[dict[str, Any]]
    hourly: list[dict[str, Any]]
    fetched_at: int
    source_kind: str = "forecast"
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
                "timezone": "GMT",
            }
        )
        url = f"https://{OPEN_METEO_HOST}/v1/forecast?{query}"
        value = await _request_json(url, OPEN_METEO_HOST, self.config)
        current = value["current"]
        return {
            "source_kind": "estimate",
            "temperature_c": _optional_float(current.get("temperature_2m")),
            "apparent_c": _optional_float(current.get("apparent_temperature")),
            "precipitation_mm": _optional_float(current.get("precipitation")),
            "wind_kph": _optional_float(current.get("wind_speed_10m")),
            "wind_direction": _optional_int(current.get("wind_direction_10m")),
            "weather_code": _optional_int(current.get("weather_code")),
            "observed_at": str(current["time"]),
            "summary": "Model-derived current conditions",
            "source_detail": "Open-Meteo current model",
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

        def summary(value: object, fallback: str) -> str:
            code = _optional_int(value)
            return summaries.get(code, fallback) if code is not None else "Unavailable"

        days = [
            {
                "name": str(day),
                "start_time": str(day),
                "high_c": _optional_float(daily["temperature_2m_max"][index]),
                "low_c": _optional_float(daily["temperature_2m_min"][index]),
                "precipitation_probability": _optional_int(
                    daily["precipitation_probability_max"][index]
                ),
                "wind_kph": _optional_float(daily["wind_speed_10m_max"][index]),
                "wind_direction": _optional_int(daily["wind_direction_10m_dominant"][index]),
                "summary": summary(daily["weather_code"][index], "Mixed conditions"),
            }
            for index, day in enumerate(daily["time"])
        ]
        hours = [
            {
                "start_time": str(stamp),
                "temperature_c": _optional_float(hourly["temperature_2m"][index]),
                "precipitation_probability": _optional_int(
                    hourly["precipitation_probability"][index]
                ),
                "summary": summary(hourly["weather_code"][index], "Mixed"),
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
            if properties.get("observationStations"):
                urls["stations"] = str(properties["observationStations"])
            if any(urllib.parse.urlparse(url).hostname != NWS_HOST for url in urls.values()):
                raise ValueError("NWS returned a forecast host outside the allowlist")
            self._forecast_urls[key] = urls
            while len(self._forecast_urls) > HTTP_CACHE_MAX:
                self._forecast_urls.pop(next(iter(self._forecast_urls)))
        return urls

    @staticmethod
    def _direction(value: object) -> int | None:
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
        if not isinstance(value, str):
            return None
        direction = points.get(value.upper())
        return round(direction) if direction is not None else None

    @staticmethod
    def _quantity(properties: dict[str, Any], name: str) -> tuple[float | None, str]:
        quantity = properties.get(name)
        if not isinstance(quantity, dict):
            return None, ""
        return _optional_float(quantity.get("value")), str(quantity.get("unitCode") or "")

    @classmethod
    def _temperature(cls, properties: dict[str, Any], name: str) -> float | None:
        value, unit = cls._quantity(properties, name)
        if value is None:
            return None
        if unit.endswith("degF"):
            return (value - 32) * 5 / 9
        return value

    @classmethod
    def _wind_kph(cls, properties: dict[str, Any]) -> float | None:
        value, unit = cls._quantity(properties, "windSpeed")
        if value is None:
            return None
        if unit.endswith("m_s-1"):
            return value * 3.6
        if unit.endswith("mi_h-1"):
            return value * 1.609344
        if unit.endswith("kt"):
            return value * 1.852
        return value

    @classmethod
    def _precipitation_mm(cls, properties: dict[str, Any]) -> float | None:
        value, unit = cls._quantity(properties, "precipitationLastHour")
        if value is None:
            return None
        if unit.endswith(":m"):
            return value * 1_000
        if unit.endswith(":cm"):
            return value * 10
        if unit.endswith("[in_i]"):
            return value * 25.4
        return value

    async def _observation(self, urls: dict[str, str]) -> dict[str, Any] | None:
        stations_url = urls.get("stations")
        if not stations_url:
            return None
        stations = await _request_json(stations_url, NWS_HOST, self.config)
        features = stations.get("features")
        if not isinstance(features, list) or not features:
            return None
        station = features[0]
        if not isinstance(station, dict):
            return None
        station_url = str(station.get("id") or station.get("@id") or "").rstrip("/")
        if urllib.parse.urlparse(station_url).hostname != NWS_HOST:
            raise ValueError("NWS returned an observation station outside the allowlist")
        latest = await _request_json(f"{station_url}/observations/latest", NWS_HOST, self.config)
        properties = latest.get("properties")
        if not isinstance(properties, dict):
            return None
        temperature = self._temperature(properties, "temperature")
        apparent = self._temperature(properties, "heatIndex")
        if apparent is None:
            apparent = self._temperature(properties, "windChill")
        wind_direction, _unit = self._quantity(properties, "windDirection")
        values = (
            temperature,
            apparent,
            self._precipitation_mm(properties),
            self._wind_kph(properties),
            wind_direction,
        )
        if not any(value is not None for value in values):
            return None
        station_properties = station.get("properties")
        station_name = (
            str(station_properties.get("name"))
            if isinstance(station_properties, dict) and station_properties.get("name")
            else station_url.rsplit("/", 1)[-1]
        )
        return {
            "source_kind": "observation",
            "temperature_c": temperature,
            "apparent_c": apparent,
            "precipitation_mm": values[2],
            "wind_kph": values[3],
            "wind_direction": round(wind_direction) if wind_direction is not None else None,
            "weather_code": None,
            "observed_at": str(properties.get("timestamp") or ""),
            "summary": str(properties.get("textDescription") or "Observed conditions"),
            "source_detail": station_name,
        }

    async def _near_term_forecast(self, urls: dict[str, str]) -> dict[str, Any]:
        forecast = await _request_json(urls["hourly"], NWS_HOST, self.config)
        periods = forecast.get("properties", {}).get("periods", [])
        if not periods:
            raise ValueError("NWS returned no hourly forecast periods")
        period = periods[0]
        temperature = _optional_float(period.get("temperature"))
        if temperature is not None and str(period.get("temperatureUnit", "F")).upper() == "F":
            temperature = (temperature - 32) * 5 / 9
        wind_match = re.search(r"\d+(?:\.\d+)?", str(period.get("windSpeed") or ""))
        wind_mph = float(wind_match.group()) if wind_match else None
        return {
            "source_kind": "forecast",
            "temperature_c": temperature,
            "apparent_c": None,
            "precipitation_mm": None,
            "wind_kph": wind_mph * 1.609344 if wind_mph is not None else None,
            "wind_direction": self._direction(period.get("windDirection")),
            "weather_code": None,
            "observed_at": str(period["startTime"]),
            "summary": str(period.get("shortForecast") or "Near-term forecast"),
            "source_detail": str(period.get("name") or "NWS hourly period"),
        }

    async def fetch(self, lat: float, lon: float) -> dict[str, Any]:
        urls = await self._urls(lat, lon)
        observation = await self._observation(urls)
        return observation if observation is not None else await self._near_term_forecast(urls)

    async def forecast(self, lat: float, lon: float) -> dict[str, Any]:
        urls = await self._urls(lat, lon)
        daily_value, hourly_value = await asyncio.gather(
            _request_json(urls["daily"], NWS_HOST, self.config),
            _request_json(urls["hourly"], NWS_HOST, self.config),
        )

        def temperature_c(period: dict[str, Any]) -> float | None:
            value = _optional_float(period.get("temperature"))
            if value is None:
                return None
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
            wind_match = re.search(r"\d+(?:\.\d+)?", str(tonight.get("windSpeed") or ""))
            days.append(
                {
                    "name": str(tonight["name"]),
                    "start_time": str(tonight["startTime"]),
                    "high_c": tonight_temperature,
                    "low_c": tonight_temperature,
                    "precipitation_probability": _optional_int(
                        (tonight.get("probabilityOfPrecipitation") or {}).get("value")
                    ),
                    "wind_kph": (float(wind_match.group()) * 1.609344 if wind_match else None),
                    "wind_direction": self._direction(tonight.get("windDirection")),
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
            wind_match = re.search(r"\d+(?:\.\d+)?", str(period.get("windSpeed") or ""))
            days.append(
                {
                    "name": str(period["name"]),
                    "start_time": str(period["startTime"]),
                    "high_c": temperature_c(period),
                    "low_c": temperature_c(following),
                    "precipitation_probability": _optional_int(
                        (period.get("probabilityOfPrecipitation") or {}).get("value")
                    ),
                    "wind_kph": (float(wind_match.group()) * 1.609344 if wind_match else None),
                    "wind_direction": self._direction(period.get("windDirection")),
                    "summary": str(period.get("shortForecast", "Forecast unavailable")),
                }
            )
        hours = [
            {
                "start_time": str(period["startTime"]),
                "temperature_c": temperature_c(period),
                "precipitation_probability": _optional_int(
                    (period.get("probabilityOfPrecipitation") or {}).get("value")
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
            raise RuntimeError("Weather unavailable; no safe cached conditions.") from None
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
            provider=provider,
            daily=payload["daily"],
            hourly=payload["hourly"],
            fetched_at=fetched_at,
            age_seconds=max(0, now - fetched_at),
            stale=stale,
        )

    @staticmethod
    def _valid_age(value: object, now: int) -> int | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0, now - int(parsed.timestamp()))

    def _snapshot(
        self,
        payload: dict[str, Any],
        provider: str,
        fetched_at: int,
        now: int,
        *,
        stale: bool = False,
    ) -> WeatherSnapshot:
        normalized = dict(payload)
        normalized.setdefault(
            "source_kind",
            "forecast"
            if provider == "nws"
            else "estimate"
            if provider == "open-meteo"
            else "observation",
        )
        normalized.setdefault("summary", None)
        normalized.setdefault("source_detail", None)
        observed_at = normalized.get("observed_at") or normalized.pop("valid_at", "")
        normalized["observed_at"] = str(observed_at)
        return WeatherSnapshot(
            provider=provider,
            fetched_at=fetched_at,
            valid_age_seconds=self._valid_age(observed_at, now),
            age_seconds=max(0, now - fetched_at),
            stale=stale,
            **normalized,
        )

    def provider_health(self) -> dict[str, dict[str, Any]]:
        return getattr(
            self.provider,
            "health",
            {self.provider.name: {"status": "unknown", "failures": 0, "last_error": None}},
        )
