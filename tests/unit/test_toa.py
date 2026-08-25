import pytest

from outpost.transport.toa import toa


def test_reference_airtimes() -> None:
    assert toa(30, "LONG_FAST") == pytest.approx(0.48, abs=0.08)
    assert toa(233, "LONG_FAST") == pytest.approx(2.1, abs=0.25)
    assert toa(233, "SHORT_FAST") == pytest.approx(0.20, abs=0.04)
