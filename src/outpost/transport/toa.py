from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    spreading_factor: int
    bandwidth_hz: int
    coding_rate: int = 1  # LoRa denominator offset: 1 means 4/5, 4 means 4/8
    preamble_symbols: int = 16


PRESETS: dict[str, Preset] = {
    "LONG_FAST": Preset(11, 250_000),
    "LONG_SLOW": Preset(12, 125_000, 4),
    # Retained for radios running firmware old enough to report this deprecated preset.
    # It is also the safe fallback because it has the highest modeled airtime.
    "VERY_LONG_SLOW": Preset(12, 62_500, 4),
    "MEDIUM_SLOW": Preset(10, 250_000),
    "MEDIUM_FAST": Preset(9, 250_000),
    "SHORT_SLOW": Preset(8, 250_000),
    "SHORT_FAST": Preset(7, 250_000),
    "LONG_MODERATE": Preset(11, 125_000, 4),
    "SHORT_TURBO": Preset(7, 500_000),
    "LONG_TURBO": Preset(11, 500_000, 4),
    "LITE_FAST": Preset(9, 125_000),
    "LITE_SLOW": Preset(10, 125_000),
    "NARROW_FAST": Preset(7, 62_500, 2),
    "NARROW_SLOW": Preset(8, 62_500, 2),
    "TINY_FAST": Preset(7, 15_600),
    "TINY_SLOW": Preset(8, 15_600, 2),
    "MEDIUM_TURBO": Preset(9, 500_000),
}
CONSERVATIVE_PRESET = "VERY_LONG_SLOW"
# Meshtastic's protobuf array can hold 233 bytes, but current firmware adds a
# present Data.bitfield before the 16-byte radio header. A 231-byte application
# payload is the largest value that also fits private portnums in the 255-byte PHY frame.
MAX_PAYLOAD_BYTES = 231
MESHTASTIC_HEADER_BYTES = 16
MESHTASTIC_DATA_BITFIELD_BYTES = 2


def _varint_bytes(value: int) -> int:
    if value < 0:
        raise ValueError("portnum must be non-negative")
    return max(1, (value.bit_length() + 6) // 7)


def mesh_packet_bytes(payload_bytes: int, portnum: int = 1) -> int:
    """Return the encrypted Data protobuf plus Meshtastic's over-air header."""
    # Data.portnum: one-byte field tag + varint enum.
    # Data.payload: one-byte field tag + varint length + application bytes.
    # Current firmware also marks Data.bitfield present (one-byte tag and value)
    # while normalizing packets received from a client API.
    return (
        MESHTASTIC_HEADER_BYTES
        + MESHTASTIC_DATA_BITFIELD_BYTES
        + 1
        + _varint_bytes(portnum)
        + 1
        + _varint_bytes(payload_bytes)
        + payload_bytes
    )


def resolve_preset(preset: str, *, wide_lora: bool = False) -> tuple[str, Preset, bool]:
    """Resolve a reported preset, conservatively containing unknown future values."""
    normalized = preset.strip().upper()
    supported = normalized in PRESETS
    resolved = normalized if supported else ("LONG_SLOW" if wide_lora else CONSERVATIVE_PRESET)
    config = PRESETS[resolved]
    if not wide_lora:
        return resolved, config, supported
    if resolved in {"SHORT_TURBO", "LONG_TURBO", "MEDIUM_TURBO"}:
        bandwidth_hz = 1_625_000
    elif resolved in {"LONG_MODERATE", "LONG_SLOW"}:
        bandwidth_hz = 406_250
    elif resolved in {
        "SHORT_FAST",
        "SHORT_SLOW",
        "MEDIUM_FAST",
        "MEDIUM_SLOW",
        "LONG_FAST",
        CONSERVATIVE_PRESET,
    }:
        bandwidth_hz = 812_500
    else:
        return resolved, config, supported
    return (
        resolved,
        Preset(
            config.spreading_factor,
            bandwidth_hz,
            config.coding_rate,
            12,
        ),
        supported,
    )


def toa(
    payload_bytes: int,
    preset: str | Preset = "LONG_FAST",
    *,
    portnum: int = 1,
    wide_lora: bool = False,
) -> float:
    """Meshtastic packet time-on-air with explicit LoRa header and CRC."""
    if payload_bytes < 0 or payload_bytes > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload_bytes must be in 0..{MAX_PAYLOAD_BYTES}")
    cfg = resolve_preset(preset, wide_lora=wide_lora)[1] if isinstance(preset, str) else preset
    return lora_packet_toa(mesh_packet_bytes(payload_bytes, portnum), cfg)


def lora_packet_toa(packet_bytes: int, preset: str | Preset = "LONG_FAST") -> float:
    """Calculate ToA from the complete encrypted packet length reported by firmware."""
    if packet_bytes < 0 or packet_bytes > 255:
        raise ValueError("packet_bytes must be in 0..255")
    cfg = PRESETS[preset] if isinstance(preset, str) else preset
    sf, bw = cfg.spreading_factor, cfg.bandwidth_hz
    symbol = (2**sf) / bw
    low_data_rate = 1 if symbol >= 0.016 else 0
    numerator = 8 * packet_bytes - 4 * sf + 28 + 16  # explicit header, CRC enabled
    denominator = 4 * (sf - 2 * low_data_rate)
    payload_symbols = 8 + max(math.ceil(numerator / denominator) * (cfg.coding_rate + 4), 0)
    return float((cfg.preamble_symbols + 4.25 + payload_symbols) * symbol)
