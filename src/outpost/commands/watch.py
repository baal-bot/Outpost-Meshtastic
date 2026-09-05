from __future__ import annotations

from typing import TypedDict

from outpost.router.models import (
    ChannelUse,
    CommandContext,
    CommandSpec,
    ContextFrame,
    Line,
    Response,
    ResponseKind,
    TrustLevel,
    TuiChoice,
    TuiScreen,
)
from outpost.transport.chunker import truncate_utf8
from outpost.transport.models import TrafficClass
from outpost.watch.incidents import IncidentService


class _WatchDefaults(TypedDict):
    module: str
    min_trust: TrustLevel
    airtime_class: TrafficClass
    max_parts: int
    rate_key: str


def _proximity(distance_km: float | None, bearing: int | None) -> str | None:
    if distance_km is None or bearing is None:
        return None
    distance = f"{distance_km:.1f}km" if distance_km < 10 else f"{distance_km:.0f}km"
    return f"{distance} {bearing:03d}°"


def specs(service: IncidentService) -> list[CommandSpec]:
    async def report(ctx: CommandContext) -> Response:
        try:
            created, similar = await service.create(ctx.args, ctx.member)
        except ValueError as error:
            return Response(ResponseKind.ERROR, [Line(str(error))])
        if similar:
            return Response(
                ResponseKind.DETAIL,
                [
                    Line(
                        f"Similar: INC {similar.local_ref} {similar.type} "
                        f"{similar.title[:24]}. CONFIRM {similar.local_ref}, "
                        "or REPORT! to file new."
                    )
                ],
            )
        assert created is not None
        if created.lat is None:
            text = (
                f"✓ INC {created.local_ref} {created.type}. No location — "
                f"send UPD {created.local_ref} <where> (public place; verified DM or ask operator)."
            )
        else:
            text = (
                f"✓ INC {created.local_ref} {created.type} · "
                f"GPS {created.lat:.3f},{created.lon:.3f}"
            )
        response = Response(ResponseKind.ACK, [Line(text)])
        if ctx.message.is_direct and ctx.session.tui_active:
            response.screen = TuiScreen(
                "incident-filed",
                "INCIDENT FILED",
                choices=(TuiChoice("View incident", f"INC {created.local_ref}"),),
            )
        return response

    async def report_force(ctx: CommandContext) -> Response:
        created, _ = await service.create(ctx.args, ctx.member, force=True)
        assert created is not None
        lines = [Line(f"✓ INC {created.local_ref} {created.type} filed.")]
        if created.lat is None:
            lines.append(
                Line(
                    f"No location — send UPD {created.local_ref} <where> "
                    "(public place; verified DM or ask operator)."
                )
            )
        return Response(ResponseKind.ACK, lines)

    async def update_location(ctx: CommandContext) -> Response:
        parts = ctx.args.strip().split(maxsplit=1)
        if len(parts) != 2 or not parts[0].isascii() or not parts[0].isdigit():
            return Response(
                ResponseKind.ERROR, [Line("UPD needs <incident number> <where>. HELP UPD.")]
            )
        if len(parts[0]) > 19:
            return Response(ResponseKind.ERROR, [Line("No active incident.")])
        reference = int(parts[0])
        if not 0 < reference <= 9_223_372_036_854_775_807:
            return Response(ResponseKind.ERROR, [Line("No active incident.")])
        try:
            value = await service.update_location(reference, ctx.member, parts[1])
        except ValueError as error:
            return Response(ResponseKind.ERROR, [Line(str(error))])
        return Response(
            ResponseKind.ACK,
            [
                Line(
                    f"✓ INC {value.local_ref} location: "
                    f"{truncate_utf8(value.location_text or '', 80)}"
                ),
                Line(f"{truncate_utf8(value.title, 40)} · {truncate_utf8(value.origin_node, 24)}"),
            ],
        )

    async def confirm(ctx: CommandContext) -> Response:
        token = ctx.args.strip()
        if not token.isdigit():
            return Response(ResponseKind.ERROR, [Line("CONFIRM needs incident number.")])
        try:
            value = await service.react(int(token), ctx.member, "confirm")
        except ValueError as error:
            return Response(ResponseKind.ERROR, [Line(str(error))])
        return Response(
            ResponseKind.ACK,
            [
                Line(f"✓ INC {value.local_ref} confirmed · ✓{value.confirm_count}"),
                Line(f"{truncate_utf8(value.title, 48)} · {truncate_utf8(value.origin_node, 24)}"),
            ],
        )

    async def dispute(ctx: CommandContext) -> Response:
        token, _, note = ctx.args.strip().partition(" ")
        if not token.isdigit():
            return Response(ResponseKind.ERROR, [Line("DISPUTE needs incident number [note].")])
        try:
            value = await service.react(int(token), ctx.member, "dispute", note)
        except ValueError as error:
            return Response(ResponseKind.ERROR, [Line(str(error))])
        return Response(
            ResponseKind.ACK,
            [
                Line(f"✓ INC {value.local_ref} disputed · {value.dispute_count}"),
                Line(f"{truncate_utf8(value.title, 48)} · {truncate_utf8(value.origin_node, 24)}"),
            ],
        )

    async def incidents(ctx: CommandContext) -> Response:
        values = await service.list(limit=50 if ctx.message.is_direct else 5)
        if not values:
            return Response(ResponseKind.LISTING, [Line("No active incidents.")])
        if ctx.message.is_direct:
            ranked = (await service.ranked_for_member(values, ctx.member))[:5]
            markers = {
                "critical": "CRITICAL",
                "urgent": "URGENT",
                "caution": "CAUTION",
                "info": "INFO",
            }
            return Response(
                ResponseKind.LISTING,
                screen=TuiScreen(
                    "incidents",
                    "ACTIVE INCIDENTS",
                    choices=tuple(
                        TuiChoice(
                            " · ".join(
                                value
                                for value in (
                                    f"INC {item.local_ref}",
                                    markers[item.severity],
                                    _proximity(distance_km, bearing),
                                    item.title[:18],
                                )
                                if value
                            ),
                            f"INC {item.local_ref}",
                        )
                        for item, distance_km, bearing in ranked
                    ),
                ),
            )
        lines = [Line(f"{len(values)} active · no position")]
        markers = {"critical": "⚠⚠", "urgent": "⚠", "caution": "!", "info": ""}
        lines.extend(
            Line(
                f"{item.local_ref} {markers[item.severity]} {item.type} "
                f"{item.title[:28]} ✓{item.confirm_count}"
            )
            for item in values
        )
        return Response(ResponseKind.LISTING, lines)

    async def detail(ctx: CommandContext) -> Response:
        token = ctx.args.strip()
        if not token.isdigit():
            return Response(ResponseKind.ERROR, [Line("INC needs incident number.")])
        value = await service.by_ref(int(token))
        if value is None:
            return Response(ResponseKind.ERROR, [Line("No incident.")])
        updates = await service.updates(value.id)
        proximity = None
        if ctx.message.is_direct:
            _, distance_km, bearing = (await service.ranked_for_member([value], ctx.member))[0]
            proximity = _proximity(distance_km, bearing)
        location = value.location_text or "No location"
        if proximity:
            location = f"{location} · {proximity} from you"
        lines = [
            Line(f"INC {value.local_ref} · {value.severity} {value.type} · {value.status}"),
            Line(value.body or value.title),
            Line(f"{location} · ✓{value.confirm_count} ?{value.dispute_count}"),
        ]
        lines.extend(
            Line(f"{update['kind']} @{update['author_label']}: {update['body'] or 'noted'}")
            for update in updates
        )
        response = Response(ResponseKind.DETAIL, lines)
        if ctx.message.is_direct:
            ctx.session.push(ContextFrame("INCIDENT", str(value.local_ref)))
            response.screen = TuiScreen(
                "incident",
                f"INCIDENT {value.local_ref}",
                choices=(
                    TuiChoice("Confirm this report", f"CONFIRM {value.local_ref}"),
                    TuiChoice("Dispute this report", f"MENU DISPUTE {value.local_ref}"),
                    TuiChoice("Acknowledge", f"MENU ACK {value.local_ref}"),
                    *(
                        (TuiChoice("Correct my location", f"MENU UPD {value.local_ref}"),)
                        if value.reporter_id == ctx.member.id
                        and value.status in {"open", "monitoring"}
                        else ()
                    ),
                ),
            )
        return response

    base = _WatchDefaults(
        module="watch",
        min_trust=TrustLevel.GUEST,
        airtime_class=TrafficClass.REPLY,
        max_parts=3,
        rate_key="incidents",
    )
    return [
        CommandSpec(
            "REPORT",
            (),
            help_short=(
                "REPORT [-nopos|-wp name] <details> · files a public incident; "
                "attached position is visible. Not 911."
            ),
            handler=report,
            mutates=True,
            channel_use=ChannelUse.REPORT,
            **base,
        ),
        CommandSpec(
            "REPORT!",
            (),
            help_short="REPORT! <details> · file despite duplicate suggestion",
            handler=report_force,
            mutates=True,
            channel_use=ChannelUse.REPORT,
            **base,
        ),
        CommandSpec(
            "UPD",
            (),
            help_short=(
                "UPD <inc> <place|-share lat lon|-share -wp name|-nopos> · "
                "correct own report; verified DM only (else ask operator). "
                "Public; no cached GPS. -share consents to coordinates."
            ),
            handler=update_location,
            mutates=True,
            channel_use=ChannelUse.REPORT,
            **base,
        ),
        CommandSpec(
            "CONFIRM",
            (),
            help_short="CONFIRM <inc> · independently confirm",
            handler=confirm,
            mutates=True,
            channel_use=ChannelUse.REPORT,
            **base,
        ),
        CommandSpec(
            "DISPUTE",
            (),
            help_short="DISPUTE <inc> [note] · flag concern",
            handler=dispute,
            mutates=True,
            channel_use=ChannelUse.REPORT,
            **base,
        ),
        CommandSpec(
            "INCIDENTS",
            ("INCS",),
            help_short="INCIDENTS · active incident list",
            handler=incidents,
            mutates=False,
            channel_use=ChannelUse.ALERT,
            **base,
        ),
        CommandSpec(
            "INC",
            ("I",),
            help_short="INC <number> · incident detail",
            handler=detail,
            mutates=False,
            channel_use=ChannelUse.ALERT,
            **base,
        ),
    ]
