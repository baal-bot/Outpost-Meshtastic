from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from outpost.config import load_config
from outpost.store import Database
from outpost.web.auth import WebAuthService


def _default_config() -> Path:
    configured = os.getenv("OUTPOST_CONFIG")
    if configured:
        return Path(configured)
    installed = Path("/etc/outpost/config.yaml")
    return installed if installed.exists() else Path("config/config.yaml")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the short-lived Outpost dashboard setup token."
    )
    parser.add_argument("action", choices=("show", "reset", "status"))
    parser.add_argument("--config", type=Path, default=_default_config())
    return parser


async def _run(action: str, config_path: Path) -> int:
    config = load_config(config_path)
    database_path = Path(config.store.path)
    if not await asyncio.to_thread(database_path.is_file):
        print("Outpost database not found. Start the service once before managing setup tokens.")
        return 1
    database = Database(database_path)
    await database.open()
    auth = WebAuthService(database, config.web.auth.session_hours)
    try:
        if action == "reset":
            setup = await auth.issue_setup_secret()
            print(
                "Issued a new one-time setup token; all dashboard sessions were invalidated.\n"
                f"It expires in {auth.setup_ttl_seconds // 60} minutes. "
                "Run: sudo outpost-setup-token show"
            )
            assert setup.path.is_file()
            return 0
        status = await auth.setup_status()
        if action == "status":
            if not status["required"]:
                print("Dashboard setup is complete; no setup token is active.")
                return 0
            if status["available"]:
                print(f"A one-time setup token is active at {auth.setup_path}.")
                return 0
            print("Dashboard setup is incomplete, but no active token remains. Run reset.")
            return 1
        if not status["available"]:
            print("No active setup token. Run: sudo outpost-setup-token reset")
            return 1
        print(auth.setup_path.read_text(encoding="utf-8").strip())
        return 0
    finally:
        await database.close()


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("local root access is required; run with sudo")
    raise SystemExit(asyncio.run(_run(args.action, args.config)))


if __name__ == "__main__":
    main()
