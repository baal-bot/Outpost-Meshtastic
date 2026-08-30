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
from outpost.watch import AlertService


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
    ]
