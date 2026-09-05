from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from tools import check_requirements as ledger

ROOT = Path(__file__).parents[2]
ID = "REQ-TEST-001"
APPROVAL = ledger.REPOSITORY + "/issues/154#issuecomment-5555091612"
CLAUSE = "**REQ-TEST-001** — The example MUST remain testable.\n"


@pytest.fixture
def example(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    spec = tmp_path / ledger.SPEC
    spec.mkdir(parents=True)
    (spec / "01-example.md").write_text(CLAUSE)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_example.py").write_text("def test_example(): assert True\n")
    source = ledger.current_inventory(tmp_path)[ID]
    snapshot = {
        "schema_version": 1,
        "original_revision": "a" * 40,
        "original_count": 1,
        "requirements": {ID: {"original": copy.deepcopy(source), "current": source}},
    }
    decisions = {
        "schema_version": 1,
        "reviewed_revision": "b" * 40,
        "defaults": {
            "state": "deferred",
            "owner_decision": "Retained; acceptance pending, no waiver.",
        },
        "domains": {
            "TEST": {
                "rationale": "No complete acceptance claim.",
                "implementation": ["tests/test_example.py"],
                "issues": [154],
            }
        },
        "requirements": {},
        "goals": {
            f"G{number}": {"state": "open", "rationale": "Field evidence pending.", "issues": [157]}
            for number in range(1, 7)
        },
    }
    return tmp_path, snapshot, decisions


def claim_tested(decisions: dict[str, Any], **overrides: Any) -> None:
    decisions["requirements"][ID] = {
        "state": "implemented_tested",
        "tested_revision": "c" * 40,
        "ci": ledger.REPOSITORY + "/actions/runs/123",
        "tests": ["tests/test_example.py::test_example"],
        **overrides,
    }


def test_repository_ledger_covers_originals_additions_and_explicit_open_gates() -> None:
    snapshot = json.loads((ROOT / ledger.SNAPSHOT).read_text())
    decisions = tomllib.loads((ROOT / ledger.DECISIONS).read_text())
    assert snapshot["original_count"] == 508
    assert len(snapshot["requirements"]) == 531
    assert all(value["state"] == "open" for value in decisions["goals"].values())
    assert all(
        ledger.resolve(decisions, key)["state"] in ledger.STATES for key in snapshot["requirements"]
    )
    assert ledger.run(ROOT, check=True) == 0


def test_inventory_handles_annotations_suffixes_and_fenced_code_without_fake_requirements() -> None:
    content = (
        "**REQ-TEST-001 (annotated)** — MUST be included.\n\n"
        "```yaml\n# Not a section\nvalue: retained\n**REQ-TEST-999** fake\n```\n\n"
        "**REQ-TEST-002A** — Uppercase amendment.\n\n"
        "**REQ-TEST-002a** — Lowercase amendment.\n\n"
        "## Appendix\nUnrelated prose.\n**REQ-TEST-003 restated** — Not a definition.\n"
    )
    result = ledger.inventory({"example.md": content})
    assert set(result) == {ID, "REQ-TEST-002A", "REQ-TEST-002a"}
    assert "value: retained" in result[ID]["text"]
    assert "Unrelated prose" not in result["REQ-TEST-002a"]["text"]


def test_duplicate_identifiers_are_not_silently_overwritten() -> None:
    with pytest.raises(ledger.ManifestError, match="duplicate requirement"):
        ledger.inventory({"one.md": CLAUSE, "two.md": CLAUSE})


@pytest.mark.parametrize("change", ["text", "added", "removed"])
def test_unreviewed_spec_changes_fail(example, change: str) -> None:
    root, snapshot, decisions = example
    path = root / ledger.SPEC / "01-example.md"
    path.write_text(
        {
            "text": CLAUSE.replace("testable", "changed"),
            "added": CLAUSE + "\n**REQ-TEST-002** new",
            "removed": "",
        }[change]
    )
    with pytest.raises(ledger.ManifestError, match="inventory changed|specification changed"):
        ledger.validate(root, snapshot, decisions)


def test_review_refresh_preserves_originals_and_requires_exact_changed_ids(example) -> None:
    root, snapshot, decisions = example
    original = copy.deepcopy(snapshot["requirements"][ID]["original"])
    path = root / ledger.SPEC / "01-example.md"
    path.write_text(CLAUSE.replace("testable", "changed"))
    with pytest.raises(ledger.ManifestError, match="exactly the changed"):
        ledger.refresh_current(root, snapshot, ["REQ-TEST-002"])
    ledger.refresh_current(root, snapshot, [ID])
    assert snapshot["requirements"][ID]["original"] == original
    ledger.validate(root, snapshot, decisions)
    claim_tested(decisions)
    with pytest.raises(ledger.ManifestError, match="changed original wording"):
        ledger.validate(root, snapshot, decisions)


def test_snapshot_digest_and_original_count_cannot_drift(example) -> None:
    root, snapshot, decisions = example
    snapshot["original_count"] = 2
    with pytest.raises(ledger.ManifestError, match="original requirement count"):
        ledger.validate(root, snapshot, decisions)
    snapshot["original_count"] = 1
    snapshot["requirements"][ID]["original"]["text"] += " tampered"
    with pytest.raises(ledger.ManifestError, match="snapshot digest mismatch"):
        ledger.validate(root, snapshot, decisions)


@pytest.mark.parametrize("state", ["accepted_replacement", "withdrawn"])
def test_replacement_and_withdrawal_require_explicit_approval(example, state: str) -> None:
    root, snapshot, decisions = example
    value = decisions["requirements"][ID] = {"state": state}
    with pytest.raises(ledger.ManifestError, match="explicit owner approval"):
        ledger.validate(root, snapshot, decisions)
    value["approval"] = APPROVAL
    if state == "accepted_replacement":
        with pytest.raises(ledger.ManifestError, match="explicit replacement wording"):
            ledger.validate(root, snapshot, decisions)
        value["replacement"] = "The reviewed alternative requirement."
    ledger.validate(root, snapshot, decisions)


def test_removed_requirement_cannot_be_hidden_by_default_deferred(example) -> None:
    root, snapshot, decisions = example
    (root / ledger.SPEC / "01-example.md").write_text("")
    ledger.refresh_current(root, snapshot, [ID])
    with pytest.raises(ledger.ManifestError, match="removed without an approved disposition"):
        ledger.validate(root, snapshot, decisions)
    decisions["requirements"][ID] = {"state": "withdrawn", "approval": APPROVAL}
    ledger.validate(root, snapshot, decisions)


@pytest.mark.parametrize("field", ["original_revision", "reviewed_revision", "tested_revision"])
def test_evidence_requires_full_revisions(example, field: str) -> None:
    root, snapshot, decisions = example
    claim_tested(decisions)
    target = snapshot if field == "original_revision" else decisions
    if field == "tested_revision":
        target = decisions["requirements"][ID]
    target[field] = "HEAD"
    with pytest.raises(ledger.ManifestError, match="full commit SHA|exact tested revision"):
        ledger.validate(root, snapshot, decisions)


@pytest.mark.parametrize("node", ["README.md", "tests/test_example.py::test_missing"])
def test_file_names_and_nonexistent_tests_do_not_prove_compliance(example, node: str) -> None:
    root, snapshot, decisions = example
    claim_tested(decisions, tests=[node])
    with pytest.raises(
        ledger.ManifestError, match="specific collected|could not collect|not collected"
    ):
        ledger.validate(root, snapshot, decisions)


def test_collected_test_accepted_but_skipped_test_rejected(example) -> None:
    root, snapshot, decisions = example
    claim_tested(decisions)
    ledger.validate(root, snapshot, decisions)
    (root / "tests/test_skipped.py").write_text(
        "import pytest\n@pytest.mark.skip(reason='not qualified')\ndef test_skipped(): pass\n"
    )
    decisions["requirements"][ID]["tests"] = ["tests/test_skipped.py::test_skipped"]
    with pytest.raises(ledger.ManifestError, match="evidence is skipped"):
        ledger.validate(root, snapshot, decisions)


def test_unknown_requirement_and_unsafe_evidence_rejected(example) -> None:
    root, snapshot, decisions = example
    decisions["requirements"]["REQ-TEST-999"] = {"state": "deferred"}
    with pytest.raises(ledger.ManifestError, match="unknown requirements"):
        ledger.validate(root, snapshot, decisions)
    decisions["requirements"].clear()
    decisions["domains"]["TEST"]["implementation"] = ["../elsewhere"]
    with pytest.raises(ledger.ManifestError, match="unsafe or missing"):
        ledger.validate(root, snapshot, decisions)


def test_all_product_gates_required_and_software_alone_cannot_close_them(example) -> None:
    root, snapshot, decisions = example
    goal = decisions["goals"].pop("G6")
    with pytest.raises(ledger.ManifestError, match="G1-G6"):
        ledger.validate(root, snapshot, decisions)
    decisions["goals"]["G6"] = goal
    goal["state"] = "measured"
    with pytest.raises(ledger.ManifestError, match="software suite alone"):
        ledger.validate(root, snapshot, decisions)
    goal.update(tested_revision="d" * 40, field_record="tests/test_example.py")
    with pytest.raises(ledger.ManifestError, match="field record must"):
        ledger.validate(root, snapshot, decisions)
    field = root / "docs/field-2026-09-05.md"
    field.write_text("Synthetic schema example, not actual field evidence.\n")
    goal["field_record"] = str(field.relative_to(root))
    ledger.validate(root, snapshot, decisions)  # Human review must establish semantic sufficiency.


def test_generator_roundtrip_stale_output_and_live_line_links(example) -> None:
    root, snapshot, decisions = example
    (root / ledger.SNAPSHOT).write_text(json.dumps(snapshot))
    (root / ledger.DECISIONS).write_text(
        'schema_version = 1\nreviewed_revision = "' + "b" * 40 + '"\n'
        '[defaults]\nstate = "deferred"\nowner_decision = "Retained."\n'
        '[domains.TEST]\nrationale = "Pending."\nimplementation = ["tests"]\nissues = [154]\n'
        + "\n".join(
            f'[goals.G{n}]\nstate = "open"\nrationale = "Pending."\nissues = [157]'
            for n in range(1, 7)
        )
    )
    assert ledger.run(root, check=False) == 0
    assert ledger.run(root, check=True) == 0
    spec = root / ledger.SPEC / "01-example.md"
    spec.write_text("# New heading\n\n" + CLAUSE)
    with pytest.raises(ledger.ManifestError, match="document is stale"):
        ledger.run(root, check=True)
    assert ledger.run(root, check=False) == 0
    output = (root / ledger.OUTPUT).read_text()
    assert "[current](outpost-spec/docs/01-example.md#L3)" in output
    assert "/blob/" + "a" * 40 + "/docs/outpost-spec/docs/01-example.md#L1" in output
