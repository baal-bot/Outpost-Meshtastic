from __future__ import annotations

import pytest

from outpost.task_supervision import TaskFailureDomain, restart_delay


def test_restart_delay_is_exponential_bounded_and_opens_circuit() -> None:
    assert [
        restart_delay(TaskFailureDomain.RESTARTABLE_LOCAL, failure) for failure in range(1, 6)
    ] == [(2, False), (4, False), (8, False), (16, False), (300, True)]
    assert [
        restart_delay(TaskFailureDomain.OPTIONAL_PROVIDER, failure) for failure in range(1, 5)
    ] == [(15, False), (30, False), (60, False), (900, True)]
    assert restart_delay(TaskFailureDomain.OPTIONAL_PROVIDER, 30) == (900, True)


def test_core_tasks_and_invalid_failure_counts_have_no_restart_delay() -> None:
    with pytest.raises(ValueError, match="fail fast"):
        restart_delay(TaskFailureDomain.CORE, 1)
    with pytest.raises(ValueError, match="must be positive"):
        restart_delay(TaskFailureDomain.RESTARTABLE_LOCAL, 0)
