from __future__ import annotations

from typing import cast

from outpost.router.models import (
    ChannelUse,
    CommandContext,
    CommandHandler,
    CommandSpec,
    Line,
    Response,
    ResponseKind,
    TrustLevel,
)
from outpost.transport.models import TrafficClass
from outpost.watch import CheckinService


def specs(service: CheckinService) -> list[CommandSpec]:
    async def ok(ctx: CommandContext) -> Response:
        result = await service.checkin(ctx.member, "ok", ctx.args.strip())
        now = service.clock.now().strftime("%H:%M")
        marker = " DRILL" if (result.get("event") or {}).get("event_kind") == "drill" else ""
        return Response(
            ResponseKind.ACK,
            [Line(f"✓{marker} ok {now}. {result['checked_in']}/{result['total']} in.")],
        )

    async def helpme(ctx: CommandContext) -> Response:
        result = await service.checkin(ctx.member, "need_help", ctx.args.strip())
        admitted = int((result.get("notification") or {}).get("admitted", 0))
        if not admitted:
            return Response(
                ResponseKind.ACK,
                [
                    Line(
                        "⚠ No responder was reached. Contact 911 or emergency services if able. "
                        "NEED HELP recorded; Outpost is not an emergency service."
                    )
                ],
                airtime_class=TrafficClass.ALERT,
            )
        suffix = f" {admitted} responder{'s' if admitted != 1 else ''} notified."
        return Response(
            ResponseKind.ACK,
            [Line(f"✓ NEED HELP recorded.{suffix} Not 911; call emergency services if able.")],
            airtime_class=TrafficClass.ALERT,
        )

    async def roster(ctx: CommandContext) -> Response:
        event = await service.current_event()
        if event is None:
            return Response(ResponseKind.DETAIL, [Line("No open watch event.")])
        value = await service.summary(event.id)
        counts = value["counts"]
        marker = "DRILL · " if event.event_kind == "drill" else ""
        return Response(
            ResponseKind.LISTING,
            [
                Line(
                    f'{marker}Event "{event.name}": {counts["ok"]} ok · '
                    f"{counts['need_help']} help · {counts['unaccounted']} unaccounted. "
                    "ROSTER? for names."
                )
            ],
        )

    async def roster_names(ctx: CommandContext) -> Response:
        event = await service.current_event()
        if event is None:
            return Response(ResponseKind.DETAIL, [Line("No open watch event.")])
        value = await service.summary(event.id)
        marker = "DRILL · " if event.event_kind == "drill" else ""
        lines = [Line(f'{marker}Event "{event.name}" roster')]
        lines.extend(
            Line(f"{row['handle'] or row['mesh_id']} · {row['status']}") for row in value["items"]
        )
        return Response(ResponseKind.LISTING, lines)

    async def event(ctx: CommandContext) -> Response:
        verb, _, remainder = ctx.args.strip().partition(" ")
        if verb.upper() == "OPEN":
            policy, separator, name = remainder.partition(" ")
            if policy not in {"all", "responders", "subscribed"} or not separator:
                return Response(
                    ResponseKind.ERROR,
                    [Line("EVENT OPEN <all|responders|subscribed> <name>.")],
                )
            try:
                value = await service.open_event(
                    name, policy, ctx.member.handle or ctx.member.mesh_id
                )
            except ValueError as error:
                return Response(ResponseKind.ERROR, [Line(str(error))])
            return Response(ResponseKind.ACK, [Line(f'✓ Event "{value.name}" opened.')])
        if verb.upper() == "CLOSE":
            current = await service.current_event()
            if current is None:
                return Response(ResponseKind.ERROR, [Line("No open watch event.")])
            await service.close_event(current.id)
            return Response(ResponseKind.ACK, [Line(f'✓ Event "{current.name}" closed.')])
        return Response(
            ResponseKind.ERROR,
            [Line("EVENT OPEN <policy> <name>, or EVENT CLOSE.")],
        )

    async def drills(ctx: CommandContext) -> Response:
        value = ctx.args.strip().upper()
        if not value:
            enabled = await service.drill_participation(ctx.member.id)
            return Response(
                ResponseKind.DETAIL,
                [Line(f"Practice welfare drills: {'on' if enabled else 'off'} · DRILLS ON|OFF")],
            )
        if value not in {"ON", "OFF"}:
            return Response(ResponseKind.ERROR, [Line("DRILLS ON or DRILLS OFF.")])
        enabled = await service.set_drill_participation(ctx.member.id, value == "ON")
        detail = (
            "You may receive practice welfare requests."
            if enabled
            else ("Practice requests stopped; real welfare checks are unchanged.")
        )
        return Response(
            ResponseKind.ACK,
            [Line(f"✓ Practice welfare drills {'on' if enabled else 'off'}. {detail}")],
        )

    base = dict(
        module="watch",
        airtime_class=TrafficClass.REPLY,
        max_parts=3,
        rate_key="commands",
    )
    return [
        CommandSpec(
            "OK",
            (),
            min_trust=TrustLevel.GUEST,
            help_short="OK [note] · welfare check-in",
            handler=ok,
            mutates=True,
            **base,
        ),
        CommandSpec(
            "HELPME",
            (),
            min_trust=TrustLevel.GUEST,
            help_short="HELPME [note] · record need-help and notify responders",
            handler=helpme,
            mutates=True,
            **base,
        ),
        CommandSpec(
            "ROSTER",
            (),
            min_trust=TrustLevel.GUEST,
            help_short="ROSTER · current welfare summary",
            handler=roster,
            mutates=False,
            **base,
        ),
        CommandSpec(
            "ROSTER?",
            (),
            min_trust=TrustLevel.RESPONDER,
            help_short="ROSTER? · responder-visible names",
            handler=roster_names,
            mutates=False,
            **base,
        ),
        CommandSpec(
            "DRILLS",
            (),
            module="watch",
            min_trust=TrustLevel.MEMBER,
            airtime_class=TrafficClass.REPLY,
            max_parts=3,
            rate_key="commands",
            help_short="DRILLS ON|OFF · practice welfare participation",
            handler=cast(CommandHandler, drills),
            mutates=True,
        ),
        CommandSpec(
            "EVENT",
            (),
            min_trust=TrustLevel.RESPONDER,
            help_short="EVENT OPEN <policy> <name>|CLOSE",
            handler=event,
            mutates=True,
            channel_use=ChannelUse.ALERT,
            **base,
        ),
    ]
