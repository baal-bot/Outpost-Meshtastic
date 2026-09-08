"""Compare NORMAL/FULL on new disposable stores; no radios, live data or power cuts."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import sqlite3
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import outpost.store.database as store_module
from outpost.bbs.mail import MailService
from outpost.clock import VirtualClock
from outpost.config import AirtimeConfig
from outpost.fed import FederationPeerService, FederationRelayService
from outpost.store import Database
from outpost.store.members import MemberRepo
from outpost.store.outbox import OutboxStore
from outpost.transport.governor import AirtimeGovernor, OutboundItem
from outpost.transport.models import TrafficClass
from outpost.transport.simulated import SimulatedRadioLink
from outpost.watch.incidents import IncidentService

KINDS = ("incident", "mail", "outbox", "signed_custody")
TABLES = {
    "incident": ("incident", "id"),
    "mail": ("mail", "id"),
    "outbox": ("outbound_work", "id"),
    "signed_custody": ("fed_relay_envelope", "envelope_id"),
}


async def identifiers(database: Database) -> dict[str, list[Any]]:
    return {
        kind: [
            row[0]
            for row in await database.read(
                f"SELECT {key} FROM {table} ORDER BY {key}"  # noqa: S608 - fixed TABLES allowlist
            )
        ]
        for kind, (table, key) in TABLES.items()
    }


async def trial(root: Path, mode: str, number: int, count: int, concurrency: int) -> dict[str, Any]:
    path = root / f"trial-{number}.db"
    database = Database(path)
    await database.open()
    clock = VirtualClock()
    radio = SimulatedRadioLink(clock)
    try:
        members = MemberRepo(database, clock)
        await members.resolve("!10000001")
        sender = await members.claim_handle("!10000001", "fixture")
        mail = MailService(database, members, clock, "synthetic")
        incidents = IncidentService(database, clock)
        governor = AirtimeGovernor(radio, AirtimeConfig(), clock, outbox=OutboxStore(database))
        peers = FederationPeerService(database, clock, "!aaaaaaaa")
        await peers.discover("!bbbbbbbb", "Synthetic producer", 1, {}, "radio")
        await database.write("UPDATE fed_peer SET state='active'")
        relay = FederationRelayService(database, peers, clock)
        await relay.initialize()
        await relay.set_policy(
            "!bbbbbbbb",
            enabled=True,
            paused=False,
            scopes=["incident"],
            max_stored_items=200,
            max_stored_bytes=131072,
            rate_per_hour=200,
            airtime_seconds_per_hour=30,
            actor="synthetic benchmark",
        )
        key = Ed25519PrivateKey.generate()
        now = int(clock.now().timestamp())
        wires = []
        for index in range(count):
            core = {
                "origin": "!bbbbbbbb",
                "destination": "!cccccccc",
                "scope": "incident",
                "idempotency_key": f"synthetic-{index}",
                "created_at": now,
                "expires_at": now + 3600,
                "hop_limit": 3,
                "payload": relay._payload_bytes("incident", {"title": f"Synthetic {index}"}),
            }
            encoded = relay._core_bytes(core)
            wires.append(
                {
                    **core,
                    "envelope_id": relay._envelope_id(encoded),
                    "origin_public_key": relay._public_bytes(key.public_key()),
                    "origin_signature": key.sign(encoded),
                    "route": ["!bbbbbbbb"],
                }
            )
        async with database.transaction() as transaction:
            default = (await transaction.read("PRAGMA synchronous"))[0][0]
        assert default == 2
        # Deliberate experiment on this new temporary writer, never a supported
        # runtime option. Reopening must restore the production FULL policy.
        await database._writer_read("PRAGMA synchronous=" + mode)
        assert (await database._writer_read("PRAGMA synchronous"))[0][0] == {
            "NORMAL": 1,
            "FULL": 2,
        }[mode]
        samples: dict[str, list[float]] = {kind: [] for kind in KINDS}
        acknowledged: dict[str, list[Any]] = {kind: [] for kind in KINDS}
        semaphore = asyncio.Semaphore(concurrency)

        async def operation(kind: str, index: int) -> None:
            async with semaphore:
                started = time.perf_counter()
                if kind == "incident":
                    incident, _ = await incidents.create(
                        f"road synthetic obstruction {index}",
                        None,
                        force=True,
                    )
                    assert incident is not None
                    acknowledged[kind].append(incident.id)
                elif kind == "mail":
                    result = await mail.send(sender, "receiver", f"Synthetic message {index}")
                    assert result is not None
                    acknowledged[kind].append(result)
                elif kind == "outbox":
                    admitted = await governor.admit(
                        OutboundItem(f"Synthetic reply {index}", "!10000001", 0, TrafficClass.REPLY)
                    )
                    assert admitted is not None
                    acknowledged[kind].append(admitted)
                else:
                    envelope_id, state = await relay.accept("!bbbbbbbb", wires[index])
                    assert state == "queued"
                    acknowledged[kind].append(envelope_id)
                samples[kind].append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        async with asyncio.TaskGroup() as tasks:
            for index in range(count):
                for kind in KINDS:
                    tasks.create_task(operation(kind, index))
        elapsed = time.perf_counter() - started
        assert not radio.sent
        before = await identifiers(database)
        assert before == {kind: sorted(values) for kind, values in acknowledged.items()}
        assert all(len(values) == count for values in before.values())
        sizes = {
            "database_bytes": path.stat().st_size,
            "wal_bytes": path.with_suffix(".db-wal").stat().st_size,
        }
    finally:
        await radio.close()
        await database.close()
    reopened = Database(path)
    await reopened.open()
    try:
        after = await identifiers(reopened)
        assert before == after
        async with reopened.transaction() as transaction:
            reopen_mode = (await transaction.read("PRAGMA synchronous"))[0][0]
        assert reopen_mode == 2
        assert (await reopened.validate_current())["integrity"] == "ok"
        assert not await reopened.read("PRAGMA foreign_key_check")
    finally:
        await reopened.close()
    return {
        "trial": number,
        "mode": mode,
        "default_writer": default,
        "reopened_writer": reopen_mode,
        "operations": count * len(KINDS),
        "concurrency": concurrency,
        "wall_seconds": elapsed,
        "acknowledged_identifiers_verified_after_reopen": True,
        "counts": {kind: len(values) for kind, values in after.items()},
        "radio_sends": 0,
        **sizes,
        "latency_ms": {
            kind: {
                "p50": statistics.median(values),
                "p95": sorted(values)[math.ceil(len(values) * 0.95) - 1],
                "max": max(values),
            }
            for kind, values in samples.items()
        },
    }


def benchmark(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "src/outpost/store/database.py"
    if Path(store_module.__file__).resolve() != source:
        raise ValueError("Use this checkout's editable development environment")
    args.output.mkdir(mode=0o700)
    git = subprocess.run(  # noqa: S603 - fixed read-only command on this checkout
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    report: dict[str, Any] = {
        "source_revision": git,
        "database_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "benchmark_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "platform": platform.machine(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "scope": "synthetic mixed domain calls on new temporary stores; no router/RF/power cut",
        "energy_wh": None,
        "power_loss_qualified": False,
        "trials": [],
    }
    with tempfile.TemporaryDirectory(prefix="stores-", dir=args.output) as scratch:
        for round_number in range(args.rounds):
            modes = ("NORMAL", "FULL") if round_number % 2 == 0 else ("FULL", "NORMAL")
            for mode in modes:
                result = asyncio.run(
                    trial(
                        Path(scratch),
                        mode,
                        len(report["trials"]) + 1,
                        args.per_kind,
                        args.concurrency,
                    )
                )
                report["trials"].append(result)
                (args.output / "measurements.json").write_text(json.dumps(report, indent=2) + "\n")
                print(json.dumps(result), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="New directory on intended filesystem"
    )
    parser.add_argument(
        "--per-kind", type=int, choices=range(1, 201), default=128, metavar="1..200"
    )
    parser.add_argument("--concurrency", type=int, choices=range(1, 17), default=8, metavar="1..16")
    parser.add_argument("--rounds", type=int, choices=range(1, 6), default=3, metavar="1..5")
    args = parser.parse_args()
    try:
        benchmark(args)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Benchmark failed: {error}\n")


if __name__ == "__main__":
    main()
