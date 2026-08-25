from datetime import UTC, datetime

import pytest

from outpost.app import OutpostApp
from outpost.config import Config
from outpost.render.renderer import render_response
from outpost.transport.models import InboundMessage


def inbound(packet_id: int, sender: str, text: str) -> InboundMessage:
    return InboundMessage(
        packet_id,
        sender,
        "!699c2f30",
        0,
        1,
        True,
        text,
        None,
        datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_phase1_two_member_journey_from_cold_database(tmp_path) -> None:
    config = Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}})
    app = OutpostApp(config)
    await app.database.open()
    try:

        async def ask(packet: int, sender: str, text: str) -> str:
            return render_response(await app.router.dispatch(inbound(packet, sender, text)))

        assert "HELP BBS" in await ask(1, "!00000001", "HELP")
        registration = await ask(2, "!00000001", "NAME dana")
        assert "@dana" in registration and "operator-readable" in registration
        assert "@ray" in await ask(3, "!00000002", "NAME ray")
        assert "roads" in await ask(4, "!00000001", "BOARDS")
        posted = await ask(5, "!00000001", "POST roads Bridge open one lane.")
        assert posted.startswith("✓ roads#")
        assert "Bridge open" in await ask(6, "!00000002", "B roads")
        assert "Bridge open one lane" in await ask(7, "!00000002", "R 1")
        assert "✓ roads#" in await ask(8, "!00000002", "RE Confirmed at noon.")
        assert "Sent to @ray" in await ask(9, "!00000001", "SEND ray Check the north side.")
        inbox = await ask(10, "!00000002", "MAIL")
        assert "@dana" in inbox
        assert "Check the north side" in await ask(11, "!00000002", "READMAIL 1")
        assert "node operator" in await ask(12, "!00000001", "HELP PRIVACY")
        assert "Subscribed roads · daily" in await ask(13, "!00000001", "SUB roads daily")
        assert "public" in await ask(14, "!00000001", "CHANS")
    finally:
        await app.database.close()
