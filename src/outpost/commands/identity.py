from __future__ import annotations

import re

from outpost.bbs.mail import MailService
from outpost.router.models import (
    CommandContext,
    CommandSpec,
    Line,
    Response,
    ResponseKind,
    TrustLevel,
)
from outpost.store.members import MemberRepo
from outpost.transport.models import TrafficClass

HANDLE = re.compile(r"^[a-z0-9_-]{2,12}$")
RESERVED = {"admin", "operator", "system", "outpost", "all", "here"}


def specs(members: MemberRepo, mail: MailService, require_approval: bool) -> list[CommandSpec]:
    async def name(ctx: CommandContext) -> Response:
        handle = ctx.args.strip().lower()
        command_names = {spec.name.lower() for spec in ctx.registry.commands()}
        if not HANDLE.fullmatch(handle) or handle in RESERVED | command_names:
            return Response(ResponseKind.ERROR, [Line("NAME: 2-12 letters, numbers, _ or -.")])
        try:
            member = await members.claim_handle(
                ctx.member.mesh_id, handle, approve=not require_approval
            )
        except ValueError:
            return Response(ResponseKind.ERROR, [Line("Handle already claimed.")])
        await mail.bind_handle(member)
        status = "pending approval" if require_approval else "member"
        return Response(
            ResponseKind.ACK,
            [
                Line(f"✓ @{handle} · {status}"),
                Line("Mail is operator-readable plaintext. HELP PRIVACY."),
            ],
        )

    async def who(ctx: CommandContext) -> Response:
        values = await members.recent()
        text = " · ".join(
            f"@{member.handle}" if member.handle else member.mesh_id for member in values
        )
        return Response(ResponseKind.LISTING, [Line(text or "Nobody heard.")])

    base = dict(
        module="identity",
        min_trust=TrustLevel.GUEST,
        airtime_class=TrafficClass.REPLY,
        max_parts=2,
        rate_key="commands",
    )
    return [
        CommandSpec(
            "NAME", ("HANDLE",), help_short="NAME <handle> · claim handle", handler=name, **base
        ),
        CommandSpec("WHO", ("NODES",), help_short="WHO · recently heard", handler=who, **base),
    ]
