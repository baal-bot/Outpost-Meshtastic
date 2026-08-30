from __future__ import annotations

import re
from typing import cast

from outpost.bbs.mail import MailService
from outpost.member_data import MemberDataService
from outpost.router.models import (
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
from outpost.store.members import MemberRepo
from outpost.transport.models import TrafficClass

HANDLE = re.compile(r"^[a-z0-9_-]{2,12}$")
RESERVED = {"admin", "operator", "system", "outpost", "all", "here"}


def specs(
    members: MemberRepo,
    mail: MailService,
    member_data: MemberDataService,
    require_approval: bool,
) -> list[CommandSpec]:
    async def name(ctx: CommandContext) -> Response:
        handle = ctx.args.strip().lower()
        command_names = {spec.name.lower() for spec in ctx.registry.known_commands()}
        if not HANDLE.fullmatch(handle) or handle in RESERVED | command_names:
            return Response(ResponseKind.ERROR, [Line("NAME: 2-12 letters, numbers, _ or -.")])
        try:
            member = await members.claim_handle(
                ctx.member.mesh_id, handle, approve=not require_approval
            )
        except ValueError as error:
            if "key conflict" in str(error):
                return Response(
                    ResponseKind.ERROR,
                    [Line("Identity key changed. Ask the Outpost operator to review it.")],
                )
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

    async def mydata(ctx: CommandContext) -> Response:
        if not ctx.message.is_direct:
            return Response(ResponseKind.ERROR, [Line("MYDATA is direct-message only.")])
        value = await member_data.summary(ctx.member)
        expires = value["position_expires_at"]
        now = int(member_data.clock.now().timestamp())
        position_window = (
            f"position deletes in {max(0, (expires - now + 3_599) // 3_600)}h"
            if expires is not None
            else "no exact position"
        )
        return Response(
            ResponseKind.DETAIL,
            [
                Line(
                    f"Held: position {value['positions']} · messages {value['messages']} · "
                    f"mail {value['mail']} · posts {value['posts']}"
                ),
                Line(
                    f"Welfare {value['welfare']} · incidents/updates {value['incidents']} · "
                    f"AI {value['ai']} · security {value['security']}"
                ),
                Line(
                    f"{position_window} · messages {value['retention']['messages_days']}d · "
                    f"mail {value['retention']['mail_days']}d"
                ),
            ],
            screen=TuiScreen(
                "member-data",
                "MY STORED DATA",
                choices=(
                    TuiChoice("Delete exact position", "FORGETPOS CONFIRM"),
                    TuiChoice("Request removal", "REMOVEME"),
                    TuiChoice("Privacy explanation", "HELP PRIVACY"),
                ),
            ),
        )

    async def forget_position(ctx: CommandContext) -> Response:
        if ctx.args.strip().upper() != "CONFIRM":
            return Response(
                ResponseKind.DETAIL,
                [Line("This deletes current and pending exact positions immediately.")],
                screen=TuiScreen(
                    "forget-position-confirm",
                    "DELETE EXACT POSITION",
                    choices=(
                        TuiChoice(
                            "Confirm permanent deletion",
                            "FORGETPOS CONFIRM",
                        ),
                    ),
                ),
            )
        result = await member_data.delete_position(
            ctx.member.id,
            actor_kind="mesh",
            actor_ref=ctx.member.mesh_id,
        )
        removed = result["positions"] + result["pending_positions"]
        text = (
            "✓ Exact position deleted."
            if removed
            else "No current or pending exact position was stored."
        )
        return Response(ResponseKind.ACK, [Line(text)])

    async def remove_me(ctx: CommandContext) -> Response:
        request, created = await member_data.request_removal(ctx.member)
        text = (
            f"✓ Removal request {request['id']} sent for operator review."
            if created
            else f"Removal request {request['id']} is already awaiting operator review."
        )
        return Response(
            ResponseKind.ACK,
            [
                Line(text),
                Line("Safety and audit records may remain under the published privacy policy."),
            ],
        )

    base = dict(
        module="identity",
        min_trust=TrustLevel.GUEST,
        airtime_class=TrafficClass.REPLY,
        max_parts=2,
        rate_key="commands",
    )
    return [
        CommandSpec(
            "NAME",
            ("HANDLE",),
            help_short="NAME <handle> · claim handle",
            handler=name,
            mutates=True,
            **base,
        ),
        CommandSpec(
            "WHO",
            ("NODES",),
            help_short="WHO · recently heard",
            handler=who,
            mutates=False,
            **base,
        ),
        CommandSpec(
            "MYDATA",
            ("DATA",),
            module="identity",
            min_trust=TrustLevel.MEMBER,
            airtime_class=TrafficClass.REPLY,
            max_parts=3,
            rate_key="commands",
            help_short="MYDATA · my retained data counts",
            mutates=False,
            handler=cast(CommandHandler, mydata),
        ),
        CommandSpec(
            "FORGETPOS",
            ("DELPOS",),
            module="identity",
            min_trust=TrustLevel.MEMBER,
            airtime_class=TrafficClass.REPLY,
            max_parts=2,
            rate_key="commands",
            help_short="FORGETPOS · delete my exact position",
            mutates=True,
            handler=cast(CommandHandler, forget_position),
        ),
        CommandSpec(
            "REMOVEME",
            (),
            module="identity",
            min_trust=TrustLevel.MEMBER,
            airtime_class=TrafficClass.REPLY,
            max_parts=2,
            rate_key="commands",
            help_short="REMOVEME · request operator-reviewed removal",
            mutates=True,
            handler=cast(CommandHandler, remove_me),
        ),
    ]
