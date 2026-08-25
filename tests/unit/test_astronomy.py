from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import sun

from outpost.clock import VirtualClock
from outpost.env import AstronomyService


def test_pittsburgh_solar_and_moon_snapshot_is_local_and_plausible() -> None:
    clock = VirtualClock(epoch=datetime(2026, 8, 24, 16, tzinfo=UTC))
    value = AstronomyService(clock).current(40.4406, -79.9959, "America/New_York")

    assert value.date == "2026-08-24"
    assert value.sunrise and value.sunrise.startswith("2026-08-24T06:39")
    assert value.sunset and value.sunset.startswith("2026-08-24T20:04")
    assert value.civil_dawn and value.civil_dawn.startswith("2026-08-24T06:10")
    assert value.civil_dusk and value.civil_dusk.startswith("2026-08-24T20:33")
    assert value.daylight_minutes == 806
    assert value.moon_phase == "Waxing gibbous"
    assert 85 <= value.moon_illumination <= 90


def test_polar_sun_can_be_unavailable_without_failure() -> None:
    clock = VirtualClock(epoch=datetime(2026, 6, 21, 12, tzinfo=UTC))
    value = AstronomyService(clock).current(89.0, 0.0, "UTC")

    assert value.sunrise is None and value.sunset is None
    assert value.daylight_minutes is None


def test_sunrise_and_sunset_within_sixty_seconds_of_reference_matrix() -> None:
    places = (
        (40.4406, -79.9959, "America/New_York"),
        (51.5074, -0.1278, "Europe/London"),
        (35.6762, 139.6503, "Asia/Tokyo"),
        (-33.8688, 151.2093, "Australia/Sydney"),
        (-33.9249, 18.4241, "Africa/Johannesburg"),
    )
    clock = VirtualClock(epoch=datetime(2026, 6, 21, 12, tzinfo=UTC))
    service = AstronomyService(clock)
    for latitude, longitude, timezone_name in places:
        timezone = ZoneInfo(timezone_name)
        day = clock.now().astimezone(timezone).date()
        reference = sun(Observer(latitude, longitude), date=day, tzinfo=timezone)
        value = service.current(latitude, longitude, timezone_name)
        assert value.sunrise is not None and value.sunset is not None
        sunrise = datetime.fromisoformat(value.sunrise)
        sunset = datetime.fromisoformat(value.sunset)
        assert abs((sunrise - reference["sunrise"]).total_seconds()) <= 60
        assert abs((sunset - reference["sunset"]).total_seconds()) <= 60
