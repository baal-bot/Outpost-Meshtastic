from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from outpost.clock import VirtualClock
from outpost.config import RadioPowerConfig
from outpost.store import Database
from outpost.store.message_log import MessageLogRepo
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.models import LocalTelemetry, Severity, TrafficClass
from outpost.transport.simulated import SimulatedRadioLink
from tests.support.application import production_governor

pytestmark = pytest.mark.production_wiring


@pytest.fixture
async def durable_queue(
    tmp_path,
) -> AsyncIterator[tuple[Database, AirtimeGovernor, VirtualClock, SimulatedRadioLink]]:
    database = Database(tmp_path / "queue.db")
    await database.open()
    clock = VirtualClock(epoch=datetime(2026, 1, 1, 12, tzinfo=UTC))
    radio = SimulatedRadioLink(clock)
    await radio.connect()
    governor = production_governor(database, clock, link=radio)
    try:
        yield database, governor, clock, radio
    finally:
        await database.close()


def exhaust_class(governor: AirtimeGovernor, traffic_class: TrafficClass) -> float:
    share = 3_600 * governor.budget_percent / 100 * governor.config.class_shares[traffic_class]
    governor.history.append((governor.clock.monotonic(), share, traffic_class, Severity.INFO))
    if traffic_class != TrafficClass.ALERT:
        while governor._rr[0] != traffic_class:
            governor._rr.rotate(-1)
    return share


@pytest.mark.parametrize(
    ("blocked_class", "eligible_class"),
    [(first, second) for first in TrafficClass for second in TrafficClass if first != second],
)
async def test_exhausted_class_does_not_starve_another_eligible_class(
    durable_queue, blocked_class: TrafficClass, eligible_class: TrafficClass
) -> None:
    database, governor, _, radio = durable_queue
    share = exhaust_class(governor, blocked_class)
    blocked = OutboundItem("blocked", "^all", 0, blocked_class, Severity.URGENT)
    eligible = OutboundItem("eligible", "^all", 0, eligible_class, Severity.URGENT)
    assert await governor.admit(blocked) is not None
    assert await governor.admit(eligible) is not None

    assert await governor.tick() is eligible
    assert [item.text for item in radio.sent] == ["eligible"]
    assert governor.queued_items() == [blocked]
    assert governor.class_airtime(blocked_class) == share
    assert governor.used_airtime <= 3_600 * governor.budget_percent / 100
    rows = await database.read("SELECT state,attempts FROM outbound_work ORDER BY id")
    assert [tuple(row) for row in rows] == [("pending", 0), ("sent", 1)]
    assert await governor.tick() is None  # Selection still honors pacing.


@pytest.mark.parametrize("traffic_class", list(TrafficClass))
async def test_smaller_eligible_item_can_pass_without_reordering_deferred_items(
    durable_queue, traffic_class: TrafficClass
) -> None:
    _, governor, clock, _ = durable_queue
    large = [
        OutboundItem(f"{i}" * 200, "^all", 0, traffic_class, Severity.URGENT, priority=99)
        for i in (1, 2)
    ]
    small = OutboundItem("short", "^all", 0, traffic_class, Severity.INFO)
    if traffic_class == TrafficClass.FEDERATION:
        for item in [*large, small]:
            item.binary_payload = item.text.encode()
            item.portnum = 260
    small_cost = governor.estimate_toa(small.payload_size, portnum=small.portnum or 1)
    large_cost = governor.estimate_toa(large[0].payload_size, portnum=large[0].portnum or 1)
    assert small_cost < large_cost
    share = 3_600 * governor.budget_percent / 100 * governor.config.class_shares[traffic_class]
    governor.history.append(
        (
            clock.monotonic() - 3_590,
            share - (small_cost + large_cost) / 2,
            traffic_class,
            Severity.INFO,
        )
    )
    for item in [*large, small]:
        assert await governor.admit(item) is not None
    assert await governor.tick() is small
    assert list(governor.queues[traffic_class]) == large
    clock.advance(60)  # The exhausted historical allowance leaves the rolling window.
    assert await governor.tick() is large[0]
    clock.advance(60)
    assert await governor.tick() is large[1]


async def test_global_remainder_can_fit_a_smaller_item_without_crossing_ceiling(
    durable_queue,
) -> None:
    _, governor, clock, _ = durable_queue
    large = OutboundItem("x" * 200, "^all", 0, TrafficClass.ALERT, Severity.URGENT)
    small = OutboundItem("short", "^all", 0, TrafficClass.REPLY)
    remaining = (
        governor.estimate_toa(large.payload_size) + governor.estimate_toa(small.payload_size)
    ) / 2
    budget = 3_600 * governor.budget_percent / 100
    governor.history.append((clock.monotonic(), budget - remaining, TrafficClass.AI, Severity.INFO))
    for item in (large, small):
        assert await governor.admit(item) is not None
    assert await governor.tick() is small
    assert governor.used_airtime <= budget
    assert governor.queued_items() == [large]


async def test_critical_reserve_and_absolute_ceiling_still_apply(durable_queue) -> None:
    database, governor, clock, radio = durable_queue
    budget = 3_600 * governor.budget_percent / 100
    total = 3_600 * (governor.budget_percent + governor.reserve_percent) / 100
    governor.history.append((clock.monotonic(), budget, TrafficClass.REPLY, Severity.INFO))
    urgent = OutboundItem("urgent", "^all", 0, TrafficClass.ALERT, Severity.URGENT)
    critical = OutboundItem("critical", "^all", 0, TrafficClass.ALERT, Severity.CRITICAL)
    reply = OutboundItem("reply", "^all", 0, TrafficClass.REPLY)
    for item in (urgent, reply, critical):
        assert await governor.admit(item) is not None
    assert await governor.tick() is critical
    assert budget < governor.used_airtime <= total
    clock.advance(60)
    assert await governor.tick() is None
    large = OutboundItem("x" * 200, "^all", 0, TrafficClass.ALERT, Severity.CRITICAL)
    small = OutboundItem("short", "^all", 0, TrafficClass.ALERT, Severity.CRITICAL)
    remaining = (
        governor.estimate_toa(large.payload_size) + governor.estimate_toa(small.payload_size)
    ) / 2
    governor.history.append(
        (
            clock.monotonic(),
            total - governor.used_airtime - remaining,
            TrafficClass.ALERT,
            Severity.CRITICAL,
        )
    )
    for item in (large, small):
        assert await governor.admit(item) is not None
    assert await governor.tick() is small
    assert governor.used_airtime <= total
    clock.advance(60)
    assert await governor.tick() is None
    assert len(radio.sent) == 2
    governor.history.append(
        (clock.monotonic(), total - governor.used_airtime, TrafficClass.ALERT, Severity.CRITICAL)
    )
    assert await governor.tick() is None
    assert governor.metrics.hard_stops == 1
    rows = await database.read("SELECT attempts FROM outbound_work WHERE state='pending'")
    assert len(rows) == 3 and all(row["attempts"] == 0 for row in rows)


@pytest.mark.parametrize("gate", ["utilisation", "quiet_hours", "low_power"])
async def test_skipping_budget_blocked_work_does_not_bypass_other_policy_gates(
    durable_queue, gate: str
) -> None:
    _, governor, clock, radio = durable_queue
    exhaust_class(governor, TrafficClass.ALERT)
    blocked = OutboundItem("ordinary alert", "^all", 0, TrafficClass.ALERT, Severity.URGENT)
    await governor.admit(blocked)
    if gate == "utilisation":
        radio.telemetry = LocalTelemetry(channel_utilisation=governor.config.utilisation_ceiling)
        deferred_class = TrafficClass.REPLY
    elif gate == "quiet_hours":
        clock.advance(11 * 3_600)
        governor.history.clear()
        exhaust_class(governor, TrafficClass.ALERT)
        deferred_class = TrafficClass.FEDERATION
    else:
        radio.telemetry = LocalTelemetry(battery_level=5)
        governor.power_config = RadioPowerConfig(shed_discretionary=True)
        deferred_class = TrafficClass.DIGEST
    deferred = OutboundItem("policy-deferred", "^all", 0, deferred_class)
    assert await governor.admit(deferred) is not None
    assert await governor.tick() is None
    assert deferred in governor.queued_items()
    critical = OutboundItem("critical", "^all", 0, TrafficClass.ALERT, Severity.CRITICAL)
    assert await governor.admit(critical) is not None
    assert await governor.tick() is critical
    clock.advance(60)
    assert await governor.tick() is None
    assert [item.text for item in radio.sent] == ["critical"]


async def test_budget_skip_respects_held_work_and_retry_schedule(durable_queue) -> None:
    database, governor, clock, _ = durable_queue
    exhaust_class(governor, TrafficClass.ALERT)
    held = OutboundItem("held", "^all", 0, TrafficClass.ALERT, Severity.CRITICAL)
    admission = await governor.admit_many_result([held], hold=True)
    assert admission.admitted == 1
    blocked = OutboundItem("blocked", "^all", 0, TrafficClass.ALERT, Severity.URGENT)
    delayed = OutboundItem("retry later", "^all", 0, TrafficClass.REPLY, priority=99)
    eligible = OutboundItem("eligible reply", "^all", 0, TrafficClass.REPLY)
    for item in (blocked, delayed, eligible):
        assert await governor.admit(item) is not None
    delayed.next_attempt_at = clock.monotonic() + 60
    await database.write(
        "UPDATE outbound_work SET next_attempt_at=? WHERE id=?",
        (clock.now().timestamp() + 60, delayed.item_id),
    )
    assert await governor.tick() is eligible
    assert held in governor.queued_items() and delayed in governor.queued_items()
    await governor.release_work(list(admission.item_ids))
    clock.advance(60)
    assert await governor.tick() is held
    clock.advance(60)
    assert await governor.tick() is delayed
    row = (
        await database.read("SELECT attempts FROM outbound_work WHERE id=?", (blocked.item_id,))
    )[0]
    assert row["attempts"] == 0


async def test_restart_keeps_class_eligibility_and_expired_work_has_no_false_send(tmp_path) -> None:
    database = Database(tmp_path / "restart.db")
    await database.open()
    clock = VirtualClock(epoch=datetime(2026, 1, 1, 12, tzinfo=UTC))
    try:
        governor = production_governor(database, clock)
        # Synthetic retained airtime history, as recovered for pre-outbox transmissions.
        share = 3_600 * governor.budget_percent / 100 * governor.config.class_shares["alert"]
        await MessageLogRepo(database, clock).record_outbound(
            peer_mesh_id="^all",
            channel=0,
            portnum=1,
            packet_id=None,
            text="Prior alert traffic",
            byte_len=19,
            toa_ms=round(share * 1_000),
            airtime_class="alert",
            outcome="sent",
            is_direct=False,
        )
        blocked_id = await governor.admit(
            OutboundItem("urgent", "^all", 0, TrafficClass.ALERT, Severity.URGENT)
        )
        expired_id = await governor.admit(OutboundItem("expires", "^all", 0, TrafficClass.REPLY))
        clock.advance(299)
        reply_id = await governor.admit(OutboundItem("reply", "^all", 0, TrafficClass.REPLY))
    finally:
        await database.close()
    clock.advance(2)
    reopened = Database(database.path)
    await reopened.open()
    try:
        radio = SimulatedRadioLink(clock)
        await radio.connect()
        governor = production_governor(reopened, clock, link=radio)
        assert await governor.recover() == 2
        assert governor.class_airtime(TrafficClass.ALERT) == pytest.approx(share)
        sent = await governor.tick()
        assert sent is not None and sent.item_id == reply_id
        clock.advance(86_400)
        assert await governor.tick() is None
        rows = await reopened.read("SELECT id,state,attempts FROM outbound_work ORDER BY id")
        assert [tuple(row) for row in rows] == [
            (blocked_id, "expired", 0),
            (expired_id, "expired", 0),
            (reply_id, "sent", 1),
        ]
        logs = await reopened.read(
            "SELECT outbox_id,outcome FROM message_log WHERE outbox_id IS NOT NULL"
        )
        # The simulated link supplies a packet ID, not a delivery receipt. Keep
        # that distinction instead of upgrading its outcome to confirmed delivery.
        assert [tuple(row) for row in logs] == [(reply_id, "pending")]
    finally:
        await reopened.close()


async def test_blocked_alert_does_not_disrupt_round_robin_or_fifo(durable_queue) -> None:
    _, governor, clock, _ = durable_queue
    exhaust_class(governor, TrafficClass.ALERT)
    await governor.admit(OutboundItem("blocked", "^all", 0, TrafficClass.ALERT, Severity.URGENT))
    classes = [cls for cls in TrafficClass if cls != TrafficClass.ALERT]
    expected = []
    for i in range(2):
        for cls in classes:
            item = OutboundItem(f"{cls.value}-{i}", "^all", 0, cls)
            assert await governor.admit(item) is not None
            expected.append(item)
    for item in expected:
        assert await governor.tick() is item
        clock.advance(10)


@pytest.mark.parametrize("cancel_during_failure", [False, True])
async def test_invalid_candidate_is_terminal_without_blocking_valid_work(
    durable_queue, monkeypatch, cancel_during_failure: bool
) -> None:
    database, governor, _, radio = durable_queue
    poison = OutboundItem("poison", "^all", 0, TrafficClass.ALERT, Severity.CRITICAL)
    cancelled = OutboundItem("cancel me", "^all", 0, TrafficClass.ALERT, Severity.CRITICAL)
    valid = OutboundItem("valid", "^all", 0, TrafficClass.REPLY)
    for item in (poison, cancelled, valid):
        assert await governor.admit(item) is not None
    # Model an invalid post-admission payload, bypassing normal admission validation.
    poison.binary_payload = b"x" * 234
    assert governor.outbox is not None
    if cancel_during_failure:
        original = governor.outbox.fail_unstarted

        async def failure_with_cancel(item_id: int, now: float, error: str) -> None:
            await original(item_id, now, error)
            assert await governor.cancel_work(cancelled.item_id)

        monkeypatch.setattr(governor.outbox, "fail_unstarted", failure_with_cancel)
    else:
        assert await governor.cancel_work(cancelled.item_id)
    assert await governor.tick() is valid
    assert [item.text for item in radio.sent] == ["valid"]
    rows = await database.read("SELECT state,attempts FROM outbound_work ORDER BY id")
    assert [tuple(row) for row in rows] == [("failed", 0), ("cancelled", 0), ("sent", 1)]
    assert governor.metrics.dropped[(TrafficClass.ALERT, "invalid_payload")] == 1
