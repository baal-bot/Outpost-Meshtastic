from __future__ import annotations

from outpost.render.catalogue import message
from outpost.router.channel_policy import available as channel_available
from outpost.router.channel_policy import decide as channel_decision
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


def _detail(text: str) -> Response:
    return Response(ResponseKind.DETAIL, [Line(text)])


def _available(ctx: CommandContext, command: str) -> bool:
    token = command.split(maxsplit=1)[0]
    spec = ctx.registry.resolve(token)
    if not spec or TrustLevel.parse(ctx.member.trust) < spec.min_trust:
        return False
    member_only_actions = {"POST", "REPLY", "SEND", "SUB", "UNSUB", "RMPOST"}
    if token in member_only_actions and ctx.member.handle is None:
        return False
    return channel_available(
        spec,
        direct=ctx.message.is_direct,
        policy=ctx.channel_policy,
    )


def _broadcast_shortcuts(ctx: CommandContext) -> str:
    candidates = (
        ("WX", "WX"),
        ("WARN", "WARN"),
        ("INCS", "INCIDENTS"),
        ("OK", "OK"),
        ("BOARDS", "BOARDS"),
        ("ASK", "ASK"),
    )
    return " · ".join(f"!{label}" for label, command in candidates if _available(ctx, command))


def _choices(
    ctx: CommandContext, values: list[tuple[str, str, str | None, tuple[str, ...]]]
) -> tuple[TuiChoice, ...]:
    return tuple(
        TuiChoice(label, command, aliases)
        for label, command, required, aliases in values
        if required is None or _available(ctx, required)
    )


def _screen(
    ctx: CommandContext,
    name: str,
    title: str,
    values: list[tuple[str, str, str | None, tuple[str, ...]]],
) -> Response:
    return Response(
        ResponseKind.DETAIL,
        screen=TuiScreen(name, title, choices=_choices(ctx, values)),
    )


def _input_screen(
    name: str,
    title: str,
    description: str,
    prompt: str,
    command: str,
) -> Response:
    return Response(
        ResponseKind.DETAIL,
        [Line(description)],
        screen=TuiScreen(name, title, input_command=command, input_prompt=prompt),
    )


def _home_screen(ctx: CommandContext) -> Response:
    return _screen(
        ctx,
        "home",
        "HOME",
        [
            ("Weather & alerts", "MENU WEATHER", "WX", ("weather", "alerts")),
            ("Incidents & safety", "MENU SAFETY", "INCIDENTS", ("safety", "incidents")),
            ("Community boards", "MENU COMMUNITY", "BOARDS", ("boards", "community")),
            ("Mail", "MENU MAIL", "MAIL", ("messages", "inbox")),
            ("People & places", "MENU PEOPLE", "WHO", ("people", "places")),
            ("Ask Outpost", "MENU ASK", "ASK", ("ask", "ai")),
            ("My account", "MENU ACCOUNT", "WHOAMI", ("account", "me")),
            ("Command shortcuts", "MENU SHORTCUTS", "HELP", ("commands", "shortcuts")),
        ],
    )


def _menu_screen(ctx: CommandContext, topic: str) -> Response:
    topic, _, topic_args = topic.strip().partition(" ")
    topic = topic.upper()
    if topic in {"", "HOME", "MAIN"}:
        return _home_screen(ctx)
    if topic in {"WEATHER", "ENV", "ALERTS"}:
        return _screen(
            ctx,
            "weather",
            "WEATHER & ALERTS",
            [
                ("Weather now", "WX", "WX", ("now",)),
                ("Today's outlook", "WX TODAY", "WX", ("today",)),
                ("Tomorrow", "WX TOMORROW", "WX", ("tomorrow",)),
                ("Next 6 hours", "WX HOURLY", "WX", ("hourly",)),
                ("5-day forecast", "FC 5", "FC", ("forecast",)),
                ("Official alerts", "WARN", "WARN", ("warnings",)),
                ("Sun & moon", "SUN", "SUN", ("sun", "moon")),
                ("Nearby earthquakes", "QUAKE", "QUAKE", ("quakes",)),
            ],
        )
    if topic in {"SAFETY", "WATCH", "INCIDENTS"}:
        return _screen(
            ctx,
            "safety",
            "INCIDENTS & SAFETY",
            [
                ("Active incidents", "INCIDENTS", "INCIDENTS", ("active",)),
                ("Report a problem", "MENU REPORT", "REPORT", ("report",)),
                ("Confirm an incident", "MENU CONFIRM", "CONFIRM", ("confirm",)),
                ("Dispute an incident", "MENU DISPUTE", "DISPUTE", ("dispute",)),
                ("Check in", "MENU CHECKIN", "OK", ("check in",)),
                ("Welfare roster", "ROSTER", "ROSTER", ("roster",)),
                ("Responder tools", "MENU RESPONDER", "ALERT", ("responder",)),
            ],
        )
    if topic in {"COMMUNITY", "BBS", "BOARDS"}:
        return _screen(
            ctx,
            "community",
            "COMMUNITY BOARDS",
            [
                ("Browse boards", "BOARDS", "BOARDS", ("browse",)),
                ("New since last visit", "NEW", "NEW", ("new",)),
                ("Search posts", "MENU SEARCH", "SEARCH", ("search",)),
                ("Create a post", "POST", "POST", ("post",)),
                ("Subscribe to a board", "MENU SUBSCRIBE", "SUB", ("subscribe",)),
                ("Unsubscribe", "MENU UNSUBSCRIBE", "UNSUB", ("unsubscribe",)),
                ("Remove my post", "MENU RMPOST", "RMPOST", ("remove",)),
            ],
        )
    if topic in {"MAIL", "MESSAGES"}:
        return _screen(
            ctx,
            "mail",
            "MAIL",
            [
                ("Open inbox", "MAIL", "MAIL", ("inbox",)),
                ("Send a message", "SEND", "SEND", ("send", "compose")),
                ("How private is mail?", "HELP PRIVACY", "HELP", ("privacy",)),
            ],
        )
    if topic in {"PEOPLE", "PLACES", "LOCATION"}:
        return _screen(
            ctx,
            "people",
            "PEOPLE & PLACES",
            [
                ("Recently heard", "WHO", "WHO", ("nearby",)),
                ("Channels", "CHANS", "CHANS", ("channels",)),
                ("Waypoints", "WPS", "WPS", ("waypoints",)),
                ("My position", "POS", "POS", ("my position",)),
                ("Find member", "MENU FIND", "POS", ("find",)),
                ("Waypoint distance", "MENU DIST", "DIST", ("distance",)),
                ("Save waypoint", "MENU WPADD", "WAYPOINT", ("save",)),
                ("Location sharing", "MENU POSITION", "POS", ("sharing",)),
            ],
        )
    if topic == "CHECKIN":
        return _screen(
            ctx,
            "checkin",
            "CHECK IN",
            [
                ("I'm OK", "OK", "OK", ("ok", "safe")),
                ("I need help", "MENU HELPME", "HELPME", ("help me",)),
                ("Current roster", "ROSTER", "ROSTER", ("roster",)),
            ],
        )
    if topic == "RESPONDER":
        return _screen(
            ctx,
            "responder",
            "RESPONDER TOOLS",
            [
                ("Named welfare roster", "ROSTER?", "ROSTER?", ("roster",)),
                ("Acknowledge incident", "MENU ACK", "ACK", ("acknowledge",)),
                ("Broadcast incident alert", "MENU ALERT", "ALERT", ("alert",)),
                ("Open welfare event", "MENU EVENTOPEN", "EVENT", ("open event",)),
                ("Close welfare event", "MENU EVENTCLOSE", "EVENT", ("close event",)),
            ],
        )
    if topic in {"ACCOUNT", "IDENTITY", "ME"}:
        return _screen(
            ctx,
            "account",
            "MY OUTPOST",
            [
                ("My identity", "WHOAMI", "WHOAMI", ("identity",)),
                ("Set my name", "MENU NAME", "NAME", ("name",)),
                ("About this node", "ABOUT", "ABOUT", ("about",)),
                ("Test connection", "PING", "PING", ("ping", "test")),
                ("Privacy", "HELP PRIVACY", "HELP", ("privacy",)),
                ("Command shortcuts", "MENU SHORTCUTS", "HELP", ("commands",)),
            ],
        )
    if topic in {"ASK", "AI"}:
        return Response(
            ResponseKind.DETAIL,
            [Line("Ask about local boards, incidents, weather, or Outpost help.")],
            screen=TuiScreen(
                "ask",
                "ASK OUTPOST",
                choices=_choices(
                    ctx,
                    [
                        ("Summarize current item", "SUM", "SUM", ("summarize",)),
                        ("Translate text", "MENU TRANSLATE", "TR", ("translate",)),
                    ],
                ),
                input_command="ASK",
                input_prompt="Or send your question",
            ),
        )
    prompts = {
        "REPORT": (
            "REPORT A PROBLEM",
            "Creates a public community incident; this is not 911.",
            "Describe what happened and where",
            "REPORT",
        ),
        "CONFIRM": (
            "CONFIRM INCIDENT",
            "Adds your independent confirmation.",
            "Send the incident number",
            "CONFIRM",
        ),
        "DISPUTE": (
            "DISPUTE INCIDENT",
            "Flags an incident for concern or correction.",
            "Send incident number, then optional note",
            "DISPUTE",
        ),
        "HELPME": (
            "I NEED HELP",
            "Records need-help and notifies responders; this is not 911.",
            "Describe what you need and where",
            "HELPME",
        ),
        "ACK": (
            "ACKNOWLEDGE",
            "Records that you saw an active incident.",
            "Send incident number, then optional note",
            "ACK",
        ),
        "ALERT": (
            "BROADCAST ALERT",
            "Responder-only alert tied to an incident.",
            "Send severity, incident number, then headline",
            "ALERT",
        ),
        "EVENTOPEN": (
            "OPEN WELFARE EVENT",
            "Audience can be all, responders, or subscribed.",
            "Send audience, then event name",
            "EVENT OPEN",
        ),
        "SEARCH": (
            "SEARCH BOARDS",
            "Searches visible community posts.",
            "Send words to search for",
            "SEARCH",
        ),
        "POST": (
            "CREATE POST",
            "Creates a public thread on a community board.",
            "Send board name, then your message",
            "POST",
        ),
        "SUBSCRIBE": (
            "SUBSCRIBE",
            "Cadence may be on_request, daily, or immediate.",
            "Send board name, then optional cadence",
            "SUB",
        ),
        "UNSUBSCRIBE": (
            "UNSUBSCRIBE",
            "Stops the selected board subscription.",
            "Send the board name",
            "UNSUB",
        ),
        "RMPOST": (
            "REMOVE MY POST",
            "Only your eligible recent post can be removed.",
            "Send board#thread.post reference",
            "RMPOST",
        ),
        "SEND": (
            "SEND MAIL",
            "Stored mail is readable by this node's operator.",
            "Send the recipient handle",
            "MENU SENDTO",
        ),
        "FIND": (
            "FIND MEMBER",
            "Only positions the member chose to share are returned.",
            "Send the member handle",
            "POS",
        ),
        "DIST": (
            "WAYPOINT DISTANCE",
            "Returns range and bearing from this Outpost.",
            "Send the waypoint name",
            "DIST",
        ),
        "WPADD": (
            "SAVE WAYPOINT",
            "Saves a public waypoint from your latest shared GPS position.",
            "Send a name for the waypoint",
            "WP ADD",
        ),
        "NAME": (
            "SET MY NAME",
            "Use 2-12 letters, numbers, underscore, or dash.",
            "Send the handle you want",
            "NAME",
        ),
        "TRANSLATE": (
            "TRANSLATE",
            "Uses the local guarded assistant.",
            "Send language, then text",
            "TR",
        ),
    }
    if topic == "POSITION":
        return _screen(
            ctx,
            "position-sharing",
            "POSITION SHARING",
            [
                ("Full precision", "POS SHARE full", "POS", ("full",)),
                ("Coarse area only", "POS SHARE coarse", "POS", ("coarse",)),
                ("Off", "POS SHARE off", "POS", ("off",)),
            ],
        )
    if topic == "SENDTO":
        recipient = topic_args.strip().removeprefix("@").split(maxsplit=1)[0]
        if not recipient:
            return _menu_screen(ctx, "SEND")
        return _input_screen(
            "mail-compose",
            f"MESSAGE TO @{recipient[:12]}",
            "Stored mail is readable by this node's operator.",
            "Send your message",
            f"SEND {recipient}",
        )
    if topic == "POSTTO":
        board = topic_args.strip().split(maxsplit=1)[0]
        if not board:
            return _menu_screen(ctx, "POST")
        return _input_screen(
            "post-compose",
            f"POST TO {board[:18].upper()}",
            "Creates a public community thread.",
            "Send your post",
            f"POST {board}",
        )
    if topic == "DELMAIL":
        reference = topic_args.strip().split(maxsplit=1)[0]
        if not reference.isdigit():
            return _menu_screen(ctx, "MAIL")
        return _screen(
            ctx,
            "delete-mail",
            "DELETE MAIL?",
            [
                ("Delete permanently", f"DELMAIL {reference}", "DELMAIL", ("delete",)),
                ("Keep message", "MAIL", "MAIL", ("keep", "cancel")),
            ],
        )
    if topic == "ALERT":
        return _screen(
            ctx,
            "alert-severity",
            "ALERT SEVERITY",
            [
                ("Caution", "MENU ALERTSEV caution", "ALERT", ()),
                ("Urgent", "MENU ALERTSEV urgent", "ALERT", ()),
                ("Critical", "MENU ALERTSEV critical", "ALERT", ()),
            ],
        )
    if topic == "ALERTSEV":
        severity = topic_args.strip().lower()
        if severity not in {"caution", "urgent", "critical"}:
            return _menu_screen(ctx, "ALERT")
        return _input_screen(
            "alert-incident",
            f"{severity.upper()} ALERT",
            "The alert must reference an active incident.",
            "Send the incident number",
            f"MENU ALERTINC {severity}",
        )
    if topic == "ALERTINC":
        severity, _, reference = topic_args.strip().partition(" ")
        if severity not in {"caution", "urgent", "critical"} or not reference.isdigit():
            return _menu_screen(ctx, "ALERT")
        return _input_screen(
            "alert-headline",
            f"{severity.upper()} / INC {reference}",
            "This will be broadcast to the configured audience.",
            "Send the alert headline",
            f"ALERT {severity} {reference}",
        )
    if topic == "EVENTOPEN":
        return _screen(
            ctx,
            "event-audience",
            "EVENT AUDIENCE",
            [
                ("Everyone", "MENU EVENTNAME all", "EVENT", ("all",)),
                ("Responders", "MENU EVENTNAME responders", "EVENT", ()),
                ("Subscribers", "MENU EVENTNAME subscribed", "EVENT", ()),
            ],
        )
    if topic == "EVENTNAME":
        audience = topic_args.strip().lower()
        if audience not in {"all", "responders", "subscribed"}:
            return _menu_screen(ctx, "EVENTOPEN")
        return _input_screen(
            "event-name",
            "OPEN WELFARE EVENT",
            f"Audience: {audience}.",
            "Send the event name",
            f"EVENT OPEN {audience}",
        )
    if topic == "EVENTCLOSE":
        return _screen(
            ctx,
            "event-close",
            "CLOSE EVENT?",
            [
                ("Close current event", "EVENT CLOSE", "EVENT", ("close",)),
                ("Keep it open", "MENU RESPONDER", "EVENT", ("keep", "cancel")),
            ],
        )
    if topic == "SHORTCUTS":
        groups = [
            ("WX", (("WX", "WX"), ("FC", "FC"), ("WARN", "WARN"))),
            (
                "Safety",
                (("INCS", "INCIDENTS"), ("REPORT", "REPORT"), ("OK", "OK"), ("HELPME", "HELPME")),
            ),
            ("Boards", (("BOARDS", "BOARDS"), ("NEW", "NEW"), ("POST", "POST"))),
            ("Mail", (("MAIL", "MAIL"), ("SEND", "SEND"))),
            ("Places", (("WHO", "WHO"), ("POS", "POS"), ("WPS", "WPS"))),
            ("AI", (("ASK", "ASK"), ("SUM", "SUM"), ("TR", "TR"))),
        ]
        lines = [
            Line(
                f"{label}: "
                + " ".join(display for display, command in names if _available(ctx, command))
            )
            for label, names in groups
            if any(_available(ctx, command) for _, command in names)
        ]
        lines.append(Line("Format: HELP <command>"))
        return Response(
            ResponseKind.DETAIL,
            lines,
            screen=TuiScreen("shortcuts", "COMMAND SHORTCUTS"),
        )
    prompt = prompts.get(topic)
    if prompt:
        title, description, input_prompt, command = prompt
        if _available(ctx, command):
            command = f"{command} {topic_args}".strip()
            return _input_screen(topic.lower(), title, description, input_prompt, command)
    return _home_screen(ctx)


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
    groups = {
        "BBS": {"bbs"},
        "MAIL": {"mail"},
        "IDENTITY": {"identity"},
        "RADIO": {"core", "directory"},
        "OPERATOR": {"operator"},
        "WATCH": {"watch"},
        "ENV": {"env"},
        "AI": {"ai"},
    }

    def group_commands(modules: set[str]) -> list[str]:
        return [
            spec.name
            for spec in ctx.registry.commands()
            if spec.module in modules
            and ctx.member.trust != "blocked"
            and TrustLevel.parse(ctx.member.trust) >= spec.min_trust
            and _available(ctx, spec.name)
        ]

    topic = ctx.args.strip().upper()
    if topic:
        if topic in groups and ctx.message.is_direct:
            return _menu_screen(ctx, topic)
        if topic in {"MENU", "HOME", "MAIN"} and ctx.message.is_direct:
            return _home_screen(ctx)
        if topic in {"ALL", "COMMANDS", "SHORTCUTS"} and ctx.message.is_direct:
            return _menu_screen(ctx, "SHORTCUTS")
        spec = ctx.registry.resolve(topic)
        if spec:
            if not _available(ctx, spec.name):
                decision = channel_decision(
                    spec,
                    direct=ctx.message.is_direct,
                    policy=ctx.channel_policy,
                )
                return _detail(decision.message or f"{spec.name} is unavailable to this account.")
            return _detail(spec.help_short)
        if topic == "PRIVACY":
            return _detail(
                "Mail is private from other members, not the node operator. "
                "The operator can view stored plaintext; every dashboard view is audited."
            )
        modules = groups.get(topic)
        if modules:
            commands = group_commands(modules)
            if not commands:
                return _detail(f"{topic} is not enabled on this Outpost.")
            suffix = " Mail is operator-readable; HELP PRIVACY." if topic == "MAIL" else ""
            return Response(
                ResponseKind.DETAIL,
                [Line(f"{topic}: {' · '.join(commands)} · HELP <command>")]
                + ([Line(suffix.strip())] if suffix else []),
            )
        topics = [name for name, modules in groups.items() if group_commands(modules)]
        return _detail(f"Help topics: {' · '.join(topics)} · PRIVACY")
    if ctx.message.is_direct:
        return _home_screen(ctx)
    shortcuts = _broadcast_shortcuts(ctx)
    return _detail(f"DM this node for the menu. Here: {shortcuts}")


async def menu(ctx: CommandContext) -> Response:
    if not ctx.message.is_direct:
        return _detail(f"Menu is DM only. Here: {_broadcast_shortcuts(ctx)}")
    return _menu_screen(ctx, ctx.args)


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
    if ctx.message.is_direct:
        return _home_screen(ctx)
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
        mutates=False,
    )
    return [
        CommandSpec("PING", (), help_short="PING · test node reachability", handler=ping, **base),
        CommandSpec("ABOUT", (), help_short="ABOUT · node and operator", handler=about, **base),
        CommandSpec(
            "HELP", ("?", "H"), help_short="HELP [cmd] · command help", handler=help_command, **base
        ),
        CommandSpec(
            "MENU",
            ("COMMANDS",),
            help_short="MENU · guided Outpost interface",
            handler=menu,
            **base,
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
