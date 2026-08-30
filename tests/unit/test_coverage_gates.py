import json
import subprocess
import sys
from pathlib import Path

CHECKER = Path(__file__).parents[2] / "tools" / "check_critical_coverage.py"


def run_checker(
    tmp_path: Path, report: dict[str, object], config: str
) -> subprocess.CompletedProcess:
    report_path = tmp_path / "coverage.json"
    config_path = tmp_path / "gates.toml"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    config_path.write_text(config, encoding="utf-8")
    return subprocess.run(  # noqa: S603 -- fixed interpreter and repository script
        [sys.executable, str(CHECKER), str(report_path), "--config", str(config_path)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_critical_coverage_gates_are_weighted_and_fail_closed(tmp_path: Path) -> None:
    report = {
        "totals": {"percent_covered": 80.0},
        "files": {
            "src/outpost/example/one.py": {"summary": {"num_statements": 90, "covered_lines": 81}},
            "src/outpost/example/two.py": {"summary": {"num_statements": 10, "covered_lines": 5}},
        },
    }
    result = run_checker(
        tmp_path,
        report,
        """
[global]
minimum = 75
[groups.example]
minimum = 85
files = ["src/outpost/example/*.py"]
[groups.missing]
minimum = 1
files = ["src/outpost/missing.py"]
""",
    )

    assert result.returncode == 1
    assert "example: 86.0%" in result.stdout
    assert "example coverage" not in result.stderr
    assert "missing has unmatched file patterns" in result.stderr


def test_critical_coverage_gate_reports_global_and_group_regressions(tmp_path: Path) -> None:
    report = {
        "totals": {"percent_covered": 69.0},
        "files": {
            "src/outpost/example.py": {"summary": {"num_statements": 10, "covered_lines": 6}}
        },
    }
    result = run_checker(
        tmp_path,
        report,
        """
[global]
minimum = 70
[groups.example]
minimum = 65
files = ["src/outpost/example.py"]
""",
    )

    assert result.returncode == 1
    assert "global coverage 69.0% is below 70.0%" in result.stderr
    assert "example coverage 60.0% is below 65.0%" in result.stderr


def test_per_file_floor_cannot_be_masked_by_a_well_covered_sibling(tmp_path: Path) -> None:
    report = {
        "totals": {"percent_covered": 90.0},
        "files": {
            "src/outpost/safety/strong.py": {
                "summary": {"num_statements": 90, "covered_lines": 90}
            },
            "src/outpost/safety/weak.py": {"summary": {"num_statements": 10, "covered_lines": 2}},
        },
    }
    result = run_checker(
        tmp_path,
        report,
        """
[global]
minimum = 70
[groups.safety]
minimum = 80
per_file_minimum = 75
files = ["src/outpost/safety/*.py"]
""",
    )

    assert result.returncode == 1
    assert "safety: 92.0%" in result.stdout
    assert "safety/src/outpost/safety/weak.py: 20.0%" in result.stdout
    assert "safety file src/outpost/safety/weak.py coverage 20.0%" in result.stderr
