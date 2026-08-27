from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml


def test_first_run_wizard_writes_valid_configuration(tmp_path, monkeypatch) -> None:
    root = Path(__file__).parents[2]
    config = tmp_path / "config.yaml"
    state = tmp_path / "onboarding.json"
    config.write_text((root / "config" / "config.example.yaml").read_text())
    spec = importlib.util.spec_from_file_location("outpost_configure", root / "deploy/configure.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "detect_rtl_sdr", lambda: [])
    monkeypatch.setattr(module, "detect_hailo10h", lambda: False)
    answers = iter(
        [
            "Test Outpost",
            "TST",
            "operator@example.net",
            "UTC",
            "imperial",
            "serial",
            "/dev/ttyACM0",
            "40.44",
            "-79.99",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        sys,
        "argv",
        ["configure.py", "--config", str(config), "--state", str(state)],
    )

    module.main()

    result = yaml.safe_load(config.read_text())
    assert result["node"]["name"] == "Test Outpost"
    assert result["node"]["units"] == "imperial"
    assert result["node"]["location"] == {"lat": 40.44, "lon": -79.99}
    assert result["radio"]["serial"]["port"] == "/dev/ttyACM0"
    assert json.loads(state.read_text())["steps"]["identity_location"]["status"] == "completed"


def test_first_run_wizard_configures_detected_sdr(tmp_path, monkeypatch) -> None:
    root = Path(__file__).parents[2]
    config = tmp_path / "config.yaml"
    config.write_text((root / "config" / "config.example.yaml").read_text())
    spec = importlib.util.spec_from_file_location(
        "outpost_sdr_configure", root / "deploy/configure.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "detect_rtl_sdr", lambda: ["51231467"])
    monkeypatch.setattr(module, "detect_hailo10h", lambda: False)
    answers = iter(
        [
            "Test Outpost",
            "TST",
            "operator@example.net",
            "UTC",
            "imperial",
            "serial",
            "/dev/ttyACM0",
            "40.44",
            "-79.99",
            "yes",
            "51231467",
            "162.550",
            "042003",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(sys, "argv", ["configure.py", "--config", str(config)])

    module.main()

    result = yaml.safe_load(config.read_text())
    assert result["env"]["same"]["enabled"] is True
    assert result["env"]["same"]["device"] == "51231467"
    assert result["env"]["same"]["frequency_mhz"] == 162.55
    assert result["env"]["same"]["county_codes"] == ["042003"]


def test_first_run_wizard_can_enable_detected_hailo(tmp_path, monkeypatch) -> None:
    root = Path(__file__).parents[2]
    config = tmp_path / "config.yaml"
    config.write_text((root / "config" / "config.example.yaml").read_text())
    spec = importlib.util.spec_from_file_location(
        "outpost_hailo_configure", root / "deploy/configure.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "detect_rtl_sdr", lambda: [])
    monkeypatch.setattr(module, "detect_hailo10h", lambda: True)
    answers = iter(
        [
            "Test Outpost",
            "TST",
            "operator@example.net",
            "UTC",
            "metric",
            "serial",
            "/dev/ttyACM0",
            "",
            "",
            "yes",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(sys, "argv", ["configure.py", "--config", str(config)])

    module.main()

    result = yaml.safe_load(config.read_text())
    assert result["modules"]["ai"]["enabled"] is True
    assert result["ai"]["provider"] == "hailo_vlm"
    assert result["ai"]["model"] == "Qwen3-VL-2B-Instruct"
