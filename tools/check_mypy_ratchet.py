#!/usr/bin/env python3
"""Run strict mypy over all of Outpost and cap known debt per source module."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).with_name("mypy-ratchet.json")
ERROR = re.compile(r"^(?P<path>.+?\.py):\d+(?::\d+)?: error:", re.MULTILINE)


def error_counts(output: str) -> dict[str, int]:
    """Return stable repository-relative error counts from mypy output."""
    counts: Counter[str] = Counter()
    for match in ERROR.finditer(output):
        path = Path(match.group("path"))
        try:
            normalized = (
                path.resolve().relative_to(ROOT).as_posix()
                if path.is_absolute()
                else path.as_posix()
            )
        except ValueError:
            normalized = path.as_posix()
        counts[normalized] += 1
    return dict(counts)


def regressions(observed: dict[str, int], baseline: dict[str, int]) -> list[str]:
    failures = []
    for module, count in sorted(observed.items()):
        ceiling = baseline.get(module, 0)
        if count > ceiling:
            failures.append(f"{module}: {count} strict errors (ceiling {ceiling})")
    return failures


def main() -> int:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    if not isinstance(baseline, dict) or not all(
        isinstance(path, str) and isinstance(count, int) and count >= 0
        for path, count in baseline.items()
    ):
        print(f"Invalid mypy ratchet baseline: {BASELINE}", file=sys.stderr)
        return 2
    command = [
        sys.executable,
        "-m",
        "mypy",
        "--config-file=/dev/null",
        "--no-incremental",
        "--strict",
        "--python-version=3.12",
        "src/outpost",
    ]
    completed = subprocess.run(  # noqa: S603 - every argument is a local constant
        command, cwd=ROOT, capture_output=True, check=False, text=True
    )
    output = completed.stdout + completed.stderr
    if completed.returncode not in {0, 1}:
        print(output, file=sys.stderr, end="")
        print(f"mypy ratchet could not run (exit {completed.returncode}).", file=sys.stderr)
        return 2
    observed = error_counts(output)
    failures = regressions(observed, baseline)
    if failures:
        print("Strict mypy debt increased:", file=sys.stderr)
        print("\n".join(f"- {failure}" for failure in failures), file=sys.stderr)
        return 1
    remaining = sum(observed.values())
    reductions = {
        module: baseline[module] - observed.get(module, 0)
        for module in baseline
        if observed.get(module, 0) < baseline[module]
    }
    print(f"Strict mypy ratchet passed: {remaining} grandfathered errors remain.")
    if reductions:
        print("Debt shrank; lower these baseline entries when convenient:")
        for module, count in sorted(reductions.items()):
            print(f"- {module}: -{count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
