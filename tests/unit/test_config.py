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
