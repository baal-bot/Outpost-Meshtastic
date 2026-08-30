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


def test_automated_evidence_must_be_a_specific_collected_test_node() -> None:
    manifest = load_manifest(ROOT / "docs" / "capabilities.toml", ROOT)
    invalid = copy.deepcopy(manifest)
    invalid["capabilities"][0]["evidence"][0]["path"] = "README.md"

    with pytest.raises(ManifestError, match="specific tests/.*::test node"):
        validate_manifest(invalid, ROOT)

    invalid["capabilities"][0]["evidence"][0]["path"] = (
        "tests/unit/test_radio_link.py::test_not_a_real_test"
    )
    with pytest.raises(ManifestError, match="pytest could not collect|is not collected"):
        validate_manifest(invalid, ROOT)


def test_skipped_test_does_not_count_as_automated_evidence(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_skipped.py").write_text(
        "import os\nimport pytest\n\n"
        "@pytest.mark.skipif(os.getenv('EVIDENCE_GATE') != '1', reason='not in CI')\n"
        "def test_claim(): pass\n",
        encoding="utf-8",
    )
    manifest = load_manifest(ROOT / "docs" / "capabilities.toml", ROOT)
    candidate = copy.deepcopy(manifest)
    capability = candidate["capabilities"][0]
    capability["maturity"] = "automated_tested"
    capability["evidence"] = [
        {
            "kind": "automated",
            "path": "tests/test_skipped.py::test_claim",
            "description": "This must not count.",
        }
    ]
    candidate["capabilities"] = [capability]

    with pytest.raises(ManifestError, match="automated evidence is skipped"):
        validate_manifest(candidate, tmp_path)


def test_maturity_cannot_exceed_its_evidence_kinds() -> None:
    manifest = load_manifest(ROOT / "docs" / "capabilities.toml", ROOT)
    invalid = copy.deepcopy(manifest)
    invalid["capabilities"][0]["maturity"] = "production_ready"

    with pytest.raises(ManifestError, match="requires evidence kind.*release"):
        validate_manifest(invalid, ROOT)
