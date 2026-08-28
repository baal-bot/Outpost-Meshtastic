#!/usr/bin/env python3
"""Select exact-commit successful CI evidence from ``gh run list`` JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")


def verified_run(payload: object, commit: str) -> dict[str, Any]:
    if not COMMIT.fullmatch(commit):
        raise ValueError("commit must be a full 40-character Git SHA")
    if not isinstance(payload, list):
        raise ValueError("GitHub CI response must be a list")
    matches = [
        item
        for item in payload
        if isinstance(item, dict) and str(item.get("headSha", "")).lower() == commit.lower()
    ]
    successful = [
        item
        for item in matches
        if item.get("status") == "completed" and item.get("conclusion") == "success"
    ]
    if not successful:
        states = sorted(
            {
                f"{item.get('status', 'unknown')}/{item.get('conclusion') or 'pending'}"
                for item in matches
            }
        )
        observed = ", ".join(states) if states else "no exact-commit runs"
        raise ValueError(f"required CI has no successful completed run ({observed})")
    return max(successful, key=lambda item: int(item.get("databaseId", 0)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True, help="full Git commit SHA")
    args = parser.parse_args()
    try:
        selected = verified_run(json.load(sys.stdin), args.commit)
    except (json.JSONDecodeError, ValueError) as error:
        print(f"CI verification failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "commit": args.commit.lower(),
                "run_id": int(selected.get("databaseId", 0)),
                "url": str(selected.get("url", "")),
                "status": "completed",
                "conclusion": "success",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
