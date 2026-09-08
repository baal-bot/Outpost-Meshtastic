"""Read-only clock evidence and elapsed-time policy for disconnected appliances.

OS synchronization is operational evidence, never RTC battery qualification.
No network request, clock adjustment, or privilege escalation occurs here.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from outpost.clock import Clock

STEP_SECONDS = 5.0
SOURCE_POLL_SECONDS = 30.0
HOLDOVER_SECONDS = 6 * 3600
DRIFT_PER_SECOND = 0.0005
MAX_ERROR_SECONDS = 30.0
MIN_EPOCH = 1_735_689_600  # 2025-01-01: conservative pre-release plausibility floor.
TIME_REMEDIATION = (
    "Time confidence is uncertain. Verify the station's UTC time and configured time source; "
    "after a clock step, restart Outpost once the OS reports synchronized time. "
    "Retained work has not been delivered by this check."
)


@dataclass(frozen=True)
class TimeSource:
    synchronized: bool = False
    error_seconds: float | None = None
    reason: str = "source_unavailable"


class _Timex(ctypes.Structure):
    # Linux LP64 ABI. Explicitly reject other ABIs before calling libc.
    _fields_ = [
        ("modes", ctypes.c_uint),
        *[(name, ctypes.c_long) for name in ("offset", "freq", "maxerror", "esterror")],
        ("status", ctypes.c_int),
        *[
            (name, ctypes.c_long)
            for name in (
                "constant",
                "precision",
                "tolerance",
                "tv_sec",
                "tv_usec",
                "tick",
                "ppsfreq",
                "jitter",
            )
        ],
        ("shift", ctypes.c_int),
        *[(name, ctypes.c_long) for name in ("stabil", "jitcnt", "calcnt", "errcnt", "stbcnt")],
        ("tai", ctypes.c_int),
        ("padding", ctypes.c_int * 11),
    ]


def kernel_time() -> TimeSource:
    """Query adjtimex with modes=0, which cannot alter kernel clock state."""
    if sys.platform != "linux" or ctypes.sizeof(ctypes.c_long) != 8:
        return TimeSource(reason="unsupported_clock_probe")
    try:
        query = ctypes.CDLL(None, use_errno=True).adjtimex
        query.argtypes = [ctypes.POINTER(_Timex)]
        query.restype = ctypes.c_int
        value = _Timex()  # All fields, especially modes, are zero.
        result = query(ctypes.byref(value))
        error = value.maxerror / 1_000_000
        good = 0 <= result < 5 and not value.status & (0x0040 | 0x1000)
        good = good and 0 <= error <= MAX_ERROR_SECONDS
        return TimeSource(
            good,
            error if error >= 0 else None,
            "kernel_clock_fault"
            if value.status & 0x1000
            else "kernel_synchronized"
            if good
            else "kernel_unsynchronized",
        )
    except (AttributeError, OSError):
        return TimeSource()


@dataclass(frozen=True)
class TimeStatus:
    state: str
    reason: str
    timestamp_safe: bool
    source: str = "linux_kernel"
    error_seconds: float | None = None
    holdover_age_seconds: float | None = None
    wall_step_seconds: float | None = None
    holdover_verified: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def detail(self) -> str:
        if self.state == "synchronized":
            return "The OS reports synchronized time; offline RTC retention is unqualified."
        if self.state == "holdover":
            return "Using bounded in-process time holdover; offline RTC retention is unqualified."
        if self.state == "simulated":
            return "Synthetic clock; no hardware timekeeping qualification."
        return TIME_REMEDIATION


class TimeMonitor:
    """Track source confidence and latch discontinuities until controlled restart."""

    def __init__(self, wall: float, mono: float, probe: Callable[[], TimeSource] | None = None):
        self._wall, self._mono = wall, mono
        self._probe = probe
        self._next_probe = float("-inf")
        self._source = TimeSource()
        self._last_good: float | None = None
        self._last_error = 0.0
        self._step: float | None = None

    def sample(self, wall: float, mono: float) -> TimeStatus:
        elapsed = mono - self._mono
        step = wall - self._wall - elapsed
        # Allow normal oscillator error/slewing and a leap second; detect both signs.
        if elapsed < 0 or abs(step) > STEP_SECONDS + max(0, elapsed) * 0.001:
            self._step = step
        self._wall, self._mono = wall, mono
        if self._step is not None:
            return TimeStatus("stepped", "wall_clock_step", False, wall_step_seconds=self._step)
        if not math.isfinite(wall) or wall < MIN_EPOCH:
            return TimeStatus("uncertain", "implausible_wall_time", False)
        if mono >= self._next_probe:
            self._source = (self._probe or kernel_time)()
            self._next_probe = mono + SOURCE_POLL_SECONDS
            error = self._source.error_seconds
            if self._source.synchronized and error is not None and 0 <= error <= MAX_ERROR_SECONDS:
                self._last_good, self._last_error = mono, error
            else:
                self._source = TimeSource(False, error, self._source.reason)
        if self._source.reason == "kernel_clock_fault":
            return TimeStatus("uncertain", "kernel_clock_fault", False)
        age = mono - self._last_good if self._last_good is not None else None
        error = self._last_error + age * DRIFT_PER_SECOND if age is not None else None
        if (
            age is not None
            and error is not None
            and age <= HOLDOVER_SECONDS
            and error <= MAX_ERROR_SECONDS
        ):
            state = "synchronized" if self._source.synchronized else "holdover"
            return TimeStatus(
                state,
                "kernel_synchronized" if state == "synchronized" else "bounded_process_holdover",
                True,
                error_seconds=error,
                holdover_age_seconds=age,
            )
        return TimeStatus(
            "uncertain",
            "holdover_exhausted" if age is not None else self._source.reason,
            False,
            error_seconds=error,
            holdover_age_seconds=age,
        )


def time_status(clock: Clock) -> TimeStatus:
    reader = getattr(clock, "time_status", None)
    if callable(reader):
        value = reader()
        if isinstance(value, TimeStatus):
            return value
    return TimeStatus("uncertain", "unmonitored_clock", False, source="unavailable")


class TimeUncertain(ValueError):
    """An operation requiring UTC validity must wait without consuming its work."""


def require_time(clock: Clock) -> None:
    if not time_status(clock).timestamp_safe:
        raise TimeUncertain(TIME_REMEDIATION)


class ElapsedTime:
    """A process-local epoch projection for durable queue TTL/backoff/accounting.

    This is not UTC synchronization. Reconstructing retained deadlines after a
    restart requires a confident wall clock before calling reset().
    """

    def __init__(self, clock: Clock):
        self.clock = clock
        self._epoch: float | None = None
        self._mono = 0.0

    def reset(self) -> None:
        self._epoch, self._mono = self.clock.now().timestamp(), self.clock.monotonic()

    def now(self) -> float:
        if self._epoch is None:
            self.reset()
        assert self._epoch is not None
        return self._epoch + max(0, self.clock.monotonic() - self._mono)


def rtc_inventory(root: Path = Path("/sys/class/rtc")) -> dict[str, object]:
    """Diagnostic hints only. Charging voltage is not battery presence or retention."""
    devices: list[dict[str, object]] = []
    try:
        for entry in sorted(root.glob("rtc[0-9]*"))[:8]:
            value: dict[str, object] = {"device": entry.name}
            for name in ("hctosys", "since_epoch", "charging_voltage"):
                try:
                    raw = (entry / name).read_text()[:64].strip()
                    value[name] = int(raw)
                except (OSError, ValueError):
                    value[name] = None
            devices.append(value)
    except OSError:
        pass
    return {"devices": devices, "battery_present": None, "retention_verified": False}


def inspection() -> dict[str, object]:
    """A standalone, sanitized witness usable before/after an offline cold boot."""
    try:
        boot = hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes()).hexdigest()
    except OSError:
        boot = None
    return {
        "observed_utc": datetime.now(UTC).isoformat(),
        "monotonic_seconds": time.monotonic(),
        "boot_fingerprint": boot,
        "source": asdict(kernel_time()),
        "rtc": rtc_inventory(),
        "physical_retention_tested": False,
    }


if __name__ == "__main__":
    print(json.dumps(inspection(), indent=2))
