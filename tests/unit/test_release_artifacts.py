import hashlib
import json
import re
import runpy
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).parents[2]
BUILD = runpy.run_path(str(ROOT / "tools" / "build_release_metadata.py"), run_name="release_build")
VERIFY = runpy.run_path(str(ROOT / "tools" / "verify_release.py"), run_name="release_verify")
build_metadata = cast(Callable[..., dict[str, Any]], BUILD["build_metadata"])
verify_release = cast(Callable[[Path, str], dict[str, Any]], VERIFY["verify_release"])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_project(root: Path) -> None:
    (root / "src/outpost/store/migrations").mkdir(parents=True)
    (root / "src/outpost/store/migrations/0147_current.sql").write_text("SELECT 1;\n")
    (root / "docs").mkdir()
    (root / "docs/capabilities.toml").write_text("format_version = 1\n")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "outpost"\nversion = "1.2.3"\nrequires-python = ">=3.12,<3.14"\n'
    )


def _fake_wheel(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "outpost-1.2.3.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: outpost\nVersion: 1.2.3\n",
        )


def _release_directory(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    source = tmp_path / "source"
    source.mkdir()
    _fake_project(source)
    release = tmp_path / "release"
    release.mkdir()
    wheel = release / "outpost-1.2.3-py3-none-any.whl"
    _fake_wheel(wheel)
    metadata = build_metadata(
        wheel,
        source,
        commit="a" * 40,
        tag="v1.2.3",
        built_at="2026-08-27T12:00:00+00:00",
    )
    (release / "RELEASE-METADATA.json").write_text(
        json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8"
    )
    (release / "outpost.spdx.json").write_text('{"spdxVersion":"SPDX-2.3"}\n', encoding="utf-8")
    names = [wheel.name, "RELEASE-METADATA.json", "outpost.spdx.json"]
    (release / "SHA256SUMS").write_text(
        "".join(f"{_sha256(release / name)}  {name}\n" for name in names), encoding="utf-8"
    )
    return release, metadata


def test_release_metadata_records_compatibility_and_exact_source(tmp_path: Path) -> None:
    release, expected = _release_directory(tmp_path)

    actual = verify_release(release, "v1.2.3")

    assert actual == expected
    assert actual["source"] == {"commit": "a" * 40, "tag": "v1.2.3"}
    assert actual["database"]["maximum_schema"] == 147
    assert actual["support"]["python"] == ">=3.12,<3.14"
    assert actual["support"]["operating_systems"] == [
        "64-bit Raspberry Pi OS Trixie",
        "Ubuntu 24.04 CI",
    ]


def test_release_verification_rejects_tampered_artifact(tmp_path: Path) -> None:
    release, _ = _release_directory(tmp_path)
    wheel = release / "outpost-1.2.3-py3-none-any.whl"
    wheel.write_bytes(wheel.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_release(release, "v1.2.3")


def test_release_verification_rejects_unlisted_artifact(tmp_path: Path) -> None:
    release, _ = _release_directory(tmp_path)
    (release / "unexpected.bin").write_bytes(b"not in the signed inventory")

    with pytest.raises(ValueError, match="checksum inventory mismatch"):
        verify_release(release, "v1.2.3")


def test_release_verification_rejects_wrong_tag(tmp_path: Path) -> None:
    release, _ = _release_directory(tmp_path)

    with pytest.raises(ValueError, match="source tag or commit"):
        verify_release(release, "v1.2.4")


def test_release_verification_requires_spdx_sbom(tmp_path: Path) -> None:
    release, _ = _release_directory(tmp_path)
    sbom = release / "outpost.spdx.json"
    sbom.write_text('{"not":"spdx"}\n', encoding="utf-8")
    checksum = release / "SHA256SUMS"
    checksum.write_text(
        checksum.read_text(encoding="utf-8").replace(
            next(line for line in checksum.read_text().splitlines() if line.endswith(sbom.name)),
            f"{_sha256(sbom)}  {sbom.name}",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not SPDX JSON"):
        verify_release(release, "v1.2.3")


def test_workflows_pin_actions_and_gate_release_on_ci() -> None:
    workflows = sorted((ROOT / ".github/workflows").glob("*.y*ml"))
    for workflow in workflows:
        content = workflow.read_text(encoding="utf-8")
        external_actions = [
            action
            for action in re.findall(r"uses:\s*([^\s#]+)", content)
            if not action.startswith("./")
        ]
        assert external_actions
        assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) for action in external_actions)

    ci = workflows[0].read_text(encoding="utf-8")
    release = workflows[1].read_text(encoding="utf-8")
    assert "workflow_call:" in ci
    assert "uses: ./.github/workflows/ci.yml" in release
    assert "needs: ci" in release
    assert "anchore/sbom-action@" in release
    assert release.count("actions/attest@") == 2
    assert "sbom-path: dist/outpost.spdx.json" in release
    assert "artifact-metadata: write" in release
