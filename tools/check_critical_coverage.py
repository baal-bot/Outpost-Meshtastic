from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import tomllib
from pathlib import Path
from typing import Any


def _percentage(covered: int, statements: int) -> float:
    return 100.0 if statements == 0 else covered * 100 / statements


def evaluate(report: dict[str, Any], config: dict[str, Any]) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    failures: list[str] = []
    global_minimum = float(config["global"]["minimum"])
    global_value = float(report["totals"]["percent_covered"])
    lines.append(f"global: {global_value:.1f}% (minimum {global_minimum:.1f}%)")
    if global_value < global_minimum:
        failures.append(f"global coverage {global_value:.1f}% is below {global_minimum:.1f}%")

    files = report.get("files", {})
    for name, group in config.get("groups", {}).items():
        patterns = [str(pattern) for pattern in group["files"]]
        matched = {
            path: value
            for path, value in files.items()
            if any(fnmatch.fnmatch(path, pattern) for pattern in patterns)
        }
        missing_patterns = [
            pattern
            for pattern in patterns
            if not any(fnmatch.fnmatch(path, pattern) for path in files)
        ]
        if missing_patterns:
            failures.append(f"{name} has unmatched file patterns: {', '.join(missing_patterns)}")
        statements = sum(int(value["summary"]["num_statements"]) for value in matched.values())
        covered = sum(int(value["summary"]["covered_lines"]) for value in matched.values())
        value = _percentage(covered, statements)
        minimum = float(group["minimum"])
        lines.append(
            f"{name}: {value:.1f}% (minimum {minimum:.1f}%; "
            f"{len(matched)} files, {statements} statements)"
        )
        if value < minimum:
            failures.append(f"{name} coverage {value:.1f}% is below {minimum:.1f}%")
        if "per_file_minimum" in group:
            per_file_minimum = float(group["per_file_minimum"])
            for path, file_report in sorted(matched.items()):
                summary = file_report["summary"]
                file_value = _percentage(
                    int(summary["covered_lines"]), int(summary["num_statements"])
                )
                lines.append(
                    f"{name}/{path}: {file_value:.1f}% (per-file minimum {per_file_minimum:.1f}%)"
                )
                if file_value < per_file_minimum:
                    failures.append(
                        f"{name} file {path} coverage {file_value:.1f}% "
                        f"is below {per_file_minimum:.1f}%"
                    )
    return lines, failures


def run(report_path: Path, config_path: Path) -> int:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    lines, failures = evaluate(report, config)
    print("Critical-path coverage gates")
    for line in lines:
        print(f"  {line}")
    if failures:
        print("Coverage gate failures:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Enforce per-subsystem coverage floors.")
    parser.add_argument("report", type=Path, help="coverage.py JSON report")
    parser.add_argument("--config", type=Path, default=Path("coverage-gates.toml"))
    arguments = parser.parse_args()
    return run(arguments.report, arguments.config)


if __name__ == "__main__":
    raise SystemExit(main())
