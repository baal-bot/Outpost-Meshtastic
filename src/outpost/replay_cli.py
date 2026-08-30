from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from outpost.config import load_config
from outpost.replay import (
    ReplayCorpus,
    ReplayError,
    ReplayHarness,
    ReplaySelection,
    load_corpus,
    provision_drill_operator,
    redacted_bundle,
    write_private_json,
)


def _default_config() -> Path:
    configured = os.getenv("OUTPOST_CONFIG")
    if configured:
        return Path(configured)
    installed = Path("/etc/outpost/config.yaml")
    try:
        use_installed = installed.is_file()
    except OSError:
        use_installed = False
    return installed if use_installed else Path("config/config.yaml")


def _timestamp(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("use a Unix timestamp or ISO-8601 instant") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _selection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start-id", type=int, help="First message_log id to include")
    parser.add_argument("--end-id", type=int, help="Last message_log id to include")
    parser.add_argument("--since", type=_timestamp, help="Earliest Unix or ISO-8601 timestamp")
    parser.add_argument("--until", type=_timestamp, help="Latest Unix or ISO-8601 timestamp")
    parser.add_argument(
        "--limit",
        type=int,
        default=1_000,
        help="Maximum inbound records, taking the newest matches (default: 1000)",
    )


def _simulation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=_default_config())
    parser.add_argument("--scratch-db", type=Path, help="Keep scratch state at a new path")
    parser.add_argument("--preset", default="LONG_FAST", help="Modem preset used for airtime")
    parser.add_argument("--region", default="US", help="Radio region used for airtime policy")
    parser.add_argument(
        "--allow-provider-access",
        action="store_true",
        help="Permit configured AI/environment providers to run during replay",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay retained mesh traffic without touching the live store or radio."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Replay into an ephemeral scratch database")
    run.add_argument("source", type=Path, help="Outpost SQLite database or replay JSON bundle")
    _selection_arguments(run)
    _simulation_arguments(run)
    run.add_argument("--output", type=Path, help="Write the deterministic result as JSON")
    run.add_argument("--force", action="store_true", help="Replace an existing result file")

    export = commands.add_parser("export", help="Create a shareable, redacted replay bundle")
    export.add_argument("source", type=Path, help="Outpost SQLite database")
    export.add_argument("--output", type=Path, required=True)
    _selection_arguments(export)
    export.add_argument(
        "--strip-bodies",
        action="store_true",
        help="Remove message text as well as binary payloads",
    )
    export.add_argument(
        "--coarsen-meters",
        type=int,
        default=1_000,
        help="Approximate position grid size (minimum: 100, default: 1000)",
    )
    export.add_argument("--force", action="store_true", help="Replace an existing bundle")

    drill = commands.add_parser("drill", help="Replay into an isolated operator dashboard")
    drill.add_argument("source", type=Path, help="Outpost SQLite database or replay JSON bundle")
    _selection_arguments(drill)
    _simulation_arguments(drill)
    drill.add_argument("--bind", default="127.0.0.1")
    drill.add_argument("--port", type=int, default=8081)
    drill.add_argument(
        "--speed",
        type=float,
        default=60.0,
        help="Replay time compression; 60 means one hour per minute (default: 60)",
    )
    drill.add_argument(
        "--allow-remote",
        action="store_true",
        help="Acknowledge that the drill dashboard will bind beyond loopback",
    )
    drill.add_argument("--output", type=Path, help="Write results when the drill stops")
    drill.add_argument("--force", action="store_true", help="Replace an existing result file")
    return parser


def _selection(args: argparse.Namespace) -> ReplaySelection:
    return ReplaySelection(
        start_id=args.start_id,
        end_id=args.end_id,
        since=args.since,
        until=args.until,
        limit=args.limit,
    )


async def _load(path: Path, selection: ReplaySelection) -> ReplayCorpus:
    return await asyncio.to_thread(load_corpus, path, selection)


def _scratch_path(args: argparse.Namespace, temporary: str) -> Path:
    return args.scratch_db or Path(temporary) / "outpost-replay.db"


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)


def _validate_destination_paths(args: argparse.Namespace) -> None:
    source: Path = args.source
    output: Path | None = getattr(args, "output", None)
    scratch: Path | None = getattr(args, "scratch_db", None)
    if output is not None and _same_path(source, output):
        raise ReplayError("output must not replace the replay source")
    if scratch is not None and _same_path(source, scratch):
        raise ReplayError("scratch database must not be the replay source")
    if output is not None and scratch is not None and _same_path(output, scratch):
        raise ReplayError("output and scratch database must use different paths")


async def _run_batch(args: argparse.Namespace) -> int:
    _validate_destination_paths(args)
    corpus = await _load(args.source, _selection(args))
    with tempfile.TemporaryDirectory(prefix="outpost-replay-") as temporary:
        harness = ReplayHarness(
            load_config(args.config),
            corpus,
            _scratch_path(args, temporary),
            preset=args.preset,
            region=args.region,
            allow_providers=args.allow_provider_access,
        )
        try:
            report = await harness.run()
        finally:
            await harness.close()
    if args.output:
        write_private_json(args.output, report, overwrite=args.force)
        print(f"Replay result written to {args.output}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


async def _run_export(args: argparse.Namespace) -> int:
    _validate_destination_paths(args)
    corpus = await _load(args.source, _selection(args))
    bundle = redacted_bundle(
        corpus,
        strip_bodies=args.strip_bodies,
        coarsen_meters=args.coarsen_meters,
    )
    write_private_json(args.output, bundle, overwrite=args.force)
    print(
        f"Redacted replay bundle written to {args.output} ({len(corpus.records)} inbound messages)"
    )
    return 0


async def _paced_feed(harness: ReplayHarness, corpus: ReplayCorpus, speed: float) -> None:
    prior = corpus.records[0].created_at if corpus.records else 0
    for record in corpus.records:
        delay = max(0.0, record.created_at - prior) / speed
        if delay:
            await asyncio.sleep(delay)
        await harness.process(record)
        print(
            f"Drill replay {record.source_id}: "
            f"{harness.processed_count}/{len(corpus.records)} processed",
            flush=True,
        )
        prior = record.created_at
    print("Drill corpus complete; dashboard remains available until stopped.", flush=True)


async def _run_drill(args: argparse.Namespace) -> int:
    if not 1 <= args.port <= 65_535:
        raise ReplayError("drill port must be 1-65535")
    if args.speed <= 0 or args.speed > 86_400:
        raise ReplayError("drill speed must be greater than 0 and at most 86400")
    loopback = args.bind in {"127.0.0.1", "::1", "localhost"}
    if not loopback and not args.allow_remote:
        raise ReplayError("non-loopback drill binding requires --allow-remote")
    _validate_destination_paths(args)
    corpus = await _load(args.source, _selection(args))
    with tempfile.TemporaryDirectory(prefix="outpost-drill-") as temporary:
        harness = ReplayHarness(
            load_config(args.config),
            corpus,
            _scratch_path(args, temporary),
            mode="drill",
            preset=args.preset,
            region=args.region,
            allow_providers=args.allow_provider_access,
        )
        await harness.prepare()
        password = await provision_drill_operator(harness.app)
        server = uvicorn.Server(
            uvicorn.Config(harness.app.web, host=args.bind, port=args.port, log_level="info")
        )
        print("DRILL MODE · simulated radio · scratch database · no RF/MQTT transmission")
        print(f"Dashboard: http://{args.bind}:{args.port}/")
        print("Username: drill")
        print(f"Ephemeral password: {password}")
        if not loopback:
            print("WARNING: remote drill access uses trusted local HTTP; use an isolated network.")
        feed_task = asyncio.create_task(_paced_feed(harness, corpus, args.speed))
        server_task = asyncio.create_task(server.serve())
        try:
            done, _ = await asyncio.wait(
                {feed_task, server_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if server_task in done:
                if not feed_task.done():
                    feed_task.cancel()
                await asyncio.gather(feed_task, return_exceptions=True)
            else:
                error = feed_task.exception()
                if error is not None:
                    server.should_exit = True
                    await server_task
                    raise error
                await server_task
        finally:
            if not feed_task.done():
                feed_task.cancel()
            await asyncio.gather(feed_task, return_exceptions=True)
            if args.output:
                write_private_json(args.output, harness.report(), overwrite=args.force)
            await harness.close()
    return 0


async def _run(args: argparse.Namespace) -> int:
    if args.command == "export":
        return await _run_export(args)
    if args.command == "drill":
        return await _run_drill(args)
    return await _run_batch(args)


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except ReplayError as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
