from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Template:
    text: str
    max_bytes: int


CATALOGUE = {
    "unknown": Template("Unknown. Send ? for help.", 28),
    "internal_error": Template("Err. Try again or send HELP.", 32),
    "no_context": Template("Home.", 8),
    "home": Template("Home.", 8),
    "back": Template("Back: {context}", 60),
    "where": Template("In: {context}", 60),
    "rate_limited": Template("Slow down. Try shortly.", 26),
}


def message(key: str, **values: object) -> str:
    template = CATALOGUE[key]
    rendered = template.text.format(**values)
    if len(rendered.encode()) > template.max_bytes:
        raise ValueError(f"catalogue output exceeded budget: {key}")
    return rendered
