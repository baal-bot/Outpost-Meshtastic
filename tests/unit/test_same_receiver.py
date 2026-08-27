from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from outpost.clock import VirtualClock
from outpost.config import SameConfig
from outpost.env import SameReceiver, SameReceiverError, SameService
from outpost.store import Database


class YieldClock(VirtualClock):
    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)


def executable(path: Path, source: str) -> str:
    path.write_text(f"#!/usr/bin/env python3\n{source}")
    path.chmod(0o755)
    return str(path)


def receiver_config(tmp_path: Path, rtl_source: str, decoder_source: str) -> SameConfig:
    return SameConfig(
        enabled=True,
        county_codes=["042003"],
        device="test-serial",
        rtl_fm_path=executable(tmp_path / "rtl_fm", rtl_source),
        samedec_path=executable(tmp_path / "samedec", decoder_source),
    )


def test_receiver_builds_validated_commands_without_a_shell(tmp_path: Path) -> None:
    config = receiver_config(tmp_path, "", "")
    receiver = SameReceiver(None, config, VirtualClock())  # type: ignore[arg-type]

    rtl, decoder = receiver.commands()

    assert rtl == [
        config.rtl_fm_path,
        "-d",
        "test-serial",
        "-f",
        "162.550M",
        "-M",
        "fm",
        "-s",
        "48000",
        "-o",
        "4",
        "-E",
        "dc",
        "-p",
        "0",
        "-F",
        "9",
        "-",
    ]
    assert decoder == [config.samedec_path, "--rate", "48000"]


@pytest.mark.asyncio
async def test_receiver_pipes_audio_and_ingests_decoder_output(tmp_path: Path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    rtl_source = (
        "import sys, time\n"
        "sys.stdout.buffer.write((1000).to_bytes(2, 'little', signed=True) * 4096)\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(2)\n"
    )
    decoder_source = (
        "import sys, time\n"
        "sys.stdin.buffer.read(2048)\n"
        "print('ZCZC-WXR-TOR-042003+0130-0010000-KPBZ/NWS-', flush=True)\n"
        "time.sleep(0.2)\n"
        "raise SystemExit(7)\n"
    )
    config = receiver_config(tmp_path, rtl_source, decoder_source)
    service = SameService(database, VirtualClock(), config)
    receiver = SameReceiver(service, config, VirtualClock())

    with pytest.raises(SameReceiverError, match="samedec exited with status 7"):
        await receiver._run_once()

    items = await service.list()
    assert len(items) == 1
    assert items[0]["event_code"] == "TOR"
    assert items[0]["review_state"] == "pending"
    assert service.health()["last_audio_at"] is not None
    await database.close()


@pytest.mark.asyncio
async def test_receiver_restarts_with_bounded_backoff_and_stops_cleanly(
    tmp_path: Path, monkeypatch
) -> None:
    config = receiver_config(tmp_path, "", "")
    clock = YieldClock()
    service = SimpleNamespace(start_monitoring=lambda: None)
    receiver = SameReceiver(service, config, clock)  # type: ignore[arg-type]
    attempts = 0

    async def fail() -> None:
        nonlocal attempts
        attempts += 1
        raise SameReceiverError("SDR unplugged")

    monkeypatch.setattr(receiver, "_run_once", fail)
    task = asyncio.create_task(receiver.run())
    for _ in range(20):
        if receiver.restart_count >= 4:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert attempts == receiver.restart_count == 4
    assert clock.value >= 2 + 4 + 8
    assert receiver.state == "stopped"
    assert receiver.last_error == "SDR unplugged"


@pytest.mark.asyncio
async def test_audio_watchdog_detects_a_hung_usb_stream(tmp_path: Path) -> None:
    config = receiver_config(tmp_path, "", "").model_copy(update={"audio_stall_seconds": 5})
    receiver = SameReceiver(None, config, VirtualClock())  # type: ignore[arg-type]

    with pytest.raises(SameReceiverError, match="produced no audio"):
        await receiver._watch_audio_stall()


def test_receiver_reports_missing_executable(tmp_path: Path) -> None:
    config = SameConfig(
        enabled=True,
        county_codes=["042003"],
        rtl_fm_path=str(tmp_path / "missing-rtl-fm"),
    )
    receiver = SameReceiver(None, config, VirtualClock())  # type: ignore[arg-type]
    with pytest.raises(SameReceiverError, match="executable is unavailable"):
        receiver.commands()
