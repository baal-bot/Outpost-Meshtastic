from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from prometheus_client import generate_latest

from outpost.clock import VirtualClock
from outpost.config import AirtimeConfig, RadioPowerConfig
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.models import LocalTelemetry, Severity, TrafficClass
from outpost.transport.simulated import SimulatedRadioLink
from outpost.transport.toa import MAX_PAYLOAD_BYTES


def test_text_payload_is_truncated_by_utf8_bytes_before_admission() -> None:
    governor = AirtimeGovernor(SimulatedRadioLink(), AirtimeConfig(), VirtualClock())
    item = OutboundItem("🚨" * 100, "^all", 0, TrafficClass.ALERT)

    assert governor.enqueue(item) is not None
    assert item.text.endswith("…")
    assert item.payload_size <= MAX_PAYLOAD_BYTES


def test_governor_tracks_live_preset_and_regional_ceiling() -> None:
    config = AirtimeConfig(budget_percent=8, emergency_reserve_percent=4)
    governor = AirtimeGovernor(
        SimulatedRadioLink(), config, VirtualClock(), preset="LONG_FAST", region="EU_866"
    )
    slow_cost = governor.estimate_toa(100)

    assert governor.reported_preset == governor.preset == "LONG_FAST"
    assert governor.region == "EU_866"
    assert governor.regional_ceiling_percent == 2.5
    assert governor.budget_percent == pytest.approx(2.5 * 8 / 12)
    assert governor.reserve_percent == pytest.approx(2.5 * 4 / 12)

    governor.sync_radio_profile("SHORT_FAST", "US")

    assert governor.reported_preset == governor.preset == "SHORT_FAST"
    assert governor.estimate_toa(100) < slow_cost
    assert governor.budget_percent == 8
    assert governor.reserve_percent == 4
    assert governor.profile_warnings == ()


def test_airtime_preview_uses_dispatch_model_and_real_preset() -> None:
    clock = VirtualClock()
    governor = AirtimeGovernor(
        SimulatedRadioLink(), AirtimeConfig(), clock, preset="LONG_FAST", region="US"
    )

    preview = governor.estimate_payloads([12, 100], traffic_class=TrafficClass.REPLY, copies=3)

    assert preview["part_count"] == 2
    assert preview["transmission_count"] == 6
    assert preview["per_copy_seconds"] == pytest.approx(
        governor.estimate_toa(12) + governor.estimate_toa(100)
    )
    assert preview["total_seconds"] == pytest.approx(preview["per_copy_seconds"] * 3)
    long_fast_cost = preview["total_seconds"]

    governor.sync_radio_profile("SHORT_FAST", "US")
    faster = governor.estimate_payloads([12, 100], traffic_class=TrafficClass.REPLY, copies=3)
    assert faster["costing_preset"] == "SHORT_FAST"
    assert faster["total_seconds"] < long_fast_cost


def test_airtime_preview_identifies_class_and_utilisation_displacement() -> None:
    shares = {**AirtimeConfig().class_shares, "reply": 0.0}
    governor = AirtimeGovernor(
        SimulatedRadioLink(),
        AirtimeConfig(class_shares=shares),
        VirtualClock(),
        region="US",
    )
    governor.channel_utilisation = governor.config.utilisation_ceiling

    preview = governor.estimate_text("field update", traffic_class=TrafficClass.REPLY)

    assert preview["requires_confirmation"] is True
    assert set(preview["breach_codes"]) == {"class_share", "utilisation_ceiling"}
    assert preview["class_budget"]["projected_seconds"] > 0
    assert preview["utilisation"]["projected_percent"] > governor.config.utilisation_ceiling


def test_critical_preview_makes_emergency_reserve_use_explicit() -> None:
    clock = VirtualClock()
    governor = AirtimeGovernor(SimulatedRadioLink(), AirtimeConfig(), clock, region="US")
    cost = governor.estimate_toa(len(b"evacuate"))
    normal_budget = 3_600 * governor.budget_percent / 100
    governor.history.append(
        (clock.monotonic(), normal_budget - cost / 2, TrafficClass.REPLY, Severity.INFO)
    )

    preview = governor.estimate_text(
        "evacuate", traffic_class=TrafficClass.ALERT, severity=Severity.CRITICAL
    )

    assert preview["breach_codes"] == ["hourly_budget"]
    assert preview["class_budget"]["exempt"] is True
    assert preview["budget"]["reserve_used_after_seconds"] > 0


def test_unknown_radio_profile_costs_conservatively_and_unknown_region_pauses() -> None:
    governor = AirtimeGovernor(
        SimulatedRadioLink(),
        AirtimeConfig(),
        VirtualClock(),
        preset="FUTURE_ULTRA_LONG",
        region="FUTURE_REGION",
    )

    assert governor.reported_preset == "FUTURE_ULTRA_LONG"
    assert governor.preset == "VERY_LONG_SLOW"
    assert governor.estimate_toa(100) >= 12
    assert governor.regional_ceiling_percent == 0
    assert governor.budget_percent == governor.reserve_percent == 0
    assert len(governor.profile_warnings) == 2


def test_explicit_regional_ceiling_is_validated() -> None:
    with pytest.raises(ValueError, match="regional airtime ceiling"):
        AirtimeGovernor(
            SimulatedRadioLink(),
            AirtimeConfig(),
            VirtualClock(),
            regional_ceiling_percent=101,
        )


def test_oversized_binary_payload_is_rejected_atomically() -> None:
    governor = AirtimeGovernor(SimulatedRadioLink(), AirtimeConfig(), VirtualClock())
    valid = OutboundItem("valid", "^all", 0, TrafficClass.FEDERATION)
    oversized = OutboundItem("", "^all", 0, TrafficClass.FEDERATION, binary_payload=b"x" * 234)

    assert governor.enqueue_many([valid, oversized]) is None
    assert governor.queued_items() == []
    assert governor.metrics.dropped[(TrafficClass.FEDERATION, "payload_too_large")] == 2


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
async def test_user_reply_continues_while_federation_waits_through_quiet_hours() -> None:
    clock = VirtualClock(epoch=datetime(2026, 1, 1, 23, tzinfo=UTC))
    link = SimulatedRadioLink()
    await link.connect()
    governor = AirtimeGovernor(link, AirtimeConfig(min_gap_s=0), clock)
    federation = OutboundItem("background", "^all", 0, TrafficClass.FEDERATION)
    reply = OutboundItem("requested", "!peer", 0, TrafficClass.REPLY)
    governor.enqueue(federation)
    governor.enqueue(reply)

    assert await governor.tick() is reply
    assert await governor.tick() is None
    assert federation in governor.queued_items()


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
async def test_low_power_shedding_is_explicit_and_never_sheds_alerts_or_replies() -> None:
    clock, link = VirtualClock(), SimulatedRadioLink()
    await link.connect()
    link.telemetry = LocalTelemetry(battery_level=10)
    power = RadioPowerConfig(shed_discretionary=True, shed_below_percent=15)
    governor = AirtimeGovernor(link, AirtimeConfig(min_gap_s=0), clock, power_config=power)
    digest = OutboundItem("digest", "!peer", 0, TrafficClass.DIGEST)
    reply = OutboundItem("reply", "!peer", 0, TrafficClass.REPLY)
    alert = OutboundItem("alert", "^all", 0, TrafficClass.ALERT, Severity.URGENT)
    governor.enqueue(digest)
    governor.enqueue(reply)
    governor.enqueue(alert)

    assert await governor.tick() is alert
    clock.advance(60)
    assert await governor.tick() is reply
    clock.advance(60)
    assert await governor.tick() is None
    assert digest in governor.queued_items()
    assert governor.metrics.throttled["low_power"] == 1


@pytest.mark.asyncio
async def test_low_power_shedding_defaults_off_and_critical_alert_keeps_reserve() -> None:
    clock = VirtualClock(epoch=datetime(2026, 1, 1, 12, tzinfo=UTC))
    link = SimulatedRadioLink()
    await link.connect()
    link.telemetry = LocalTelemetry(battery_level=5)
    normal = AirtimeGovernor(link, AirtimeConfig(min_gap_s=0), clock)
    bulletin = OutboundItem("bulletin", "^all", 0, TrafficClass.BULLETIN)
    normal.enqueue(bulletin)
    assert await normal.tick() is bulletin

    config = AirtimeConfig(budget_percent=0.01, emergency_reserve_percent=0.01, min_gap_s=0)
    shedding = AirtimeGovernor(
        link,
        config,
        clock,
        preset="SHORT_FAST",
        power_config=RadioPowerConfig(shed_discretionary=True, shed_below_percent=15),
    )
    shedding.history.append((clock.monotonic(), 0.36, TrafficClass.REPLY, Severity.INFO))
    critical = OutboundItem("critical", "^all", 0, TrafficClass.ALERT, Severity.CRITICAL)
    shedding.enqueue(critical)
    assert await shedding.tick() is critical


@pytest.mark.asyncio
async def test_governor_exports_battery_metrics_and_no_battery_as_nan() -> None:
    clock, link = VirtualClock(), SimulatedRadioLink()
    await link.connect()
    link.telemetry = LocalTelemetry(battery_level=47)
    governor = AirtimeGovernor(link, AirtimeConfig(), clock)

    await governor.tick()
    metrics = generate_latest().decode()
    assert "outpost_radio_battery_level_percent 47.0" in metrics
    assert "outpost_radio_battery_reported 1.0" in metrics

    link.telemetry = LocalTelemetry(battery_level=None)
    await governor.tick()
    metrics = generate_latest().decode()
    assert "outpost_radio_battery_level_percent NaN" in metrics
    assert "outpost_radio_battery_reported 0.0" in metrics


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
async def test_post_startup_config_mutation_cannot_crash_dispatch() -> None:
    clock, link = VirtualClock(), SimulatedRadioLink()
    await link.connect()
    config = AirtimeConfig(min_gap_s=0)
    config.class_shares.pop("reply")
    config.quiet_hours.start = "10pm"
    config.quiet_hours.classes = ["reply"]
    governor = AirtimeGovernor(link, config, clock)
    governor.enqueue(OutboundItem("reply", "!peer", 0, TrafficClass.REPLY))
    governor.enqueue(OutboundItem("critical", "^all", 3, TrafficClass.ALERT, Severity.CRITICAL))

    sent = await governor.tick()
    assert sent is not None and sent.traffic_class == TrafficClass.ALERT
    assert await governor.tick() is None
    assert governor.queue_depths()["reply"] == 1


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


@pytest.mark.asyncio
async def test_held_batch_cannot_transmit_until_committed() -> None:
    clock, link = VirtualClock(), SimulatedRadioLink()
    await link.connect()
    governor = AirtimeGovernor(link, AirtimeConfig(min_gap_s=0), clock)
    item = OutboundItem("held", "!peer", 0, TrafficClass.REPLY)

    item_ids = governor.enqueue_many([item], hold=True)

    assert item_ids is not None
    assert await governor.tick() is None
    assert link.sent == []
    governor.release_many(item_ids)
    assert await governor.tick() is item


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
