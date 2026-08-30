from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from outpost.clock import VirtualClock
from outpost.situation import BriefingCapability, SituationBriefingService
from outpost.store import Database
from outpost.store.members import MemberRepo

pytestmark = pytest.mark.production_wiring

DAY = 86_400


async def delivery_service(tmp_path):  # type: ignore[no-untyped-def]
    clock = VirtualClock(epoch=datetime(2026, 8, 30, 12, tzinfo=UTC))
    database = Database(tmp_path / "outpost.db")
    await database.open()
    member = await MemberRepo(database, clock).resolve("!00000001")
    await database.write(
        "UPDATE member SET handle='alex',trust='member' WHERE id=?",
        (member.id,),
    )
    service = SituationBriefingService(
        database,
        clock,
        lambda: {"radio": "up", "queues": {}, "inbound": {}},
    )
    return database, clock, service, member.id


async def insert_outbound(
    database: Database,
    *,
    created_at: int,
    outcome: str,
    count: int,
    latency_ms: int | None = None,
    drop_reason: str | None = None,
) -> None:
    for _ in range(count):
        await database.write(
            "INSERT INTO message_log(direction,peer_mesh_id,channel,portnum,is_direct,"
            "byte_len,outcome,drop_reason,latency_ms,transport,created_at) "
            "VALUES('out','!00000002',0,1,1,10,?,?,?,'mesh',?)",
            (outcome, drop_reason, latency_ms, created_at),
        )


async def insert_snr(
    database: Database,
    *,
    created_at: int,
    snr: float,
    count: int,
    transport: str = "radio",
) -> None:
    for _ in range(count):
        await database.write(
            "INSERT INTO message_log(direction,peer_mesh_id,channel,portnum,is_direct,"
            "byte_len,outcome,rx_snr,transport,created_at) "
            "VALUES('in','!00000001',0,1,1,10,'received',?,?,?)",
            (snr, transport, created_at),
        )


def delivery_items(snapshot: dict[str, object]) -> list[dict[str, object]]:
    items = snapshot["items"]
    assert isinstance(items, list)
    return [item for item in items if item["section"] == "delivery"]


@pytest.mark.asyncio
async def test_delivery_health_is_healthy_then_cautions_on_durable_trends(tmp_path) -> None:
    database, clock, service, member_id = await delivery_service(tmp_path)
    now = int(clock.now().timestamp())
    try:
        await insert_outbound(
            database,
            created_at=now - 2 * DAY,
            outcome="acked",
            count=20,
            latency_ms=2_000,
        )
        await insert_outbound(
            database,
            created_at=now - 3_600,
            outcome="acked",
            count=5,
            latency_ms=1_000,
        )
        await insert_outbound(
            database,
            created_at=now - 3_500,
            outcome="acked",
            count=5,
            latency_ms=2_000,
        )
        await insert_snr(database, created_at=now - 2 * DAY, snr=6.0, count=6)
        await insert_snr(database, created_at=now - 3_600, snr=5.0, count=3)
        # MQTT observations do not measure this Outpost radio's receive path.
        await insert_snr(
            database,
            created_at=now - 3_600,
            snr=-100.0,
            count=3,
            transport="mqtt",
        )

        healthy = await service.snapshot(BriefingCapability.OPERATOR)
        healthy_items = delivery_items(healthy)
        channel = next(item for item in healthy_items if item["ref"] == "D0")
        receive = next(item for item in healthy_items if item["ref"] == f"R{member_id}")
        assert channel["severity"] == "info"
        assert channel["state"] == "steady"
        assert "24h 10/10 (100%)" in str(channel["detail"])
        assert "prior 14d 20/20 (100%)" in str(channel["detail"])
        assert "median ACK 1.5s" in str(channel["detail"])
        assert receive["severity"] == "info"
        assert "Receive path @alex +5.0 dB" == receive["title"]

        await database.write(
            "UPDATE message_log SET outcome='timeout',drop_reason='ack timeout',latency_ms=NULL "
            "WHERE direction='out' AND created_at>=? AND id IN ("
            "SELECT id FROM message_log WHERE direction='out' AND created_at>=? "
            "ORDER BY id LIMIT 8)",
            (now - DAY, now - DAY),
        )
        await database.write(
            "UPDATE message_log SET rx_snr=-8 WHERE direction='in' AND transport='radio' "
            "AND created_at>=?",
            (now - DAY,),
        )

        degraded = await service.snapshot(BriefingCapability.OPERATOR)
        degraded_items = delivery_items(degraded)
        channel = next(item for item in degraded_items if item["ref"] == "D0")
        receive = next(item for item in degraded_items if item["ref"] == f"R{member_id}")
        assert (channel["severity"], channel["state"], channel["hazard"]) == (
            "caution",
            "degrading",
            True,
        )
        assert "24h 2/10 (20%)" in str(channel["detail"])
        assert "leading failure ack timeout" in str(channel["detail"])
        assert (receive["severity"], receive["state"], receive["hazard"]) == (
            "caution",
            "degrading",
            True,
        )
        assert "trend -14.0 dB" in str(receive["detail"])
        assert {change["ref"] for change in degraded["changes"]} >= {"D0", f"R{member_id}"}

        public = await service.snapshot(BriefingCapability.PUBLIC)
        rendered = json.dumps(public)
        assert "@alex" not in rendered and "!00000001" not in rendered
        public_receive = next(item for item in delivery_items(public) if item["ref"] == "DR")
        assert public_receive["title"] == "Member receive paths declining"
        assert public_receive["severity"] == "caution"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_delivery_health_reports_insufficient_history_without_zero_rate(tmp_path) -> None:
    database, clock, service, _member_id = await delivery_service(tmp_path)
    try:
        snapshot = await service.snapshot(BriefingCapability.OPERATOR)
        section = next(section for section in snapshot["sections"] if section["id"] == "delivery")
        assert section["max_items"] == 12
        assert section["stale_after_seconds"] == DAY
        assert len(section["items"]) == 2
        assert all(item["state"] == "insufficient" for item in section["items"])
        rendered = json.dumps(section)
        assert "insufficient history" in rendered
        assert "0%" not in rendered

        await insert_outbound(
            database,
            created_at=int(clock.now().timestamp()) - 3_600,
            outcome="acked",
            count=1,
        )
        sparse = await service.snapshot(BriefingCapability.OPERATOR)
        channel = next(item for item in delivery_items(sparse) if item["ref"] == "D0")
        assert "24h 1 terminal outcomes (need 5)" in str(channel["detail"])
        assert "prior 14d 0 (need 20)" in str(channel["detail"])
        assert channel["uncertainty"] == "insufficient history"
    finally:
        await database.close()
