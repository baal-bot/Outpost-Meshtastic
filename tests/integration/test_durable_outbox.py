from __future__ import annotations

import pytest

from outpost.clock import VirtualClock
from outpost.config import AirtimeConfig
from outpost.store import Database
from outpost.store.message_log import MessageLogRepo
from outpost.store.outbox import OutboxStore
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.models import Severity, TrafficClass
from outpost.transport.simulated import SimulatedRadioLink


async def durable_governor(
    path: object,
    clock: VirtualClock,
    link: SimulatedRadioLink | None = None,
) -> tuple[Database, AirtimeGovernor, SimulatedRadioLink]:
    database = Database(path)  # type: ignore[arg-type]
    await database.open()
    radio = link or SimulatedRadioLink()
    governor = AirtimeGovernor(
        radio,
        AirtimeConfig(min_gap_s=0),
        clock,
        outbox=OutboxStore(database),
    )
    return database, governor, radio


@pytest.mark.asyncio
async def test_admission_is_durable_and_safety_recovers_first(tmp_path) -> None:
    path = tmp_path / "outpost.db"
    clock = VirtualClock()
    first_db, first, _ = await durable_governor(path, clock)
    low = OutboundItem("low priority", "!00000003", 0, TrafficClass.REPLY, priority=0)
    reply = OutboundItem("routine", "!00000001", 0, TrafficClass.REPLY, priority=99)
    alert = OutboundItem("urgent", "!00000002", 0, TrafficClass.ALERT, Severity.CRITICAL)

    low_id = await first.admit(low)
    reply_id = await first.admit(reply)
    alert_id = await first.admit(alert)

    rows = await first_db.read("SELECT id,state FROM outbound_work ORDER BY id")
    assert [(row["id"], row["state"]) for row in rows] == [
        (low_id, "pending"),
        (reply_id, "pending"),
        (alert_id, "pending"),
    ]
    await first_db.close()

    second_db, recovered, radio = await durable_governor(path, clock)
    assert await recovered.recover() == 3
    await radio.connect()
    sent = await recovered.tick()
    assert sent is not None
    assert sent.traffic_class == TrafficClass.ALERT
    assert sent.item_id == alert_id
    clock.advance(60)
    sent = await recovered.tick()
    assert sent is not None and sent.item_id == reply_id
    assert (
        await recovered.admit(
            OutboundItem("routine", "!00000001", 0, TrafficClass.REPLY, priority=99)
        )
        is None
    )
    await second_db.close()


@pytest.mark.asyncio
async def test_interrupted_pre_send_attempt_is_requeued(tmp_path) -> None:
    path = tmp_path / "outpost.db"
    clock = VirtualClock()
    database, governor, _ = await durable_governor(path, clock)
    item_id = await governor.admit(OutboundItem("pre-send", "^all", 0, TrafficClass.REPLY))
    assert item_id is not None and governor.outbox is not None
    assert await governor.outbox.start_attempt(item_id, clock.now().timestamp(), 313) is True
    await database.close()

    restarted_db, restarted, radio = await durable_governor(path, clock)
    assert await restarted.recover() == 1
    await radio.connect()
    assert await restarted.tick() is not None
    row = (
        await restarted_db.read("SELECT state,attempts FROM outbound_work WHERE id=?", (item_id,))
    )[0]
    assert (row["state"], row["attempts"]) == ("sent", 2)
    assert [
        row["state"]
        for row in await restarted_db.read(
            "SELECT state FROM outbound_attempt WHERE outbox_id=? ORDER BY attempt_no",
            (item_id,),
        )
    ] == ["uncertain", "sent"]
    assert len(radio.sent) == 1
    await restarted_db.close()


@pytest.mark.asyncio
async def test_restart_expires_old_work_and_preserves_supersession(tmp_path) -> None:
    path = tmp_path / "outpost.db"
    clock = VirtualClock()
    database, governor, _ = await durable_governor(path, clock)
    old_id = await governor.admit(
        OutboundItem(
            "repeat",
            "^all",
            0,
            TrafficClass.ALERT,
            queue_key="alert:7:repeat",
        )
    )
    replacement_id = await governor.admit(
        OutboundItem(
            "all clear",
            "^all",
            0,
            TrafficClass.ALERT,
            supersedes="alert:7:repeat",
        )
    )
    assert [item.item_id for item in governor.queued_items()] == [replacement_id]
    assert (await database.read("SELECT state FROM outbound_work WHERE id=?", (old_id,)))[0][
        "state"
    ] == "superseded"
    await database.close()

    clock.advance(86_401)
    recovered_db, recovered, _ = await durable_governor(path, clock)
    assert await recovered.recover() == 0
    assert (
        await recovered_db.read("SELECT state FROM outbound_work WHERE id=?", (replacement_id,))
    )[0]["state"] == "expired"
    await recovered_db.close()


@pytest.mark.asyncio
async def test_interrupted_post_send_logging_recovers_idempotently(tmp_path) -> None:
    path = tmp_path / "outpost.db"
    clock = VirtualClock()
    database, governor, radio = await durable_governor(path, clock)
    await radio.connect()
    item_id = await governor.admit(OutboundItem("recover me", "^all", 0, TrafficClass.REPLY))
    assert item_id is not None
    assert governor.outbox is not None
    complete = governor.outbox.complete_attempt

    async def power_cut(*args: object, **kwargs: object) -> int:
        raise RuntimeError("injected power cut")

    governor.outbox.complete_attempt = power_cut  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="power cut"):
        await governor.tick()
    assert len(radio.sent) == 1
    assert (await database.read("SELECT state FROM outbound_work WHERE id=?", (item_id,)))[0][
        "state"
    ] == "sending"
    assert (await database.read("SELECT COUNT(*) count FROM message_log"))[0]["count"] == 0
    governor.outbox.complete_attempt = complete  # type: ignore[method-assign]
    await database.close()

    restarted_db, restarted, restarted_radio = await durable_governor(path, clock)
    assert await restarted.recover() == 1
    await restarted_radio.connect()
    assert await restarted.tick() is not None
    rows = await restarted_db.read(
        "SELECT id,outbox_id,outcome FROM message_log WHERE outbox_id=?", (item_id,)
    )
    assert len(rows) == 1
    assert rows[0]["outcome"] == "pending"
    assert [
        row["state"]
        for row in await restarted_db.read(
            "SELECT state FROM outbound_attempt WHERE outbox_id=? ORDER BY attempt_no",
            (item_id,),
        )
    ] == ["uncertain", "sent"]
    assert (
        await restarted_db.read("SELECT state,attempts FROM outbound_work WHERE id=?", (item_id,))
    )[0]["state"] == "sent"
    await restarted_db.close()


@pytest.mark.asyncio
async def test_acknowledgement_is_correlated_and_not_resent_after_restart(tmp_path) -> None:
    path = tmp_path / "outpost.db"
    clock = VirtualClock()
    database, governor, radio = await durable_governor(path, clock)
    await radio.connect()
    item_id = await governor.admit(OutboundItem("direct", "!00000001", 0, TrafficClass.REPLY))
    assert await governor.tick() is not None
    packet_id = radio.sent and 1
    row = (await database.read("SELECT state,packet_id FROM outbound_work WHERE id=?", (item_id,)))[
        0
    ]
    assert (row["state"], row["packet_id"]) == ("awaiting_ack", packet_id)
    await database.close()

    restarted_db, restarted, restarted_radio = await durable_governor(path, clock)
    assert await restarted.recover() == 0
    assert restarted.used_airtime == pytest.approx(governor.used_airtime, abs=0.001)
    await restarted_radio.connect()
    assert await restarted.tick() is None
    log = MessageLogRepo(restarted_db, clock)
    assert await log.resolve_ack(packet_id, "acked") is True
    assert await log.resolve_ack(packet_id, "acked") is True
    assert (await restarted_db.read("SELECT state FROM outbound_work WHERE id=?", (item_id,)))[0][
        "state"
    ] == "acked"
    assert (await restarted_db.read("SELECT COUNT(*) count FROM message_log"))[0]["count"] == 1
    await restarted_db.close()


class FailingRadio(SimulatedRadioLink):
    async def _send_text(
        self, text: str, *, dest: str, channel: int, want_ack: bool, priority: int
    ):  # type: ignore[no-untyped-def]
        raise ConnectionError("injected radio failure")


@pytest.mark.asyncio
async def test_failed_work_is_visible_and_operator_cancellable(tmp_path) -> None:
    clock = VirtualClock()
    radio = FailingRadio()
    database, governor, _ = await durable_governor(tmp_path / "outpost.db", clock, radio)
    await radio.connect()
    item_id = await governor.admit(OutboundItem("cannot send", "!00000001", 0, TrafficClass.REPLY))
    for advance in (5, 10, 20):
        assert await governor.tick() is None
        clock.advance(advance)
    assert governor.outbox is not None
    visible = await governor.outbox.list_operator_work()
    assert visible[0]["id"] == item_id
    assert visible[0]["state"] == "failed"
    assert visible[0]["attempts"] == 3
    assert await governor.cancel_work(item_id or 0) is True
    assert (await database.read("SELECT state FROM outbound_work WHERE id=?", (item_id,)))[0][
        "state"
    ] == "cancelled"
    await database.close()
