"""Generate and validate original-spec dispositions without inferring acceptance.

Run as ``python -m tools.check_requirements`` from the repository root. The frozen
snapshot is generated separately, so adding/removing/editing a requirement cannot
silently inherit a passing disposition in CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from tools.check_capabilities import ManifestError, _validate_automated_nodes

SPEC = Path("docs/outpost-spec/docs")
SNAPSHOT = Path("docs/requirements-snapshot.json")
DECISIONS = Path("docs/requirement-dispositions.toml")
OUTPUT = Path("docs/REQUIREMENT-DISPOSITIONS.md")
REPOSITORY = "https://github.com/baal-bot/Outpost-Meshtastic"
DEFINITION = re.compile(r"^\*\*(REQ-[A-Z]+-\d+[A-Za-z]?)(?:\*\*|\s*\([^\n]*?\)\*\*)")
STATES = {"implemented_tested", "accepted_replacement", "deferred", "withdrawn"}
SHA = re.compile(r"[0-9a-f]{40}")


def digest(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def inventory(documents: dict[str, str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path, content in sorted(documents.items()):
        lines = content.splitlines()
        starts = []
        fenced = False
        for number, line in enumerate(lines):
            if line.startswith(("```", "~~~")):
                fenced = not fenced
            if not fenced and (match := DEFINITION.match(line)):
                starts.append((number, match[1]))
        for offset, (number, identifier) in enumerate(starts):
            if identifier in found:
                raise ManifestError(
                    f"duplicate requirement {identifier}: {found[identifier]['path']} and {path}"
                )
            end = starts[offset + 1][0] if offset + 1 < len(starts) else len(lines)
            inside_code = False
            for following in range(number + 1, end):
                if lines[following].startswith(("```", "~~~")):
                    inside_code = not inside_code
                if not inside_code and lines[following].startswith("#"):
                    end = following
                    break
            text = "\n".join(lines[number:end]).strip().removesuffix("---").strip()
            found[identifier] = {
                "path": path,
                "line": number + 1,
                "text": text,
                "digest": digest(text),
            }
    return found


def current_inventory(root: Path) -> dict[str, dict[str, Any]]:
    return inventory(
        {str(path.relative_to(root)): path.read_text() for path in (root / SPEC).glob("*.md")}
    )


def capture(root: Path, revision: str) -> dict[str, Any]:
    if not SHA.fullmatch(revision):
        raise ManifestError("original revision must be a full commit SHA")
    names = subprocess.check_output(  # noqa: S603 - fixed git command, validated revision
        ["git", "ls-tree", "-r", "--name-only", revision, "--", str(SPEC)],  # noqa: S607
        cwd=root,
        text=True,
    ).splitlines()
    documents = {
        path: subprocess.check_output(  # noqa: S603 - fixed git command, repository paths
            ["git", "show", f"{revision}:{path}"],  # noqa: S607
            cwd=root,
            text=True,
        )
        for path in names
        if path.endswith(".md")
    }
    original = inventory(documents)
    current = current_inventory(root)
    if not original or not current:
        raise ManifestError("empty requirement inventory")
    return {
        "schema_version": 1,
        "original_revision": revision,
        "original_count": len(original),
        "requirements": {
            identifier: {
                "original": original.get(identifier),
                "current": current.get(identifier),
            }
            for identifier in sorted(original.keys() | current.keys())
        },
    }


def safe_reference(root: Path, reference: str) -> None:
    path = Path(reference.split("::", 1)[0].split("#", 1)[0])
    if path.is_absolute() or ".." in path.parts or not path.parts or not (root / path).exists():
        raise ManifestError(f"unsafe or missing evidence/source path: {reference}")
    if not (root / path).resolve().is_relative_to(root.resolve()):
        raise ManifestError(f"evidence/source path escapes repository: {reference}")


def refresh_current(root: Path, snapshot: dict[str, Any], identifiers: list[str]) -> None:
    """Refresh explicitly reviewed current clauses, never their original definitions."""
    actual = current_inventory(root)
    records = snapshot["requirements"]
    changed = {
        identifier
        for identifier in actual.keys() | records.keys()
        if actual.get(identifier, {}).get("digest")
        != (records.get(identifier, {}).get("current") or {}).get("digest")
    }
    if set(identifiers) != changed or len(identifiers) != len(changed):
        raise ManifestError(
            f"refresh must name exactly the changed requirements: {sorted(changed)}"
        )
    for identifier in identifiers:
        records.setdefault(identifier, {"original": None})["current"] = actual.get(identifier)
    snapshot["requirements"] = dict(sorted(records.items()))


def resolve(decisions: dict[str, Any], identifier: str) -> dict[str, Any]:
    area = identifier.split("-")[1]
    if area not in decisions["domains"]:
        raise ManifestError(f"missing disposition domain: {area}")
    return {
        **decisions.get("defaults", {}),
        **decisions["domains"][area],
        **decisions.get("requirements", {}).get(identifier, {}),
    }


def validate(root: Path, snapshot: dict[str, Any], decisions: dict[str, Any]) -> None:
    if snapshot.get("schema_version") != 1 or decisions.get("schema_version") != 1:
        raise ManifestError("unsupported requirement ledger schema")
    for field, value in (
        ("original_revision", snapshot.get("original_revision", "")),
        ("reviewed_revision", decisions.get("reviewed_revision", "")),
    ):
        if not SHA.fullmatch(value):
            raise ManifestError(f"{field} must be a full commit SHA, not HEAD")
    records = snapshot["requirements"]
    actual = current_inventory(root)
    expected = {
        identifier for identifier, record in records.items() if record["current"] is not None
    }
    if expected != actual.keys():
        raise ManifestError(f"requirement inventory changed: {sorted(expected ^ actual.keys())}")
    if (
        sum(record["original"] is not None for record in records.values())
        != snapshot["original_count"]
    ):
        raise ManifestError("original requirement count changed")
    unknown = decisions.get("requirements", {}).keys() - records.keys()
    if unknown:
        raise ManifestError(f"disposition references unknown requirements: {sorted(unknown)}")
    automated = []
    for identifier, record in records.items():
        for kind in ("original", "current"):
            if (source := record[kind]) is not None and source["digest"] != digest(source["text"]):
                raise ManifestError(f"{identifier} {kind} snapshot digest mismatch")
        if identifier in actual and actual[identifier]["digest"] != record["current"]["digest"]:
            raise ManifestError(
                f"{identifier} specification changed; review its disposition and snapshot"
            )
        value = resolve(decisions, identifier)
        if value.get("state") not in STATES:
            raise ManifestError(f"{identifier} invalid disposition")
        if not all(
            isinstance(value.get(key), str) and value[key].strip()
            for key in ("rationale", "owner_decision")
        ):
            raise ManifestError(f"{identifier} requires rationale and owner decision")
        if not value.get("issues") or not all(
            type(number) is int and number > 0 for number in value["issues"]
        ):
            raise ManifestError(f"{identifier} requires tracking issues")
        for reference in value.get("implementation", []) + value.get("related", []):
            safe_reference(root, reference)
        if not value.get("implementation"):
            raise ManifestError(f"{identifier} requires an inspected implementation location")
        state = value["state"]
        if record["current"] is None and state not in {"accepted_replacement", "withdrawn"}:
            raise ManifestError(f"{identifier} was removed without an approved disposition")
        if state in {"accepted_replacement", "withdrawn"}:
            approval = value.get("approval", "")
            if not re.fullmatch(
                re.escape(REPOSITORY) + r"/issues/[1-9]\d*#issuecomment-[1-9]\d*", approval
            ):
                raise ManifestError(f"{identifier} requires an explicit owner approval comment")
        if state == "accepted_replacement" and not value.get("replacement", "").strip():
            raise ManifestError(f"{identifier} requires explicit replacement wording")
        if state == "implemented_tested":
            if (
                record["original"] is not None
                and record["original"]["digest"] != record["current"]["digest"]
            ):
                raise ManifestError(
                    f"{identifier} changed original wording cannot silently claim "
                    "original compliance"
                )
        if state == "implemented_tested" or value.get("tests"):
            if not SHA.fullmatch(value.get("tested_revision", "")) or not re.fullmatch(
                re.escape(REPOSITORY) + r"/actions/runs/[1-9]\d*", value.get("ci", "")
            ):
                raise ManifestError(f"{identifier} requires an exact tested revision and CI run")
            nodes = value.get("tests", [])
            if not nodes or any(
                not node.startswith("tests/") or "::test" not in node for node in nodes
            ):
                raise ManifestError(f"{identifier} requires specific collected test nodes")
            automated.extend((identifier, node) for node in nodes)
    goals = decisions.get("goals", {})
    if set(goals) != {f"G{number}" for number in range(1, 7)}:
        raise ManifestError("G1-G6 must all have explicit dispositions")
    for identifier, goal in goals.items():
        if (
            goal.get("state") not in {"open", "measured"}
            or not goal.get("rationale")
            or not goal.get("issues")
        ):
            raise ManifestError(f"{identifier} requires gate state, rationale and issues")
        for reference in goal.get("related", []):
            safe_reference(root, reference)
        if goal["state"] == "measured" and (
            not goal.get("field_record") or not SHA.fullmatch(goal.get("tested_revision", ""))
        ):
            raise ManifestError(f"{identifier} cannot be closed by a software suite alone")
        if goal.get("field_record"):
            safe_reference(root, goal["field_record"])
            field_record = Path(goal["field_record"])
            if field_record.parts[0] != "docs" or field_record.suffix != ".md":
                raise ManifestError(f"{identifier} field record must be a dated document in docs/")
    # Reuse the capability checker's real collection/skip gate, not a parallel
    # file-existence approximation. Semantic sufficiency still requires review.
    _validate_automated_nodes(root, automated)


def cell(value: object) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def links(numbers: list[int]) -> str:
    return " ".join(f"[#{number}]({REPOSITORY}/issues/{number})" for number in numbers)


def render(
    snapshot: dict[str, Any], decisions: dict[str, Any], current: dict[str, dict[str, Any]]
) -> str:
    records = snapshot["requirements"]
    counts = Counter(resolve(decisions, identifier)["state"] for identifier in records)
    lines = [
        "<!-- Generated by python -m tools.check_requirements; "
        "edit the TOML decisions and reviewed snapshot. -->",
        "# Original requirement dispositions",
        "",
        f"Original spec: `{snapshot['original_revision']}` "
        f"({snapshot['original_count']} REQ definitions). "
        f"Inspected implementation: `{decisions['reviewed_revision']}`. "
        f"This ledger covers {len(records)} original/current requirement identities plus G1–G6.",
        "",
        "`deferred` means **full requirement acceptance is still unproven or outstanding**. "
        "It does "
        "not mean the feature is absent, implementation is paused, or the owner approved a waiver. "
        "Related evidence is a starting point, not automatic proof of every MUST in a requirement. "
        "An existing capability or closed defect issue is not whole-requirement acceptance.",
        "",
        "`implemented_tested` requires reviewed original wording, exact test nodes and a passing "
        "revision/run. Changed requirements need an explicit owner decision before being labelled "
        "`accepted_replacement`; `withdrawn` also requires that approval. This ledger approves no "
        "new scope reduction or hardware gate waiver.",
        "",
        "[Disposition policy and significant conflicts](REQUIREMENT-RECONCILIATION.md) · "
        "[Capability evidence](FEATURES.md) · " + links([154, 130, 41, 118, 44, 128]),
        "",
        "Disposition counts: "
        + ", ".join(f"{name}: {counts[name]}" for name in sorted(STATES))
        + ".",
        "",
        "## Original product gates",
        "",
        "| Gate | State | Evidence / outstanding work |",
        "| --- | --- | --- |",
    ]
    for identifier, goal in sorted(decisions["goals"].items()):
        related = " ".join(
            f"[{path}]({path.removeprefix('docs/')})" for path in goal.get("related", [])
        )
        lines.append(
            f"| {identifier} | {goal['state']} | {cell(goal['rationale'])} "
            f"{related} {links(goal['issues'])} |"
        )
    for area in sorted(decisions["domains"]):
        lines.extend(
            [
                "",
                f"## {area}",
                "",
                "| Requirement | Disposition | Rationale, owner decision and evidence |",
                "| --- | --- | --- |",
            ]
        )
        for identifier, record in records.items():
            if identifier.split("-")[1] != area:
                continue
            value = resolve(decisions, identifier)
            original = record["original"]
            live = current.get(identifier)
            source = original or live
            revision = snapshot["original_revision"] if original else decisions["reviewed_revision"]
            label = (
                f"[{identifier}]({REPOSITORY}/blob/{revision}/{source['path']}#L{source['line']})"
            )
            if original is None:
                target = f"{source['path'].removeprefix('docs/')}#L{source['line']}"
                label = f"[{identifier}]({target}) (added)"
            elif live is None or original["digest"] != live["digest"]:
                label += " (changed)"
            if live is not None:
                label += f" · [current]({live['path'].removeprefix('docs/')}#L{live['line']})"
            implementation = " ".join(
                f"[source]({REPOSITORY}/tree/{decisions['reviewed_revision']}/{path})"
                for path in value["implementation"]
            )
            tests = " ".join(
                f"[{node}]({REPOSITORY}/blob/{value['tested_revision']}/{node.split('::')[0]})"
                for node in value.get("tests", [])
            )
            related = " ".join(
                f"[related]({REPOSITORY}/blob/{decisions['reviewed_revision']}/{path})"
                for path in value.get("related", [])
            )
            evidence = implementation + " " + tests + " " + related + " " + links(value["issues"])
            if value.get("ci"):
                evidence += f" [CI]({value['ci']})"
            if value.get("approval"):
                evidence += f" [owner approval]({value['approval']})"
            if value.get("replacement"):
                evidence += f" **Replacement:** {cell(value['replacement'])}"
            lines.append(
                f"| {label} | {value['state']} | {cell(value['rationale'])} "
                f"**Decision:** {cell(value['owner_decision'])} {evidence} |"
            )
    return "\n".join(lines) + "\n"


def run(root: Path, *, check: bool) -> int:
    snapshot = json.loads((root / SNAPSHOT).read_text())
    decisions = tomllib.loads((root / DECISIONS).read_text())
    validate(root, snapshot, decisions)
    expected = render(snapshot, decisions, current_inventory(root))
    output = root / OUTPUT
    if check:
        if not output.exists() or output.read_text() != expected:
            raise ManifestError("requirement disposition document is stale")
    else:
        output.write_text(expected)
    print(
        f"Requirement ledger current: {snapshot['original_count']} original, "
        f"{len(snapshot['requirements'])} total, G1-G6 explicit."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument(
        "--capture-original", help="Create a NEW reviewed snapshot from a full original Git SHA"
    )
    action.add_argument(
        "--refresh-current",
        nargs="+",
        metavar="REQ-ID",
        help="Refresh exactly the reviewed changed clauses; preserve original definitions",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        if args.capture_original:
            destination = root / SNAPSHOT
            if destination.exists():
                raise ManifestError("snapshot already exists; update reviewed entries explicitly")
            destination.write_text(
                json.dumps(capture(root, args.capture_original), indent=2) + "\n"
            )
            print(f"Captured original/current requirement definitions in {SNAPSHOT}")
            return 0
        if args.refresh_current:
            destination = root / SNAPSHOT
            snapshot = json.loads(destination.read_text())
            refresh_current(root, snapshot, args.refresh_current)
            decisions = tomllib.loads((root / DECISIONS).read_text())
            validate(root, snapshot, decisions)
            destination.write_text(json.dumps(snapshot, indent=2) + "\n")
            print("Refreshed reviewed current clauses: " + ", ".join(args.refresh_current))
        return run(root, check=args.check)
    except (ManifestError, KeyError, TypeError, OSError, ValueError) as error:
        print(f"Requirement ledger: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
