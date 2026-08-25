from datetime import UTC, datetime

import pytest

from outpost.app import OutpostApp
from outpost.config import Config
from outpost.render.renderer import render_response
from outpost.transport.models import InboundMessage


def inbound(packet_id: int, text: str) -> InboundMessage:
    return InboundMessage(
        packet_id,
        "!00000001",
        "!699c2f30",
        0,
        1,
        True,
        text,
        None,
        datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_phase1_commands_work_from_cold_session(tmp_path) -> None:
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "modules": {"env": {"enabled": True}},
            "env": {"user_agent": "Outpost tests (operator: test@example.org)"},
        }
    )
    app = OutpostApp(config)
    await app.database.open()
    try:
        assert "roads" in render_response(await app.router.dispatch(inbound(1, "BOARDS")))
        assert "@dana" in render_response(await app.router.dispatch(inbound(2, "NAME dana")))
        posted = render_response(
            await app.router.dispatch(inbound(3, "POST roads Bridge open one lane."))
        )
        assert posted.startswith("✓ roads#")
        listing = render_response(await app.router.dispatch(inbound(4, "B roads")))
        assert "Bridge open" in listing
        opened = render_response(await app.router.dispatch(inbound(5, "R 1")))
        assert "Bridge open one lane." in opened
        replied = render_response(await app.router.dispatch(inbound(6, "RE Confirmed at 16:10.")))
        assert replied.startswith("✓ roads#") and replied.endswith(".2.")
        first_new = render_response(await app.router.dispatch(inbound(7, "NEW")))
        assert "roads 1" in first_new
        assert render_response(await app.router.dispatch(inbound(8, "NEW"))) == "Nothing new."
        assert render_response(await app.router.dispatch(inbound(9, "!WP"))) == (
            "No saved waypoints."
        )
        await app.waypoints.create("Spring", 40.12345, -79.54321, "water", "")
        waypoint = render_response(await app.router.dispatch(inbound(10, "!WP spring")))
        assert "https://maps.google.com/?q=40.12345,-79.54321" in waypoint
        assert app.router.registry.resolve("WPS").name == "WPS"
    finally:
        await app.database.close()
