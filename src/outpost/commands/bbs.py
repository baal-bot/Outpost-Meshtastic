from __future__ import annotations

from outpost.bbs.service import BBSService
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
from outpost.transport.models import TrafficClass


def _response(text: str, kind: ResponseKind = ResponseKind.DETAIL) -> Response:
    return Response(kind, [Line(text)])


def _age(timestamp: int, now: int) -> str:
    seconds = max(0, now - timestamp)
    if seconds < 3_600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86_400:
        return f"{seconds // 3_600}h"
    if seconds < 604_800:
        return f"{seconds // 86_400}d"
    return f"{seconds // 604_800}w"


def specs(service: BBSService, self_delete_minutes: int = 30) -> list[CommandSpec]:
    async def boards(ctx: CommandContext) -> Response:
        values = await service.boards(ctx.member)
        if ctx.message.is_direct:
            choices = tuple(
                TuiChoice(
                    f"{board.title} ({board.thread_count})",
                    f"BOARD {board.slug}",
                    (board.slug,),
                )
                for board in values
            )
            return Response(
                ResponseKind.LISTING,
                [Line("No boards are available.")] if not choices else [],
                screen=TuiScreen("boards", "COMMUNITY BOARDS", choices=choices),
            )
        text = " · ".join(
            f"{board.slug} {board.thread_count}" if board.thread_count else board.slug
            for board in values
        )
        return _response(text or "No boards.", ResponseKind.LISTING)

    async def board(ctx: CommandContext) -> Response:
        slug = ctx.args.strip().lower()
        if not slug:
            return _response("BOARD needs a name. Try: B roads", ResponseKind.ERROR)
        values = await service.threads(slug, ctx.member)
        exists = await service.board(slug, ctx.member)
        if exists is None:
            return _response(f'No board "{slug}".', ResponseKind.ERROR)
        ctx.session.context = [ContextFrame("BOARD", slug)]
        ctx.session.page_refs = [value.id for value in values]
        ctx.session.cursor_kind = "board"
        ctx.session.cursor_target = slug
        ctx.session.cursor_offset = len(values)
        ctx.session.cursor_expires_at = service.clock.monotonic() + service.page_ttl_seconds
        if not values:
            if ctx.message.is_direct:
                return Response(
                    ResponseKind.LISTING,
                    [Line("No threads yet.")],
                    screen=TuiScreen(
                        "board",
                        f"BOARD: {slug.upper()}",
                        input_command="POST",
                        input_prompt="Send text to create the first post",
                    ),
                )
            return _response(f"{slug} · no threads")
        now = int(service.clock.now().timestamp())
        if ctx.message.is_direct:
            return Response(
                ResponseKind.LISTING,
                screen=TuiScreen(
                    "board",
                    f"BOARD: {slug.upper()}",
                    choices=tuple(
                        TuiChoice(
                            f"{value.subject[:30]} · {max(0, value.post_count - 1)} replies · "
                            f"{_age(value.last_post_at, now)}",
                            f"READ {index}",
                        )
                        for index, value in enumerate(values, 1)
                    ),
                    input_command="POST",
                    input_prompt="Reply with a number to read, or send text to post",
                ),
            )
        lines = [f"{slug} · {len(values)} recent"]
        for index, value in enumerate(values, 1):
            replies = max(0, value.post_count - 1)
            lines.append(
                f"{index} {value.subject[:28]} {replies}rep "
                f"{_age(value.last_post_at, now)} @{value.author_label}"
            )
        return Response(ResponseKind.LISTING, [Line(line) for line in lines])

    async def post(ctx: CommandContext) -> Response:
        raw = ctx.args.strip()
        if not raw and ctx.message.is_direct:
            if ctx.member.handle is None:
                return _response("Claim a NAME before posting.", ResponseKind.ERROR)
            boards_available = await service.boards(ctx.member)
            return Response(
                ResponseKind.DETAIL,
                screen=TuiScreen(
                    "post-board",
                    "CHOOSE A BOARD",
                    choices=tuple(
                        TuiChoice(board.title, f"MENU POSTTO {board.slug}", (board.slug,))
                        for board in boards_available
                    ),
                    input_command="MENU POSTTO",
                    input_prompt="Choose a number, or send a board name",
                ),
            )
        context_board = next(
            (frame.ref for frame in reversed(ctx.session.context) if frame.kind == "BOARD"), None
        )
        if context_board:
            first, separator, rest = raw.partition(" ")
            known = await service.board(first.lower(), ctx.member) if separator else None
            slug, body = (first.lower(), rest) if known else (context_board, raw)
        else:
            slug, separator, body = raw.partition(" ")
            if not separator:
                return _response("POST needs board + text. Try: POST roads Bridge open.")
        try:
            created = await service.create_thread(slug, body, ctx.member)
        except (ValueError, PermissionError) as error:
            return _response(str(error), ResponseKind.ERROR)
        response = _response(f"✓ {created.board_slug}#{created.id}.", ResponseKind.ACK)
        if ctx.message.is_direct and ctx.session.tui_active:
            response.screen = TuiScreen(
                "post-sent",
                "POST SENT",
                choices=(TuiChoice("Return to board", f"BOARD {created.board_slug}"),),
            )
        return response

    def resolve_thread(ctx: CommandContext, token: str) -> int | None:
        if "#" in token:
            token = token.rsplit("#", 1)[1]
        if not token.isdigit():
            return None
        number = int(token)
        if 1 <= number <= len(ctx.session.page_refs):
            return ctx.session.page_refs[number - 1]
        return number

    async def read(ctx: CommandContext) -> Response:
        token = ctx.args.strip() or "1"
        thread_id = resolve_thread(ctx, token)
        if thread_id is None:
            return _response("READ needs a thread number.", ResponseKind.ERROR)
        value = await service.thread(thread_id, ctx.member)
        if value is None:
            return _response("No thread.", ResponseKind.ERROR)
        ctx.session.push(ContextFrame("THREAD", str(thread_id)))
        ctx.session.cursor_kind = "replies"
        ctx.session.cursor_target = str(thread_id)
        ctx.session.cursor_offset = 1
        ctx.session.cursor_expires_at = service.clock.monotonic() + service.page_ttl_seconds
        replies = max(0, value.post_count - 1)
        response = Response(
            ResponseKind.DETAIL,
            [
                Line(f"{value.board_slug}#{thread_id} {value.subject} · @{value.author_label}"),
                Line(value.body),
                Line(f"{replies} replies · RE <text> to reply"),
            ],
        )
        if ctx.message.is_direct:
            response.lines[-1] = Line(f"{replies} replies")
            response.screen = TuiScreen(
                "thread",
                f"THREAD: {value.board_slug}#{thread_id}",
                choices=(TuiChoice("More replies", "MORE", key="+"),) if replies else (),
                input_command="REPLY",
                input_prompt="Send text to reply",
            )
        return response

    async def reply(ctx: CommandContext) -> Response:
        raw = ctx.args.strip()
        thread_context = next(
            (frame.ref for frame in reversed(ctx.session.context) if frame.kind == "THREAD"), None
        )
        first, separator, rest = raw.partition(" ")
        explicit = resolve_thread(ctx, first) if separator else None
        if explicit is not None:
            thread_id, body = explicit, rest
        elif thread_context:
            thread_id, body = int(thread_context), raw
        else:
            return _response("RE needs thread + text. Try: RE roads#42 Open.")
        try:
            created = await service.reply(thread_id, body, ctx.member)
        except (ValueError, PermissionError) as error:
            return _response(str(error), ResponseKind.ERROR)
        response = _response(
            f"✓ {created.board_slug}#{created.thread_id}.{created.seq}.", ResponseKind.ACK
        )
        if ctx.message.is_direct and ctx.session.tui_active:
            response.screen = TuiScreen(
                "reply-sent",
                "REPLY SENT",
                choices=(TuiChoice("Return to thread", f"READ {created.thread_id}"),),
            )
        return response

    async def search(ctx: CommandContext) -> Response:
        terms = ctx.args.strip()
        if not terms:
            return _response("SEARCH needs terms. Try: SEARCH bridge", ResponseKind.ERROR)
        try:
            values = await service.search(terms, ctx.member)
        except Exception:
            values = []
        if not values:
            return _response("No match. Try SEARCH bridge, or B roads.")
        now = int(service.clock.now().timestamp())
        if ctx.message.is_direct:
            ctx.session.page_refs = [value.thread_id for value in values]
            return Response(
                ResponseKind.LISTING,
                screen=TuiScreen(
                    "search-results",
                    "SEARCH RESULTS",
                    choices=tuple(
                        TuiChoice(
                            f"{value.subject[:32]} · {value.board_slug} · "
                            f"{_age(value.created_at, now)}",
                            f"READ {value.thread_id}",
                        )
                        for value in values
                    ),
                ),
            )
        return _response(
            " · ".join(
                f"{value.board_slug}#{value.thread_id} {value.subject[:32]} "
                f"{_age(value.created_at, now)}"
                for value in values
            ),
            ResponseKind.LISTING,
        )

    async def new(ctx: CommandContext) -> Response:
        counts = await service.new_counts(ctx.member)
        if not counts:
            return _response("Nothing new.")
        return _response(
            "NEW " + " · ".join(f"{slug} {count}" for slug, count in counts.items()),
            ResponseKind.LISTING,
        )

    async def more(ctx: CommandContext) -> Response:
        if (
            ctx.session.cursor_kind is None
            or service.clock.monotonic() >= ctx.session.cursor_expires_at
        ):
            return _response("Expired. Repeat the command.", ResponseKind.ERROR)
        if ctx.session.cursor_target is None:
            return _response("No more.")
        if ctx.session.cursor_kind == "board":
            slug = ctx.session.cursor_target
            board_values = await service.threads(
                slug,
                ctx.member,
                limit=5,
                offset=ctx.session.cursor_offset,
            )
            if not board_values:
                ctx.session.cursor_kind = None
                return _response("No more.")
            ctx.session.cursor_offset += len(board_values)
            ctx.session.page_refs = [value.id for value in board_values]
            now = int(service.clock.now().timestamp())
            if ctx.message.is_direct:
                return Response(
                    ResponseKind.LISTING,
                    screen=TuiScreen(
                        "board",
                        f"BOARD: {slug.upper()}",
                        choices=tuple(
                            TuiChoice(
                                f"{value.subject[:30]} · "
                                f"{max(0, value.post_count - 1)} replies · "
                                f"{_age(value.last_post_at, now)}",
                                f"READ {index}",
                            )
                            for index, value in enumerate(board_values, 1)
                        ),
                        input_command="POST",
                        input_prompt="Reply with a number to read, or send text to post",
                    ),
                )
            return Response(
                ResponseKind.LISTING,
                [
                    Line(
                        f"{index} {value.subject[:28]} {max(0, value.post_count - 1)}rep "
                        f"{_age(value.last_post_at, now)} @{value.author_label}"
                    )
                    for index, value in enumerate(board_values, 1)
                ],
            )
        if ctx.session.cursor_kind != "replies":
            return _response("No more.")
        thread_id = int(ctx.session.cursor_target)
        values = await service.replies(
            thread_id,
            ctx.member,
            after_seq=ctx.session.cursor_offset,
            limit=3,
        )
        if not values:
            ctx.session.cursor_kind = None
            return _response("No more.")
        ctx.session.cursor_offset = values[-1].seq
        now = int(service.clock.now().timestamp())
        lines = [f"#{thread_id} replies"]
        lines.extend(
            f"{value.seq - 1} @{value.author_label} {_age(value.created_at, now)}: {value.body}"
            for value in values
        )
        if values[-1].seq < values[-1].post_count:
            lines.append(f"…MORE {values[-1].post_count - values[-1].seq}")
        else:
            ctx.session.cursor_kind = None
        response = Response(ResponseKind.LISTING, [Line(line) for line in lines])
        if ctx.message.is_direct:
            has_more = ctx.session.cursor_kind == "replies"
            response.screen = TuiScreen(
                "thread-replies",
                f"THREAD: #{thread_id}",
                choices=(TuiChoice("More replies", "MORE", key="+"),) if has_more else (),
                input_command="REPLY",
                input_prompt="Send text to reply",
            )
        return response

    async def subscribe(ctx: CommandContext) -> Response:
        parts = ctx.args.strip().lower().split()
        if not parts:
            return _response("SUB needs board [on_request|daily|immediate].", ResponseKind.ERROR)
        slug = parts[0]
        cadence = parts[1] if len(parts) > 1 else "on_request"
        try:
            await service.subscribe(slug, ctx.member, cadence)
        except (ValueError, PermissionError) as error:
            return _response(str(error), ResponseKind.ERROR)
        return _response(f"✓ Subscribed {slug} · {cadence}.", ResponseKind.ACK)

    async def unsubscribe(ctx: CommandContext) -> Response:
        slug = ctx.args.strip().lower()
        try:
            removed = await service.unsubscribe(slug, ctx.member)
        except ValueError as error:
            return _response(str(error), ResponseKind.ERROR)
        return _response(f"✓ Unsubscribed {slug}." if removed else f"Not subscribed {slug}.")

    async def remove_post(ctx: CommandContext) -> Response:
        reference = ctx.args.strip()
        if "#" in reference:
            reference = reference.rsplit("#", 1)[1]
        thread_text, dot, seq_text = reference.partition(".")
        if not thread_text.isdigit() or (dot and not seq_text.isdigit()):
            return _response("RMPOST needs board#thread.seq.", ResponseKind.ERROR)
        try:
            removed = await service.remove_own_post(
                int(thread_text), int(seq_text or 1), ctx.member, self_delete_minutes
            )
        except PermissionError as error:
            return _response(str(error), ResponseKind.ERROR)
        return _response("✓ Removed." if removed else "No post.")

    base = dict(
        module="bbs",
        min_trust=TrustLevel.GUEST,
        airtime_class=TrafficClass.REPLY,
        max_parts=3,
        rate_key="commands",
    )
    return [
        CommandSpec(
            "BOARDS",
            ("BL",),
            help_short="BOARDS · list boards",
            handler=boards,
            mutates=False,
            channel_use=ChannelUse.BBS_READ,
            **base,
        ),
        CommandSpec(
            "BOARD",
            ("B",),
            help_short="BOARD <name> · recent threads",
            handler=board,
            mutates=False,
            channel_use=ChannelUse.BBS_READ,
            **base,
        ),
        CommandSpec(
            "POST",
            ("P",),
            help_short="POST <board> <text>",
            handler=post,
            mutates=True,
            channel_use=ChannelUse.BBS_WRITE,
            **base,
        ),
        CommandSpec(
            "READ",
            ("R",),
            help_short="READ <n> · open thread",
            handler=read,
            mutates=False,
            channel_use=ChannelUse.BBS_READ,
            **base,
        ),
        CommandSpec(
            "REPLY",
            ("RE",),
            help_short="RE [thread] <text>",
            handler=reply,
            mutates=True,
            channel_use=ChannelUse.BBS_WRITE,
            **base,
        ),
        CommandSpec(
            "SEARCH",
            ("S",),
            help_short="SEARCH <terms>",
            handler=search,
            mutates=False,
            channel_use=ChannelUse.BBS_READ,
            **base,
        ),
        CommandSpec(
            "NEW",
            ("N",),
            help_short="NEW · unread digest",
            handler=new,
            mutates=True,
            channel_use=ChannelUse.BBS_WRITE,
            **base,
        ),
        CommandSpec(
            "MORE",
            ("+", "M+"),
            help_short="MORE · next page",
            handler=more,
            mutates=False,
            channel_use=ChannelUse.BBS_READ,
            **base,
        ),
        CommandSpec(
            "SUB",
            (),
            help_short="SUB <board> [cadence]",
            handler=subscribe,
            mutates=True,
            channel_use=ChannelUse.BBS_WRITE,
            **base,
        ),
        CommandSpec(
            "UNSUB",
            (),
            help_short="UNSUB <board>",
            handler=unsubscribe,
            mutates=True,
            channel_use=ChannelUse.BBS_WRITE,
            **base,
        ),
        CommandSpec(
            "RMPOST",
            (),
            help_short="RMPOST <ref>",
            handler=remove_post,
            mutates=True,
            channel_use=ChannelUse.BBS_WRITE,
            **base,
        ),
    ]
