from __future__ import annotations

from .models import CommandSpec


class CommandRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, CommandSpec] = {}
        self._active: set[str] = set()

    def register(self, spec: CommandSpec, *, enabled: bool = True) -> None:
        for name in (spec.name, *spec.aliases):
            key = name.upper()
            if key in self._specs:
                raise ValueError(f"duplicate command or alias: {name}")
            self._specs[key] = spec
            if enabled:
                self._active.add(key)

    def resolve(self, token: str) -> CommandSpec | None:
        key = token.upper()
        return self._specs.get(key) if key in self._active else None

    def known(self, token: str) -> CommandSpec | None:
        """Return a reserved command even when its module is disabled."""
        return self._specs.get(token.upper())

    def commands(self) -> list[CommandSpec]:
        return sorted(
            {spec.name: spec for key, spec in self._specs.items() if key in self._active}.values(),
            key=lambda spec: spec.name,
        )

    def known_commands(self) -> list[CommandSpec]:
        return sorted(
            {spec.name: spec for spec in self._specs.values()}.values(),
            key=lambda spec: spec.name,
        )
