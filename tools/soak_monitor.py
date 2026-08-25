#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def outpost_process() -> tuple[int | None, int | None]:
    for process in Path("/proc").glob("[0-9]*"):
        try:
            command = (process / "cmdline").read_bytes()
            if b"-m\0outpost\0" not in command:
                continue
            status = (process / "status").read_text().splitlines()
            rss_line = next(line for line in status if line.startswith("VmRSS:"))
            return int(process.name), int(rss_line.split()[1])
        except (FileNotFoundError, PermissionError, StopIteration, ValueError):
            continue
    return None, None


def integrity(database: Path) -> str:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080/api/v1/status")
    parser.add_argument("--database", type=Path, default=Path(".data/outpost.db"))
    parser.add_argument("--output", type=Path, default=Path(".data/soak.jsonl"))
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--hours", type=float, default=72)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.hours * 3_600
    sample = 0
    while time.monotonic() < deadline:
        record: dict[str, object] = {"ts": datetime.now(UTC).isoformat()}
        try:
            with urllib.request.urlopen(args.url, timeout=5) as response:  # noqa: S310
                record.update(json.load(response))
            record["ok"] = True
        except Exception as error:
            record.update({"ok": False, "error": type(error).__name__})
        pid, rss_kib = outpost_process()
        record.update({"pid": pid, "rss_kib": rss_kib})
        if sample % 60 == 0 and args.database.exists():
            record["integrity"] = integrity(args.database)
        with args.output.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, separators=(",", ":")) + "\n")
        sample += 1
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
