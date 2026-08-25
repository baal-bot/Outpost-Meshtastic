from outpost.clock import VirtualClock
from outpost.config import ReconnectConfig
from outpost.transport.simulated import SimulatedRadioLink
from outpost.transport.supervisor import RadioSupervisor


def test_liveness_timeout_uses_injected_clock() -> None:
    clock = VirtualClock()
    supervisor = RadioSupervisor(
        SimulatedRadioLink(), ReconnectConfig(), clock, liveness_timeout_s=300
    )
    assert supervisor.is_stale() is False
    clock.advance(299)
    assert supervisor.is_stale() is False
    clock.advance(1)
    assert supervisor.is_stale() is True
