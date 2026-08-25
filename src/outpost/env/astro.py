from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from outpost.clock import Clock


@dataclass(frozen=True)
class AstronomySnapshot:
    date: str
    timezone: str
    sunrise: str | None
    sunset: str | None
    civil_dawn: str | None
    civil_dusk: str | None
    daylight_minutes: int | None
    moon_phase: str
    moon_illumination: int
    moon_age_days: float

    def json(self) -> dict[str, object]:
        return asdict(self)


class AstronomyService:
    """Offline NOAA-style solar approximation plus synodic moon calculation."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    @staticmethod
    def _solar_utc(day: date, lat: float, lon: float, *, rise: bool, zenith: float) -> float | None:
        number = day.timetuple().tm_yday
        longitude_hour = lon / 15
        approximate = number + ((6 if rise else 18) - longitude_hour) / 24
        anomaly = 0.9856 * approximate - 3.289
        longitude = (
            anomaly
            + 1.916 * math.sin(math.radians(anomaly))
            + 0.020 * math.sin(math.radians(2 * anomaly))
            + 282.634
        ) % 360
        ascension = math.degrees(math.atan(0.91764 * math.tan(math.radians(longitude)))) % 360
        ascension += math.floor(longitude / 90) * 90 - math.floor(ascension / 90) * 90
        ascension /= 15
        sin_declination = 0.39782 * math.sin(math.radians(longitude))
        cos_declination = math.cos(math.asin(sin_declination))
        denominator = cos_declination * math.cos(math.radians(lat))
        if abs(denominator) < 1e-12:
            return None
        cosine = (
            math.cos(math.radians(zenith)) - sin_declination * math.sin(math.radians(lat))
        ) / denominator
        if not -1 <= cosine <= 1:
            return None
        angle = math.degrees(math.acos(cosine))
        hour_angle = 360 - angle if rise else angle
        local_mean = hour_angle / 15 + ascension - 0.06571 * approximate - 6.622
        return (local_mean - longitude_hour) % 24

    @staticmethod
    def _local(day: date, hours: float | None, timezone: ZoneInfo) -> datetime | None:
        if hours is None:
            return None
        value = (
            datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(hours=hours)
        ).astimezone(timezone)
        if value.date() < day:
            value += timedelta(days=1)
        elif value.date() > day:
            value -= timedelta(days=1)
        return value

    @staticmethod
    def _moon(now: datetime) -> tuple[str, int, float]:
        origin = datetime(2000, 1, 6, 18, 14, tzinfo=UTC)
        cycle = 29.53058867
        age = ((now.astimezone(UTC) - origin).total_seconds() / 86400) % cycle
        fraction = age / cycle
        illumination = round((1 - math.cos(2 * math.pi * fraction)) / 2 * 100)
        names = (
            "New moon",
            "Waxing crescent",
            "First quarter",
            "Waxing gibbous",
            "Full moon",
            "Waning gibbous",
            "Last quarter",
            "Waning crescent",
        )
        return names[round(fraction * 8) % 8], illumination, round(age, 1)

    def current(self, lat: float, lon: float, timezone_name: str) -> AstronomySnapshot:
        timezone = ZoneInfo(timezone_name)
        now = self.clock.now()
        day = now.astimezone(timezone).date()
        sunrise = self._local(
            day, self._solar_utc(day, lat, lon, rise=True, zenith=90.833), timezone
        )
        sunset = self._local(
            day, self._solar_utc(day, lat, lon, rise=False, zenith=90.833), timezone
        )
        dawn = self._local(day, self._solar_utc(day, lat, lon, rise=True, zenith=96), timezone)
        dusk = self._local(day, self._solar_utc(day, lat, lon, rise=False, zenith=96), timezone)
        phase, illumination, age = self._moon(now)
        daylight = round((sunset - sunrise).total_seconds() / 60) if sunrise and sunset else None

        def stamp(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return AstronomySnapshot(
            str(day),
            timezone_name,
            stamp(sunrise),
            stamp(sunset),
            stamp(dawn),
            stamp(dusk),
            daylight,
            phase,
            illumination,
            age,
        )
