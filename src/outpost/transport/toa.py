from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    spreading_factor: int
    bandwidth_hz: int
    coding_rate: int = 1  # LoRa denominator offset: 1 means 4/5
    preamble_symbols: int = 16


PRESETS = {
    "LONG_FAST": Preset(11, 250_000),
    "MEDIUM_SLOW": Preset(10, 250_000),
    "SHORT_FAST": Preset(7, 250_000),
}
MAX_PAYLOAD_BYTES = 233


def toa(payload_bytes: int, preset: str | Preset = "LONG_FAST") -> float:
    """Semtech LoRa time-on-air with explicit header and CRC (REQ-TRANSPORT-001)."""
    if payload_bytes < 0 or payload_bytes > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload_bytes must be in 0..{MAX_PAYLOAD_BYTES}")
    cfg = PRESETS[preset] if isinstance(preset, str) else preset
    sf, bw = cfg.spreading_factor, cfg.bandwidth_hz
    symbol = (2**sf) / bw
    low_data_rate = 1 if symbol >= 0.016 else 0
    numerator = 8 * payload_bytes - 4 * sf + 28 + 16  # explicit header, CRC enabled
    denominator = 4 * (sf - 2 * low_data_rate)
    payload_symbols = 8 + max(math.ceil(numerator / denominator) * (cfg.coding_rate + 4), 0)
    return float((cfg.preamble_symbols + 4.25 + payload_symbols) * symbol)
