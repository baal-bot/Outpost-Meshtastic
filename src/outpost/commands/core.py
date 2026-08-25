from __future__ import annotations

from outpost.render.catalogue import message
from outpost.router.models import (
    CommandContext,
    CommandSpec,
    Line,
    Response,
    ResponseKind,
    TrustLevel,
)
from outpost.transport.models import TrafficClass


def _detail(text: str) -> Response:
    return Response(ResponseKind.DETAIL, [Line(text)])


async def ping(ctx: CommandContext) -> Response:
    snr = "?" if ctx.message.rx_snr is None else f"{ctx.message.rx_snr:g}dB"
    hops = "?hop" if ctx.message.hops_away is None else f"{ctx.message.hops_away}hop"
    return _detail(f"pong {snr} {hops}")


async def about(ctx: CommandContext) -> Response:
    return _detail(
        f"{ctx.node_name} v{ctx.version} · op {ctx.operator_contact} · "
        f"{ctx.disclaimer}{ctx.attribution}"
    )


async def help_command(ctx: CommandContext) -> Response:
    topic = ctx.args.strip().upper()
    if topic:
        spec = ctx.registry.resolve(topic)
        if spec:
            return _detail(spec.help_short)
        if topic == "PRIVACY":
            return _detail(
                "Mail is private from other members, not the node operator. "
                "The operator can view stored plaintext; every dashboard view is audited."
            )
        groups = {
            "BBS": {"bbs"},
            "MAIL": {"mail"},
            "IDENTITY": {"identity"},
            "RADIO": {"core", "directory"},
            "OPERATOR": {"operator"},
            "WATCH": {"watch"},
            "ENV": {"env"},
        }
        modules = groups.get(topic)
        if modules:
            commands = [
                spec.name
                for spec in ctx.registry.commands()
                if spec.module in modules
                and ctx.member.trust != "blocked"
                and TrustLevel.parse(ctx.member.trust) >= spec.min_trust
            ]
            suffix = " Mail is operator-readable; HELP PRIVACY." if topic == "MAIL" else ""
            return Response(
                ResponseKind.DETAIL,
                [Line(f"{topic}: {' · '.join(commands)} · HELP <command>")]
                + ([Line(suffix.strip())] if suffix else []),
            )
        return _detail("Help topics: BBS · MAIL · IDENTITY · RADIO · WATCH · ENV · PRIVACY")
    return Response(
        ResponseKind.DETAIL,
        [
            Line("HELP BBS · MAIL · IDENTITY · RADIO · WATCH · ENV · PRIVACY"),
            Line("Start: NAME <handle> · BOARDS · NEW · WHO"),
        ],
    )


async def whoami(ctx: CommandContext) -> Response:
    handle = f"@{ctx.member.handle}" if ctx.member.handle else "unnamed"
    return _detail(f"{handle} · {ctx.member.trust} · {ctx.member.mesh_id}")


async def where(ctx: CommandContext) -> Response:
    if not ctx.session.context:
        return _detail(message("no_context"))
    value = " > ".join(f"{frame.kind.lower()} {frame.ref}" for frame in ctx.session.context)
    return _detail(message("where", context=value))


async def home(ctx: CommandContext) -> Response:
    ctx.session.context.clear()
    ctx.session.pending = None
    return _detail(message("home"))


async def back(ctx: CommandContext) -> Response:
    if ctx.session.context:
        ctx.session.context.pop()
    value = "home" if not ctx.session.context else ctx.session.context[-1].ref
    return _detail(message("back", context=value))


def specs() -> list[CommandSpec]:
    base = dict(
        module="core",
        min_trust=TrustLevel.GUEST,
        airtime_class=TrafficClass.REPLY,
        max_parts=2,
        rate_key="commands",
    )
    return [
        CommandSpec("PING", (), help_short="PING · test node reachability", handler=ping, **base),
        CommandSpec("ABOUT", (), help_short="ABOUT · node and operator", handler=about, **base),
        CommandSpec(
            "HELP", ("?", "H"), help_short="HELP [cmd] · command help", handler=help_command, **base
        ),
        CommandSpec(
            "WHOAMI", ("ME",), help_short="WHOAMI · identity and trust", handler=whoami, **base
        ),
        CommandSpec("WHERE", ("W?",), help_short="WHERE · current context", handler=where, **base),
        CommandSpec("HOME", ("..", "/"), help_short="HOME · clear context", handler=home, **base),
        CommandSpec(
            "BACK", ("<",), help_short="BACK · leave current context", handler=back, **base
        ),
    ]
