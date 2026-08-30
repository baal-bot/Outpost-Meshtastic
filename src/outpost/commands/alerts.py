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
    TuiChoice,
    TuiScreen,
)
from outpost.transport.models import TrafficClass
from outpost.watch import AlertService


def _proximity(distance_km: float | None, bearing: int | None) -> str | None:
    if distance_km is None or bearing is None:
        return None
    distance = f"{distance_km:.1f}km" if distance_km < 10 else f"{distance_km:.0f}km"
    return f"{distance} {bearing:03d}°"


def specs(service: AlertService) -> list[CommandSpec]:
    async def alert(ctx: CommandContext) -> Response:
        severity, _, remainder = ctx.args.strip().partition(" ")
        reference, _, headline = remainder.partition(" ")
        if not reference.isdigit() or not headline:
            return Response(
                ResponseKind.ERROR,
                [Line("ALERT needs <caution|urgent|critical> <incident> <headline>.")],
            )
        try:
            value = await service.raise_alert(
                severity.lower(),
                headline,
                ctx.member.handle or ctx.member.mesh_id,
                incident_ref=int(reference),
                source="incident",
            )
        except ValueError as error:
            return Response(ResponseKind.ERROR, [Line(str(error))])
        delivery = (
            f"{value.last_delivery_count} transmission"
            f"{'s' if value.last_delivery_count != 1 else ''} queued"
            if value.last_delivery_count
            else "no recipient reached; operator review required"
        )
        return Response(
            ResponseKind.ACK,
            [Line(f"✓ ALERT {value.id} recorded for INC {reference}; {delivery}.")],
        )

    async def ack(ctx: CommandContext) -> Response:
        reference, _, note = ctx.args.strip().partition(" ")
        if not reference.isdigit():
            return Response(ResponseKind.ERROR, [Line("ACK needs incident number.")])
        try:
            value = await service.acknowledge(int(reference), ctx.member, note)
        except ValueError as error:
            return Response(ResponseKind.ERROR, [Line(str(error))])
        return Response(ResponseKind.ACK, [Line(f"✓ ack INC {reference} · {value.ack_count}")])

    async def active(ctx: CommandContext) -> Response:
        values = await service.list()
        token = ctx.args.strip()
        if token:
            if not token.isdigit():
                return Response(ResponseKind.ERROR, [Line("ALERTS needs alert number.")])
            value = next((item for item in values if item.id == int(token)), None)
            if value is None:
                return Response(ResponseKind.ERROR, [Line("No active alert by that number.")])
            proximity = None
            if ctx.message.is_direct:
                _, distance_km, bearing = (await service.ranked_for_member([value], ctx.member))[0]
                proximity = _proximity(distance_km, bearing)
            lines = [
                Line(
                    f"ALERT {value.id} · {value.severity.upper()}"
                    f"{f' · INC {value.incident_ref}' if value.incident_ref else ''}"
                ),
                Line(value.headline),
            ]
            if proximity:
                radius = (
                    f" · radius {value.radius_m / 1000:.1f}km" if value.radius_m is not None else ""
                )
                lines.append(Line(f"{proximity} from you{radius}"))
            response = Response(ResponseKind.DETAIL, lines)
            if ctx.message.is_direct:
                response.screen = TuiScreen(
                    "active-alert",
                    f"ALERT {value.id}",
                    choices=(TuiChoice("Back to active alerts", "ALERTS"),),
                )
            return response
        if not values:
            return Response(ResponseKind.LISTING, [Line("No active Outpost alerts.")])
        if ctx.message.is_direct:
            ranked = (await service.ranked_for_member(values, ctx.member))[:5]
            return Response(
                ResponseKind.LISTING,
                screen=TuiScreen(
                    "active-alerts",
                    "ACTIVE ALERTS",
                    choices=tuple(
                        TuiChoice(
                            " · ".join(
                                part
                                for part in (
                                    f"ALERT {value.id}",
                                    value.severity.upper(),
                                    _proximity(distance_km, bearing),
                                    value.headline[:18],
                                )
                                if part
                            ),
                            f"ALERTS {value.id}",
                        )
                        for value, distance_km, bearing in ranked
                    ),
                ),
            )
        return Response(
            ResponseKind.LISTING,
            [
                Line(f"ALERT {value.id} · {value.severity.upper()} · {value.headline[:60]}")
                for value in values[:3]
            ],
        )

    return [
        CommandSpec(
            "ALERT",
            (),
            module="watch",
            min_trust=TrustLevel.RESPONDER,
            airtime_class=TrafficClass.REPLY,
            max_parts=1,
            rate_key="commands",
            help_short="ALERT <severity> <inc> <headline> · responder broadcast",
            handler=alert,
            mutates=True,
            channel_use=ChannelUse.ALERT,
        ),
        CommandSpec(
            "ACK",
            ("ACKNOWLEDGE",),
            module="watch",
            min_trust=TrustLevel.GUEST,
            airtime_class=TrafficClass.REPLY,
            max_parts=1,
            rate_key="commands",
            help_short="ACK <inc> [note] · acknowledge active alert",
            handler=ack,
            mutates=True,
            channel_use=ChannelUse.REPORT,
        ),
        CommandSpec(
            "ALERTS",
            (),
            module="watch",
            min_trust=TrustLevel.GUEST,
            airtime_class=TrafficClass.REPLY,
            max_parts=2,
            rate_key="commands",
            help_short="ALERTS [number] · active alerts; distance/bearing in DM",
            handler=cast(CommandHandler, active),
            mutates=False,
            channel_use=ChannelUse.ALERT,
        ),
    ]
