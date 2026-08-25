from __future__ import annotations

from outpost.bbs.channels import ChannelDirectory
from outpost.router.models import (
    CommandContext,
    CommandSpec,
    Line,
    Response,
    ResponseKind,
    TrustLevel,
)
from outpost.transport.models import TrafficClass


def specs(directory: ChannelDirectory) -> list[CommandSpec]:
    async def chans(ctx: CommandContext) -> Response:
        values = await directory.list(ctx.member)
        text = " · ".join(
            f"{value.name}: {value.description}" if value.description else value.name
            for value in values
        )
        return Response(ResponseKind.LISTING, [Line(text or "No channels listed.")])

    async def chan(ctx: CommandContext) -> Response:
        if not ctx.message.is_direct:
            return Response(ResponseKind.ERROR, [Line("CHAN details are DM only.")])
        value = await directory.detail(ctx.args.strip(), ctx.member, direct=True)
        if value is None:
            return Response(ResponseKind.ERROR, [Line("No channel, or NAME required.")])
        parts = [value.name, value.description or "", f"slot {value.slot}"]
        if value.psk_b64:
            parts.append(f"key {value.psk_b64}")
        return Response(ResponseKind.DETAIL, [Line(" · ".join(part for part in parts if part))])

    base = dict(
        module="directory",
        min_trust=TrustLevel.GUEST,
        airtime_class=TrafficClass.REPLY,
        max_parts=2,
        rate_key="commands",
    )
    return [
        CommandSpec("CHANS", (), help_short="CHANS · community channels", handler=chans, **base),
        CommandSpec("CHAN", (), help_short="CHAN <name> · DM details", handler=chan, **base),
    ]
