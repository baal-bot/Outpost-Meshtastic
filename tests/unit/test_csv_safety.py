from __future__ import annotations

from pathlib import Path

import pytest

from outpost.csv_safety import csv_safe


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "  =", "\t+"])
def test_csv_safe_neutralizes_spreadsheet_formula_prefixes(prefix: str) -> None:
    value = f"{prefix}payload"
    assert csv_safe(value) == f"'{value}"


def test_every_csv_writer_uses_the_shared_sanitizer() -> None:
    source_root = Path(__file__).parents[2] / "src" / "outpost"
    writers = []
    for path in source_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "csv.DictWriter" in source:
            writers.append(path.relative_to(source_root).as_posix())
            assert "csv_safe_row" in source, path
    assert sorted(writers) == [
        "watch/checkin.py",
        "watch/reports.py",
        "web/member_triage.py",
    ]


def test_every_csv_response_is_backed_by_an_audited_writer() -> None:
    api = (Path(__file__).parents[2] / "src" / "outpost" / "web" / "api.py").read_text(
        encoding="utf-8"
    )
    assert api.count('media_type="text/csv"') == 3
    assert "checkins.csv_export(event_id)" in api
    assert "effective_incident_reports.csv_export(value)" in api
    assert "member_triage.export(member_ids" in api
