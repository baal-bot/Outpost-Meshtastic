from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from outpost.ai.budget import (
    EVIDENCE_PREAMBLE,
    SEARCH_KB_RESULT_TOKENS,
    conservative_tokens,
)
from outpost.config import AIBudgetConfig
from outpost.store import Database

_MAX_KB_SLUG_LENGTH = 64
_CHUNK_OVERLAP_TARGET_TOKENS = 24


def kb_chunk_token_limit(evidence_tokens: int) -> int:
    """Largest chunk text that fits both normal evidence and search_kb output."""
    reference_tokens = conservative_tokens(f"[kb:{'x' * _MAX_KB_SLUG_LENGTH}] ")
    evidence_overhead = conservative_tokens(EVIDENCE_PREAMBLE + "\n") + reference_tokens
    return max(
        0,
        min(
            evidence_tokens - evidence_overhead,
            SEARCH_KB_RESULT_TOKENS - reference_tokens,
        ),
    )


def _chunk_overlap_tokens(title: str, token_limit: int) -> int:
    body_limit = token_limit - conservative_tokens(f"{title}: ")
    return min(_CHUNK_OVERLAP_TARGET_TOKENS, max(0, body_limit // 5))


def _prefix_within_tokens(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if conservative_tokens(text) <= limit:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if conservative_tokens(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    candidate = text[:low]
    minimum = max(1, int(len(candidate) * 0.55))
    boundary = max(
        candidate.rfind("\n\n", minimum),
        candidate.rfind("\n", minimum),
        candidate.rfind(". ", minimum),
        candidate.rfind("; ", minimum),
        candidate.rfind(", ", minimum),
        candidate.rfind(" ", minimum),
    )
    if boundary >= minimum:
        candidate = candidate[: boundary + (1 if candidate[boundary] != "\n" else 0)]
    return candidate.rstrip()


def _suffix_within_tokens(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high) // 2
        if conservative_tokens(text[middle:]) <= limit:
            high = middle
        else:
            low = middle + 1
    candidate = text[low:]
    boundary = max(candidate.find(" "), candidate.find("\n"))
    if 0 <= boundary < len(candidate) // 3:
        candidate = candidate[boundary + 1 :]
    return candidate.lstrip()


def chunk_knowledge_document(
    title: str, body: str, evidence_tokens: int
) -> tuple[list[tuple[str, int]], int, str | None]:
    """Return overlapping, budget-safe chunk text and its effective profile."""
    token_limit = kb_chunk_token_limit(evidence_tokens)
    prefix = f"{title}: "
    body_limit = token_limit - conservative_tokens(prefix)
    if body_limit < 1:
        text = prefix + body
        return (
            [(text, conservative_tokens(text))],
            token_limit,
            "The configured evidence budget is too small to retrieve this document.",
        )

    overlap_tokens = min(_CHUNK_OVERLAP_TARGET_TOKENS, max(0, body_limit // 5))
    remaining = body
    chunks: list[tuple[str, int]] = []
    while remaining:
        segment = _prefix_within_tokens(remaining, body_limit)
        if not segment:
            text = prefix + remaining
            chunks.append((text, conservative_tokens(text)))
            return (
                chunks,
                token_limit,
                "Part of this document cannot fit the configured retrieval budget.",
            )
        text = prefix + segment
        chunks.append((text, conservative_tokens(text)))
        if len(segment) >= len(remaining):
            break
        overlap = _suffix_within_tokens(segment, overlap_tokens)
        consumed = len(segment)
        remaining = (overlap + " " + remaining[consumed:].lstrip()).strip()

    warning = next(
        (
            "Part of this document cannot fit the configured retrieval budget."
            for _text, tokens in chunks
            if tokens > token_limit
        ),
        None,
    )
    return chunks, token_limit, warning


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not normalized:
        raise ValueError("knowledge-base slug cannot be empty")
    return normalized[:64]


@dataclass(frozen=True)
class InteractionRecord:
    member_id: int | None
    channel: int
    question: str
    question_class: str
    provider: str
    model: str
    evidence_refs: tuple[str, ...]
    answer: str | None
    grounded: bool
    refused: bool
    refusal_reason: str | None
    outcome: str
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    ttft_ms: int | None = None
    total_ms: int | None = None


@dataclass(frozen=True)
class KBDocumentSaveResult:
    document_id: int
    chunk_count: int
    retrievable: bool
    warning: str | None

    def as_dict(self) -> dict[str, int | bool | str | None]:
        return {
            "id": self.document_id,
            "chunk_count": self.chunk_count,
            "retrievable": self.retrievable,
            "warning": self.warning,
        }


class AIStore:
    def __init__(self, database: Database, *, evidence_tokens: int | None = None) -> None:
        self.database = database
        self.evidence_tokens = (
            AIBudgetConfig().evidence_tokens if evidence_tokens is None else evidence_tokens
        )

    async def log(self, record: InteractionRecord) -> int:
        return await self.database.write(
            """
            INSERT INTO ai_interaction(
              member_id,channel,question,question_class,provider,model,tools_called,
              evidence_refs,answer,grounded,refused,refusal_reason,outcome,
              prompt_tokens,output_tokens,ttft_ms,total_ms,created_at
            ) VALUES(?,?,?,?,?,?,'[]',?,?,?,?,?,?,?,?,?,?,unixepoch())
            """,
            (
                record.member_id,
                record.channel,
                record.question[:1000],
                record.question_class,
                record.provider,
                record.model,
                json.dumps(record.evidence_refs),
                record.answer,
                int(record.grounded),
                int(record.refused),
                record.refusal_reason,
                record.outcome,
                record.prompt_tokens,
                record.output_tokens,
                record.ttft_ms,
                record.total_ms,
            ),
        )

    async def interactions(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.database.read(
            """
            SELECT ai.*,COALESCE(m.handle,m.mesh_id) member
            FROM ai_interaction ai LEFT JOIN member m ON m.id=ai.member_id
            ORDER BY ai.created_at DESC LIMIT ?
            """,
            (min(max(limit, 1), 250),),
        )
        values: list[dict[str, Any]] = []
        for raw in rows:
            value = dict(raw)
            value["evidence_refs"] = json.loads(value["evidence_refs"])
            value["tools_called"] = json.loads(value["tools_called"])
            values.append(value)
        return values

    async def rate(self, interaction_id: int, rating: int) -> bool:
        if rating not in {-1, 0, 1}:
            raise ValueError("rating must be -1, 0, or 1")
        rows = await self.database.read(
            "SELECT id FROM ai_interaction WHERE id=?", (interaction_id,)
        )
        if not rows:
            return False
        await self.database.write(
            "UPDATE ai_interaction SET rated=? WHERE id=?", (rating, interaction_id)
        )
        return True

    async def documents(self) -> list[dict[str, Any]]:
        token_limit = kb_chunk_token_limit(self.evidence_tokens)
        rows = await self.database.read(
            """
            SELECT d.*,COUNT(c.id) chunk_count,COALESCE(MAX(c.token_count),0) max_chunk_tokens
            FROM kb_document d LEFT JOIN kb_chunk c ON c.document_id=d.id
            GROUP BY d.id ORDER BY d.pinned DESC,d.title COLLATE NOCASE
            """
        )
        values = [dict(row) for row in rows]
        for value in values:
            max_chunk_tokens = int(value.pop("max_chunk_tokens"))
            value["retrievable"] = bool(
                value["chunk_count"]
                and value["chunk_token_limit"] == token_limit
                and value["chunk_overlap_tokens"]
                == _chunk_overlap_tokens(str(value["title"]), token_limit)
                and max_chunk_tokens <= token_limit
            )
            value["warning"] = (
                None
                if value["retrievable"]
                else "This document does not fit the current AI retrieval budget."
            )
        return values

    async def save_document(
        self,
        *,
        title: str,
        body: str,
        slug: str | None = None,
        source: str = "operator",
        document_id: int | None = None,
    ) -> KBDocumentSaveResult:
        title = title.strip()
        body = body.strip()
        if not title or not body:
            raise ValueError("title and body are required")
        if len(title) > 120 or len(body.encode()) > 12_000:
            raise ValueError("knowledge-base document is too large")
        document_slug = _slug(slug or title)
        chunks, token_limit, warning = chunk_knowledge_document(title, body, self.evidence_tokens)
        try:
            async with self.database.transaction() as transaction:
                if document_id is None:
                    document_id = await transaction.write(
                        """
                        INSERT INTO kb_document(slug,title,body,source,created_at,updated_at)
                        VALUES(?,?,?,?,unixepoch(),unixepoch())
                        """,
                        (document_slug, title, body, source),
                    )
                else:
                    rows = await transaction.read(
                        "SELECT id FROM kb_document WHERE id=?", (document_id,)
                    )
                    if not rows:
                        raise ValueError("knowledge-base document not found")
                    await transaction.write(
                        """
                        UPDATE kb_document SET slug=?,title=?,body=?,source=?,updated_at=unixepoch()
                        WHERE id=?
                        """,
                        (document_slug, title, body, source, document_id),
                    )
                    await transaction.write(
                        "DELETE FROM kb_chunk WHERE document_id=?", (document_id,)
                    )
                for sequence, (text, tokens) in enumerate(chunks, 1):
                    await transaction.write(
                        "INSERT INTO kb_chunk(document_id,seq,text,token_count) VALUES(?,?,?,?)",
                        (document_id, sequence, text, tokens),
                    )
                await transaction.write(
                    "UPDATE kb_document SET chunk_token_limit=?,chunk_overlap_tokens=? WHERE id=?",
                    (token_limit, _chunk_overlap_tokens(title, token_limit), document_id),
                )
        except sqlite3.IntegrityError as error:
            if "kb_document.slug" in str(error):
                raise ValueError("knowledge-base slug already exists") from error
            raise
        assert document_id is not None
        retrievable = warning is None and all(tokens <= token_limit for _text, tokens in chunks)
        return KBDocumentSaveResult(document_id, len(chunks), retrievable, warning)

    async def rechunk_stale_documents(self) -> int:
        """Rebuild chunks after the migration or an evidence-budget change."""
        token_limit = kb_chunk_token_limit(self.evidence_tokens)
        rows = await self.database.read(
            "SELECT id,title,body,chunk_token_limit,chunk_overlap_tokens FROM kb_document"
        )
        stale = [
            row
            for row in rows
            if int(row["chunk_token_limit"]) != token_limit
            or int(row["chunk_overlap_tokens"])
            != _chunk_overlap_tokens(str(row["title"]), token_limit)
        ]
        for row in stale:
            chunks, _limit, _warning = chunk_knowledge_document(
                str(row["title"]), str(row["body"]), self.evidence_tokens
            )
            async with self.database.transaction() as transaction:
                await transaction.write("DELETE FROM kb_chunk WHERE document_id=?", (row["id"],))
                for sequence, (text, tokens) in enumerate(chunks, 1):
                    await transaction.write(
                        "INSERT INTO kb_chunk(document_id,seq,text,token_count) VALUES(?,?,?,?)",
                        (row["id"], sequence, text, tokens),
                    )
                await transaction.write(
                    "UPDATE kb_document SET chunk_token_limit=?,chunk_overlap_tokens=? WHERE id=?",
                    (
                        token_limit,
                        _chunk_overlap_tokens(str(row["title"]), token_limit),
                        row["id"],
                    ),
                )
        return len(stale)

    async def delete_document(self, document_id: int) -> bool:
        rows = await self.database.read("SELECT id FROM kb_document WHERE id=?", (document_id,))
        if not rows:
            return False
        await self.database.write("DELETE FROM kb_document WHERE id=?", (document_id,))
        return True

    async def promote_interaction(self, interaction_id: int, title: str) -> KBDocumentSaveResult:
        rows = await self.database.read(
            "SELECT answer FROM ai_interaction WHERE id=?", (interaction_id,)
        )
        if not rows or not rows[0]["answer"]:
            raise ValueError("interaction has no answer to promote")
        answer = re.sub(r"^\[AI\??\]\s*", "", str(rows[0]["answer"]))
        answer = re.sub(r"\s+src:\s*\S+\s*$", "", answer).strip()
        return await self.save_document(
            title=title, body=answer, source=f"interaction:{interaction_id}"
        )

    async def refusal_rules(self) -> list[dict[str, Any]]:
        rows = await self.database.read("SELECT * FROM ai_refusal_rule ORDER BY created_at DESC")
        return [dict(row) for row in rows]

    async def add_refusal_rule(self, phrase: str, reason: str, actor: str) -> int:
        phrase, reason = phrase.strip(), reason.strip()
        if len(phrase) < 3 or len(phrase) > 120 or not reason or len(reason) > 120:
            raise ValueError("refusal phrase or reason has an invalid length")
        try:
            return await self.database.write(
                """
                INSERT INTO ai_refusal_rule(phrase,reason,created_by,created_at)
                VALUES(?,?,?,unixepoch())
                """,
                (phrase, reason, actor),
            )
        except sqlite3.IntegrityError as error:
            if "ai_refusal_rule.phrase" in str(error):
                raise ValueError("refusal phrase already exists") from error
            raise

    async def matching_rule(self, question: str) -> str | None:
        rows = await self.database.read("SELECT phrase,reason FROM ai_refusal_rule WHERE enabled=1")
        lowered = question.casefold()
        return next(
            (str(row["reason"]) for row in rows if str(row["phrase"]).casefold() in lowered),
            None,
        )
