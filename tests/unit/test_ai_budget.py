from __future__ import annotations

import pytest

from outpost.ai.budget import (
    BudgetError,
    EvidenceChunk,
    TokenBudgeter,
    conservative_tokens,
)
from outpost.config import AIBudgetConfig


def test_default_plan_never_encroaches_on_output_or_margin() -> None:
    budgeter = TokenBudgeter(AIBudgetConfig(), 2048)
    plan = budgeter.plan(
        system="local grounded assistant " * 20,
        tools="tool schema " * 20,
        history=("old turn " * 100, "new turn " * 100, "latest turn " * 100),
        question="question " * 100,
    )

    assert plan.committed_tokens <= 2048
    assert plan.output_reserve == 220
    assert plan.safety_margin == 308
    assert plan.evidence_limit <= 820
    assert len(plan.history) <= 2
    assert conservative_tokens(plan.question) <= 110
    assert plan.question.endswith("[truncated]")


def test_smaller_context_shrinks_evidence_first() -> None:
    config = AIBudgetConfig()
    prompt = {
        "system": "s" * 600,
        "tools": "t" * 400,
        "history": ("h" * 300, "i" * 300),
        "question": "q" * 300,
    }
    large = TokenBudgeter(config, 4096).plan(**prompt)
    small = TokenBudgeter(config, 1600).plan(**prompt)

    assert large.evidence_limit == 820
    assert small.evidence_limit < large.evidence_limit
    assert small.output_reserve == large.output_reserve == 220
    assert small.safety_margin >= 240


def test_system_prompt_limit_and_tiny_provider_are_hard_failures() -> None:
    with pytest.raises(BudgetError, match="at least 1600"):
        TokenBudgeter(AIBudgetConfig(), 1599)
    budgeter = TokenBudgeter(AIBudgetConfig(system_tokens=64), 2048)
    with pytest.raises(BudgetError, match="system prompt"):
        budgeter.plan(system="word " * 100, question="question")


def test_evidence_pack_is_greedy_deduplicated_and_source_bounded() -> None:
    budgeter = TokenBudgeter(AIBudgetConfig(evidence_tokens=80), 2048)
    plan = budgeter.plan(system="system", question="question")
    pack = budgeter.pack_evidence(
        plan,
        [
            EvidenceChunk("board:roads#1", "board", "highest", 10),
            EvidenceChunk("board:roads#1", "board", "duplicate", 9),
            EvidenceChunk("board:roads#2", "board", "second", 8),
            EvidenceChunk("board:roads#3", "board", "capped", 7),
            EvidenceChunk("inc:31", "incident", "tree down", 6),
        ],
        per_source_cap=2,
    )

    assert [item.ref for item in pack.chunks] == [
        "board:roads#1",
        "board:roads#2",
        "inc:31",
    ]
    assert "duplicate" not in pack.text
    assert "capped" not in pack.text
    assert pack.tokens <= plan.evidence_limit
    assert "UNTRUSTED DATA" in pack.text
