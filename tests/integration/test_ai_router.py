from __future__ import annotations

from datetime import UTC, datetime

import pytest

from outpost.app import OutpostApp
from outpost.config import Config
from outpost.render.renderer import render_response
from outpost.transport.models import InboundMessage


def inbound(packet_id: int, text: str, *, direct: bool, channel: int) -> InboundMessage:
    return InboundMessage(
        packet_id,
        "!00000001",
        "!699c2f30" if direct else "^all",
        channel,
        1,
        direct,
        text,
        None,
        datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_ai_channel_policy_and_direct_message_fallback(tmp_path) -> None:
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "modules": {"ai": {"enabled": True}},
            "ai": {"provider": "null"},
            "channels": {
                0: {"name": "public", "ai": False},
                2: {"name": "outpost", "ai": True},
            },
        }
    )
    app = OutpostApp(config)
    await app.database.open()
    try:
        await app.router.dispatch(inbound(1, "NAME relay", direct=True, channel=0))

        public = render_response(
            await app.router.dispatch(
                inbound(2, "!ASK how do I make a board post", direct=False, channel=0)
            )
        )
        enabled = render_response(
            await app.router.dispatch(
                inbound(3, "!ASK how do I make a board post", direct=False, channel=2)
            )
        )
        bare_channel = await app.router.dispatch(
            inbound(4, "How do I make a board post", direct=False, channel=2)
        )
        bare_dm = render_response(
            await app.router.dispatch(
                inbound(5, "How do I make a board post", direct=True, channel=0)
            )
        )
        help_ai = render_response(
            await app.router.dispatch(inbound(6, "HELP AI", direct=True, channel=0))
        )

        assert "AI is off" in public and "DM ASK" in public
        assert enabled == bare_dm == "[AI] Use POST <board> <text>."
        assert bare_channel.lines == []
        assert "ASK" in help_ai
    finally:
        await app.database.close()
