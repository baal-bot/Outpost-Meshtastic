from __future__ import annotations

from outpost.bbs.mail import MailService
from outpost.router.models import (
    CommandContext,
    CommandSpec,
    Line,
    Response,
    ResponseKind,
    TrustLevel,
    TuiChoice,
    TuiScreen,
)
from outpost.transport.models import TrafficClass


def specs(service: MailService) -> list[CommandSpec]:
    async def send(ctx: CommandContext) -> Response:
        handle, separator, body = ctx.args.strip().partition(" ")
        if not separator:
            if ctx.message.is_direct and not handle:
                if ctx.member.handle is None or ctx.member.trust == "guest":
                    return Response(
                        ResponseKind.ERROR,
                        [Line("Claim a NAME before sending mail.")],
                    )
                recent = [
                    member
                    for member in await service.members.recent(limit=7)
                    if member.handle and member.id != ctx.member.id
                ][:6]
                return Response(
                    ResponseKind.DETAIL,
                    screen=TuiScreen(
                        "mail-recipient",
                        "SEND MAIL TO",
                        choices=tuple(
                            TuiChoice(f"@{member.handle}", f"MENU SENDTO {member.handle}")
                            for member in recent
                        ),
                        input_command="MENU SENDTO",
                        input_prompt="Choose a person, or send their handle",
                    ),
                )
            return Response(ResponseKind.ERROR, [Line("SEND needs handle + text.")])
        try:
            await service.send(ctx.member, handle, body)
        except (ValueError, PermissionError) as error:
            return Response(ResponseKind.ERROR, [Line(str(error))])
        response = Response(ResponseKind.ACK, [Line(f"✓ Sent to @{handle.lower()}.")])
        if ctx.message.is_direct and ctx.session.tui_active:
            response.screen = TuiScreen(
                "mail-sent",
                "MAIL SENT",
                choices=(
                    TuiChoice("Open inbox", "MAIL"),
                    TuiChoice("Send another", "SEND"),
                ),
            )
        return response

    async def inbox(ctx: CommandContext) -> Response:
        values = await service.inbox(ctx.member)
        if not values:
            return Response(ResponseKind.DETAIL, [Line("No mail.")])
        listing = " · ".join(
            f"{index} @{value.from_label}" for index, value in enumerate(values, 1)
        )
        ctx.session.page_refs = [value.id for value in values]
        if ctx.message.is_direct:
            return Response(
                ResponseKind.LISTING,
                screen=TuiScreen(
                    "mail-inbox",
                    "MAIL INBOX",
                    choices=tuple(
                        TuiChoice(
                            f"From @{value.from_label}",
                            f"READMAIL {index}",
                        )
                        for index, value in enumerate(values, 1)
                    ),
                ),
            )
        return Response(
            ResponseKind.LISTING,
            [Line(f"{len(values)} mail: {listing} · RM <n> to read")],
        )

    async def readmail(ctx: CommandContext) -> Response:
        token = ctx.args.strip()
        if not token.isdigit():
            return Response(ResponseKind.ERROR, [Line("READMAIL needs a number.")])
        number = int(token)
        mail_id = (
            ctx.session.page_refs[number - 1]
            if 1 <= number <= len(ctx.session.page_refs)
            else number
        )
        value = await service.read(ctx.member, mail_id)
        if value is None:
            return Response(ResponseKind.ERROR, [Line("No mail.")])
        ctx.session.last_mail_id = value.id
        ctx.session.last_mail_sender = value.from_label
        response = Response(
            ResponseKind.DETAIL,
            [Line(f"@{value.from_label}"), Line(value.body), Line("RR <text> to reply")],
        )
        if ctx.message.is_direct:
            response.lines[-1] = Line("Stored mail · operator-readable")
            response.screen = TuiScreen(
                "mail-message",
                f"MAIL FROM @{value.from_label}",
                choices=(TuiChoice("Delete this message", f"MENU DELMAIL {value.id}"),),
                input_command="REPLYMAIL",
                input_prompt="Send text to reply",
            )
        return response

    async def replymail(ctx: CommandContext) -> Response:
        if ctx.session.last_mail_sender is None:
            return Response(ResponseKind.ERROR, [Line("Read mail first, or use SEND.")])
        body = ctx.args.strip()
        try:
            await service.reply(
                ctx.member,
                ctx.session.last_mail_id or 0,
                ctx.session.last_mail_sender,
                body,
            )
        except (ValueError, PermissionError) as error:
            return Response(ResponseKind.ERROR, [Line(str(error))])
        response = Response(
            ResponseKind.ACK,
            [Line(f"✓ Sent to @{ctx.session.last_mail_sender}.")],
        )
        if ctx.message.is_direct and ctx.session.tui_active:
            response.screen = TuiScreen(
                "mail-replied",
                "REPLY SENT",
                choices=(TuiChoice("Open inbox", "MAIL"),),
            )
        return response

    async def delmail(ctx: CommandContext) -> Response:
        token = ctx.args.strip()
        if not token.isdigit():
            return Response(ResponseKind.ERROR, [Line("DELMAIL needs a number.")])
        number = int(token)
        mail_id = (
            ctx.session.page_refs[number - 1]
            if 1 <= number <= len(ctx.session.page_refs)
            else number
        )
        removed = await service.delete(ctx.member, mail_id)
        return Response(
            ResponseKind.ACK if removed else ResponseKind.ERROR,
            [Line("✓ Mail deleted." if removed else "No mail.")],
        )

    base = dict(
        module="mail",
        min_trust=TrustLevel.GUEST,
        airtime_class=TrafficClass.REPLY,
        max_parts=3,
        rate_key="mail",
    )
    return [
        CommandSpec(
            "SEND",
            ("SM",),
            help_short="SEND <handle> <text>",
            handler=send,
            mutates=True,
            **base,
        ),
        CommandSpec(
            "MAIL",
            ("MB",),
            help_short="MAIL · inbox",
            handler=inbox,
            mutates=False,
            **base,
        ),
        CommandSpec(
            "READMAIL",
            ("RM",),
            help_short="READMAIL <n> · read mail",
            handler=readmail,
            mutates=True,
            **base,
        ),
        CommandSpec(
            "REPLYMAIL",
            ("RR",),
            help_short="RR <text> · reply mail",
            handler=replymail,
            mutates=True,
            **base,
        ),
        CommandSpec(
            "DELMAIL",
            ("DM",),
            help_short="DELMAIL <n>",
            handler=delmail,
            mutates=True,
            **base,
        ),
    ]
