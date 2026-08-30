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


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"airtime": {"class_shares": {"alert": 0.5}}}, "class_shares is missing"),
        ({"airtime": {"max_parts": {"reply": 3}}}, "max_parts is missing"),
        ({"airtime": {"quiet_hours": {"start": "10pm"}}}, "quiet_hours.start"),
        ({"airtime": {"quiet_hours": {"start": "22:00-04:00"}}}, "without an offset"),
        (
            {"airtime": {"quiet_hours": {"classes": ["alerts", "bulletins"]}}},
            "quiet_hours.classes has unknown",
        ),
        ({"router": {"page_sizes": {"boards": 6}}}, "page_sizes is missing"),
    ],
)
def test_structured_dispatch_config_must_be_complete_and_parseable(
    values: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Config.model_validate(values)


def test_unsafe_no_auth_is_rejected() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Config.model_validate(
            {"web": {"bind": "0.0.0.0", "auth": {"mode": "none"}}}  # noqa: S104
        )


def test_web_transport_defaults_to_offline_trusted_http() -> None:
    config = Config()

    assert config.web.transport.mode == "trusted_http"
    assert config.web.transport.certificate_file is None
    assert config.web.transport.trusted_proxies == []


@pytest.mark.parametrize(
    ("transport", "message"),
    [
        ({"mode": "direct_https"}, "certificate_file"),
        (
            {
                "mode": "direct_https",
                "certificate_file": "relative.pem",
                "private_key_file": "/etc/outpost/tls/key.pem",
            },
            "absolute",
        ),
        ({"mode": "trusted_proxy"}, "explicit trusted proxy"),
        (
            {"mode": "trusted_proxy", "trusted_proxies": ["0.0.0.0/0"]},
            "must not trust every address",
        ),
        (
            {"mode": "trusted_proxy", "trusted_proxies": ["not-an-address"]},
            "invalid trusted proxy",
        ),
        ({"mode": "trusted_http", "public_port": 8443}, "only to trusted_proxy"),
    ],
)
def test_invalid_web_transport_is_rejected(transport: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Config.model_validate({"web": {"transport": transport}})


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


def test_ai_runtime_policy_defaults_are_bounded() -> None:
    config = Config()
    assert config.ai.max_concurrency == 1
    assert config.ai.queue_depth == 3
    assert config.ai.max_tool_rounds == 2
    assert config.ai.provider == "hailo_vlm"
    assert config.ai.model == "Qwen3-VL-2B-Instruct"
    assert config.ai.required_for_readiness
    assert config.ai.hailo_vlm.context_tokens == 2048
    assert config.ai.hailo.context_tokens == 2048


@pytest.mark.parametrize(
    "ai",
    [
        {"provider": "hailo_vlm", "hailo_vlm": {"context_tokens": 1599}},
        {"provider": "hailo", "hailo": {"context_tokens": 1599}},
        {"max_concurrency": 0},
        {"max_tool_rounds": 3},
        {"openai_compat": {"api_key_env": "not a safe env name"}},
    ],
)
def test_invalid_ai_runtime_policy_is_rejected(ai: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"ai": ai})
