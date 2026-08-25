from datetime import UTC, datetime

import pytest

from outpost.clock import VirtualClock
from outpost.store import Database
from outpost.store.message_log import MessageLogRepo
from outpost.transport.models import InboundMessage


@pytest.mark.asyncio
async def test_inbound_and_outbound_messages_are_durable(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    repo = MessageLogRepo(database, VirtualClock())
    inbound = InboundMessage(
        7,
        "!12345678",
        "!699c2f30",
        0,
        1,
        True,
        "PING",
        None,
        datetime.now(UTC),
    )
    await repo.record_inbound(inbound)
    await repo.record_outbound(
        peer_mesh_id="!12345678",
        channel=0,
        portnum=1,
        packet_id=8,
        text="pong",
        byte_len=4,
        toa_ms=400,
        airtime_class="reply",
        outcome="pending",
        is_direct=True,
    )
    entries = await repo.recent()
    assert [entry.direction for entry in entries] == ["out", "in"]
    assert entries[0].text == "pong"
    assert await repo.resolve_ack(8, "acked") is True
    assert (await repo.recent())[0].outcome == "acked"
    assert await repo.resolve_ack(999, "acked") is False
    await database.close()
