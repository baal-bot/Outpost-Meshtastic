"""Delayed boot synchronization through the clock and durable production governor."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from outpost import clock as clock_module
from outpost.clock import SystemClock, VirtualClock
from outpost.timekeeping import HOLDOVER_SECONDS, TimeSource, TimeUncertain, time_status
from outpost.transport.models import TrafficClass
from tests.integration.test_federation_relay import A, B, allow_relay, relay_node
from tests.integration.test_federation_revisions import nodes as nodes
from tests.integration.test_governor_timing import packet
from tests.integration.test_governor_timing import queue as queue
from tests.integration.test_outage_readiness import appliance as appliance
from tests.integration.test_outage_readiness import observe
from tests.integration.test_peer_time import exchange, pair
from tests.integration.test_time_confidence import monitor
from tests.support.application import production_governor

pytestmark = pytest.mark.production_wiring


@pytest.mark.parametrize("correction", [-21600, 21600, 259.806])
def test_startup_step_needs_two_fresh_native_checks_and_only_recovers_once(monkeypatch, correction):
    clock = VirtualClock()
    source = monitor(monkeypatch, clock, synchronized=False)
    clock.advance(20.94)
    clock.epoch += timedelta(seconds=correction)
    source[0] = TimeSource(True, 0.01, "kernel_synchronized")
    status = time_status(clock)
    assert status.reason == "startup_clock_settling" and not status.timestamp_safe
    assert "Recovery is automatic" in status.detail
    for _ in range(29):
        clock.advance(1)
        assert not time_status(clock).timestamp_safe
    clock.advance(1)
    status = time_status(clock)
    assert status.timestamp_safe and status.startup_recovered
    assert status.state == "synchronized" and not status.holdover_verified
    assert "automatic startup clock recovery" in status.detail
    # A later jump cannot reopen the boot exception, even with a good OS flag.
    clock.epoch -= timedelta(seconds=correction)
    assert time_status(clock).reason == "wall_clock_step"
    clock.advance(60)
    assert not time_status(clock).timestamp_safe


@pytest.mark.parametrize(
    "source_failure",
    [
        TimeSource(),
        TimeSource(False, None, "kernel_unsynchronized"),
        TimeSource(True, 0.01, "kernel_clock_fault"),
        TimeSource(True, 31),
        TimeSource(True, float("nan")),
    ],
)
def test_startup_recovery_restarts_confirmation_after_source_failure(monkeypatch, source_failure):
    clock = VirtualClock()
    source = monitor(monkeypatch, clock, synchronized=False)
    clock.epoch += timedelta(seconds=260)
    source[0] = TimeSource(True, 0.01)
    assert not time_status(clock).timestamp_safe
    source[0] = source_failure
    clock.advance(30)
    assert not time_status(clock).timestamp_safe
    source[0] = TimeSource(True, 0.01)
    clock.advance(30)
    assert not time_status(clock).timestamp_safe
    clock.advance(30)
    assert time_status(clock).startup_recovered


@pytest.mark.parametrize("interruption", ["second_step", "probe_gap", "implausible_date"])
def test_startup_recovery_requires_stable_plausible_recent_time(monkeypatch, interruption):
    clock = VirtualClock()
    source = monitor(monkeypatch, clock, synchronized=False)
    clock.epoch += timedelta(seconds=260)
    source[0] = TimeSource(True, 0.01)
    assert not time_status(clock).timestamp_safe
    if interruption == "second_step":
        clock.advance(29)
        clock.epoch -= timedelta(seconds=10)
    elif interruption == "probe_gap":
        clock.advance(61)
    else:
        clock.epoch -= timedelta(days=3650)
        assert time_status(clock).reason == "implausible_wall_time"
        clock.epoch += timedelta(days=3650)
    assert not time_status(clock).timestamp_safe
    clock.advance(30)
    assert time_status(clock).startup_recovered


def test_startup_without_source_stays_available_but_cannot_invent_holdover(monkeypatch):
    clock = VirtualClock()
    source = monitor(monkeypatch, clock, synchronized=False)
    clock.epoch += timedelta(seconds=260)
    assert not time_status(clock).timestamp_safe
    clock.advance(HOLDOVER_SECONDS * 2)
    assert not time_status(clock).timestamp_safe
    source[0] = TimeSource(True, 0.01)
    clock.advance(30)
    assert not time_status(clock).timestamp_safe
    clock.advance(30)
    assert time_status(clock).timestamp_safe
    source[0] = TimeSource()
    clock.advance(30)
    assert time_status(clock).state == "holdover"
    clock.advance(HOLDOVER_SECONDS)
    assert time_status(clock).reason == "holdover_exhausted"


def test_monotonic_regression_cannot_use_startup_recovery(monkeypatch):
    clock = VirtualClock(value=100)
    source = monitor(monkeypatch, clock, synchronized=False)
    clock.value = 0
    source[0] = TimeSource(True, 0.01)
    assert not time_status(clock).timestamp_safe
    clock.advance(60)
    assert not time_status(clock).timestamp_safe


@pytest.mark.parametrize("initially_synchronized", [False, True])
def test_system_clock_records_source_before_slow_application_startup(
    monkeypatch, initially_synchronized
):
    virtual = VirtualClock()
    source = [TimeSource(initially_synchronized, 0.01)]
    monkeypatch.setattr(clock_module, "time", SimpleNamespace(monotonic=virtual.monotonic))
    monkeypatch.setattr(SystemClock, "now", lambda self: virtual.now())
    monkeypatch.setattr("outpost.timekeeping.kernel_time", lambda: source[0])
    clock = SystemClock()
    virtual.advance(20.94)
    virtual.epoch += timedelta(seconds=259.806)
    source[0] = TimeSource(True, 0.01)
    assert not clock.time_status().timestamp_safe
    virtual.advance(30)
    assert clock.time_status().timestamp_safe is (not initially_synchronized)


@pytest.mark.parametrize("offset", [-21600, 21600, -259.806])
async def test_boot_correction_recovers_queue_expiry_and_airtime_without_restart(
    queue, monkeypatch, offset
):
    database, old_governor, clock, radio = queue
    sent = packet("plain")
    sent.text = "already sent"
    await old_governor.admit(sent)
    assert await old_governor.tick() is sent
    cost = old_governor.used_airtime
    valid = packet("plain", TrafficClass.FEDERATION)
    expired = packet("plain", TrafficClass.FEDERATION)
    expired.text = "deadline will pass during startup"
    await old_governor.admit_many([valid, expired])
    await database.write(
        "UPDATE outbound_work SET expires_at=? WHERE id=?",
        (clock.now().timestamp() + 30, expired.item_id),
    )
    before = [dict(row) for row in await database.read("SELECT * FROM outbound_work")]
    clock.epoch += timedelta(seconds=offset)
    source = monitor(monkeypatch, clock, synchronized=False)
    governor = production_governor(database, clock, link=radio)
    assert await governor.recover() == 0
    assert await governor.tick() is None
    assert (
        await governor.admit_many_result([packet("plain")])
    ).rejection_reason == "time_uncertain"
    clock.advance(20.94)
    clock.epoch -= timedelta(seconds=offset)
    source[0] = TimeSource(True, 0.01)
    assert not time_status(clock).timestamp_safe
    assert await governor.tick() is None
    assert [dict(row) for row in await database.read("SELECT * FROM outbound_work")] == before
    clock.advance(30)
    resumed = await governor.tick()
    assert resumed is not None and resumed.item_id == valid.item_id
    assert time_status(clock).startup_recovered
    assert governor.time_recovery_wait == 0
    assert governor.used_airtime == pytest.approx(cost * 2, abs=0.001)  # Durable milliseconds.
    assert len(governor.history) == 2
    assert len(radio.sent) == 2
    rows = {row["id"]: row for row in await database.read("SELECT * FROM outbound_work")}
    assert rows[expired.item_id]["state"] == "expired"
    assert rows[expired.item_id]["attempts"] == 0
    assert rows[valid.item_id]["attempts"] == 1
    assert await governor.tick() is None
    assert len(radio.sent) == 2


async def test_peer_trust_closes_startup_exception_before_first_native_sync(nodes, monkeypatch):
    local, remote, _ = await pair(nodes, monkeypatch)
    source = monitor(monkeypatch, local.clock, synchronized=False)
    await exchange(local, remote)
    assert time_status(local.clock).source == "federation_peer"
    assert time_status(local.clock).timestamp_safe
    local.clock.epoch += timedelta(seconds=260)
    source[0] = TimeSource(True, 0.01)
    assert time_status(local.clock).reason == "wall_clock_step"
    local.clock.advance(60)
    assert not time_status(local.clock).timestamp_safe


@pytest.mark.parametrize("marker,wait,cost", [("true", 3570, 0), ("[9.0]", 0, 9)])
async def test_startup_recovery_preserves_durable_probe_costs_and_unknown_history_silence(
    queue, monkeypatch, marker, wait, cost
):
    database, _, clock, radio = queue
    await database.write(
        "INSERT INTO runtime_setting(key,value,updated_at) VALUES('airtime.time_recovery',?,0)",
        (marker,),
    )
    source = monitor(monkeypatch, clock, synchronized=False)
    governor = production_governor(database, clock, link=radio)
    await governor.recover()
    clock.epoch += timedelta(seconds=260)
    source[0] = TimeSource(True, 0.01)
    assert not time_status(clock).timestamp_safe
    clock.advance(30)
    assert await governor.tick() is None
    assert time_status(clock).startup_recovered
    assert governor.time_recovery_wait == wait
    assert governor.used_airtime == cost
    assert not radio.sent
    assert (
        await database.read("SELECT value FROM runtime_setting WHERE key='airtime.time_recovery'")
    )[0][0] == marker


@pytest.mark.parametrize("offset", [-21600, 21600])
async def test_startup_recovery_preserves_signed_expiry_and_replay_checks(
    tmp_path, monkeypatch, offset
):
    ad, ac, ap, ar = await relay_node(tmp_path, "source", A)
    bd, bc, bp, br = await relay_node(tmp_path, "target", B)
    try:
        await allow_relay(bd, bp, br, A)
        expired = await ar.create(B, "incident", {"status": "old"}, expires_in=60)
        valid = await ar.create(B, "incident", {"status": "current"}, expires_in=3600)
        expired_wire, valid_wire = await ar.wire(expired), await ar.wire(valid)
        bc.epoch += timedelta(seconds=offset)
        source = monitor(monkeypatch, bc, synchronized=False)
        with pytest.raises(TimeUncertain):
            await br.accept(A, valid_wire)
        bc.advance(61)
        bc.epoch -= timedelta(seconds=offset)
        source[0] = TimeSource(True, 0.01)
        assert not time_status(bc).timestamp_safe
        with pytest.raises(TimeUncertain):
            await br.accept(A, valid_wire)
        assert not await bd.read("SELECT * FROM fed_relay_envelope")
        bc.advance(30)
        assert time_status(bc).startup_recovered
        with pytest.raises(ValueError, match="timestamps"):
            await br.accept(A, expired_wire)
        assert await br.accept(A, valid_wire) == (valid, "delivered")
        assert await br.accept(A, valid_wire) == (valid, "delivered")
        assert len(await bd.read("SELECT * FROM fed_relay_envelope")) == 1
    finally:
        await ad.close()
        await bd.close()


async def test_startup_recovery_refreshes_cached_readiness_without_renewing_observations(
    appliance, monkeypatch
):
    await observe(appliance, "station_power")
    observation_sql = (
        "SELECT value FROM runtime_setting WHERE key='readiness.observation.station_power'"
    )
    original = (await appliance.database.read(observation_sql))[0][0]
    source = monitor(monkeypatch, appliance.clock, synchronized=False)
    appliance.clock.epoch += timedelta(seconds=259.806)
    source[0] = TimeSource(True, 0.01)
    await appliance.self_check.run("startup")
    assert (await appliance.self_check.latest())["trigger"] == "startup"
    appliance.clock.advance(30)
    report = await appliance.self_check.latest()
    assert report["trigger"] == "startup-time-recovered"
    clock_check = next(c for c in report["checks"] if c["name"] == "time_confidence")
    assert clock_check["evidence"]["timestamp_safe"] and clock_check["state"] == "unknown"
    assert (await appliance.database.read(observation_sql))[0][0] == original
    assert (
        len(
            await appliance.database.read(
                "SELECT * FROM audit_log WHERE action='readiness.observation'"
            )
        )
        == 1
    )
    saved_sql = "SELECT value FROM runtime_setting WHERE key='readiness.self_check'"
    saved = (await appliance.database.read(saved_sql))[0][0]
    appliance.clock.advance(5)
    await appliance.self_check.latest()
    assert (await appliance.database.read(saved_sql))[0][0] == saved
