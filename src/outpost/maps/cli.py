"""Prepare on a connected computer, then export/import through removable storage."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from pathlib import Path

from outpost.config import DEFAULT_TILES_PATH
from outpost.maps.packs import active_manifest, verify_pack
from outpost.maps.regions import MapError, Region
from outpost.maps.setup import MapSetupService


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install bounded regional maps and a world overview."
    )
    parser.add_argument("--tiles-path", type=Path, default=Path(DEFAULT_TILES_PATH))
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare", help="Plan a region; --download installs the reviewed plan"
    )
    prepare.add_argument("--latitude", type=float)
    prepare.add_argument("--longitude", type=float)
    prepare.add_argument("--radius-km", type=float, default=20)
    prepare.add_argument("--max-zoom", type=int, default=14)
    prepare.add_argument("--limit-mib", type=int, default=512)
    prepare.add_argument("--download", action="store_true")
    commands.add_parser("status")
    resume = commands.add_parser("resume")
    resume.add_argument("id")
    for action in ("import", "export"):
        command = commands.add_parser(action)
        command.add_argument("file", type=Path)
    args = parser.parse_args()
    service = MapSetupService(args.tiles_path)

    def wait() -> dict[str, object]:
        while service.status()["busy"]:
            time.sleep(0.25)
        result = service.status()["job"]
        print(json.dumps(result, indent=2))
        if result["state"] in {"failed", "paused"}:
            raise MapError(result.get("detail", "Map setup interrupted."))
        return dict(result)

    try:
        if args.command == "status":
            print(json.dumps(service.status(), indent=2))
        elif args.command == "prepare":
            if (args.latitude is None) != (args.longitude is None):
                raise MapError("Supply both latitude and longitude, or neither for overview only.")
            region = (
                None
                if args.latitude is None
                else Region(args.latitude, args.longitude, args.radius_km, args.max_zoom)
            )
            plan = service.plan(region, args.limit_mib * 1024**2)
            wait()
            if args.download:
                service.download(plan["id"])
                wait()
        elif args.command == "resume":
            service.download(args.id)
            wait()
        elif args.command == "import":
            print(json.dumps(service.import_pack(args.file), indent=2))
        elif args.command == "export":
            manifest = active_manifest(service.root)
            if manifest is None:
                raise MapError("No verified vector map pack is installed.")
            source = service.root / manifest["file"]
            verify_pack(source)
            with args.file.open("xb") as destination, source.open("rb") as stream:
                shutil.copyfileobj(stream, destination)
            print("Verified map pack exported. Import it on the offline station.")
        return 0
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Map setup failed: {error}\n")
    except KeyboardInterrupt:
        service.pause()
        parser.exit(130, "Map download paused. Resume using the saved plan ID.\n")
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
