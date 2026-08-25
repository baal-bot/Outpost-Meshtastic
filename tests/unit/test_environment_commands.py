from types import SimpleNamespace

import pytest

from outpost.clock import VirtualClock
from outpost.commands.environment import _fit_radio, specs
from outpost.config import Config
from outpost.env import AstronomyService


def test_radio_text_fitter_preserves_utf8_budget() -> None:
    value = _fit_radio("⚠" * 100)
    assert len(value.encode()) <= 200
    assert value.endswith("…")


@pytest.mark.asyncio
async def test_sun_command_explicitly_describes_polar_unavailability() -> None:
    config = Config.model_validate(
        {"node": {"location": {"lat": 89.0, "lon": 0.0}, "timezone": "UTC"}}
    )
    commands = specs(
        SimpleNamespace(),
        config,
        astronomy=AstronomyService(VirtualClock()),
    )
    sun = next(command for command in commands if command.name == "SUN")
    response = await sun.handler(SimpleNamespace(args=""))
    assert "no sunrise" in response.lines[0].text
    assert "no sunset" in response.lines[0].text
    assert len(response.lines[0].text.encode()) <= 200


def test_environment_gate_commands_are_single_part() -> None:
    config = Config.model_validate({"node": {"location": {"lat": 40.44, "lon": -79.99}}})
    commands = specs(
        SimpleNamespace(),
        config,
        cap_alerts=SimpleNamespace(),
        astronomy=AstronomyService(VirtualClock()),
        seismic=SimpleNamespace(),
    )
    by_name = {command.name: command for command in commands}
    assert all(by_name[name].max_parts == 1 for name in ("WX", "FC", "SUN", "WARN", "QUAKE"))
