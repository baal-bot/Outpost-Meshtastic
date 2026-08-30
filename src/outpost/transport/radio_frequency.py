from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RegionPlan:
    start_mhz: float
    end_mhz: float
    spacing_mhz: float = 0.0
    padding_mhz: float = 0.0
    presets: frozenset[str] | None = None


# Synchronized with Meshtastic firmware's RegionInfo table. Legacy enum values that are
# no longer present in that table are intentionally omitted so Outpost cannot invent a
# frequency plan for them.
_STANDARD_PRESETS = frozenset(
    {
        "LONG_FAST",
        "LONG_SLOW",
        "MEDIUM_SLOW",
        "MEDIUM_FAST",
        "SHORT_SLOW",
        "SHORT_FAST",
        "LONG_MODERATE",
        "SHORT_TURBO",
        "LONG_TURBO",
    }
)
_EU_PRESETS = _STANDARD_PRESETS - {"SHORT_TURBO", "LONG_TURBO"}
_LITE_PRESETS = frozenset({"LITE_FAST", "LITE_SLOW"})
_NARROW_PRESETS = frozenset({"NARROW_FAST", "NARROW_SLOW"})

_REGIONS = {
    "US": RegionPlan(902.0, 928.0, presets=_STANDARD_PRESETS),
    "EU_433": RegionPlan(433.0, 434.0, presets=_STANDARD_PRESETS),
    "EU_868": RegionPlan(869.4, 869.65, presets=_EU_PRESETS),
    "EU_866": RegionPlan(865.6, 867.6, spacing_mhz=0.4, padding_mhz=0.0375, presets=_LITE_PRESETS),
    "EU_N_868": RegionPlan(869.4, 869.65, padding_mhz=0.0104, presets=_NARROW_PRESETS),
    "CN": RegionPlan(470.0, 510.0, presets=_STANDARD_PRESETS),
    "JP": RegionPlan(920.5, 923.5, presets=_STANDARD_PRESETS),
    "ANZ": RegionPlan(915.0, 928.0, presets=_STANDARD_PRESETS),
    "ANZ_433": RegionPlan(433.05, 434.79, presets=_STANDARD_PRESETS),
    "RU": RegionPlan(868.7, 869.2, presets=_STANDARD_PRESETS),
    "KR": RegionPlan(920.0, 923.0, presets=_STANDARD_PRESETS),
    "TW": RegionPlan(920.0, 925.0, presets=_STANDARD_PRESETS),
    "IN": RegionPlan(865.0, 867.0, presets=_STANDARD_PRESETS),
    "NZ_865": RegionPlan(864.0, 868.0, presets=_STANDARD_PRESETS),
    "TH": RegionPlan(920.0, 925.0, presets=_STANDARD_PRESETS),
    "UA_433": RegionPlan(433.0, 434.7, presets=_STANDARD_PRESETS),
    "MY_433": RegionPlan(433.0, 435.0, presets=_STANDARD_PRESETS),
    "MY_919": RegionPlan(919.0, 924.0, presets=_STANDARD_PRESETS),
    "SG_923": RegionPlan(917.0, 925.0, presets=_STANDARD_PRESETS),
    "PH_433": RegionPlan(433.0, 434.7, presets=_STANDARD_PRESETS),
    "PH_868": RegionPlan(868.0, 869.4, presets=_STANDARD_PRESETS),
    "PH_915": RegionPlan(915.0, 918.0, presets=_STANDARD_PRESETS),
    "KZ_433": RegionPlan(433.075, 434.775, presets=_STANDARD_PRESETS),
    "KZ_863": RegionPlan(863.0, 868.0, presets=_STANDARD_PRESETS),
    "NP_865": RegionPlan(865.0, 868.0, presets=_STANDARD_PRESETS),
    "BR_902": RegionPlan(902.0, 907.5, presets=_STANDARD_PRESETS),
    "LORA_24": RegionPlan(2400.0, 2483.5, presets=_STANDARD_PRESETS),
}

_BANDWIDTH_KHZ = {
    "SHORT_TURBO": 500.0,
    "LONG_TURBO": 500.0,
    "SHORT_FAST": 250.0,
    "SHORT_SLOW": 250.0,
    "MEDIUM_FAST": 250.0,
    "MEDIUM_SLOW": 250.0,
    "LONG_FAST": 250.0,
    "LONG_MODERATE": 125.0,
    "LONG_SLOW": 125.0,
    "LITE_FAST": 125.0,
    "LITE_SLOW": 125.0,
    "NARROW_FAST": 62.5,
    "NARROW_SLOW": 62.5,
}

_DEFAULT_CHANNEL_NAMES = {
    "SHORT_TURBO": "ShortTurbo",
    "SHORT_FAST": "ShortFast",
    "SHORT_SLOW": "ShortSlow",
    "MEDIUM_FAST": "MediumFast",
    "MEDIUM_SLOW": "MediumSlow",
    "LONG_FAST": "LongFast",
    "LONG_MODERATE": "LongMod",
    "LONG_SLOW": "LongSlow",
    "LONG_TURBO": "LongTurbo",
    "LITE_FAST": "LiteFast",
    "LITE_SLOW": "LiteSlow",
    "NARROW_FAST": "NarrowFast",
    "NARROW_SLOW": "NarrowSlow",
}

_UNRESTRICTED_DUTY_REGIONS = frozenset(
    {
        "US",
        "CN",
        "JP",
        "ANZ",
        "ANZ_433",
        "RU",
        "KR",
        "TW",
        "IN",
        "NZ_865",
        "MY_433",
        "MY_919",
        "SG_923",
        "PH_433",
        "PH_868",
        "PH_915",
        "KZ_433",
        "KZ_863",
        "NP_865",
        "BR_902",
        "ITU1_2M",
        "ITU2_2M",
        "ITU3_2M",
        "ITU2_125CM",
        "ITU1_70CM",
        "ITU2_70CM",
        "ITU3_70CM",
        "LORA_24",
    }
)
_REGIONAL_DUTY_CYCLE_PERCENT = {
    **dict.fromkeys(_UNRESTRICTED_DUTY_REGIONS, 100.0),
    "EU_433": 10.0,
    "EU_868": 10.0,
    "EU_866": 2.5,
    "EU_N_868": 10.0,
    "TH": 10.0,
    "UA_433": 10.0,
    # Supported by the 2.7 firmware generation and retained for upgraded radios.
    "UA_868": 1.0,
}


def regional_duty_cycle_percent(region: str) -> float | None:
    """Return the Meshtastic firmware duty-cycle ceiling for a reported region."""
    return _REGIONAL_DUTY_CYCLE_PERCENT.get(region.strip().upper())


def _djb2(value: str) -> int:
    result = 5381
    for character in value:
        result = ((result << 5) + result + ord(character)) & 0xFFFFFFFF
    return result


def frequency_plan(
    region: str, preset: str, requested_slot: int, primary_channel_name: str = ""
) -> dict[str, object]:
    region = region.upper()
    preset = preset.upper()
    plan = _REGIONS.get(region)
    if plan is None:
        raise ValueError(
            f"Outpost has no current Meshtastic frequency plan for region {region}; "
            "leave the radio unchanged or use a matching Meshtastic client"
        )
    bandwidth = _BANDWIDTH_KHZ.get(preset)
    if bandwidth is None:
        raise ValueError(f"Outpost cannot calculate frequencies for modem preset {preset}")
    if plan.presets is not None and preset not in plan.presets:
        raise ValueError(f"modem preset {preset} is not valid for region {region}")
    step_mhz = bandwidth / 1000.0 + plan.spacing_mhz + 2 * plan.padding_mhz
    slots = round((plan.end_mhz - plan.start_mhz + plan.spacing_mhz) / step_mhz)
    if slots < 1:
        raise ValueError(f"region {region} has no usable slots for modem preset {preset}")
    if not 0 <= requested_slot <= slots:
        raise ValueError(f"frequency slot must be 0 (automatic) or 1-{slots} for {region} {preset}")
    automatic = requested_slot == 0
    channel_name = primary_channel_name.strip() or _DEFAULT_CHANNEL_NAMES[preset]
    effective_slot = _djb2(channel_name) % slots + 1 if automatic else requested_slot
    frequency_mhz = (
        plan.start_mhz + bandwidth / 2000.0 + plan.padding_mhz + (effective_slot - 1) * step_mhz
    )
    return {
        "requested_slot": requested_slot,
        "effective_slot": effective_slot,
        "slot_count": slots,
        "frequency_mhz": round(frequency_mhz, 6),
        "automatic": automatic,
        "channel_name": channel_name,
        "explanation": (
            f"Automatic uses a stable hash of primary channel name '{channel_name}' and "
            f"currently resolves to slot {effective_slot} of {slots}."
            if automatic
            else f"Explicit slot {effective_slot} of {slots}."
        ),
    }
