from __future__ import annotations

import pytest
from pydantic import ValidationError

from outpost.config import Config


def test_default_config_is_valid() -> None:
    assert Config().airtime.budget_percent == 8


@pytest.mark.parametrize(
    "airtime",
    [
        {"class_shares": {"reply": 0.8, "alert": 0.3}},
        {"class_shares": {"bogus": 1.0}},
        {"budget_percent": 18, "emergency_reserve_percent": 4},
        {"budget_percent": 20, "emergency_reserve_percent": 5, "utilisation_ceiling": 25},
    ],
)
def test_invalid_airtime_config_is_rejected(airtime: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"airtime": airtime})


def test_unsafe_no_auth_is_rejected() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Config.model_validate(
            {"web": {"bind": "0.0.0.0", "auth": {"mode": "none"}}}  # noqa: S104
        )


@pytest.mark.parametrize(
    "security",
    [
        {"safety_repeat_window_seconds": 9},
        {"safety_repeat_window_seconds": 3601},
        {"safety_attempt_retention_hours": 0},
        {"safety_attempt_retention_hours": 721},
    ],
)
def test_safety_floor_policy_has_safe_bounds(security: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"security": security})


@pytest.mark.parametrize(
    "same",
    [
        {"enabled": True},
        {"frequency_mhz": 162.41},
        {"county_codes": ["42003"]},
        {"county_codes": ["042003", "042003"]},
        {"device": "../../dev/null"},
        {"sample_rate": 96000},
        {"audio_stall_seconds": 2},
        {"restart_initial_seconds": 30, "restart_max_seconds": 10},
    ],
)
def test_invalid_same_receiver_config_is_rejected(same: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"env": {"same": same}})


def test_same_receiver_config_accepts_noaa_channel_serial_and_county() -> None:
    config = Config.model_validate(
        {
            "env": {
                "same": {
                    "enabled": True,
                    "frequency_mhz": 162.55,
                    "county_codes": ["042003"],
                    "device": "51231467",
                    "sample_rate": 48000,
                }
            }
        }
    )
    assert config.env.same.enabled
