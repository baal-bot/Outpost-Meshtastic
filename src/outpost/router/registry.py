from __future__ import annotations

from .models import CommandSpec


class CommandRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, CommandSpec] = {}

    def register(self, spec: CommandSpec) -> None:
        for name in (spec.name, *spec.aliases):
            key = name.upper()
            if key in self._specs:
                raise ValueError(f"duplicate command or alias: {name}")
            self._specs[key] = spec

    def resolve(self, token: str) -> CommandSpec | None:
        return self._specs.get(token.upper())

    def commands(self) -> list[CommandSpec]:
        return sorted(
            {spec.name: spec for spec in self._specs.values()}.values(), key=lambda s: s.name
        )
