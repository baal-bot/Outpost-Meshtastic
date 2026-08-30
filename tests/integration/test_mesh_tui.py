from datetime import UTC, datetime

import pytest

from outpost.app import OutpostApp
from outpost.config import Config
from outpost.render import render_response
from outpost.router.models import (
    CommandContext,
    CommandSpec,
    Line,
    Response,
    ResponseKind,
    TrustLevel,
)
from outpost.transport.chunker import chunk_text
from outpost.transport.models import InboundMessage, TrafficClass


def inbound(packet_id: int, sender: str, text: str, *, direct: bool = True) -> InboundMessage:
    return InboundMessage(
        packet_id,
        sender,
        "!699c2f30",
        0,
        1,
        direct,
        text,
        None,
        datetime.now(UTC),
    )


def full_config(path) -> Config:
    return Config.model_validate(
        {
            "store": {"path": str(path)},
            "modules": {
                "bbs": {"enabled": True},
                "ai": {"enabled": True},
                "watch": {"enabled": True},
                "env": {"enabled": True},
            },
            "env": {"user_agent": "Outpost TUI tests (operator: test@example.org)"},
            "node": {"location": {"lat": 40.4406, "lon": -79.9959}},
        }
    )


async def send(app: OutpostApp, packet: int, sender: str, text: str) -> str:
    return render_response(await app.router.dispatch(inbound(packet, sender, text)))


@pytest.mark.asyncio
async def test_home_is_capability_and_trust_aware_and_fits_one_packet(tmp_path) -> None:
    app = OutpostApp(full_config(tmp_path / "outpost.db"))
    await app.database.open()
    try:
        guest = await send(app, 1, "!00000001", "?")
        assert guest.startswith("OUTPOST / HOME\n1 Weather & alerts")
        assert "Ask Outpost" not in guest
        assert "0 Home · ? Menu" in guest
        assert chunk_text(guest) == [guest]

        # Every fixed discovery screen must fit one radio payload.
        for packet, selection in enumerate(("1", "2", "3", "4", "5", "7"), 20):
            screen_user = f"!{packet:08x}"
            await send(app, packet * 2, screen_user, "?")
            screen = await send(app, packet * 2 + 1, screen_user, selection)
            assert chunk_text(screen) == [screen]

        await send(app, 40, "!00000040", "?")
        await send(app, 41, "!00000040", "3")
        board_directory = await send(app, 42, "!00000040", "1")
        assert chunk_text(board_directory) == [board_directory]

        assert "@dana" in await send(app, 2, "!00000001", "NAME dana")
        member = await send(app, 3, "!00000001", "?")
        assert "2 Situation brief" in member
        assert "7 Ask Outpost" in member
        assert chunk_text(member) == [member]

        invalid = await send(app, 4, "!00000001", "9")
        assert invalid == "Reply 1-8, or 0 for Home."
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_numeric_bbs_journey_and_bare_text_composition(tmp_path) -> None:
    app = OutpostApp(full_config(tmp_path / "outpost.db"))
    await app.database.open()
    try:
        sender = "!00000001"
        assert "@dana" in await send(app, 1, sender, "NAME dana")
        await send(app, 2, sender, "?")
        community = await send(app, 3, sender, "4")
        assert community.startswith("OUTPOST / COMMUNITY BOARDS")
        boards = await send(app, 4, sender, "1")
        assert "Roads & Access" in boards
        roads = await send(app, 5, sender, "2")
        assert "OUTPOST / BOARD: ROADS" in roads
        assert "Send text to create the first post" in roads

        created = await send(app, 6, sender, "Bridge open one lane near Mill Road.")
        assert "POST SENT" in created and "✓ roads#" in created
        listing = await send(app, 7, sender, "1")
        assert "Bridge open one lane" in listing
        opened = await send(app, 8, sender, "1")
        assert "OUTPOST / THREAD: roads#" in opened
        assert "Send text to reply" in opened
        replied = await send(app, 9, sender, "Confirmed at noon.")
        assert "REPLY SENT" in replied and replied.rstrip().endswith("0 Home · ? Menu")

        posts = await app.database.read("SELECT body FROM post ORDER BY id")
        assert [row["body"] for row in posts] == [
            "Bridge open one lane near Mill Road.",
            "Confirmed at noon.",
        ]
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_guided_mail_and_incident_inputs_need_no_command_syntax(tmp_path) -> None:
    app = OutpostApp(full_config(tmp_path / "outpost.db"))
    await app.database.open()
    try:
        assert "@dana" in await send(app, 1, "!00000001", "NAME dana")
        assert "@ray" in await send(app, 2, "!00000002", "NAME ray")

        await send(app, 3, "!00000002", "?")
        await send(app, 4, "!00000002", "5")
        compose = await send(app, 5, "!00000002", "2")
        assert "OUTPOST / SEND MAIL TO" in compose
        message = await send(app, 6, "!00000002", "1")
        assert "OUTPOST / MESSAGE TO @dana" in message
        sent = await send(app, 7, "!00000002", "Check the north entrance.")
        assert "MAIL SENT" in sent and "Sent to @dana" in sent

        await send(app, 12, "!00000001", "?")
        await send(app, 13, "!00000001", "5")
        inbox = await send(app, 14, "!00000001", "1")
        assert "OUTPOST / MAIL INBOX" in inbox
        opened = await send(app, 15, "!00000001", "1")
        assert "Check the north entrance." in opened
        confirm_delete = await send(app, 16, "!00000001", "1")
        assert confirm_delete.startswith("OUTPOST / DELETE MAIL?")
        kept = await send(app, 17, "!00000001", "2")
        assert "OUTPOST / MAIL INBOX" in kept

        await send(app, 18, "!00000003", "?")
        await send(app, 19, "!00000003", "2")
        report = await send(app, 20, "!00000003", "2")
        assert "Describe what happened and where" in report
        filed = await send(app, 21, "!00000003", "Tree blocks Oak Street near the school.")
        assert "INCIDENT FILED" in filed and "✓ INC" in filed
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_typo_intent_channel_boundary_and_cold_number_recovery(tmp_path) -> None:
    app = OutpostApp(
        Config.model_validate(
            {
                "store": {"path": str(tmp_path / "outpost.db")},
                "channels": {0: {"name": "public", "bbs": "full"}},
            }
        )
    )
    await app.database.open()
    try:
        fuzzy = await send(app, 1, "!00000001", "bords")
        assert fuzzy.startswith("OUTPOST / COMMUNITY BOARDS")

        menu = await send(app, 2, "!00000002", "what can you do?")
        assert menu.startswith("OUTPOST / HOME")

        cold_number = await send(app, 3, "!00000003", "1")
        assert cold_number == "No active menu. Send ? to start again."

        channel = render_response(
            await app.router.dispatch(inbound(4, "!00000004", "!?", direct=False))
        )
        assert channel.startswith("DM this node for the menu.")
        assert "OUTPOST /" not in channel
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_disabled_report_is_reserved_and_cannot_fuzzy_delete_a_post(tmp_path) -> None:
    app = OutpostApp(Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}}))
    await app.database.open()
    try:
        sender = "!00000001"
        assert "@dana" in await send(app, 1, sender, "NAME dana")
        assert "✓ gen#1" in await send(app, 2, sender, "POST gen Tree blocks road")

        rejected = await send(app, 3, sender, "REPORT gen#1.1")

        assert rejected == "REPORT unavailable · Watch is disabled."
        rows = await app.database.read("SELECT body,hidden FROM post WHERE thread_id=1")
        assert [dict(row) for row in rows] == [{"body": "Tree blocks road", "hidden": 0}]
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_mutating_fuzzy_match_requires_numbered_confirmation(tmp_path) -> None:
    app = OutpostApp(Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}}))
    await app.database.open()
    try:
        sender = "!00000001"
        await send(app, 1, sender, "NAME dana")
        await send(app, 2, sender, "POST gen Temporary post")

        prompt = await send(app, 3, sender, "RMPOSTX gen#1.1")

        assert prompt.startswith("OUTPOST / CONFIRM COMMAND\nNothing was run.\n1 RMPOST")
        assert (
            int((await app.database.read("SELECT hidden FROM post WHERE id=1"))[0]["hidden"]) == 0
        )

        channel_prompt = render_response(
            await app.router.dispatch(inbound(4, "!00000002", "!RMPOSTX gen#1.1", direct=False))
        )
        assert channel_prompt == "Not run · send exact RMPOST, or DM ? for help."
        assert (
            int((await app.database.read("SELECT hidden FROM post WHERE id=1"))[0]["hidden"]) == 0
        )

        confirmed = await send(app, 5, sender, "1")
        assert "✓ Removed." in confirmed
        assert (
            int((await app.database.read("SELECT hidden FROM post WHERE id=1"))[0]["hidden"]) == 1
        )
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_ambiguous_fuzzy_match_offers_choices_without_executing(tmp_path) -> None:
    app = OutpostApp(Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}}))
    await app.database.open()
    calls: list[tuple[str, str]] = []

    def command(name: str) -> CommandSpec:
        async def handler(ctx: CommandContext) -> Response:
            calls.append((name, ctx.args))
            return Response(ResponseKind.ACK, [Line(f"ran {name}")])

        return CommandSpec(
            name,
            (),
            module="test",
            min_trust=TrustLevel.GUEST,
            airtime_class=TrafficClass.REPLY,
            max_parts=1,
            rate_key="commands",
            help_short=name,
            mutates=False,
            handler=handler,
        )

    try:
        app.router.registry.register(command("CART"))
        app.router.registry.register(command("CAST"))

        prompt = await send(app, 1, "!00000001", "CAT payload")

        assert prompt.startswith("OUTPOST / CHOOSE COMMAND\nNothing was run.\n1 CART\n2 CAST")
        assert calls == []
        assert "ran CART" in await send(app, 2, "!00000001", "1")
        assert calls == [("CART", "payload")]
    finally:
        await app.database.close()


def test_mutating_command_typos_never_execute_across_trust_levels(tmp_path) -> None:
    config = full_config(tmp_path / "outpost.db")
    app = OutpostApp(config)
    mutations = {spec.name for spec in app.router.registry.known_commands() if spec.mutates}
    assert mutations == {
        "ACK",
        "ALERT",
        "CONFIRM",
        "DELMAIL",
        "DISPUTE",
        "DRILLS",
        "EVENT",
        "FORGETPOS",
        "HELPME",
        "NAME",
        "NEW",
        "OK",
        "OP",
        "OPS",
        "POS",
        "POST",
        "READMAIL",
        "REPLY",
        "REPLYMAIL",
        "REPORT",
        "REPORT!",
        "REMOVEME",
        "RMPOST",
        "SEND",
        "SUB",
        "UNSUB",
        "WAYPOINT",
    }

    for spec in app.router.registry.known_commands():
        if not spec.mutates:
            continue
        for name in (spec.name, *spec.aliases):
            if len(name) < 3:
                continue
            typo = f"{name}X"
            assert app.router.registry.known(typo) is None
            for trust in TrustLevel:
                resolution = app.router.intents.resolve(
                    f"{typo} preserved arguments", trust.name.lower(), app.router.registry
                )
                if trust >= spec.min_trust:
                    assert resolution.mode in {"mutation_confirmation", "ambiguous"}
                    assert spec.name in resolution.candidates
                else:
                    assert spec.name not in resolution.candidates
                assert resolution.mode != "fuzzy"


@pytest.mark.asyncio
async def test_global_commands_interrupt_flows_and_home_recovers_after_lost_state(tmp_path) -> None:
    app = OutpostApp(full_config(tmp_path / "outpost.db"))
    await app.database.open()
    try:
        sender = "!00000001"
        assert "@dana" in await send(app, 1, sender, "NAME dana")

        await send(app, 2, sender, "?")
        await send(app, 3, sender, "4")
        choose_board = await send(app, 4, sender, "4")
        assert choose_board.startswith("OUTPOST / CHOOSE A BOARD")

        # A global command wins over the pending compose action and cancels it.
        interrupted = await send(app, 5, sender, "PING")
        assert interrupted.startswith("OUTPOST / PING")
        assert await send(app, 6, sender, "1") == "No active menu. Send ? to start again."

        # HOME and ? reconstruct navigation without relying on a received prior screen.
        home = await send(app, 7, sender, "HOME")
        assert home.startswith("OUTPOST / HOME")
        weather = await send(app, 8, sender, "1")
        assert weather.startswith("OUTPOST / WEATHER & ALERTS")
        reset = await send(app, 9, sender, "?")
        assert reset.startswith("OUTPOST / HOME")
    finally:
        await app.database.close()
