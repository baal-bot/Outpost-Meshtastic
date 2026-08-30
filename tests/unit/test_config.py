from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import outpost.config as config_module
from outpost.app import OutpostApp
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
    ],
)
def test_structured_dispatch_config_must_be_complete_and_parseable(
    values: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Config.model_validate(values)


@pytest.mark.parametrize("mode", ["password", "users", "none"])
def test_removed_auth_mode_is_rejected(mode: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Config.model_validate({"web": {"auth": {"mode": mode}}})


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
        {"openai_compat": {"api_key_env": "not a safe env name"}},
    ],
)
def test_invalid_ai_runtime_policy_is_rejected(ai: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"ai": ai})


@pytest.mark.parametrize(
    "values",
    [
        {"airtime": {"broadcast_max_per_hour": 6}},
        {"airtime": {"coalesce_window_s": 15}},
        {"router": {"page_sizes": {"boards": 6}}},
        {"ai": {"cold_placeholder_enabled": False}},
        {"ai": {"cold_placeholder_threshold_s": 15}},
        {"ai": {"embeddings": {"enabled": False}}},
        {"ai": {"max_tool_rounds": 2}},
        {"security": {"handle_change_per_hours": 24}},
        {"security": {"handle_reserve_days": 30}},
        {"fed": {"mqtt": {"discovery_enabled": False}}},
        {"fed": {"mqtt": {"server": "mqtt.example"}}},
        {"fed": {"mqtt": {"port": 1883}}},
        {"fed": {"mqtt": {"topic_root": "msh"}}},
        {"mail": {"notify_window_hours": 12}},
        {"watch": {"self_resolve_hours": 24}},
        {"web": {"auth": {"mode": "none"}}},
    ],
)
def test_removed_no_effect_settings_are_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Config.model_validate(values)


def test_remaining_config_fields_have_runtime_references() -> None:
    source_root = Path(config_module.__file__).parent
    referenced: set[str] = set()
    for source in source_root.rglob("*.py"):
        if source.name in {"config.py", "self_check.py"}:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        referenced.update(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))

    dynamic_fields = {"caution", "urgent", "critical"}
    missing: list[str] = []
    for name, model in vars(config_module).items():
        if not isinstance(model, type) or not issubclass(model, BaseModel):
            continue
        if model is config_module.StrictModel:
            continue
        missing.extend(
            f"{name}.{field}"
            for field in model.model_fields
            if field not in referenced and field not in dynamic_fields
        )
    assert missing == []


def test_live_configurable_paging_and_incident_policy_reach_runtime_services(tmp_path) -> None:
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "router": {"page_ttl_minutes": 3},
            "watch": {
                "position_max_age_minutes": 4,
                "dedupe_radius_m": 25,
                "dedupe_window_minutes": 6,
            },
        }
    )
    app = OutpostApp(config)

    assert app.bbs.page_ttl_seconds == 180
    assert app.incidents.position_max_age_seconds == 240
    assert app.incidents.dedupe_radius_m == 25
    assert app.incidents.dedupe_window_minutes == 6
