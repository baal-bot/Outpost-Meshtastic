from __future__ import annotations

import re

BOUNDARY = re.compile(r"(?<=[.!?])\s+|(?=\n[-*] )|\s+")


def _fit(text: str, budget: int) -> tuple[str, str]:
    if len(text.encode()) <= budget:
        return text, ""
    candidates = [match.start() for match in BOUNDARY.finditer(text)]
    valid = [idx for idx in candidates if len(text[:idx].rstrip().encode()) <= budget]
    if not valid:
        raise ValueError("a single word exceeds the byte budget")
    idx = valid[-1]
    return text[:idx].rstrip(), text[idx:].lstrip()


def chunk_text(text: str, *, byte_limit: int = 200, max_parts: int = 3) -> list[str]:
    text = "\n".join(" ".join(line.split()) for line in text.strip().splitlines() if line.strip())
    if not text:
        return []
    if len(text.encode()) <= byte_limit:
        return [text]
    # Reserve the worst suffix width before choosing boundaries, then add actual suffixes.
    suffix_width = len(f" ({max_parts}/{max_parts})".encode())
    pieces: list[str] = []
    rest = text
    while rest and len(pieces) < max_parts:
        piece, rest = _fit(rest, byte_limit - suffix_width)
        pieces.append(piece)
    if rest:
        marker = "…MORE"
        pieces[-1], _ = _fit(pieces[-1], byte_limit - suffix_width - len(marker.encode()))
        pieces[-1] = pieces[-1].rstrip(".,;: ") + marker
    count = len(pieces)
    result = [f"{piece} ({idx}/{count})" for idx, piece in enumerate(pieces, 1)]
    if any(len(part.encode()) > byte_limit for part in result):
        raise AssertionError("chunker exceeded byte budget")
    return result
