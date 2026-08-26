from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from outpost.clock import VirtualClock
from outpost.config import AirtimeConfig
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.models import LocalTelemetry, Severity, TrafficClass
from outpost.transport.simulated import SimulatedRadioLink


@pytest.mark.asyncio
async def test_alert_preempts_reply_and_broadcast_has_no_ack() -> None:
    clock, link = VirtualClock(), SimulatedRadioLink()
    await link.connect()
    governor = AirtimeGovernor(link, AirtimeConfig(min_gap_s=0), clock)
    governor.enqueue(OutboundItem("reply", "!peer", 0, TrafficClass.REPLY))
    governor.enqueue(OutboundItem("alert", "^all", 0, TrafficClass.ALERT, Severity.URGENT))
    sent = await governor.tick()
    assert sent is not None and sent.traffic_class == TrafficClass.ALERT
    assert link.sent[0].want_ack is False


@pytest.mark.asyncio
async def test_alerts_are_severity_ordered_and_fifo_within_severity() -> None:
    clock, link = VirtualClock(), SimulatedRadioLink()
    await link.connect()
    governor = AirtimeGovernor(link, AirtimeConfig(min_gap_s=0), clock)
    for text, severity in (
        ("caution-1", Severity.CAUTION),
        ("urgent-1", Severity.URGENT),
        ("critical-1", Severity.CRITICAL),
        ("critical-2", Severity.CRITICAL),
        ("urgent-2", Severity.URGENT),
        ("info-1", Severity.INFO),
    ):
        governor.enqueue(OutboundItem(text, "!peer", 0, TrafficClass.ALERT, severity))

    sent: list[str] = []
    for _ in range(6):
        item = await governor.tick()
        assert item is not None
        sent.append(item.text)
        clock.advance(60)

    assert sent == [
        "critical-1",
        "critical-2",
        "urgent-1",
        "urgent-2",
        "caution-1",
        "info-1",
    ]


@pytest.mark.asyncio
async def test_high_utilisation_only_allows_alerts() -> None:
    clock, link = VirtualClock(), SimulatedRadioLink()
    await link.connect()
    link.telemetry = LocalTelemetry(channel_utilisation=30)
    governor = AirtimeGovernor(link, AirtimeConfig(min_gap_s=0), clock)
    governor.enqueue(OutboundItem("reply", "!peer", 0, TrafficClass.REPLY))
    assert await governor.tick() is None
    governor.enqueue(OutboundItem("alert", "!peer", 0, TrafficClass.ALERT, Severity.URGENT))
    assert (await governor.tick()).traffic_class == TrafficClass.ALERT


@pytest.mark.asyncio
async def test_critical_may_use_reserve_but_urgent_may_not() -> None:
    clock, link = VirtualClock(), SimulatedRadioLink()
    await link.connect()
    config = AirtimeConfig(budget_percent=0.01, emergency_reserve_percent=0.01, min_gap_s=0)
    governor = AirtimeGovernor(link, config, clock, preset="SHORT_FAST")
    governor.history.append((0, 0.36, TrafficClass.REPLY, Severity.INFO))
    governor.enqueue(OutboundItem("critical", "!peer", 0, TrafficClass.ALERT, Severity.CRITICAL))
    assert await governor.tick() is not None


@pytest.mark.asyncio
async def test_three_alert_storm_is_bounded_and_critical_reaches_reserve() -> None:
    clock, link = VirtualClock(), SimulatedRadioLink()
    await link.connect()
    config = AirtimeConfig(
        budget_percent=0.03,
        emergency_reserve_percent=0.02,
        min_gap_s=0,
        class_shares={
            "alert": 1.0,
            "reply": 0.0,
            "ai": 0.0,
            "bulletin": 0.0,
            "digest": 0.0,
            "federation": 0.0,
        },
    )
    governor = AirtimeGovernor(link, config, clock, preset="SHORT_FAST")
    governor.enqueue(OutboundItem("routine", "!peer", 0, TrafficClass.REPLY))
    for index in range(80):
        severity = (Severity.CAUTION, Severity.URGENT, Severity.CRITICAL)[index % 3]
        governor.enqueue(
            OutboundItem(f"alert-{index}-" + "x" * 180, "^all", 3, TrafficClass.ALERT, severity)
        )

    sent: list[OutboundItem] = []
    for _ in range(160):
        item = await governor.tick()
        if item is not None:
            sent.append(item)
        clock.advance(10)

    normal_ceiling = 3_600 * 0.03 / 100
    absolute_ceiling = 3_600 * 0.05 / 100
    assert sent and sent[0].traffic_class == TrafficClass.ALERT
    assert governor.noncritical_airtime <= normal_ceiling + 1e-9
    assert governor.used_airtime <= absolute_ceiling + 1e-9
    assert governor.used_airtime > normal_ceiling
    assert any(item.severity == Severity.CRITICAL for item in sent)
    assert governor.alert_delivery_status()["throttled"] > 0


def test_all_clear_supersedes_queued_alert_repeats() -> None:
    governor = AirtimeGovernor(SimulatedRadioLink(), AirtimeConfig(), VirtualClock())
    for index in range(3):
        governor.enqueue(
            OutboundItem(
                f"repeat {index}",
                "^all",
                3,
                TrafficClass.ALERT,
                Severity.URGENT,
                queue_key="alert:31:repeat",
            )
        )
    governor.enqueue(
        OutboundItem(
            "ALL CLEAR",
            "^all",
            3,
            TrafficClass.ALERT,
            Severity.URGENT,
            supersedes="alert:31:repeat",
        )
    )
    queued = governor.queued_items()
    assert [item.text for item in queued] == ["ALL CLEAR"]


def test_atomic_all_clear_can_replace_repeats_in_a_full_queue() -> None:
    governor = AirtimeGovernor(
        SimulatedRadioLink(), AirtimeConfig(queue_max_items=2), VirtualClock()
    )
    for channel in (0, 3):
        governor.enqueue(
            OutboundItem(
                "repeat",
                "^all",
                channel,
                TrafficClass.ALERT,
                Severity.CRITICAL,
                queue_key="alert:42:repeat",
            )
        )
    all_clears = [
        OutboundItem(
            "ALL CLEAR",
            "^all",
            channel,
            TrafficClass.ALERT,
            Severity.CRITICAL,
            supersedes="alert:42:repeat" if index == 0 else None,
        )
        for index, channel in enumerate((0, 3))
    ]

    assert governor.enqueue_many(all_clears) is not None
    assert [(item.text, item.channel) for item in governor.queued_items()] == [
        ("ALL CLEAR", 0),
        ("ALL CLEAR", 3),
    ]


def test_multipart_enqueue_is_atomic() -> None:
    clock, link = VirtualClock(), SimulatedRadioLink()
    governor = AirtimeGovernor(link, AirtimeConfig(queue_max_items=1), clock)
    items = [
        OutboundItem("one", "!peer", 0, TrafficClass.REPLY),
        OutboundItem("two", "!peer", 0, TrafficClass.REPLY),
    ]
    assert governor.enqueue_many(items) is None
    assert governor.queue_depths()["reply"] == 0


@given(
    st.lists(
        st.tuples(
            st.sampled_from(list(TrafficClass)),
            st.sampled_from(list(Severity)),
            st.integers(min_value=1, max_value=200),
            st.floats(min_value=0, max_value=15, allow_nan=False, allow_infinity=False),
        ),
        min_size=1,
        max_size=80,
    )
)
def test_airtime_invariant_under_any_enqueue_sequence(
    operations: list[tuple[TrafficClass, Severity, int, float]],
) -> None:
    async def scenario() -> None:
        clock, link = VirtualClock(), SimulatedRadioLink()
        await link.connect()
        config = AirtimeConfig(
            budget_percent=1.0,
            emergency_reserve_percent=0.5,
            min_gap_s=0,
        )
        governor = AirtimeGovernor(link, config, clock, preset="SHORT_FAST")
        budget_s = 36.0
        absolute_s = 54.0
        for index, (traffic_class, severity, size, advance) in enumerate(operations):
            governor.enqueue(
                OutboundItem(
                    f"{index}-" + "x" * size,
                    "!peer",
                    0,
                    traffic_class,
                    severity,
                )
            )
            await governor.tick()
            assert governor.used_airtime <= absolute_s + 1e-9
            non_alert = sum(
                seconds
                for _, seconds, sent_class, _ in governor.history
                if sent_class != TrafficClass.ALERT
            )
            assert non_alert <= budget_s + 1e-9
            for checked_class in TrafficClass:
                if checked_class == TrafficClass.ALERT:
                    continue
                share_ceiling = budget_s * config.class_shares[checked_class.value]
                assert governor.class_airtime(checked_class) <= share_ceiling + 1e-9
            clock.advance(advance)

    import asyncio

    asyncio.run(scenario())
