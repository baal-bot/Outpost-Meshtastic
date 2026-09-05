"""Explicit public incident locations; never infer a correcting member's GPS."""

from __future__ import annotations

import re
from dataclasses import dataclass

from outpost.store.database import Transaction

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_PAIR = re.compile(rf"({_NUMBER})(?:\s*,\s*|\s+)({_NUMBER})")


@dataclass(frozen=True)
class IncidentLocation:
    lat: float | None
    lon: float | None
    text: str
    suppressed: bool


async def parse_location(
    transaction: Transaction, text: str, *, previously_suppressed: bool
) -> IncidentLocation:
    if not text or any(not character.isprintable() for character in text):
        raise ValueError("Location needs a single line of printable text.")
    if len(text.encode("utf-8")) > 200:
        raise ValueError("Location input must be at most 200 UTF-8 bytes.")
    value = text.strip()
    share = value.lower().startswith("-share ")
    suppress = value.lower() == "-nopos" or value.lower().startswith("-nopos ")
    if share or suppress:
        value = value.split(maxsplit=1)[1].strip() if " " in value else ""
    if suppress and not value:
        return IncidentLocation(None, None, "Location withheld", True)
    if not value:
        raise ValueError("Location is required.")

    pair = _PAIR.fullmatch(value)
    waypoint = re.fullmatch(r"-wp\s+(\S+)", value, re.IGNORECASE)
    lat: float | None = None
    lon: float | None = None
    if pair:
        lat, lon = float(pair[1]), float(pair[2])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Latitude must be -90..90 and longitude -180..180.")
    elif waypoint:
        rows = await transaction.read(
            "SELECT name,latitude,longitude FROM waypoint WHERE lower(slug)=lower(?)",
            (waypoint[1],),
        )
        if not rows:
            raise ValueError("Waypoint not found. Send WPS.")
        lat, lon = float(rows[0]["latitude"]), float(rows[0]["longitude"])
        value = str(rows[0]["name"])
    elif share or value.startswith("-") or _PAIR.search(value):
        raise ValueError("Use a place name, -share <lat> <lon>, or -share -wp <name>.")

    if lat is not None and lon is not None:
        if not share or suppress:
            raise ValueError("Coordinates are public. Use -share before coordinates or -wp.")
        if waypoint and len(value.encode("utf-8")) > 160:
            raise ValueError("Waypoint label must be at most 160 UTF-8 bytes.")
        return IncidentLocation(lat, lon, value if waypoint else f"{lat:.5f},{lon:.5f}", False)
    if len(value.encode("utf-8")) > 160:
        raise ValueError("Place name must be at most 160 UTF-8 bytes.")
    return IncidentLocation(None, None, value, suppress or previously_suppressed)
