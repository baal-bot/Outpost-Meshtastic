from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from outpost.router.intents import BUILTIN_INTENTS, IntentResolver
from outpost.self_check import CHECK_NAMES


def test_readiness_checklist_exactly_tracks_implemented_checks() -> None:
    checklist = Path("docs/SAFETY-READINESS.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"check: `([a-z_]+)`", checklist))
    assert documented == CHECK_NAMES


def test_intent_status_counts_loaded_and_rejected_entries(tmp_path: Path) -> None:
    path = tmp_path / "intents.yaml"
    path.write_text(
        "- pattern: '^shelter$'\n  command: 'BOARDS'\n"
        "- pattern: '[invalid'\n  command: 'MENU'\n"
        "- not_a_mapping: true\n",
        encoding="utf-8",
    )
    resolver = IntentResolver(str(path))
    assert resolver.status() == {
        "path": str(path),
        "exists": True,
        "loaded": 1,
        "rejected": 2,
        "builtin": len(BUILTIN_INTENTS),
        "state": "partial",
        "error": None,
        "issues": [
            {"index": 2, "reason": "invalid regex: unterminated character set"},
            {"index": 3, "reason": "pattern and command must be non-empty strings"},
        ],
    }


def test_intent_status_reports_missing_and_malformed_maps(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "intents.yaml"
    with caplog.at_level(logging.WARNING, logger="outpost.router.intents"):
        missing = IntentResolver(str(path)).status()
    assert missing["exists"] is False
    assert missing["state"] == "missing"
    assert missing["error"] == "configured intent map was not found"
    assert f"Intent map {path} file rejected: configured intent map was not found" in caplog.text

    path.write_text("not: a-list\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="outpost.router.intents"):
        malformed = IntentResolver(str(path)).status()
    assert malformed["exists"] is True
    assert malformed["loaded"] == 0
    assert malformed["state"] == "error"
    assert malformed["error"] == "TypeError: intent map must contain a list"
    assert f"Intent map {path} file rejected: TypeError" in caplog.text


def test_intent_warnings_name_file_entry_and_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "intents.yaml"
    path.write_text(
        "- patern: '^typo$'\n  command: MENU\n- pattern: '[broken'\n  command: MENU\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="outpost.router.intents"):
        status = IntentResolver(str(path)).status()

    assert status["state"] == "rejected_all"
    assert status["rejected"] == 2
    assert str(path) in caplog.text
    assert "entry 1 rejected: pattern and command must be non-empty strings" in caplog.text
    assert "entry 2 rejected: invalid regex" in caplog.text


def test_failed_intent_read_retries_without_an_mtime_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "intents.yaml"
    path.write_text("- pattern: '^shelter$'\n  command: BOARDS\n", encoding="utf-8")
    original = Path.read_text
    attempts = 0

    def flaky_read(target: Path, *args: object, **kwargs: object) -> str:
        nonlocal attempts
        if target == path and attempts == 0:
            attempts += 1
            raise OSError("temporary read failure")
        attempts += 1
        return original(target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", flaky_read)
    resolver = IntentResolver(str(path))
    with caplog.at_level(logging.WARNING, logger="outpost.router.intents"):
        first = resolver.status()
    second = resolver.status()

    assert first["state"] == "error"
    assert "configured intent map could not be read" in caplog.text
    assert second["state"] == "ready"
    assert second["loaded"] == 1
    assert attempts == 2


def test_empty_and_unparseable_intent_maps_are_distinct(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "intents.yaml"
    path.write_text("[]\n", encoding="utf-8")
    assert IntentResolver(str(path)).status()["state"] == "empty"

    path.write_text("- pattern: '[unterminated\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="outpost.router.intents"):
        broken = IntentResolver(str(path)).status()
    assert broken["state"] == "error"
    assert "configured intent map could not be parsed" in str(broken["error"])
    assert "configured intent map could not be parsed" in caplog.text
