import pytest
from hypothesis import given
from hypothesis import strategies as st

from outpost.transport.chunker import chunk_text, truncate_utf8


def test_multibyte_chunks_are_valid_and_bounded() -> None:
    text = "café roads " * 50 + "警報 end"
    parts = chunk_text(text, max_parts=4)
    assert parts
    assert all(len(part.encode()) <= 200 for part in parts)
    assert all("�" not in part for part in parts)


def test_short_screen_keeps_intentional_line_breaks() -> None:
    parts = chunk_text("OUTPOST / HOME\n1 Weather\n2 Mail\n0 Home")

    assert parts == ["OUTPOST / HOME\n1 Weather\n2 Mail\n0 Home"]


@pytest.mark.parametrize(
    "text",
    [
        "警報発令中避難所へ移動してください" * 20,
        "แจ้งเตือนฉุกเฉินโปรดอพยพ" * 20,
        "🚨🧑🏽‍🚒🏥" * 40,
        "e\u0301" * 150,
        "אבגדהוזחטי" * 30,
        "a3f02b19" * 50,
    ],
)
def test_unbreakable_unicode_is_chunked_without_failure(text: str) -> None:
    parts = chunk_text(text, max_parts=3)

    assert 1 <= len(parts) <= 3
    assert all(len(part.encode()) <= 200 for part in parts)
    assert all(part.encode().decode() == part for part in parts)
    assert parts[-1].endswith((" (1/1)", " (2/2)", " (3/3)"))


def test_utf8_truncation_reserves_marker_bytes() -> None:
    result = truncate_utf8("🚨" * 100, 233)

    assert result.endswith("…")
    assert len(result.encode()) <= 233
    assert result.encode().decode() == result


@given(
    st.lists(
        st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=20),
        min_size=1,
        max_size=30,
    )
)
def test_chunker_never_exceeds_budget(words: list[str]) -> None:
    words = [word.replace("\x00", "") for word in words if word and len(word.encode()) <= 120]
    if not words:
        return
    parts = chunk_text(" ".join(words), max_parts=4)
    assert all(len(part.encode()) <= 200 for part in parts)
