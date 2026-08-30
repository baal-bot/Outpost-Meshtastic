import ast
from pathlib import Path

TEST_ROOT = Path(__file__).parents[1]


def test_integration_governors_cannot_bypass_the_durable_outbox() -> None:
    violations: list[str] = []
    for suite in (TEST_ROOT / "integration", TEST_ROOT / "acceptance"):
        for path in suite.glob("test_*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                name = function.id if isinstance(function, ast.Name) else None
                if name != "AirtimeGovernor":
                    continue
                if not any(keyword.arg == "outbox" for keyword in node.keywords):
                    violations.append(f"{path.relative_to(TEST_ROOT)}:{node.lineno}")

    assert violations == [], (
        "Integration governors must use production_governor() or explicitly pass outbox=: "
        + ", ".join(violations)
    )


def test_shared_production_fixtures_are_in_the_production_coverage_run() -> None:
    missing_markers: list[str] = []
    for suite in (TEST_ROOT / "integration", TEST_ROOT / "acceptance"):
        for path in suite.glob("test_*.py"):
            source = path.read_text(encoding="utf-8")
            if not any(name in source for name in ("production_governor", "fresh_install")):
                continue
            if "pytestmark = pytest.mark.production_wiring" not in source:
                missing_markers.append(str(path.relative_to(TEST_ROOT)))

    assert missing_markers == [], (
        "Production-wired suites must feed production-coverage.json: " + ", ".join(missing_markers)
    )
