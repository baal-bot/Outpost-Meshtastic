import pytest

from outpost.transport.toa import (
    CONSERVATIVE_PRESET,
    PRESETS,
    lora_packet_toa,
    mesh_packet_bytes,
    resolve_preset,
    toa,
)


def test_reference_airtimes() -> None:
    # Firmware reference: an 18-byte Data payload becomes a 40-byte radio packet.
    assert mesh_packet_bytes(18) == 40
    assert toa(18, "LONG_FAST") == pytest.approx(0.559104)
    assert toa(231, "LONG_FAST") == pytest.approx(2.156544)
    assert toa(231, "SHORT_FAST") == pytest.approx(0.203904)


def test_portnum_and_payload_varints_are_included_in_airtime() -> None:
    assert mesh_packet_bytes(127, 1) == 149
    assert mesh_packet_bytes(128, 260) == 152
    assert toa(0, portnum=260) > toa(0, portnum=1)
    with pytest.raises(ValueError, match="portnum"):
        toa(10, portnum=-1)


def test_hardware_packet_timing_vectors() -> None:
    # Heltec V4 / firmware 2.7.26 measurements captured from `Packet TX` logs.
    assert lora_packet_toa(244, "LONG_FAST") == pytest.approx(2.074, abs=0.001)
    assert lora_packet_toa(45, "SHORT_FAST") == pytest.approx(0.050, abs=0.001)
    assert lora_packet_toa(50, "SHORT_FAST") == pytest.approx(0.052, abs=0.001)


def test_every_meshtastic_preset_has_a_distinct_cost() -> None:
    costs = {name: toa(100, name) for name in PRESETS}

    assert len(costs) == 17
    assert len(set(costs.values())) == len(costs)
    assert costs["SHORT_TURBO"] < costs["SHORT_FAST"] < costs["LONG_FAST"]
    assert costs["LONG_FAST"] < costs["LONG_SLOW"] < costs["VERY_LONG_SLOW"]


def test_unknown_preset_uses_most_conservative_model() -> None:
    name, preset, supported = resolve_preset("FUTURE_ULTRA_LONG")

    assert supported is False
    assert name == CONSERVATIVE_PRESET
    assert preset == PRESETS[CONSERVATIVE_PRESET]
    assert toa(100, "FUTURE_ULTRA_LONG") == max(toa(100, name) for name in PRESETS)


def test_wide_lora_uses_firmware_bandwidth_profile() -> None:
    assert toa(100, "LONG_FAST", wide_lora=True) < toa(100, "LONG_FAST")
    name, _preset, supported = resolve_preset("FUTURE_PRESET", wide_lora=True)
    assert (name, supported) == ("LONG_SLOW", False)
