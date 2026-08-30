from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from outpost.ai.budget import (
    SEARCH_KB_RESULT_TOKENS,
    EvidenceChunk,
    conservative_tokens,
)
from outpost.ai.providers.models import ToolDefinition
from outpost.ai.retrieval import RetrievalEngine
from outpost.router.models import TrustLevel
from outpost.store.members import Member


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SearchBoardsInput(ToolInput):
    query: str = Field(min_length=2, max_length=120)
    board: str | None = Field(default=None, min_length=1, max_length=64)
    limit: int = Field(default=5, ge=1, le=8)


class RecentPostsInput(ToolInput):
    board: str | None = Field(default=None, min_length=1, max_length=64)
    hours: int = Field(default=24, ge=1, le=720)
    limit: int = Field(default=5, ge=1, le=8)


class GetThreadInput(ToolInput):
    thread_ref: str = Field(min_length=1, max_length=96)
    limit: int = Field(default=5, ge=1, le=8)


class ActiveIncidentsInput(ToolInput):
    radius_km: float | None = Field(default=None, gt=0, le=100)
    type: str | None = Field(default=None, min_length=1, max_length=40)


class GetIncidentInput(ToolInput):
    ref: str = Field(pattern=r"^(?:inc:)?\d+$")


class PlaceInput(ToolInput):
    place: str | None = Field(default=None, min_length=1, max_length=80)


class ForecastInput(PlaceInput):
    days: int = Field(default=2, ge=1, le=3)


class NoArgsInput(ToolInput):
    pass


class FindMemberInput(ToolInput):
    handle_or_partial: str = Field(min_length=1, max_length=32, pattern=r"^@?[A-Za-z0-9_-]+$")


class SearchKBInput(ToolInput):
    query: str = Field(min_length=2, max_length=120)
    limit: int = Field(default=5, ge=1, le=8)


class ListCommandsInput(ToolInput):
    topic: str | None = Field(default=None, min_length=1, max_length=32)


@dataclass(frozen=True)
class ToolResult:
    name: str
    content: str
    chunks: tuple[EvidenceChunk, ...]
    truncated: bool = False


@dataclass(frozen=True)
class ReadTool:
    name: str
    description: str
    input_model: type[ToolInput]
    max_result_tokens: int

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.input_model.model_json_schema(),
        )


class ToolValidationError(ValueError):
    pass


class ReadOnlyToolCatalogue:
    """Strict, bounded access to local read models; no tool can mutate Outpost state."""

    TOOLS: ClassVar[tuple[ReadTool, ...]] = (
        ReadTool("search_boards", "Search readable board posts.", SearchBoardsInput, 180),
        ReadTool("recent_posts", "List recent posts on readable boards.", RecentPostsInput, 180),
        ReadTool("get_thread", "Read posts from one readable thread.", GetThreadInput, 220),
        ReadTool("active_incidents", "List active local incidents.", ActiveIncidentsInput, 180),
        ReadTool("get_incident", "Read one incident and recent updates.", GetIncidentInput, 180),
        ReadTool("current_weather", "Read cached current weather.", PlaceInput, 140),
        ReadTool("forecast", "Read the cached short forecast.", ForecastInput, 180),
        ReadTool("active_weather_alerts", "Read active reviewed weather alerts.", NoArgsInput, 160),
        ReadTool(
            "find_member", "Find directory facts; never returns position.", FindMemberInput, 100
        ),
        ReadTool("node_status", "Read local radio and queue status.", NoArgsInput, 100),
        ReadTool(
            "search_kb",
            "Search operator-verified local knowledge.",
            SearchKBInput,
            SEARCH_KB_RESULT_TOKENS,
        ),
        ReadTool("list_commands", "List commands from the live registry.", ListCommandsInput, 140),
    )

    def __init__(self, retrieval: RetrievalEngine) -> None:
        self.retrieval = retrieval
        self._tools = {tool.name: tool for tool in self.TOOLS}

    def definitions(self, names: set[str] | None = None) -> tuple[ToolDefinition, ...]:
        return tuple(
            tool.definition() for tool in self.TOOLS if names is None or tool.name in names
        )

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        member: Member,
        registry: Any,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolValidationError("unknown AI tool")
        try:
            values = tool.input_model.model_validate(arguments)
        except ValidationError as error:
            raise ToolValidationError("invalid AI tool arguments") from error
        handler = getattr(self, f"_{name}")
        chunks = await handler(values, member, registry)
        return self._bounded(tool, chunks)

    def _bounded(self, tool: ReadTool, chunks: list[EvidenceChunk]) -> ToolResult:
        accepted: list[EvidenceChunk] = []
        lines: list[str] = []
        used = 0
        for chunk in chunks:
            line = f"[{chunk.ref}] {chunk.text}"
            tokens = conservative_tokens(line)
            if used + tokens > tool.max_result_tokens:
                continue
            accepted.append(chunk)
            lines.append(line)
            used += tokens
        return ToolResult(
            tool.name,
            "\n".join(lines) if lines else "NO_RESULTS",
            tuple(accepted),
            len(accepted) < len(chunks),
        )

    async def _search_boards(
        self, values: SearchBoardsInput, member: Member, _registry: Any
    ) -> list[EvidenceChunk]:
        chunks = await self.retrieval._boards(values.query, member)
        if values.board:
            prefix = f"board:{values.board.casefold()}#"
            chunks = [chunk for chunk in chunks if chunk.ref.casefold().startswith(prefix)]
        return chunks[: values.limit]

    async def _recent_posts(
        self, values: RecentPostsInput, member: Member, _registry: Any
    ) -> list[EvidenceChunk]:
        where = "AND b.slug=?" if values.board else ""
        params: tuple[Any, ...] = (
            (values.hours, values.board, values.limit * 3)
            if values.board
            else (values.hours, values.limit * 3)
        )
        rows = await self.retrieval.database.read(
            f"""
            SELECT p.thread_id,p.author_label,p.body,p.created_at,b.slug,b.min_read_trust
            FROM post p JOIN thread t ON t.id=p.thread_id JOIN board b ON b.id=t.board_id
            WHERE p.hidden=0 AND t.hidden=0 AND b.archived=0
              AND p.created_at>=unixepoch()-(?*3600) {where}
            ORDER BY p.created_at DESC LIMIT ?
            """,  # noqa: S608 - where is a closed internal choice
            params,
        )
        chunks: list[EvidenceChunk] = []
        for row in rows:
            if TrustLevel.parse(member.trust) < TrustLevel.parse(str(row["min_read_trust"])):
                continue
            chunks.append(
                EvidenceChunk(
                    f"board:{row['slug']}#{row['thread_id']}",
                    "board",
                    f"@{row['author_label']} {row['body']}",
                    float(row["created_at"]),
                )
            )
        return chunks[: values.limit]

    async def _get_thread(
        self, values: GetThreadInput, member: Member, _registry: Any
    ) -> list[EvidenceChunk]:
        match = re.search(r"(?:board:[^#]+#)?(\d+)$", values.thread_ref, re.IGNORECASE)
        if match is None:
            return []
        rows = await self.retrieval.database.read(
            """
            SELECT p.thread_id,p.seq,p.author_label,p.body,b.slug,b.min_read_trust
            FROM post p JOIN thread t ON t.id=p.thread_id JOIN board b ON b.id=t.board_id
            WHERE p.thread_id=? AND p.hidden=0 AND t.hidden=0 AND b.archived=0
            ORDER BY p.seq LIMIT ?
            """,
            (int(match.group(1)), values.limit),
        )
        if rows and TrustLevel.parse(member.trust) < TrustLevel.parse(
            str(rows[0]["min_read_trust"])
        ):
            return []
        return [
            EvidenceChunk(
                f"board:{row['slug']}#{row['thread_id']}",
                "board",
                f"post {row['seq']} @{row['author_label']} {row['body']}",
                10 - int(row["seq"]) / 100,
            )
            for row in rows
        ]

    async def _active_incidents(
        self, values: ActiveIncidentsInput, _member: Member, _registry: Any
    ) -> list[EvidenceChunk]:
        chunks = await self.retrieval._incidents("active incident")
        if values.type:
            chunks = [chunk for chunk in chunks if values.type.casefold() in chunk.text.casefold()]
        return chunks

    async def _get_incident(
        self, values: GetIncidentInput, _member: Member, _registry: Any
    ) -> list[EvidenceChunk]:
        reference = values.ref.casefold().removeprefix("inc:")
        chunks = await self.retrieval._incidents(f"incident {reference}")
        updates = await self.retrieval.database.read(
            """
            SELECT iu.kind,iu.body,iu.author_label FROM incident_update iu
            JOIN incident i ON i.id=iu.incident_id WHERE i.local_ref=?
            ORDER BY iu.seq DESC LIMIT 3
            """,
            (int(reference),),
        )
        if chunks and updates:
            detail = "; ".join(
                f"{row['kind']} @{row['author_label']} {row['body'] or ''}" for row in updates
            )
            first = chunks[0]
            chunks[0] = EvidenceChunk(
                first.ref, first.source, f"{first.text}; {detail}", first.score
            )
        return chunks

    async def _current_weather(
        self, _values: PlaceInput, _member: Member, _registry: Any
    ) -> list[EvidenceChunk]:
        return [chunk for chunk in await self.retrieval._weather() if "alert@" not in chunk.ref][:1]

    async def _forecast(
        self, values: ForecastInput, _member: Member, _registry: Any
    ) -> list[EvidenceChunk]:
        return [chunk for chunk in await self.retrieval._weather() if "alert@" not in chunk.ref][
            : values.days
        ]

    async def _active_weather_alerts(
        self, _values: NoArgsInput, _member: Member, _registry: Any
    ) -> list[EvidenceChunk]:
        return [chunk for chunk in await self.retrieval._weather() if "alert@" in chunk.ref]

    async def _find_member(
        self, values: FindMemberInput, _member: Member, _registry: Any
    ) -> list[EvidenceChunk]:
        handle = values.handle_or_partial.removeprefix("@").casefold()
        rows = await self.retrieval.database.read(
            """
            SELECT handle,trust,last_seen FROM member
            WHERE handle LIKE ? AND directory_state NOT IN ('archived','ignored')
            ORDER BY CASE WHEN handle=? THEN 0 ELSE 1 END,last_seen DESC LIMIT 5
            """,
            (f"%{handle}%", handle),
        )
        return [
            EvidenceChunk(
                f"member:{row['handle']}",
                "directory",
                f"@{row['handle']} trust={row['trust']} last_seen={row['last_seen']}",
                10,
            )
            for row in rows
        ]

    async def _node_status(
        self, _values: NoArgsInput, _member: Member, _registry: Any
    ) -> list[EvidenceChunk]:
        return self.retrieval._status()

    async def _search_kb(
        self, values: SearchKBInput, _member: Member, _registry: Any
    ) -> list[EvidenceChunk]:
        return (await self.retrieval._knowledge(values.query))[: values.limit]

    async def _list_commands(
        self, values: ListCommandsInput, member: Member, registry: Any
    ) -> list[EvidenceChunk]:
        topic = values.topic.casefold() if values.topic else None
        commands = [
            spec
            for spec in registry.commands()
            if TrustLevel.parse(member.trust) >= spec.min_trust
            and (topic is None or topic in spec.module.casefold() or topic in spec.name.casefold())
        ]
        return [
            EvidenceChunk(f"cmd:{spec.name}", "commands", spec.help_short, 10) for spec in commands
        ]
