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
    clock = VirtualClock()
    repo = MessageLogRepo(database, clock)
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
        via_mqtt=True,
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
    clock.advance(2)
    assert await repo.resolve_ack(8, "acked") is True
    assert (await repo.recent())[0].outcome == "acked"
    latency = (await database.read("SELECT latency_ms FROM message_log WHERE packet_id=8"))[0]
    assert latency["latency_ms"] == 2_000
    clock.advance(5)
    assert await repo.resolve_ack(8, "acked") is True
    latency = (await database.read("SELECT latency_ms FROM message_log WHERE packet_id=8"))[0]
    assert latency["latency_ms"] == 2_000
    assert await repo.resolve_ack(999, "acked") is False
    transports = await database.read("SELECT direction,transport FROM message_log ORDER BY id")
    assert [dict(row) for row in transports] == [
        {"direction": "in", "transport": "mqtt"},
        {"direction": "out", "transport": "mesh"},
    ]
    await database.close()
