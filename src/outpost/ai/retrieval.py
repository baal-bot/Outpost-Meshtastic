from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from outpost.ai.budget import EvidenceChunk
from outpost.router.models import TrustLevel
from outpost.store import Database
from outpost.store.members import Member

_WORDS = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]+")
_STOP = {
    "about",
    "any",
    "are",
    "can",
    "could",
    "did",
    "does",
    "for",
    "from",
    "has",
    "have",
    "how",
    "into",
    "is",
    "latest",
    "most",
    "newest",
    "now",
    "posted",
    "please",
    "the",
    "this",
    "today",
    "was",
    "what",
    "when",
    "where",
    "which",
    "who",
    "will",
    "with",
}


class QuestionClass(StrEnum):
    LOCAL_KNOWLEDGE = "local_knowledge"
    BOARD_CONTENT = "board_content"
    INCIDENT = "incident"
    WEATHER = "weather"
    DIRECTORY = "directory"
    NODE_STATUS = "node_status"
    HOWTO = "howto"
    GENERAL = "general"


@dataclass(frozen=True)
class RetrievalResult:
    classes: tuple[QuestionClass, ...]
    chunks: tuple[EvidenceChunk, ...]
    deterministic_answer: str | None = None
    allow_ungrounded: bool = False


def classify(question: str) -> tuple[QuestionClass, ...]:
    text = " ".join(question.casefold().split())
    classes: list[QuestionClass] = []
    if re.search(r"\bhow (?:do|can|should) i\b|\bwhat command\b|\bhow to\b", text):
        classes.append(QuestionClass.HOWTO)
    if re.search(r"\bweather\b|\bforecast\b|\brain\b|\bwind\b|\btemperature\b|\bstorm\b", text):
        classes.append(QuestionClass.WEATHER)
    if re.search(r"\bincident|\bhazard|\bblocked|\bresolved|\burgent|\balert", text):
        classes.append(QuestionClass.INCIDENT)
    if re.search(r"\bmember|\blast heard|\bfind (?:a |the )?(?:member|person)|\bwho is @", text):
        classes.append(QuestionClass.DIRECTORY)
    if re.search(r"\bnode status|\boutpost status|\bradio status|\bairtime|\buptime", text):
        classes.append(QuestionClass.NODE_STATUS)
    if re.search(
        r"\bboard|\bpost|\bthread|\blatest|\bnewest|\broad|\btrail|\bbridge|"
        r"\brepeater|\bradio net|\bvolunteer|\bsupplies",
        text,
    ):
        classes.append(QuestionClass.BOARD_CONTENT)
    if re.search(
        r"\bopen|\bhours|\bshelter|\bburn|\bplow|\bcontact|\bwater|\bfuel|"
        r"\bpantry|\bcharging|\blibrary|\blocal service|\banimal control",
        text,
    ):
        classes.append(QuestionClass.LOCAL_KNOWLEDGE)
    if not classes:
        classes.append(QuestionClass.GENERAL)
    return tuple(dict.fromkeys(classes))


def allows_ungrounded(question: str) -> bool:
    text = " ".join(question.casefold().split())
    return bool(
        re.search(r"\btranslate\b|\bconvert\b|\bhow many (?:miles|km|feet|meters)\b", text)
        or re.fullmatch(r"[\d\s+*/().=-]+", text)
        or re.search(r"\b(?:summarize|rephrase) (?:this|the following):", text)
        or re.search(r"\bexplain (?:the concept of |what )", text)
    )


def _query(question: str) -> str:
    return " OR ".join(f'"{word}"' for word in _terms(question))


def _terms(text: str, *, limit: int | None = 12) -> tuple[str, ...]:
    words = [word.casefold() for word in _WORDS.findall(text)]
    useful = [word for word in words if word not in _STOP and len(word) > 1]
    if limit is not None:
        useful = useful[:limit]
    return tuple(dict.fromkeys(useful))


def _decay(created_at: int, now: int, half_life_days: float) -> float:
    age_days = max(0, now - created_at) / 86_400
    return math.exp(-math.log(2) * age_days / half_life_days)


def _age(created_at: int, now: int) -> str:
    seconds = max(0, now - created_at)
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


class RetrievalEngine:
    def __init__(
        self,
        database: Database,
        *,
        now: Callable[[], int],
        node_status: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.database = database
        self.now = now
        self.node_status = node_status

    async def retrieve(self, question: str, member: Member, registry: Any) -> RetrievalResult:
        classes = classify(question)
        if QuestionClass.HOWTO in classes:
            answer = self._howto(question, registry)
            if answer is not None:
                return RetrievalResult(classes, (), deterministic_answer=answer)
        # Operator-curated knowledge applies to every question class. Classification
        # chooses additional sources; it must never hide an applicable local document.
        chunks: list[EvidenceChunk] = await self._knowledge(question)
        if QuestionClass.BOARD_CONTENT in classes or QuestionClass.GENERAL in classes:
            chunks.extend(await self._boards(question, member))
        if QuestionClass.INCIDENT in classes:
            chunks.extend(await self._incidents(question))
        if QuestionClass.WEATHER in classes:
            chunks.extend(await self._weather())
        if QuestionClass.DIRECTORY in classes:
            chunks.extend(await self._directory(question))
        if QuestionClass.NODE_STATUS in classes:
            chunks.extend(self._status())
        return RetrievalResult(classes, tuple(chunks), allow_ungrounded=allows_ungrounded(question))

    @staticmethod
    def _howto(question: str, registry: Any) -> str | None:
        text = question.casefold()
        preferred = (
            ("position", "POS"),
            ("location", "POS"),
            ("mail", "SEND"),
            ("message", "SEND"),
            ("report", "REPORT"),
            ("hazard", "REPORT"),
            ("make a board post", "POST"),
            ("post", "POST"),
            ("list boards", "BOARDS"),
            ("board", "BOARDS"),
            ("read a thread", "READ"),
            ("thread", "READ"),
        )
        name = next((command for phrase, command in preferred if phrase in text), None)
        spec = registry.resolve(name) if name else None
        if spec is None:
            return None
        return f"[AI] Use {spec.help_short}."

    async def _knowledge(self, question: str) -> list[EvidenceChunk]:
        query = _query(question)
        if not query:
            return []
        rows = await self.database.read(
            """
            SELECT d.slug,c.seq,c.text,d.updated_at,bm25(kb_fts) rank
            FROM kb_fts f JOIN kb_chunk c ON c.id=f.rowid
            JOIN kb_document d ON d.id=c.document_id
            WHERE kb_fts MATCH ? ORDER BY rank LIMIT 8
            """,
            (query,),
        )
        now = self.now()
        terms = set(_terms(question))
        minimum_overlap = 1 if len(terms) < 2 else 2
        chunks: list[EvidenceChunk] = []
        for row in rows:
            overlap = len(terms.intersection(_terms(str(row["text"]), limit=None)))
            if overlap < minimum_overlap:
                continue
            chunks.append(
                EvidenceChunk(
                    f"kb:{row['slug']}" + (f"#{row['seq']}" if int(row["seq"]) > 1 else ""),
                    "kb",
                    str(row["text"]),
                    (
                        overlap
                        * 20
                        * _decay(int(row["updated_at"]), now, 180)
                        / (1 + abs(float(row["rank"])))
                    ),
                )
            )
        return chunks

    async def _boards(self, question: str, member: Member) -> list[EvidenceChunk]:
        query = _query(question)
        if not query:
            return []
        rows = await self.database.read(
            """
            SELECT p.thread_id,p.seq,p.author_label,p.body,p.created_at,b.slug,
                   b.min_read_trust,bm25(post_fts) rank
            FROM post_fts f JOIN post p ON p.id=f.rowid
            JOIN thread t ON t.id=p.thread_id JOIN board b ON b.id=t.board_id
            WHERE post_fts MATCH ? AND p.hidden=0 AND t.hidden=0 AND b.archived=0
            ORDER BY rank,p.created_at DESC LIMIT 16
            """,
            (query,),
        )
        now = self.now()
        chunks: list[EvidenceChunk] = []
        for row in rows:
            if TrustLevel.parse(member.trust) < TrustLevel.parse(str(row["min_read_trust"])):
                continue
            chunks.append(
                EvidenceChunk(
                    f"board:{row['slug']}#{row['thread_id']}",
                    "board",
                    f"{_age(int(row['created_at']), now)} @{row['author_label']} {row['body']}",
                    12 * _decay(int(row["created_at"]), now, 14) / (1 + abs(float(row["rank"]))),
                )
            )
        return chunks[:8]

    async def _incidents(self, question: str) -> list[EvidenceChunk]:
        reference = re.search(r"\b(?:incident|inc)\s*#?(\d+)\b", question, re.IGNORECASE)
        where = (
            "local_ref=?"
            if reference
            else (
                "status IN ('open','monitoring') OR "
                "(status IN ('resolved','expired') AND updated_at>=unixepoch()-172800)"
            )
        )
        params: tuple[Any, ...] = (int(reference.group(1)),) if reference else ()
        rows = await self.database.read(
            f"""
            SELECT local_ref,title,body,severity,status,location_text,updated_at
            FROM incident WHERE {where} ORDER BY updated_at DESC LIMIT 8
            """,  # noqa: S608 - where is a closed internal choice
            params,
        )
        now = self.now()
        return [
            EvidenceChunk(
                f"inc:{row['local_ref']}",
                "incident",
                " · ".join(
                    str(value)
                    for value in (
                        row["severity"],
                        row["status"],
                        row["title"],
                        row["body"],
                        row["location_text"],
                        _age(int(row["updated_at"]), now),
                    )
                    if value
                ),
                16 * _decay(int(row["updated_at"]), now, 2),
            )
            for row in rows
        ]

    async def _weather(self) -> list[EvidenceChunk]:
        now = self.now()
        rows = await self.database.read(
            """
            SELECT cache_key,provider,payload,fetched_at FROM env_cache
            WHERE cache_key LIKE 'weather:%' OR cache_key LIKE 'forecast:%'
            ORDER BY fetched_at DESC LIMIT 2
            """
        )
        chunks: list[EvidenceChunk] = []
        for row in rows:
            value = json.loads(row["payload"])
            fields: tuple[str, ...]
            if str(row["cache_key"]).startswith("weather:"):
                fields = (
                    f"{value.get('temperature_c')}C",
                    str(value.get("summary") or ""),
                    f"wind {value.get('wind_kph')}km/h",
                    f"rain {value.get('precipitation_mm')}mm",
                )
            else:
                daily = value.get("daily", [])[:2]
                fields = tuple(
                    f"{day.get('name')} {day.get('summary')} "
                    f"{day.get('high_c')}/{day.get('low_c')}C rain "
                    f"{day.get('precipitation_probability')}%"
                    for day in daily
                    if isinstance(day, dict)
                )
            text = " · ".join(field for field in fields if "None" not in field and field.strip())
            chunks.append(
                EvidenceChunk(
                    f"wx:{row['provider']}@{row['fetched_at']}",
                    "weather",
                    f"{_age(int(row['fetched_at']), now)} {text}",
                    18 * _decay(int(row["fetched_at"]), now, 0.25),
                )
            )
        alerts = await self.database.read(
            """
            SELECT identifier,event,headline,area_desc,expires_at,updated_at FROM cap_alert
            WHERE decision='accepted' AND review_state<>'dismissed' AND expires_epoch>?
            ORDER BY updated_at DESC,id LIMIT 3
            """,
            (now,),
        )
        chunks.extend(
            EvidenceChunk(
                f"wx:alert@{row['identifier']}",
                "weather",
                (
                    f"{row['event']} · {row['headline']} · {row['area_desc']} · "
                    f"until {row['expires_at']}"
                ),
                25,
            )
            for row in alerts
        )
        return chunks

    async def _directory(self, question: str) -> list[EvidenceChunk]:
        rows = await self.database.read(
            """
            SELECT handle,trust,last_seen FROM member
            WHERE handle IS NOT NULL AND directory_state NOT IN ('archived','ignored')
            ORDER BY last_seen DESC LIMIT 100
            """
        )
        text = question.casefold()
        selected = [row for row in rows if str(row["handle"]).casefold() in text] or rows[:5]
        now = self.now()
        return [
            EvidenceChunk(
                f"member:{row['handle']}",
                "directory",
                (
                    f"@{row['handle']} · {row['trust']} · "
                    f"last heard {_age(int(row['last_seen']), now)}"
                ),
                10,
            )
            for row in selected[:5]
        ]

    def _status(self) -> list[EvidenceChunk]:
        if self.node_status is None:
            return []
        value = self.node_status()
        radio = value.get("radio", {})
        queues = value.get("queues", {})
        radio_state = (
            radio.get("state", radio.get("connected", "unknown"))
            if isinstance(radio, dict)
            else radio
        )
        queued = queues.get("total", 0) if isinstance(queues, dict) else queues
        text = f"radio {radio_state}; queued {queued}; version {value.get('version', 'unknown')}"
        return [EvidenceChunk("node:status", "node_status", text, 20)]
