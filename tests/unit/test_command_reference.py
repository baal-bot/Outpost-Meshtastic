from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).parents[2]
COMMANDS = runpy.run_path(
    str(ROOT / "tools" / "check_commands.py"),
    run_name="outpost_command_reference",
)
registered_specs = cast(Callable[[], list[Any]], COMMANDS["registered_specs"])
run = cast(Callable[..., int], COMMANDS["run"])


def test_registered_commands_and_aliases_are_documented() -> None:
    specs = registered_specs()
    tokens = {token for spec in specs for token in (spec.name, *spec.aliases)}

    assert {"OPS", "ASK", "SUM", "TR", "TRANSLATE"} <= tokens
    assert {"ACKNOWLEDGE", "EARTHQUAKE", "SUBSCRIBE", "UNSUBSCRIBE"} <= tokens
    assert {"REPORT!", "ROSTER?"} <= tokens
    assert "CONVERSATION" not in tokens
    assert run(ROOT / "docs" / "COMMANDS.md", check=True) == 0


def test_command_reference_generator_detects_and_repairs_drift(tmp_path: Path) -> None:
    document = tmp_path / "COMMANDS.md"
    document.write_text((ROOT / "docs" / "COMMANDS.md").read_text(encoding="utf-8"))

    assert run(document, check=True) == 0
    document.write_text(
        document.read_text(encoding="utf-8").replace("`ABOUT` · node", "`ABOUT` · stale", 1),
        encoding="utf-8",
    )
    assert run(document, check=True) == 1
    assert run(document, check=False) == 0
    assert run(document, check=True) == 0
