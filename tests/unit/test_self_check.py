from __future__ import annotations

import re
from pathlib import Path

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
        "error": None,
    }


def test_intent_status_reports_missing_and_malformed_maps(tmp_path: Path) -> None:
    path = tmp_path / "intents.yaml"
    missing = IntentResolver(str(path)).status()
    assert missing["exists"] is False
    assert missing["error"] == "configured intent map was not found"

    path.write_text("not: a-list\n", encoding="utf-8")
    malformed = IntentResolver(str(path)).status()
    assert malformed["exists"] is True
    assert malformed["loaded"] == 0
    assert malformed["error"] == "TypeError: intent map must contain a list"
