"""Production clock/queue/custody boundaries with synthetic sources and real stores."""

import ctypes
from datetime import timedelta

import pytest

from outpost import timekeeping
from outpost.clock import VirtualClock
from outpost.timekeeping import (
    HOLDOVER_SECONDS,
    ElapsedTime,
    TimeMonitor,
    TimeSource,
    TimeUncertain,
    inspection,
    kernel_time,
    require_time,
    rtc_inventory,
    time_status,
)
from outpost.transport.models import TrafficClass
from tests.integration.test_federation_relay import A, B, allow_relay, relay_node
from tests.integration.test_federation_revisions import nodes as nodes
from tests.integration.test_federation_revisions import report
from tests.integration.test_governor_timing import delay_once, packet
from tests.integration.test_governor_timing import queue as queue
from tests.integration.test_incident_worker import pair
from tests.integration.test_outage_readiness import appliance as appliance
from tests.integration.test_outage_readiness import checks
from tests.support.application import production_governor

pytestmark = pytest.mark.production_wiring


def monitor(monkeypatch, clock, *, synchronized=True):
    source = [
        TimeSource(
            synchronized,
            0.01 if synchronized else None,
            "kernel_synchronized" if synchronized else "kernel_unsynchronized",
        )
    ]
    observer = TimeMonitor(clock.now().timestamp(), clock.monotonic(), lambda: source[0])
    monkeypatch.setattr(clock, "_time_monitor", observer, raising=False)
    monkeypatch.setattr(
        clock, "time_status", lambda: observer.sample(clock.now().timestamp(), clock.monotonic())
    )
    time_status(clock)
    return source


@pytest.mark.parametrize("hours", [-6, 6])
def test_cold_boot_requires_source_not_plausible_date_or_timezone(monkeypatch, hours):
    clock = VirtualClock()
    clock.epoch += timedelta(hours=hours)
    source = monitor(monkeypatch, clock, synchronized=False)
    assert not time_status(clock).timestamp_safe
    with pytest.raises(TimeUncertain, match="UTC"):
        require_time(clock)
    source[0] = TimeSource(True, 0.02, "kernel_synchronized")
    clock.advance(31)
    assert time_status(clock).state == "synchronized"
    assert not time_status(clock).holdover_verified


def test_holdover_is_finite_and_does_not_refresh_from_an_absent_source(monkeypatch):
    clock = VirtualClock()
    source = monitor(monkeypatch, clock)
    source[0] = TimeSource(False, None, "kernel_unsynchronized")
    clock.advance(31)
    assert time_status(clock).state == "holdover"
    clock.advance(HOLDOVER_SECONDS - 31)
    assert time_status(clock).timestamp_safe
    clock.advance(1)
    assert not time_status(clock).timestamp_safe
    assert time_status(clock).reason == "holdover_exhausted"


@pytest.mark.parametrize("hours", [-6, 6])
def test_steps_latch_until_restart_even_if_kernel_still_claims_sync(monkeypatch, hours):
    clock = VirtualClock()
    monitor(monkeypatch, clock)
    clock.epoch += timedelta(hours=hours)
    assert time_status(clock).state == "stepped"
    clock.epoch -= timedelta(hours=hours)
    clock.advance(31)
    assert not time_status(clock).timestamp_safe
    monitor(monkeypatch, clock)  # Explicit new process baseline after restoring correct UTC.
    assert time_status(clock).timestamp_safe


def test_implausible_time_large_error_and_small_slew(monkeypatch):
    observer = TimeMonitor(1, 0, lambda: TimeSource(True, 0.01))
    assert observer.sample(1, 0).reason == "implausible_wall_time"
    clock = VirtualClock()
    source = monitor(monkeypatch, clock, synchronized=False)
    source[0] = TimeSource(True, 31, "kernel_unsynchronized")
    clock.advance(31)
    assert not time_status(clock).timestamp_safe
    source[0] = TimeSource(True, 0.01)
    clock.advance(31)
    assert time_status(clock).timestamp_safe
    clock.advance(1000)
    clock.epoch += timedelta(seconds=0.5)
    assert time_status(clock).timestamp_safe
    assert not time_status(object()).timestamp_safe


@pytest.mark.parametrize(
    "result,status,error,good",
    [
        (0, 0, 10_000, True),
        (1, 0x10, 10_000, True),
        (5, 0x40, 10_000, False),
        (0, 0x1000, 10_000, False),
        (-1, 0, 0, False),
        (0, 0, -1, False),
        (0, 0, 31_000_000, False),
    ],
)
def test_kernel_probe_is_strictly_read_only(monkeypatch, result, status, error, good):
    class Query:
        def __call__(self, pointer):
            value = ctypes.cast(pointer, ctypes.POINTER(timekeeping._Timex)).contents
            assert value.modes == 0
            assert bytes(value) == bytes(ctypes.sizeof(value))
            value.status, value.maxerror = status, error
            return result

    class Library:
        adjtimex = Query()

    monkeypatch.setattr(timekeeping.ctypes, "CDLL", lambda *args, **kwargs: Library())
    assert kernel_time().synchronized is good


def test_missing_probe_and_rtc_never_certify_a_battery(monkeypatch, tmp_path):
    monkeypatch.setattr(timekeeping.sys, "platform", "other")
    assert not kernel_time().synchronized
    monkeypatch.setattr(timekeeping.sys, "platform", "linux")
    monkeypatch.setattr(timekeeping.ctypes, "CDLL", lambda *args, **kwargs: object())
    assert kernel_time().reason == "source_unavailable"
    assert rtc_inventory(tmp_path)["devices"] == []
    rtc = tmp_path / "rtc0"
    rtc.mkdir()
    (rtc / "hctosys").write_text("1")
    (rtc / "since_epoch").write_text("invalid")
    (rtc / "charging_voltage").write_text("3000000")
    value = rtc_inventory(tmp_path)
    assert value["devices"][0]["hctosys"] == 1
    assert value["battery_present"] is None and not value["retention_verified"]


@pytest.mark.parametrize("hours", [-6, 6])
async def test_queue_wall_steps_do_not_expire_or_dispatch_retained_work(queue, monkeypatch, hours):
    database, governor, clock, radio = queue
    monitor(monkeypatch, clock)
    item = packet("plain", TrafficClass.FEDERATION)
    await governor.admit_many([item])
    before = dict((await database.read("SELECT * FROM outbound_work"))[0])
    clock.epoch += timedelta(hours=hours)
    assert await governor.tick() is None
    assert not radio.sent
    assert dict((await database.read("SELECT * FROM outbound_work"))[0]) == before
    assert governor.metrics.throttled["time_uncertain"] >= 1
    assert (
        await governor.admit_many_result([packet("plain", TrafficClass.FEDERATION)])
    ).rejection_reason == "time_uncertain"
    recovered = production_governor(database, clock, link=radio)
    assert await recovered.recover() == 0
    assert dict((await database.read("SELECT * FROM outbound_work"))[0]) == before
    clock.epoch -= timedelta(hours=hours)
    monitor(monkeypatch, clock)
    assert await recovered.tick() is not None
    assert len(radio.sent) == 1
    assert (await database.read("SELECT attempts FROM outbound_work"))[0][0] == 1


@pytest.mark.parametrize("phase", ["writer", "guard", "telemetry"])
@pytest.mark.parametrize("hours", [-6, 6])
async def test_step_during_dispatch_wait_cannot_consume_or_send_work(
    queue, monkeypatch, phase, hours
):
    database, governor, clock, radio = queue
    monitor(monkeypatch, clock)
    item = packet(phase, TrafficClass.FEDERATION)
    await governor.admit_many([item])
    delay_once(
        monkeypatch,
        governor,
        radio,
        phase,
        lambda: setattr(clock, "epoch", clock.epoch + timedelta(hours=hours)),
    )
    assert await governor.tick() is None
    assert not radio.sent
    row = (await database.read("SELECT state,attempts FROM outbound_work"))[0]
    assert tuple(row) == ("pending", 0)
    assert not await database.read("SELECT * FROM outbound_attempt")


@pytest.mark.parametrize("hours", [-6, 6])
async def test_elapsed_queue_deadline_survives_raw_wall_changes(queue, hours):
    database, governor, clock, radio = queue
    await governor.admit_many([packet("plain")])
    await radio.close()
    clock.epoch += timedelta(hours=hours)
    clock.advance(299)
    await governor.tick()
    assert (await database.read("SELECT state FROM outbound_work"))[0][0] == "pending"
    clock.advance(2)
    await governor.tick()
    assert (await database.read("SELECT state FROM outbound_work"))[0][0] == "expired"
    assert not radio.sent


@pytest.mark.parametrize("hours", [-6, 6])
async def test_custody_uncertainty_retains_payload_and_never_acknowledges_delivery(
    tmp_path, monkeypatch, hours
):
    ad, ac, ap, ar = await relay_node(tmp_path, "source", A)
    bd, bc, bp, br = await relay_node(tmp_path, "target", B)
    try:
        await allow_relay(ad, ap, ar, B)
        await allow_relay(bd, bp, br, A)
        monitor(monkeypatch, ac)
        monitor(monkeypatch, bc)
        uid = await ar.create(B, "incident", {"status": "synthetic"}, expires_in=3600)
        envelope = await ar.wire(uid)
        before = dict((await ad.read("SELECT * FROM fed_relay_envelope"))[0])
        ac.epoch += timedelta(hours=hours)
        bc.epoch += timedelta(hours=hours)
        assert await ar.expire() == 0
        assert await ar.recover_stalled() == 0
        assert await ar.next_hop(uid) is None
        with pytest.raises(TimeUncertain):
            await ar.reserve_forward(uid, B, 1)
        with pytest.raises(TimeUncertain):
            await ar.create(B, "incident", {"status": "new"})
        with pytest.raises(TimeUncertain):
            await br.accept(A, envelope)
        assert not await bd.read("SELECT * FROM fed_relay_envelope")
        assert dict((await ad.read("SELECT * FROM fed_relay_envelope"))[0]) == before
        assert (await ar.summary())["time_warning"]
        assert (await ar.queue())[0]["time_blocked"]
        ac.epoch -= timedelta(hours=hours)
        bc.epoch -= timedelta(hours=hours)
        monitor(monkeypatch, ac)
        monitor(monkeypatch, bc)
        assert await br.accept(A, envelope) == (uid, "delivered")
        assert await br.accept(A, envelope) == (uid, "delivered")
        assert len(await bd.read("SELECT * FROM fed_relay_envelope")) == 1
        bc.advance(3601)
        assert await br.expire() == 1
        assert (await bd.read("SELECT payload_cbor FROM fed_relay_envelope"))[0][0] is None
        with pytest.raises(ValueError, match="timestamps"):
            await br.accept(A, envelope)
    finally:
        await ad.close()
        await bd.close()


@pytest.mark.parametrize("hours", [-6, 6])
async def test_custody_clock_skew_still_enforces_signature_lifetime(tmp_path, hours):
    ad, ac, ap, ar = await relay_node(tmp_path, "source", A)
    bd, bc, bp, br = await relay_node(tmp_path, "target", B)
    try:
        await allow_relay(bd, bp, br, A)
        ac.epoch += timedelta(hours=hours)
        uid = await ar.create(B, "incident", {"status": "synthetic"}, expires_in=3600)
        with pytest.raises(ValueError, match="time confidence"):
            await br.accept(A, await ar.wire(uid))
        assert not await bd.read("SELECT * FROM fed_relay_envelope")
    finally:
        await ad.close()
        await bd.close()


async def test_readiness_and_retention_explain_time_uncertainty(appliance, monkeypatch):
    clock = appliance.clock
    monitor(monkeypatch, clock, synchronized=False)
    result = checks(await appliance.self_check.run("test"))["time_confidence"]
    assert result["state"] == "unknown"
    assert result["evidence"]["timestamp_safe"] is False
    assert "UTC" in result["detail"]
    with pytest.raises(TimeUncertain):
        await appliance.maintenance.run()
    assert not await appliance.maintenance.due()
    assert "time_confidence" in (await appliance.maintenance.health())["failures"]


async def test_normal_clock_slew_is_not_a_readiness_step(appliance, monkeypatch):
    clock = appliance.clock
    monitor(monkeypatch, clock)
    for _ in range(12):
        clock.advance(1000)
        clock.epoch += timedelta(seconds=0.5)
        assert time_status(clock).timestamp_safe
        assert not appliance.self_check._clock_changed()


@pytest.mark.parametrize("hours", [-6, 6])
async def test_incident_worker_preserves_intents_and_reports_time_block(nodes, monkeypatch, hours):
    source, sp, target, _ = await pair(nodes)
    monitor(monkeypatch, source.clock)
    incident = await report(source)
    await source._incident_delivery_once()
    before = [dict(r) for r in await source.database.read("SELECT * FROM fed_incident_intent")]
    source.clock.epoch += timedelta(hours=hours)
    await source._incident_delivery_once()
    assert [
        dict(r) for r in await source.database.read("SELECT * FROM fed_incident_intent")
    ] == before
    assert (
        "time_uncertain"
        in (
            await source.database.read(
                "SELECT cursor FROM fed_cursor WHERE peer_id=? AND stream='_incident_worker'",
                (sp.id,),
            )
        )[0][0]
    )
    assert await source.governor.tick() is None
    assert not source.radio.sent
    assert (await source.incidents.by_id(incident.id)).title == incident.title


def test_elapsed_projection_is_not_utc_confidence():
    clock = VirtualClock()
    elapsed = ElapsedTime(clock)
    stamp = elapsed.now()
    clock.epoch += timedelta(hours=6)
    clock.advance(10)
    assert elapsed.now() == stamp + 10
    elapsed.reset()
    assert elapsed.now() == clock.now().timestamp()


@pytest.mark.parametrize("hours", [-6, 6])
@pytest.mark.parametrize("traffic_class", [TrafficClass.REPLY, TrafficClass.ALERT])
async def test_local_replies_and_alerts_continue_with_elapsed_limits(
    queue, monkeypatch, hours, traffic_class
):
    database, governor, clock, radio = queue
    monitor(monkeypatch, clock)
    await governor.admit_many([packet("plain", traffic_class)])
    epoch = clock.now().timestamp()
    clock.epoch += timedelta(hours=hours)
    assert time_status(clock).state == "stepped"
    assert await governor.tick() is not None
    assert len(radio.sent) == 1
    attempt = (await database.read("SELECT started_at,completed_at FROM outbound_attempt"))[0]
    assert tuple(attempt) == (epoch, epoch)
    assert governor.used_airtime > 0
    prior = governor.used_airtime
    clock.epoch -= timedelta(hours=2 * hours)
    assert governor.used_airtime == prior


def test_fault_does_not_fall_back_to_holdover(monkeypatch):
    clock = VirtualClock()
    source = monitor(monkeypatch, clock)
    source[0] = TimeSource(False, 0.01, "kernel_clock_fault")
    clock.advance(31)
    assert not time_status(clock).timestamp_safe
    assert time_status(clock).reason == "kernel_clock_fault"


def test_focused_inspection_is_sanitized_and_never_self_certifies():
    result = inspection()
    assert result["physical_retention_tested"] is False
    assert result["rtc"]["battery_present"] is None
    assert "observed_utc" in result and "monotonic_seconds" in result
    assert not {"config", "hostname", "location", "serial", "credentials"} & result.keys()


async def test_time_step_inside_custody_dispatch_rolls_back_domain_work(tmp_path, monkeypatch):
    ad, ac, ap, ar = await relay_node(tmp_path, "source", A)
    bd, bc, bp, br = await relay_node(tmp_path, "target", B)
    try:
        await allow_relay(bd, bp, br, A)
        monitor(monkeypatch, bc)
        uid = await ar.create(B, "incident", {"status": "synthetic"})

        async def handler(tx, context, payload):
            await tx.write(
                "INSERT INTO runtime_setting(key,value,updated_at) VALUES('time-test','true',0)"
            )
            bc.epoch += timedelta(hours=6)

        br.register_handler("incident", handler)
        with pytest.raises(TimeUncertain):
            await br.accept(A, await ar.wire(uid))
        assert not await bd.read("SELECT * FROM fed_relay_envelope")
        assert not await bd.read("SELECT * FROM runtime_setting WHERE key='time-test'")
        assert not await bd.read(
            "SELECT * FROM fed_relay_event WHERE event_kind='dispatch_succeeded'"
        )
    finally:
        await ad.close()
        await bd.close()
