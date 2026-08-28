from __future__ import annotations

from outpost.router.models import (
    ChannelUse,
    CommandContext,
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
        return Response(
            ResponseKind.ACK,
            [Line(f"✓ ok {now}. {result['checked_in']}/{result['total']} in.")],
        )

    async def helpme(ctx: CommandContext) -> Response:
        result = await service.checkin(ctx.member, "need_help", ctx.args.strip())
        suffix = " Responders notified." if result["event"] else " Help recorded."
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
        return Response(
            ResponseKind.LISTING,
            [
                Line(
                    f'Event "{event.name}": {counts["ok"]} ok · '
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
        lines = [Line(f'Event "{event.name}" roster')]
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
