#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from outpost.config import Config
from outpost.onboarding import record_step


def ask(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def detect_rtl_sdr() -> list[str]:
    devices: list[str] = []
    for vendor_path in sorted(Path("/sys/bus/usb/devices").glob("*/idVendor")):
        product_path = vendor_path.with_name("idProduct")
        if not product_path.exists():
            continue
        if vendor_path.read_text().strip().lower() != "0bda":
            continue
        if product_path.read_text().strip().lower() not in {"2832", "2838"}:
            continue
        serial_path = vendor_path.with_name("serial")
        devices.append(serial_path.read_text().strip() if serial_path.exists() else "0")
    return devices


def detect_hailo10h() -> bool:
    for vendor_path in sorted(Path("/sys/bus/pci/devices").glob("*/vendor")):
        device_path = vendor_path.with_name("device")
        if not device_path.exists():
            continue
        if vendor_path.read_text().strip().lower() == "0x1e60" and (
            device_path.read_text().strip().lower() == "0x45c4"
        ):
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure a new Outpost installation")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    data: dict[str, Any] = yaml.safe_load(args.config.read_text()) or {}
    node, radio = data["node"], data["radio"]
    print("\nOutpost first-run setup. Press Enter to accept each default.\n")
    print(
        "Web access defaults to operator-only trusted local HTTP; community access remains "
        "on the mesh. Optional HTTPS/proxy modes can be configured after setup.\n"
    )
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
    rtl_devices = detect_rtl_sdr()
    if rtl_devices:
        same = data["env"]["same"]
        enabled = ask("Enable detected RTL-SDR weather warning receiver (yes/no)", "no")
        if enabled.lower() in {"y", "yes"}:
            same["enabled"] = True
            same["device"] = ask("RTL-SDR serial", rtl_devices[0])
            same["frequency_mhz"] = float(
                ask("NOAA Weather Radio frequency MHz", str(same["frequency_mhz"]))
            )
            county = ask("Six-digit SAME county code", "").strip()
            if not county:
                raise SystemExit("A SAME county code is required when the receiver is enabled")
            same["county_codes"] = [value.strip() for value in county.split(",") if value.strip()]
    if detect_hailo10h():
        enabled = ask("Enable detected Hailo-10H local AI provider (yes/no)", "no")
        if enabled.lower() in {"y", "yes"}:
            data["modules"]["ai"]["enabled"] = True
            data["ai"]["provider"] = "hailo_vlm"
            data["ai"]["model"] = "Qwen3-VL-2B-Instruct"
    Config.model_validate(data)
    temporary = args.config.with_suffix(".yaml.new")
    temporary.write_text(yaml.safe_dump(data, sort_keys=False))
    temporary.replace(args.config)
    if args.state is not None:
        record_step(args.state, "identity_location", "completed")
    print(f"\nValidated configuration written to {args.config}")


if __name__ == "__main__":
    main()
