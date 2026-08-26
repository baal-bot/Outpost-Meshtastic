from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml


def test_first_run_wizard_writes_valid_configuration(tmp_path, monkeypatch) -> None:
    root = Path(__file__).parents[2]
    config = tmp_path / "config.yaml"
    config.write_text((root / "config" / "config.example.yaml").read_text())
    spec = importlib.util.spec_from_file_location("outpost_configure", root / "deploy/configure.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
    monkeypatch.setattr(sys, "argv", ["configure.py", "--config", str(config)])

    module.main()

    result = yaml.safe_load(config.read_text())
    assert result["node"]["name"] == "Test Outpost"
    assert result["node"]["units"] == "imperial"
    assert result["node"]["location"] == {"lat": 40.44, "lon": -79.99}
    assert result["radio"]["serial"]["port"] == "/dev/ttyACM0"
