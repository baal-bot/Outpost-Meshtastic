#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from outpost.config import Config


def ask(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure a new Outpost installation")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    data: dict[str, Any] = yaml.safe_load(args.config.read_text()) or {}
    node, radio = data["node"], data["radio"]
    print("\nOutpost first-run setup. Press Enter to accept each default.\n")
    node["name"] = ask("Outpost name", str(node["name"]))
    node["short_name"] = ask("Radio short name (1-4 bytes)", str(node["short_name"]))
    node["operator_contact"] = ask("Operator contact", str(node["operator_contact"]))
    timezone = (
        Path("/etc/timezone").read_text().strip()
        if Path("/etc/timezone").exists()
        else str(node["timezone"])
    )
    node["timezone"] = ask("Timezone", timezone)
    node["units"] = ask("Units (metric/imperial)", str(node["units"])).lower()
    candidates = (
        sorted(Path("/dev/serial/by-id").glob("*")) if Path("/dev/serial/by-id").exists() else []
    )
    detected = str(candidates[0]) if len(candidates) == 1 else str(radio["serial"]["port"])
    radio["transport"] = ask("Radio transport (serial/tcp/ble)", str(radio["transport"])).lower()
    if radio["transport"] == "serial":
        radio["serial"]["port"] = ask("Radio serial device", detected)
    lat = input("Latitude (optional): ").strip()
    lon = input("Longitude (optional): ").strip()
    if lat or lon:
        if not lat or not lon:
            raise SystemExit("Both latitude and longitude are required when setting a location")
        node["location"] = {"lat": float(lat), "lon": float(lon)}
    Config.model_validate(data)
    temporary = args.config.with_suffix(".yaml.new")
    temporary.write_text(yaml.safe_dump(data, sort_keys=False))
    temporary.replace(args.config)
    print(f"\nValidated configuration written to {args.config}")


if __name__ == "__main__":
    main()
