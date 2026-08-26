from datetime import UTC, datetime

import pytest

from outpost.app import OutpostApp
from outpost.config import Config
from outpost.router.models import ResponseKind
from outpost.store.members import MemberRepo
from outpost.transport.models import InboundMessage


def inbound(packet_id: int, text: str) -> InboundMessage:
    return InboundMessage(
        packet_id=packet_id,
        from_id="!00000001",
        to_id="!699c2f30",
        channel=0,
        portnum=1,
        is_direct=True,
        text=text,
        payload=None,
        rx_time=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_repeated_safety_commands_do_not_amplify_records_or_responder_airtime(
    tmp_path,
) -> None:
    path = tmp_path / "outpost.db"
    config = Config.model_validate(
        {
            "store": {"path": str(path)},
            "modules": {"watch": {"enabled": True}},
            "security": {"safety_repeat_window_seconds": 300},
        }
    )
    app = OutpostApp(config)
    await app.database.open()
    members = MemberRepo(app.database, app.clock)
    await members.resolve("!00000001")
    responder = await members.resolve("!00000002")
    await app.database.write("UPDATE member SET trust='responder' WHERE id=?", (responder.id,))

    first = await app.router.dispatch(inbound(1, "HELPME Water rising"))
    repeat = await app.router.dispatch(inbound(2, "HELPME Water rising"))
    silent_repeat = await app.router.dispatch(inbound(3, "HELPME Water rising"))
    changed = await app.router.dispatch(inbound(4, "HELPME Water at window"))

    assert first.kind == ResponseKind.ACK
    assert repeat.kind == ResponseKind.NONE
    assert silent_repeat.kind == ResponseKind.NONE
    assert changed.kind == ResponseKind.ACK
    assert len(app.governor.queued_items()) == 2
    assert (await app.database.read("SELECT COUNT(*) count FROM checkin"))[0]["count"] == 2

    forced = await app.router.dispatch(inbound(5, "REPORT! tree blocking road"))
    forced_repeat = await app.router.dispatch(inbound(6, "REPORT! tree blocking road"))
    changed_report = await app.router.dispatch(inbound(7, "REPORT! wire blocking road"))
    assert forced.kind == ResponseKind.ACK
    assert forced_repeat.kind == ResponseKind.NONE
    assert changed_report.kind == ResponseKind.ACK
    assert (await app.database.read("SELECT COUNT(*) count FROM incident"))[0]["count"] == 2

    invalid = await app.router.dispatch(inbound(8, "REPORT"))
    invalid_retry = await app.router.dispatch(inbound(9, "REPORT"))
    assert invalid.kind == ResponseKind.ERROR
    assert invalid_retry.kind == ResponseKind.ERROR
    assert "coalesced" not in invalid_retry.lines[0].text

    before_airtime = len(app.governor.queued_items())
    await app._handle_inbound_message(inbound(10, "HELPME Roof collapse"))
    admitted_airtime = len(app.governor.queued_items())
    await app._handle_inbound_message(inbound(11, "HELPME Roof collapse"))
    assert admitted_airtime == before_airtime + 2
    assert len(app.governor.queued_items()) == admitted_airtime
    await app.database.close()

    restarted = OutpostApp(config)
    await restarted.database.open()
    try:
        durable_repeat = await restarted.router.dispatch(inbound(12, "HELPME Water rising"))
        assert durable_repeat.kind == ResponseKind.NONE
        assert len(restarted.governor.queued_items()) == 0
        assert (await restarted.database.read("SELECT COUNT(*) count FROM checkin"))[0][
            "count"
        ] == 3
    finally:
        await restarted.database.close()
