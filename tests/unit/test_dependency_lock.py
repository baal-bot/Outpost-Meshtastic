from packaging.requirements import Requirement

from tools.check_dependency_lock import (
    consistency_errors,
    installed_errors,
    latest_compatible,
    parse_lock,
)


def test_lock_parser_requires_unique_exact_unconditional_pins() -> None:
    pins, errors = parse_lock(
        """
        Good_Name==1.2.3
        duplicate-name==1
        duplicate_name==2
        ranged>=1
        marked==1; python_version > '3.12'
        """
    )

    assert pins == {"good-name": "1.2.3", "duplicate-name": "1"}
    assert len(errors) == 3


def test_project_ranges_and_installed_graph_must_match_lock() -> None:
    pins = {"alpha": "1.5", "beta": "2"}

    assert consistency_errors(pins, [Requirement("alpha>=1,<2"), Requirement("missing>=1")]) == [
        "direct runtime dependency missing is missing from the lock"
    ]
    assert installed_errors(pins, {"alpha": "1.5", "beta": "3", "surprise": "1"}) == [
        "locked package beta==2, but installed version is 3",
        "installed runtime package surprise==1 has no lock entry",
    ]


def test_latest_compatible_excludes_out_of_range_prerelease_yanked_and_wrong_python() -> None:
    requirement = Requirement("sample>=1,<3")
    payload = {
        "releases": {
            "1.0": [{"yanked": False, "requires_python": ">=3.12"}],
            "2.0": [{"yanked": True, "requires_python": ">=3.12"}],
            "2.1": [{"yanked": False, "requires_python": ">=99"}],
            "2.2rc1": [{"yanked": False, "requires_python": ">=3.12"}],
            "3.0": [{"yanked": False, "requires_python": ">=3.12"}],
        }
    }

    assert str(latest_compatible(requirement, payload)) == "1.0"
