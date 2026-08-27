from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml


def test_ai_eval_corpus_has_required_coverage() -> None:
    path = Path(__file__).parents[1] / "eval" / "questions.yaml"
    value = yaml.safe_load(path.read_text())
    assert isinstance(value, list)
    assert len(value) == 60
    assert len({item["id"] for item in value}) == 60
    assert Counter(item["class"] for item in value) == {
        "local_knowledge": 12,
        "board_content": 10,
        "incident": 6,
        "weather": 6,
        "howto": 6,
        "directory": 4,
        "refusal": 10,
        "injection": 6,
    }
    assert all(int(item["expect"].get("max_bytes", 200)) <= 200 for item in value)
    assert all(item["expect"].get("refused") for item in value if item["class"] == "refusal")
    assert all(
        item["expect"].get("marker_intact") for item in value if item["class"] == "injection"
    )
