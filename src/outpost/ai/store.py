from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from outpost.ai.budget import conservative_tokens
from outpost.store import Database


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


class AIStore:
    def __init__(self, database: Database) -> None:
        self.database = database

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
        rows = await self.database.read(
            "SELECT * FROM kb_document ORDER BY pinned DESC,title COLLATE NOCASE"
        )
        return [dict(row) for row in rows]

    async def save_document(
        self,
        *,
        title: str,
        body: str,
        slug: str | None = None,
        source: str = "operator",
        document_id: int | None = None,
    ) -> int:
        title = title.strip()
        body = body.strip()
        if not title or not body:
            raise ValueError("title and body are required")
        if len(title) > 120 or len(body.encode()) > 12_000:
            raise ValueError("knowledge-base document is too large")
        document_slug = _slug(slug or title)
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
                await transaction.write(
                    "INSERT INTO kb_chunk(document_id,seq,text,token_count) VALUES(?,1,?,?)",
                    (
                        document_id,
                        f"{title}: {body}",
                        conservative_tokens(f"{title}: {body}"),
                    ),
                )
        except sqlite3.IntegrityError as error:
            if "kb_document.slug" in str(error):
                raise ValueError("knowledge-base slug already exists") from error
            raise
        assert document_id is not None
        return document_id

    async def delete_document(self, document_id: int) -> bool:
        rows = await self.database.read("SELECT id FROM kb_document WHERE id=?", (document_id,))
        if not rows:
            return False
        await self.database.write("DELETE FROM kb_document WHERE id=?", (document_id,))
        return True

    async def promote_interaction(self, interaction_id: int, title: str) -> int:
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
