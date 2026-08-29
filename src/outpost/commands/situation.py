from __future__ import annotations

import json
import math
from typing import Any, cast

from outpost.router.models import (
    ChannelUse,
    CommandContext,
    CommandHandler,
    CommandSpec,
    Line,
    Response,
    ResponseKind,
    TrustLevel,
    TuiChoice,
    TuiScreen,
)
from outpost.situation import BriefingCapability, SituationBriefingService
from outpost.transport.models import TrafficClass

PAGE_SIZE = 2
SNAPSHOT_SECONDS = 10 * 60


def _screen(
    name: str,
    title: str,
    lines: list[Line],
    choices: tuple[TuiChoice, ...] = (),
    *,
    max_parts: int = 2,
) -> Response:
    return Response(
        ResponseKind.DETAIL,
        lines,
        screen=TuiScreen(name, title, choices=choices, parent_command="SITREP"),
        max_parts=max_parts,
    )


def _clear_snapshots(ctx: CommandContext) -> None:
    for key in tuple(ctx.session.tui_snapshots):
        if key.startswith("sitrep:"):
            ctx.session.tui_snapshots.pop(key, None)


def _stored_items(snapshot: dict[str, Any], section: str) -> list[str]:
    sources = {source["id"]: source for source in snapshot["sources"]}
    if section == "incidents":
        items = [item for item in snapshot["items"] if item["section"] in {"alerts", "incidents"}]
    else:
        items = [item for item in snapshot["items"] if item["section"] == section]
    result = []
    for item in items:
        markers = []
        for source_id in item["source_ids"]:
            source = sources.get(source_id)
            if source is None:
                continue
            flags = ("!stale" if source["stale"] else "") + (
                "!conflict" if source["conflict"] else ""
            )
            markers.append(f"{source_id}@{source['age']}{flags}")
        value = dict(item)
        value["markers"] = markers
        result.append(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return result


def specs(service: SituationBriefingService) -> list[CommandSpec]:
    async def current(ctx: CommandContext, *, include_ai: bool = False) -> dict[str, Any]:
        capability = BriefingCapability.from_trust(str(ctx.member.trust))
        return await service.snapshot(capability, include_ai=include_ai)

    async def home(ctx: CommandContext) -> Response:
        snapshot = await current(ctx)
        _clear_snapshots(ctx)
        for section in ("weather", "incidents", "welfare", "community", "network"):
            ctx.session.tui_snapshots[f"sitrep:{section}"] = _stored_items(snapshot, section)
        ctx.session.tui_snapshot_expires_at = service.clock.monotonic() + SNAPSHOT_SECONDS
        counts = {
            section: len([item for item in snapshot["items"] if item["section"] == section])
            for section in ("alerts", "incidents", "welfare", "weather")
        }
        hazards = len(
            [item for item in snapshot["items"] if item["section"] == "weather" and item["hazard"]]
        )
        welfare = len(
            [item for item in snapshot["items"] if item["section"] == "welfare" and item["hazard"]]
        )
        summary = (
            f"{counts['alerts']} alerts · {counts['incidents']} urgent inc · "
            f"{welfare} welfare · {hazards} wx hazards · {len(snapshot['changes'])} changes"
        )
        return _screen(
            "sitrep-home",
            "SITREP",
            [Line(summary)],
            (
                TuiChoice("Weather", "SITREP WEATHER"),
                TuiChoice("Incidents", "SITREP INCIDENTS"),
                TuiChoice("Welfare", "SITREP WELFARE"),
                TuiChoice("Community", "SITREP COMMUNITY"),
                TuiChoice("Network", "SITREP NETWORK"),
            ),
            max_parts=1,
        )

    async def detail(ctx: CommandContext, section: str, page_text: str) -> Response:
        try:
            page = int(page_text or "1")
        except ValueError:
            page = 0
        if page < 1:
            return Response(ResponseKind.ERROR, [Line(f"Use SITREP {section.upper()} [page].")])
        key = f"sitrep:{section}"
        stored = ctx.session.tui_snapshots.get(key)
        if stored is None:
            if page != 1:
                return Response(ResponseKind.ERROR, [Line("Brief expired. Send SITREP again.")])
            snapshot = await current(ctx)
            stored = _stored_items(snapshot, section)
            ctx.session.tui_snapshots[key] = stored
            ctx.session.tui_snapshot_expires_at = service.clock.monotonic() + SNAPSHOT_SECONDS
        pages = max(1, math.ceil(len(stored) / PAGE_SIZE))
        if page > pages:
            return Response(ResponseKind.ERROR, [Line(f"Only {pages} page(s).")])
        lines: list[Line] = []
        for raw in stored[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]:
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            marker = str(item.get("markers", ["source unknown"])[0])
            uncertainty = f" · {item['uncertainty']}" if item.get("uncertainty") else ""
            lines.append(
                Line(
                    f"{item['ref']} {str(item['severity']).upper()} "
                    f"{str(item['title'])[:76]} · {marker}{uncertainty}"
                )
            )
            lines.append(Line(str(item["detail"])[:110]))
        if not lines:
            lines.append(Line(f"No {section} items in this briefing."))
        choices: list[TuiChoice] = []
        if page > 1:
            choices.append(TuiChoice("Previous page", f"SITREP {section.upper()} {page - 1}"))
        if page < pages:
            choices.append(TuiChoice("Next page", f"SITREP {section.upper()} {page + 1}"))
        lines.insert(0, Line(f"Page {page}/{pages} · source ID@age"))
        return _screen(f"sitrep-{section}", section.upper(), lines, tuple(choices), max_parts=2)

    async def narration(ctx: CommandContext) -> Response:
        snapshot = await current(ctx, include_ai=True)
        ai = snapshot["ai"]
        if not ai.get("text"):
            return _screen(
                "sitrep-ai",
                "SITREP SUMMARY",
                [
                    Line(
                        f"Optional AI summary unavailable ({ai['outcome']}). "
                        "Deterministic SITREP facts are unchanged."
                    )
                ],
            )
        return _screen(
            "sitrep-ai",
            "SITREP SUMMARY",
            [Line(str(ai["text"]))],
            max_parts=2,
        )

    async def sitrep(ctx: CommandContext) -> Response:
        if not ctx.message.is_direct:
            return Response(ResponseKind.ERROR, [Line("DM this node for SITREP.")])
        section, _, page = ctx.args.strip().partition(" ")
        section = section.casefold()
        if not section or section in {"home", "status"}:
            return await home(ctx)
        if section in {"weather", "incidents", "welfare", "community", "network"}:
            return await detail(ctx, section, page.strip())
        if section in {"ai", "summary"}:
            return await narration(ctx)
        return Response(
            ResponseKind.ERROR,
            [Line("Use SITREP or choose Weather, Incidents, Welfare, Community, or Network.")],
        )

    return [
        CommandSpec(
            "SITREP",
            ("BRIEF",),
            module="operations",
            min_trust=TrustLevel.MEMBER,
            airtime_class=TrafficClass.REPLY,
            max_parts=2,
            rate_key="commands",
            help_short="SITREP · evidence-backed local situation brief",
            mutates=False,
            handler=cast(CommandHandler, sitrep),
            channel_use=ChannelUse.GENERAL,
        )
    ]
