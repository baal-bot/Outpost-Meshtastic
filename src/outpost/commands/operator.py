from __future__ import annotations

import re

from outpost.bbs.service import BBSService
from outpost.router.models import (
    CommandContext,
    CommandSpec,
    Line,
    Response,
    ResponseKind,
    TrustLevel,
)
from outpost.transport.models import TrafficClass


def specs(bbs: BBSService) -> list[CommandSpec]:
    async def op(ctx: CommandContext) -> Response:
        verb, _, args = ctx.args.strip().partition(" ")
        if verb.upper() == "STATUS":
            hidden, audited = await bbs.moderation_status()
            return Response(
                ResponseKind.DETAIL, [Line(f"Moderation: {hidden} hidden · {audited} audited")]
            )
        if verb.upper() == "RM":
            reference, _, reason = args.strip().partition(" ")
            match = re.fullmatch(r"(\d+)(?:\.(\d+))?", reference)
            if match is None:
                return Response(ResponseKind.ERROR, [Line("Use OP RM <thread>[.<post>] <reason>.")])
            thread_id = int(match.group(1))
            seq = int(match.group(2) or 1)
            removed = await bbs.moderate_remove(thread_id, seq, ctx.member, reason)
            if not removed:
                return Response(ResponseKind.ERROR, [Line("No visible post at that reference.")])
            return Response(ResponseKind.ACK, [Line(f"Removed {thread_id}.{seq}; audit recorded.")])
        return Response(
            ResponseKind.ERROR, [Line("Use OP STATUS or OP RM <thread>[.<post>] <reason>.")]
        )

    return [
        CommandSpec(
            "OP",
            (),
            module="operator",
            min_trust=TrustLevel.OPERATOR,
            airtime_class=TrafficClass.REPLY,
            max_parts=1,
            rate_key="commands",
            help_short="OP STATUS|RM · operator tools",
            handler=op,
            mutates=True,
        )
    ]
