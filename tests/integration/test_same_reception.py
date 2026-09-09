"""Reception evidence through the production receiver/store, with synthetic input."""

import asyncio
import math
from datetime import timedelta

import pytest

from outpost.clock import SystemClock, VirtualClock
from outpost.config import SameConfig
from outpost.env import SameReceiver, SameService
from outpost.store import Database
from outpost.timekeeping import TimeStatus
from tests.integration.test_same import LIVE_HEADER, alert_service
from tests.unit.test_same_receiver import YieldClock, receiver_config

pytestmark = pytest.mark.production_wiring

TEST_HEADER = "ZCZC-WXR-RWT-042003+0030-0010000-TEST    -"


@pytest.fixture
async def reception(tmp_path):
    database = Database(tmp_path / "reception.db")
    await database.open()
    clock = VirtualClock()
    config = SameConfig(enabled=True, county_codes=["042003"])
    service = SameService(database, clock, config)
    receiver = SameReceiver(service, config, clock)
    try:
        yield service, receiver, clock
    finally:
        await database.close()


@pytest.mark.parametrize(
    "rms,expected",
    [
        (0, "below_threshold"),
        (299, "below_threshold"),
        (300, "above_threshold"),
        (32767, "above_threshold"),
        (math.nan, "unknown"),
        (math.inf, "unknown"),
        (-1, "unknown"),
    ],
)
async def test_audio_level_never_establishes_decode_or_station_qualification(
    reception, rms, expected
):
    service, receiver, clock = reception
    receiver.state = "listening"
    service.record_audio(rms)
    health = receiver.health()
    assert health["pipeline_state"] == "running"
    assert health["audio_state"] == "fresh"
    assert health["signal_state"] == expected
    assert health["decode_state"] == "never"
    assert health["signal_quality_verified"] is False
    assert health["last_verified_decode"] is None
    clock.advance(service.config.audio_stall_seconds)
    assert receiver.health()["audio_state"] == "stale"
    assert receiver.health()["signal_state"] == "unknown"


async def test_recent_noise_does_not_hide_a_subsequent_weak_audio_buffer(reception):
    service, receiver, _ = reception
    service.record_audio(1000)
    service.record_audio(5)
    assert receiver.health()["signal_state"] == "below_threshold"
    service.config.signal_rms_threshold = 0
    service.record_audio(0)
    assert receiver.health()["signal_state"] == "below_threshold"


async def test_ingested_fixture_and_end_marker_do_not_qualify_a_receiver(reception):
    service, receiver, _ = reception
    await service.ingest(TEST_HEADER)
    service.record_signal()
    health = receiver.health()
    assert health["last_decode_at"] is not None
    assert health["decode_state"] == "never"
    assert health["audio_state"] == "never"
    assert health["signal_state"] == "unknown"
    with pytest.raises(ValueError, match="invalid receiver"):
        await service.ingest(TEST_HEADER + "trailing noise", from_receiver=True)
    assert receiver.health()["last_verified_decode"] is None


async def test_decode_freshness_uses_elapsed_time_and_invalidates_changed_receiver(reception):
    service, receiver, clock = reception
    await service.ingest(TEST_HEADER, from_receiver=True)
    health = receiver.health()
    assert health["decode_state"] == "fresh"
    assert health["last_verified_decode"]["is_test"] is True
    assert health["last_verified_decode"]["relevant"] is True
    clock.epoch += timedelta(days=20)
    assert receiver.health()["decode_age_seconds"] == 0
    service.config.ppm = 10
    assert receiver.health()["decode_state"] == "configuration_changed"
    service.config.ppm = 0
    clock.advance(service.config.decode_stale_hours * 3600)
    assert receiver.health()["decode_state"] == "stale"


@pytest.mark.parametrize("kind", ["expired", "future"])
async def test_old_or_future_headers_remain_visible_without_current_reception_claim(
    reception, kind
):
    service, receiver, clock = reception
    if kind == "expired":
        clock.advance(3600)
        header = TEST_HEADER
    else:
        header = TEST_HEADER.replace("0010000", "0020000")
    await service.ingest(header, from_receiver=True)
    health = receiver.health()
    assert health["decode_state"] == "message_not_current"
    assert health["last_verified_decode"]["message_current"] is False


@pytest.mark.parametrize("elapsed", [-1, math.nan, math.inf])
async def test_invalid_elapsed_time_never_reports_fresh_decode(reception, elapsed):
    service, receiver, clock = reception
    await service.ingest(TEST_HEADER, from_receiver=True)
    clock.value = elapsed
    evidence = service.reception_evidence()
    assert evidence["decode_state"] == "clock_uncertain"
    assert evidence["decode_age_seconds"] is None
    clock.value = 0


async def test_receiver_restart_clears_audio_and_marks_last_decode_historical(reception):
    service, receiver, _ = reception
    service.record_audio(1000)
    await service.ingest(TEST_HEADER, from_receiver=True)
    service.reset_pipeline_evidence()
    health = receiver.health()
    assert health["decode_state"] == "prior_pipeline"
    assert health["audio_state"] == "never"
    assert health["signal_state"] == "unknown"
    assert health["last_verified_decode"]["is_test"] is True
    await service.ingest(TEST_HEADER, from_receiver=True)
    assert receiver.health()["decode_state"] == "fresh"
    assert len(await service.database.read("SELECT * FROM same_event")) == 1
    # A complete service restart keeps records but requires fresh reception evidence.
    restarted = SameService(service.database, service.clock, service.config)
    assert restarted.health()["decode_state"] == "never"
    assert len(await restarted.list()) == 1


async def test_reception_with_untrusted_utc_cannot_later_claim_verified_timing(
    reception, monkeypatch
):
    service, receiver, clock = reception
    original = clock.time_status
    monkeypatch.setattr(clock, "time_status", lambda: TimeStatus("uncertain", "no_source", False))
    await service.ingest(TEST_HEADER, from_receiver=True)
    assert receiver.health()["decode_state"] == "clock_uncertain"
    monkeypatch.setattr(clock, "time_status", original)
    assert receiver.health()["decode_state"] == "clock_uncertain"
    await service.ingest(TEST_HEADER, from_receiver=True)
    assert receiver.health()["decode_state"] == "fresh"


def test_receiver_inventory_without_event_loop_stays_unverified():
    service = SameService(None, SystemClock(), SameConfig())  # type: ignore[arg-type]
    assert service.health()["decode_state"] == "never"
    assert service.health()["audio_state"] == "never"


@pytest.mark.parametrize("failed_component", ["rtl_fm", "samedec"])
async def test_repeated_process_loss_recovers_without_duplicate_actionable_warnings(
    tmp_path, failed_component
):
    marker = tmp_path / "decoder-starts"
    rtl = (
        "import pathlib, sys, time\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "count = int(marker.read_text()) + 1 if marker.exists() else 1\n"
        "marker.write_text(str(count))\n"
        "started = time.monotonic()\n"
        "while True:\n"
        " sys.stdout.buffer.write(b'\\x00\\x04' * 4096)\n"
        " sys.stdout.buffer.flush()\n"
        " time.sleep(0.02)\n"
        f" if {failed_component == 'rtl_fm'!r} and count <= 3 "
        "and time.monotonic() - started > 0.2: raise SystemExit(8)\n"
    )
    decoder = (
        "import pathlib, sys, time\n"
        "sys.stdin.buffer.read(2048)\n"
        f"count = int(pathlib.Path({str(marker)!r}).read_text())\n"
        "print('ZCZC-broken', flush=True)\n"
        f"print({TEST_HEADER!r}, flush=True)\n"
        f"print({LIVE_HEADER!r}, flush=True)\n"
        "time.sleep(0.05)\n"
        f"if {failed_component == 'samedec'!r} and count <= 3: raise SystemExit(7)\n"
        "while sys.stdin.buffer.read(2048): pass\n"
    )
    config = receiver_config(tmp_path, rtl, decoder)
    clock = YieldClock()
    database = Database(tmp_path / "receiver.db")
    await database.open()
    service = SameService(database, clock, config)
    receiver = SameReceiver(service, config, clock)
    recovered = asyncio.Event()

    def progress():
        health = receiver.health()
        if (
            receiver.restart_count == 3
            and receiver.state == "listening"
            and health["decode_state"] == "fresh"
            and health["last_verified_decode"]["event_code"] == "TOR"
        ):
            recovered.set()

    receiver._on_progress = progress
    alerts, governor = alert_service(database, clock)
    task = asyncio.create_task(receiver.run())
    try:
        await asyncio.wait_for(recovered.wait(), timeout=15)
        rows = await service.list()
        assert len(rows) == 2
        drill = next(row for row in rows if row["is_test"])
        warning = next(row for row in rows if not row["is_test"])
        assert drill["review_state"] == "logged"
        assert warning["review_state"] == "pending"
        assert not await database.read("SELECT * FROM alert")
        assert governor.queued_items() == []
        assert not await database.read("SELECT * FROM outbound_work")
        with pytest.raises(ValueError, match="not eligible"):
            await service.approve(drill["id"], alerts)
        approved = await service.approve(warning["id"], alerts)
        await service.ingest(LIVE_HEADER, from_receiver=True)
        assert len(await database.read("SELECT * FROM alert")) == 1
        assert (
            next(row for row in await service.list() if not row["is_test"])["linked_alert_id"]
            == approved["id"]
        )
        assert receiver.last_exit_code is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert receiver._rtl is None and receiver._decoder is None
        await database.close()
