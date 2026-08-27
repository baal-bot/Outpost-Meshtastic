from __future__ import annotations

import copy
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).parents[2]
CAPABILITIES = runpy.run_path(
    str(ROOT / "tools" / "check_capabilities.py"),
    run_name="outpost_capability_evidence",
)
ManifestError = CAPABILITIES["ManifestError"]
load_manifest = cast(Callable[[Path, Path], dict[str, Any]], CAPABILITIES["load_manifest"])
validate_manifest = cast(Callable[[dict[str, Any], Path], None], CAPABILITIES["validate_manifest"])
run = cast(Callable[..., int], CAPABILITIES["run"])


def test_capability_manifest_and_generated_documents_are_current() -> None:
    manifest = load_manifest(ROOT / "docs" / "capabilities.toml", ROOT)

    assert list(manifest["states"]) == list(CAPABILITIES["STATE_ORDER"])
    assert len(manifest["capabilities"]) >= 16
    assert not any(item["maturity"] == "production_ready" for item in manifest["capabilities"])
    local_ai = next(item for item in manifest["capabilities"] if item["id"] == "local_ai")
    assert local_ai["maturity"] == "hardware_gated"
    assert (
        run(
            ROOT / "docs" / "capabilities.toml",
            ROOT / "docs" / "FEATURES.md",
            ROOT / "README.md",
            ROOT,
            check=True,
        )
        == 0
    )


def test_capability_generator_round_trip_and_stale_detection(tmp_path: Path) -> None:
    features = tmp_path / "FEATURES.md"
    readme = tmp_path / "README.md"
    readme.write_text(
        "before\n<!-- capability-summary:start -->\nstale\n<!-- capability-summary:end -->\nafter\n"
    )

    assert (
        run(
            ROOT / "docs" / "capabilities.toml",
            features,
            readme,
            ROOT,
            check=False,
        )
        == 0
    )
    assert (
        run(
            ROOT / "docs" / "capabilities.toml",
            features,
            readme,
            ROOT,
            check=True,
        )
        == 0
    )
    features.write_text(features.read_text() + "stale\n")
    assert (
        run(
            ROOT / "docs" / "capabilities.toml",
            features,
            readme,
            ROOT,
            check=True,
        )
        == 1
    )


def test_capability_manifest_rejects_missing_evidence() -> None:
    manifest = load_manifest(ROOT / "docs" / "capabilities.toml", ROOT)
    invalid = copy.deepcopy(manifest)
    invalid["capabilities"][0]["evidence"][0]["path"] = "tests/does-not-exist.py"

    with pytest.raises(ManifestError, match="evidence does not exist"):
        validate_manifest(invalid, ROOT)
