from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

MAX_LATITUDE = 85.05112878
OVERVIEW_ZOOM = 6
MAX_TILES = 60_000
MAX_DOWNLOAD_BYTES = 1024**3


class MapError(ValueError):
    """An actionable map setup or validation failure, safe to display to an operator."""


def coordinate(value: object, limit: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MapError("Enter numeric latitude and longitude.")
    number = float(value)
    if not math.isfinite(number) or not -limit <= number <= limit:
        raise MapError("The selected coordinates are outside the supported map area.")
    return number


@dataclass(frozen=True)
class Region:
    latitude: float
    longitude: float
    radius_km: float = 20
    max_zoom: int = 14

    def __post_init__(self) -> None:
        coordinate(self.latitude, MAX_LATITUDE)
        coordinate(self.longitude, 180)
        if (
            isinstance(self.radius_km, bool)
            or not isinstance(self.radius_km, (int, float))
            or not math.isfinite(self.radius_km)
            or not 1 <= self.radius_km <= 100
        ):
            raise MapError("Regional radius must be between 1 and 100 km.")
        if type(self.max_zoom) is not int or not 7 <= self.max_zoom <= 15:
            raise MapError("Regional detail must be between zoom 7 and 15.")
        if abs(self.latitude) + math.degrees(self.radius_km / 6371.0088) > MAX_LATITUDE:
            raise MapError(
                "This radius extends beyond the supported polar map boundary; reduce it."
            )

    def boxes(self) -> list[list[float]]:
        delta = self.radius_km / 6371.0088
        latitude = math.radians(self.latitude)
        half_width = math.degrees(math.asin(math.sin(delta) / math.cos(latitude)))
        south, north = self.latitude - math.degrees(delta), self.latitude + math.degrees(delta)
        west, east = self.longitude - half_width, self.longitude + half_width
        if west < -180:
            return [[west + 360, south, 180, north], [-180, south, east, north]]
        if east > 180:
            return [[west, south, 180, north], [-180, south, east - 360, north]]
        return [[west, south, east, north]]

    def document(self) -> dict[str, Any]:
        return {**asdict(self), "bounds": self.boxes()}


def tile_xy(latitude: float, longitude: float, zoom: int) -> tuple[int, int]:
    width = 1 << zoom
    x = math.floor((longitude + 180) / 360 * width)
    y = math.floor((1 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2 * width)
    return max(0, min(width - 1, x)), max(0, min(width - 1, y))


def tiles_for_region(region: Region | None) -> list[tuple[str, int, int, int]]:
    ranges: list[tuple[str, int, int, int, int, int]] = [
        ("overview", zoom, 0, (1 << zoom) - 1, 0, (1 << zoom) - 1)
        for zoom in range(OVERVIEW_ZOOM + 1)
    ]
    if region is not None:
        for zoom in range(OVERVIEW_ZOOM + 1, region.max_zoom + 1):
            for west, south, east, north in region.boxes():
                left, bottom = tile_xy(south, west, zoom)
                right, top = tile_xy(north, east, zoom)
                ranges.append(("region", zoom, left, right, top, bottom))
    count = sum(
        (right - left + 1) * (bottom - top + 1) for _, _, left, right, top, bottom in ranges
    )
    if count > MAX_TILES:
        raise MapError("This region has too many tiles; choose a smaller radius or less detail.")
    return sorted(
        {
            (kind, zoom, x, y)
            for kind, zoom, left, right, top, bottom in ranges
            for x in range(left, right + 1)
            for y in range(top, bottom + 1)
        }
    )


def contains(region: dict[str, Any], latitude: float, longitude: float) -> bool:
    return any(w <= longitude <= e and s <= latitude <= n for w, s, e, n in region["bounds"])


def position_suggestion(
    raw: dict[str, Any], now: int, configured: tuple[float, float] | None = None
) -> dict[str, Any]:
    if configured is not None:
        coordinate(configured[0], MAX_LATITUDE)
        coordinate(configured[1], 180)
        return {
            "available": True,
            "source": "configured",
            "latitude": configured[0],
            "longitude": configured[1],
            "age_seconds": None,
            "detail": "Configured installation location; adjust the map area if needed.",
        }
    try:
        latitude = coordinate(raw.get("latitude"), MAX_LATITUDE)
        longitude = coordinate(raw.get("longitude"), 180)
    except MapError:
        return {
            "available": False,
            "source": "unavailable",
            "detail": "No usable local-radio position. Enter coordinates or import a map pack.",
        }
    source = raw.get("source")
    stamp = raw.get("timestamp")
    age = now - stamp if type(stamp) is int and stamp > 0 else None
    if source in {"LOC_INTERNAL", "LOC_EXTERNAL"} and age is not None and 0 <= age <= 900:
        return {
            "available": True,
            "source": "radio_gps" if source == "LOC_INTERNAL" else "external_gps",
            "latitude": latitude,
            "longitude": longitude,
            "age_seconds": age,
            "detail": "Recent position reported by the connected radio.",
        }
    return {
        "available": False,
        "source": "manual_radio" if source == "LOC_MANUAL" else "unavailable",
        "age_seconds": age,
        "candidate": {"latitude": latitude, "longitude": longitude},
        "detail": "Radio position is fixed, stale or unaged. Enter a location manually.",
    }
