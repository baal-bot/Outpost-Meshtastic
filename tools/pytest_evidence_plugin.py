"""Pytest collection hook used by the capability evidence validator."""

from __future__ import annotations

import json

from _pytest.main import Session
from _pytest.skipping import evaluate_skip_marks

PREFIX = "OUTPOST_EVIDENCE_COLLECTION="


def pytest_collection_finish(session: Session) -> None:
    collected = {}
    for item in session.items:
        skipped = evaluate_skip_marks(item)
        collected[item.nodeid] = None if skipped is None else skipped.reason
    print(f"{PREFIX}{json.dumps(collected, separators=(',', ':'), sort_keys=True)}")
