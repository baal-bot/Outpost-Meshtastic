"""Tests supply OS time evidence explicitly instead of relying on the runner's NTP."""

import pytest

from outpost import timekeeping


@pytest.fixture(autouse=True)
def synthetic_os_time(monkeypatch):
    monkeypatch.setattr(
        timekeeping,
        "kernel_time",
        lambda: timekeeping.TimeSource(True, 0.01, "synthetic_os_synchronization"),
    )
