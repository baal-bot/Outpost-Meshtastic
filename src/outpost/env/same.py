from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from outpost.clock import Clock
from outpost.config import SameConfig
from outpost.store import Database

HEADER = re.compile(
    r"ZCZC-(?P<originator>[A-Z]{3})-(?P<event>[A-Z0-9]{3})-"
    r"(?P<locations>\d{6}(?:-\d{6})*)\+(?P<purge>\d{4})-"
    r"(?P<day>\d{3})(?P<time>\d{4})-(?P<callsign>.{1,8})-"
)
EVENTS = {
    "TOR": "Tornado Warning",
    "SVR": "Severe Thunderstorm Warning",
    "FFW": "Flash Flood Warning",
    "FLW": "Flood Warning",
    "WSW": "Winter Storm Warning",
    "RWT": "Required Weekly Test",
    "RMT": "Required Monthly Test",
    "DMO": "Practice/Demo Warning",
}
TEST_CODES = {"RWT", "RMT", "DMO"}


@dataclass(frozen=True)
class SameMessage:
    header: str
    originator: str
    event_code: str
    event_name: str
    location_codes: list[str]
    purge_minutes: int
    issued_day: int
    issued_time: str
    callsign: str
    is_test: bool
    relevant: bool

    def json(self) -> dict[str, Any]:
        return asdict(self)


class SameService:
    def __init__(self, database: Database, clock: Clock, config: SameConfig) -> None:
        self.database, self.clock, self.config = database, clock, config
        self.last_decode_at: int | None = None
        self.last_signal_at: int | None = None

    def parse(self, text: str) -> SameMessage:
        match = HEADER.search(text.upper())
        if match is None:
            raise ValueError("invalid SAME header")
        event = match.group("event")
        locations = match.group("locations").strip("-").split("-")
        purge = match.group("purge")
        purge_minutes = int(purge[:2]) * 60 + int(purge[2:])
        configured = set(self.config.county_codes)
        relevant = (
            not configured or bool(configured.intersection(locations)) or "000000" in locations
        )
        return SameMessage(
            header=match.group(0),
            originator=match.group("originator"),
            event_code=event,
            event_name=EVENTS.get(event, event),
            location_codes=locations,
            purge_minutes=purge_minutes,
            issued_day=int(match.group("day")),
            issued_time=match.group("time"),
            callsign=match.group("callsign").strip(),
            is_test=event in TEST_CODES,
            relevant=relevant,
        )

    async def ingest(self, text: str) -> tuple[SameMessage, bool]:
        value = self.parse(text)
        now = int(self.clock.now().timestamp())
        existing = await self.database.read(
            "SELECT id FROM same_event WHERE header=?", (value.header,)
        )
        if not existing:
            await self.database.write(
                """INSERT INTO same_event(header,originator,event_code,event_name,location_codes,
                   purge_minutes,issued_day,issued_time,callsign,is_test,relevant,received_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    value.header,
                    value.originator,
                    value.event_code,
                    value.event_name,
                    json.dumps(value.location_codes, separators=(",", ":")),
                    value.purge_minutes,
                    value.issued_day,
                    value.issued_time,
                    value.callsign,
                    int(value.is_test),
                    int(value.relevant),
                    now,
                ),
            )
        self.last_decode_at = self.last_signal_at = now
        return value, not existing

    def record_signal(self) -> None:
        self.last_signal_at = int(self.clock.now().timestamp())

    def health(self) -> dict[str, Any]:
        now = int(self.clock.now().timestamp())
        alarm_seconds = self.config.silence_alarm_minutes * 60
        silent = self.last_signal_at is None or now - self.last_signal_at > alarm_seconds
        return {
            "enabled": self.config.enabled,
            "status": "disabled" if not self.config.enabled else "no_signal" if silent else "up",
            "last_decode_at": self.last_decode_at,
            "last_signal_at": self.last_signal_at,
        }
