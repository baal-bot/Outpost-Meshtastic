from __future__ import annotations

import json
from pathlib import Path

from outpost.audit import display_audit_detail, encode_audit_detail


def test_audit_detail_redacts_nested_and_plain_text_secrets() -> None:
    encoded = encode_audit_detail(
        {
            "safe": "visible",
            "settings": {"api_key": "private", "label": "retained"},
            "items": [{"password": "hidden"}],
        }
    )

    assert json.loads(encoded or "{}") == {
        "items": [{"password": "[REDACTED]"}],
        "safe": "visible",
        "settings": {"api_key": "[REDACTED]", "label": "retained"},
    }
    assert encode_audit_detail("token=private; result=retained") == (
        "token=[REDACTED]; result=retained"
    )
    displayed, format_name = display_audit_detail(encoded)
    assert format_name == "json"
    assert "private" not in (displayed or "")


def test_all_python_audit_writes_use_the_shared_helper() -> None:
    source = Path(__file__).parents[2] / "src" / "outpost"
    offenders = [
        path.relative_to(source).as_posix()
        for path in source.rglob("*.py")
        if path.name != "audit.py" and "INSERT INTO audit_log" in path.read_text()
    ]

    assert offenders == []
