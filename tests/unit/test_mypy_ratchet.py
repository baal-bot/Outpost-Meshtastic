from tools.check_mypy_ratchet import error_counts, regressions


def test_error_counts_groups_mypy_diagnostics_by_module() -> None:
    output = "\n".join(
        (
            "src/outpost/commands/core.py:10: error: first [arg-type]",
            "src/outpost/commands/core.py:20:4: error: second [misc]",
            "src/outpost/new.py:1: note: not an error",
        )
    )

    assert error_counts(output) == {"src/outpost/commands/core.py": 2}


def test_ratchet_allows_reductions_and_rejects_new_or_increased_debt() -> None:
    baseline = {"src/outpost/commands/core.py": 2}

    assert regressions({"src/outpost/commands/core.py": 1}, baseline) == []
    assert regressions({"src/outpost/commands/core.py": 3}, baseline) == [
        "src/outpost/commands/core.py: 3 strict errors (ceiling 2)"
    ]
    assert regressions({"src/outpost/new.py": 1}, baseline) == [
        "src/outpost/new.py: 1 strict errors (ceiling 0)"
    ]
