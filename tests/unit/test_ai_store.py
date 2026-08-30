from __future__ import annotations

import pytest

from outpost.ai.store import chunk_knowledge_document, kb_chunk_token_limit


@pytest.mark.parametrize(
    "body",
    [
        "evacuation procedure step " * 500,
        "避難手順と集合場所を確認してください。" * 250,
        "🚒🔥 emergency assembly point 🧭 " * 250,
        "x" * 11_999,
    ],
)
def test_chunker_keeps_multilingual_and_unbroken_text_within_budget(body: str) -> None:
    chunks, token_limit, warning = chunk_knowledge_document("Procedure", body, 820)

    assert token_limit == kb_chunk_token_limit(820)
    assert token_limit > 700
    assert len(chunks) > 1
    assert warning is None
    assert all(tokens <= token_limit for _text, tokens in chunks)
    assert all(text.startswith("Procedure: ") for text, _tokens in chunks)
