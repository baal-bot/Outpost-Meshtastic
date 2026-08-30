from __future__ import annotations

import ast
from pathlib import Path


def test_admission_results_cannot_be_discarded() -> None:
    root = Path(__file__).parents[2] / "src" / "outpost"
    discarded: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Await):
                continue
            call = node.value.value
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr in {"admit", "admit_many", "admit_many_result"}:
                discarded.append(f"{path.relative_to(root)}:{node.lineno}")
    assert discarded == [], f"discarded admission results: {discarded}"
