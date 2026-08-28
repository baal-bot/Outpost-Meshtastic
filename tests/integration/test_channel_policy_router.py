from __future__ import annotations

from datetime import UTC, datetime

import pytest

from outpost.app import OutpostApp
from outpost.config import Config
from outpost.render import render_response
from outpost.transport.models import InboundMessage


def inbound(packet: int, text: str, *, channel: int, direct: bool = False) -> InboundMessage:
    return InboundMessage(
        packet_id=packet,
        from_id="!00000001",
        to_id="!699c2f30" if direct else "^all",
        channel=channel,
        portnum=1,
        is_direct=direct,
        text=text if direct else f"!{text}",
        payload=None,
        rx_time=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_channel_policy_blocks_mutations_before_handlers_and_audits_no_content(
    tmp_path,
) -> None:
    app = OutpostApp(
        Config.model_validate(
            {
                "store": {"path": str(tmp_path / "outpost.db")},
                "modules": {"ai": {"enabled": True}, "watch": {"enabled": True}},
                "ai": {"provider": "null"},
                "watch": {"emergency_keywords_enabled": True},
                "channels": {
                    0: {
                        "name": "read-only",
                        "bbs": "read_only",
                        "accept_reports": False,
                        "alerts": False,
                        "ai": False,
                    },
                    1: {
                        "name": "full",
                        "bbs": "full",
                        "accept_reports": True,
                        "alerts": True,
                        "ai": True,
                    },
                },
            }
        )
    )
    await app.database.open()
    try:
        await app.router.dispatch(inbound(1, "NAME relay", channel=7, direct=True))

        blocked_post = render_response(
            await app.router.dispatch(inbound(2, "POST gen secret-policy-probe", channel=0))
        )
        blocked_report = render_response(
            await app.router.dispatch(inbound(3, "REPORT road secret-incident-probe", channel=0))
        )
        blocked_alerts = render_response(
            await app.router.dispatch(inbound(4, "INCIDENTS", channel=0))
        )
        blocked_ai = render_response(await app.router.dispatch(inbound(5, "ASK status", channel=0)))
        unknown_channel = render_response(
            await app.router.dispatch(inbound(6, "POST gen unknown-channel-probe", channel=7))
        )
        await app._handle_inbound_message(
            inbound(11, "REPORT emergency secret-keyword-probe", channel=0)
        )
        channel_help = render_response(await app.router.dispatch(inbound(9, "HELP BBS", channel=0)))
        channel_menu = render_response(await app.router.dispatch(inbound(10, "?", channel=0)))

        assert "read-only" in blocked_post
        assert "reports are off" in blocked_report
        assert "Alerts are off" in blocked_alerts
        assert "AI is off" in blocked_ai
        assert unknown_channel.startswith("Channel unavailable")
        assert "BOARDS" in channel_help and "POST" not in channel_help
        assert "!BOARDS" in channel_menu
        assert "!WARN" not in channel_menu and "!ASK" not in channel_menu
        assert (await app.database.read("SELECT COUNT(*) count FROM post"))[0]["count"] == 0
        assert (await app.database.read("SELECT COUNT(*) count FROM incident"))[0]["count"] == 0

        assert "✓ gen#" in render_response(
            await app.router.dispatch(inbound(7, "POST gen allowed post", channel=1))
        )
        assert "✓ INC" in render_response(
            await app.router.dispatch(inbound(8, "REPORT road allowed report", channel=1))
        )

        audits = await app.database.read(
            "SELECT target,detail,outcome FROM audit_log "
            "WHERE action='command.channel_policy_rejected' ORDER BY id"
        )
        assert len(audits) == 6
        assert {row["outcome"] for row in audits} == {"denied"}
        audit_text = " ".join(f"{row['target']} {row['detail']}" for row in audits)
        assert "secret-policy-probe" not in audit_text
        assert "secret-incident-probe" not in audit_text
        assert "secret-keyword-probe" not in audit_text
        assert "unknown-channel-probe" not in audit_text
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_direct_messages_are_not_restricted_by_broadcast_channel_policy(tmp_path) -> None:
    app = OutpostApp(
        Config.model_validate(
            {
                "store": {"path": str(tmp_path / "outpost.db")},
                "channels": {0: {"name": "off", "bbs": "none"}},
            }
        )
    )
    await app.database.open()
    try:
        await app.router.dispatch(inbound(1, "NAME relay", channel=7, direct=True))
        response = render_response(
            await app.router.dispatch(
                inbound(2, "POST gen direct message still works", channel=7, direct=True)
            )
        )

        assert "✓ gen#" in response
        assert (await app.database.read("SELECT COUNT(*) count FROM post"))[0]["count"] == 1
    finally:
        await app.database.close()
