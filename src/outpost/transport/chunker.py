from __future__ import annotations

import re

BOUNDARY = re.compile(r"(?<=[.!?])\s+|(?=\n[-*] )|\s+")


def _utf8_prefix(text: str, budget: int) -> str:
    """Return the longest code-point-safe prefix within a UTF-8 byte budget."""
    if budget <= 0:
        return ""
    used = 0
    end = 0
    for end, character in enumerate(text, 1):
        used += len(character.encode())
        if used > budget:
            return text[: end - 1]
    return text[:end]


def truncate_utf8(text: str, byte_limit: int, *, marker: str = "…") -> str:
    """Truncate text without splitting a Unicode code point or exceeding byte_limit."""
    if byte_limit < 0:
        raise ValueError("byte_limit must be non-negative")
    if len(text.encode()) <= byte_limit:
        return text
    safe_marker = _utf8_prefix(marker, byte_limit)
    remaining = byte_limit - len(safe_marker.encode())
    return _utf8_prefix(text, remaining) + safe_marker


def _fit(text: str, budget: int) -> tuple[str, str]:
    if len(text.encode()) <= budget:
        return text, ""
    candidates = [match.start() for match in BOUNDARY.finditer(text)]
    valid = [idx for idx in candidates if len(text[:idx].rstrip().encode()) <= budget]
    if valid:
        idx = valid[-1]
    else:
        prefix = _utf8_prefix(text, budget)
        if not prefix:
            raise ValueError("byte budget cannot hold one Unicode code point")
        idx = len(prefix)
    return text[:idx].rstrip(), text[idx:].lstrip()


def chunk_text(text: str, *, byte_limit: int = 200, max_parts: int = 3) -> list[str]:
    if max_parts < 1:
        raise ValueError("max_parts must be positive")
    text = "\n".join(" ".join(line.split()) for line in text.strip().splitlines() if line.strip())
    if not text:
        return []
    if len(text.encode()) <= byte_limit:
        return [text]
    # Reserve the worst suffix width before choosing boundaries, then add actual suffixes.
    suffix_width = len(f" ({max_parts}/{max_parts})".encode())
    if byte_limit - suffix_width < 4:
        raise ValueError("byte_limit is too small for multipart UTF-8 text")
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
