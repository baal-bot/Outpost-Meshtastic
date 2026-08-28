from hypothesis import given
from hypothesis import strategies as st

from outpost.transport.chunker import chunk_text


def test_multibyte_chunks_are_valid_and_bounded() -> None:
    text = "café roads " * 50 + "警報 end"
    parts = chunk_text(text, max_parts=4)
    assert parts
    assert all(len(part.encode()) <= 200 for part in parts)
    assert all("�" not in part for part in parts)


def test_short_screen_keeps_intentional_line_breaks() -> None:
    parts = chunk_text("OUTPOST / HOME\n1 Weather\n2 Mail\n0 Home")

    assert parts == ["OUTPOST / HOME\n1 Weather\n2 Mail\n0 Home"]


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
