#!/usr/bin/env python3
"""Observe a receive-only SDR pipeline or require an actual decoded broadcast test."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from outpost.clock import SystemClock
from outpost.config import SameConfig
from outpost.env import SameReceiver, SameService
from outpost.store import Database


async def check(args: argparse.Namespace) -> None:
    clock = SystemClock()
    config = SameConfig(
        enabled=True,
        frequency_mhz=args.frequency,
        county_codes=[args.county],
        device=args.device,
        signal_rms_threshold=args.signal_threshold,
    )
    with tempfile.TemporaryDirectory(prefix="outpost-same-") as temporary:
        database = Database(Path(temporary) / "acceptance.db")
        await database.open()
        service = SameService(database, clock, config)
        receiver = SameReceiver(service, config, clock)
        task = asyncio.create_task(receiver.run())
        deadline = clock.monotonic() + args.timeout
        first_listening = False
        try:
            while clock.monotonic() < deadline:
                health = receiver.health()
                if (
                    health["pipeline_state"] == "running"
                    and health["audio_state"] == "fresh"
                    and health["signal_state"] == "above_threshold"
                ):
                    if not first_listening:
                        first_listening = True
                        print(
                            f"SAME receiver listening at {args.frequency:.3f} MHz "
                            f"on SDR {args.device}; audio above threshold, RF quality unverified",
                            flush=True,
                        )
                    decode = health["last_verified_decode"]
                    test_received = (
                        health["decode_state"] == "fresh"
                        and decode is not None
                        and decode["is_test"]
                        and decode["relevant"]
                    )
                    if (not args.require_restart or receiver.restart_count > 0) and (
                        not args.require_test_decode or test_received
                    ):
                        print(
                            (
                                "SAME test decoded in the current pipeline; "
                                "retain the station/antenna qualification witness"
                                if args.require_test_decode
                                else "SDR pipeline check passed; station/test decode unqualified"
                            )
                            + f"; restarts={receiver.restart_count}",
                            flush=True,
                        )
                        print(
                            json.dumps(
                                {
                                    "observed_at": int(clock.now().timestamp()),
                                    "pipeline_state": health["pipeline_state"],
                                    "audio_state": health["audio_state"],
                                    "decode_state": health["decode_state"],
                                    "last_verified_decode": decode,
                                    "receiver_restarts": receiver.restart_count,
                                    "station_qualification_verified": False,
                                },
                                sort_keys=True,
                            ),
                            flush=True,
                        )
                        return
                await asyncio.sleep(0.25)
            raise SystemExit(
                "SAME observation timed out without the requested evidence: "
                f"state={receiver.state}, restarts={receiver.restart_count}, "
                f"error={receiver.last_error!r}"
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True, help="RTL-SDR serial or index")
    parser.add_argument("--frequency", type=float, default=162.55)
    parser.add_argument("--county", default="000000")
    parser.add_argument("--signal-threshold", type=int, default=300)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--require-test-decode",
        action="store_true",
        help="Require a current, relevant RWT/RMT/NPT/DMO header from the receiver",
    )
    parser.add_argument(
        "--require-restart",
        action="store_true",
        help="Wait for a decoder restart, for use while intentionally resetting the SDR",
    )
    args = parser.parse_args()
    if not 1 <= args.timeout <= 86_400:
        parser.error("--timeout must be between 1 and 86400 seconds")
    asyncio.run(check(args))


if __name__ == "__main__":
    main()
