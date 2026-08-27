#!/usr/bin/env python3
"""Run a receive-only RTL-SDR/SAME hardware acceptance check."""

from __future__ import annotations

import argparse
import asyncio
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
                if receiver.state == "listening" and health["last_signal_at"] is not None:
                    if not first_listening:
                        first_listening = True
                        print(
                            f"SAME receiver listening at {args.frequency:.3f} MHz "
                            f"on SDR {args.device}; signal present",
                            flush=True,
                        )
                    if not args.require_restart or receiver.restart_count > 0:
                        print(
                            f"SAME hardware acceptance passed; restarts={receiver.restart_count}",
                            flush=True,
                        )
                        return
                await asyncio.sleep(0.25)
            raise SystemExit(
                "SAME hardware acceptance timed out: "
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
        "--require-restart",
        action="store_true",
        help="Wait for a decoder restart, for use while intentionally resetting the SDR",
    )
    asyncio.run(check(parser.parse_args()))


if __name__ == "__main__":
    main()
