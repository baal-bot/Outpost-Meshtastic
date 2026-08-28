from __future__ import annotations

from outpost.ai import AIService
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


def specs(service: AIService) -> list[CommandSpec]:
    async def run(ctx: CommandContext, question: str) -> Response:
        result = await service.answer(
            question,
            ctx.member,
            -1 if ctx.message.is_direct else ctx.message.channel,
            ctx.registry,
        )
        kind = (
            ResponseKind.ERROR
            if result.outcome in {"invalid", "provider_error"}
            else ResponseKind.DETAIL
        )
        return Response(
            kind,
            [Line(result.text)],
            airtime_class=result.airtime_class,
        )

    async def ask(ctx: CommandContext) -> Response:
        return await run(ctx, ctx.args)

    async def summarize(ctx: CommandContext) -> Response:
        target = ctx.args.strip()
        if not target and ctx.session.context:
            frame = ctx.session.context[-1]
            target = f"{frame.kind} {frame.ref}"
        if not target:
            return Response(ResponseKind.ERROR, [Line("SUM needs a board, thread, or incident.")])
        return await run(ctx, f"Summarize {target}")

    async def translate(ctx: CommandContext) -> Response:
        language, separator, text = ctx.args.strip().partition(" ")
        if not separator or not text:
            return Response(ResponseKind.ERROR, [Line("TR needs <language> <text>.")])
        return await run(ctx, f"Translate to {language}: {text}")

    base = dict(
        module="ai",
        min_trust=TrustLevel.MEMBER,
        airtime_class=TrafficClass.AI,
        max_parts=2,
        rate_key="commands",
        mutates=False,
        channel_use=ChannelUse.AI,
    )
    return [
        CommandSpec(
            "ASK", ("A", "AI"), help_short="ASK <question> · local answers", handler=ask, **base
        ),
        CommandSpec("SUM", (), help_short="SUM <board|thread|incident>", handler=summarize, **base),
        CommandSpec("TR", (), help_short="TR <language> <text>", handler=translate, **base),
    ]
