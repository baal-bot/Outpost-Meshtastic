from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TaskFailureDomain(StrEnum):
    """How a long-running task affects the offline mesh service."""

    CORE = "core"
    RESTARTABLE_LOCAL = "restartable_local"
    OPTIONAL_PROVIDER = "optional_provider"


@dataclass(frozen=True)
class RestartPolicy:
    initial_seconds: int
    maximum_seconds: int
    circuit_threshold: int


RESTART_POLICIES = {
    TaskFailureDomain.RESTARTABLE_LOCAL: RestartPolicy(2, 300, 5),
    TaskFailureDomain.OPTIONAL_PROVIDER: RestartPolicy(15, 900, 4),
}


def restart_delay(domain: TaskFailureDomain, consecutive_failures: int) -> tuple[int, bool]:
    """Return a bounded retry delay and whether the task circuit is open."""

    if domain is TaskFailureDomain.CORE:
        raise ValueError("core tasks fail fast and do not use restart backoff")
    if consecutive_failures < 1:
        raise ValueError("consecutive_failures must be positive")
    policy = RESTART_POLICIES[domain]
    circuit_open = consecutive_failures >= policy.circuit_threshold
    if circuit_open:
        return policy.maximum_seconds, True
    delay = policy.initial_seconds * (2 ** (consecutive_failures - 1))
    return min(delay, policy.maximum_seconds), False
