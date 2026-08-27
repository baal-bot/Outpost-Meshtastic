from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from outpost.clock import Clock
from outpost.config import SameConfig
from outpost.operator_context import current_actor
from outpost.store import Database
from outpost.watch import AlertService

HEADER = re.compile(
    r"ZCZC-(?P<originator>[A-Z]{3})-(?P<event>[A-Z0-9]{3})-"
    r"(?P<locations>\d{6}(?:-\d{6})*)\+(?P<purge>\d{4})-"
    r"(?P<day>\d{3})(?P<time>\d{4})-(?P<callsign>.{1,8})-"
)
EVENTS = {
    "CAE": "Child Abduction Emergency",
    "CDW": "Civil Danger Warning",
    "CEM": "Civil Emergency Message",
    "EQW": "Earthquake Warning",
    "EVI": "Evacuation Immediate",
    "FFW": "Flash Flood Warning",
    "FLW": "Flood Warning",
    "FRW": "Fire Warning",
    "HMW": "Hazardous Materials Warning",
    "HUW": "Hurricane Warning",
    "LAE": "Local Area Emergency",
    "NPT": "National Periodic Test",
    "NUW": "Nuclear Power Plant Warning",
    "RHW": "Radiological Hazard Warning",
    "RMT": "Required Monthly Test",
    "RWT": "Required Weekly Test",
    "SMW": "Special Marine Warning",
    "SVR": "Severe Thunderstorm Warning",
    "TOR": "Tornado Warning",
    "TSW": "Tsunami Warning",
    "VOW": "Volcano Warning",
    "WSW": "Winter Storm Warning",
    "DMO": "Practice/Demo Warning",
}
TEST_CODES = {"RWT", "RMT", "NPT", "DMO"}
CRITICAL_CODES = {"CDW", "EQW", "EVI", "FFW", "NUW", "RHW", "TOR", "TSW", "VOW"}
WATCH_CODES = {"AVA", "BZA", "CFA", "FFA", "HUA", "HWA", "SVA", "TOA", "TRA", "TSA"}
CAP_EXPIRY_TOLERANCE_SECONDS = 30 * 60


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
    significance: str
    issued_at: int
    expires_at: int

    def json(self) -> dict[str, Any]:
        return asdict(self)


class SameService:
    def __init__(self, database: Database, clock: Clock, config: SameConfig) -> None:
        self.database, self.clock, self.config = database, clock, config
        self.last_decode_at: int | None = None
        self.last_signal_at: int | None = None
        self.last_audio_at: int | None = None
        self.monitor_started_at: int | None = None

    @staticmethod
    def _significance(event: str) -> str:
        if event in TEST_CODES:
            return "test"
        if event in CRITICAL_CODES:
            return "warning"
        if event in WATCH_CODES or event.endswith("A"):
            return "watch"
        if event.endswith("S"):
            return "statement"
        if event.endswith("E"):
            return "emergency"
        return "warning"

    @staticmethod
    def _issued_at(day: int, time_text: str, now: datetime) -> datetime:
        hour, minute = int(time_text[:2]), int(time_text[2:])
        if not 1 <= day <= 366 or hour > 23 or minute > 59:
            raise ValueError("invalid SAME issue time")
        candidates: list[datetime] = []
        for year in (now.year - 1, now.year, now.year + 1):
            max_day = int(datetime(year, 12, 31, tzinfo=UTC).strftime("%j"))
            if day <= max_day:
                candidates.append(
                    datetime(year, 1, 1, tzinfo=UTC)
                    + timedelta(days=day - 1, hours=hour, minutes=minute)
                )
        return min(candidates, key=lambda value: abs((value - now).total_seconds()))

    def parse(self, text: str) -> SameMessage:
        match = HEADER.search(text.upper())
        if match is None:
            raise ValueError("invalid SAME header")
        event = match.group("event")
        locations = match.group("locations").strip("-").split("-")
        purge = match.group("purge")
        purge_hours, purge_remainder = int(purge[:2]), int(purge[2:])
        if purge_remainder > 59 or not (purge_hours or purge_remainder):
            raise ValueError("invalid SAME purge time")
        purge_minutes = purge_hours * 60 + purge_remainder
        configured = set(self.config.county_codes)
        relevant = bool(configured.intersection(locations)) or "000000" in locations
        issued = self._issued_at(
            int(match.group("day")), match.group("time"), self.clock.now().astimezone(UTC)
        )
        return SameMessage(
            header=match.group(0),
            originator=match.group("originator"),
            event_code=event,
            event_name=EVENTS.get(event, f"SAME event {event}"),
            location_codes=locations,
            purge_minutes=purge_minutes,
            issued_day=int(match.group("day")),
            issued_time=match.group("time"),
            callsign=match.group("callsign").strip(),
            is_test=event in TEST_CODES,
            relevant=relevant,
            significance=self._significance(event),
            issued_at=int(issued.timestamp()),
            expires_at=int((issued + timedelta(minutes=purge_minutes)).timestamp()),
        )

    @staticmethod
    def _normalized_event(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", value.lower())

    async def _matching_cap(self, message: SameMessage) -> dict[str, Any] | None:
        rows = await self.database.read(
            "SELECT id,event,expires_at,review_state,linked_alert_id,raw_json FROM cap_alert "
            "WHERE decision='accepted' AND review_state IN ('pending','approved')"
        )
        expected_event = self._normalized_event(message.event_name)
        locations = set(message.location_codes)
        for row in rows:
            try:
                expires = datetime.fromisoformat(
                    str(row["expires_at"]).replace("Z", "+00:00")
                ).timestamp()
                feature = json.loads(row["raw_json"])
                properties = feature.get("properties") or {}
                geocodes = properties.get("geocode") or {}
                cap_locations = {str(value) for value in geocodes.get("SAME", [])}
                event_codes = properties.get("eventCode") or {}
                cap_event_codes = {
                    str(value)
                    for value in (
                        event_codes.get("SAME", []) if isinstance(event_codes, dict) else []
                    )
                }
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                message.event_code not in cap_event_codes
                and self._normalized_event(str(row["event"])) != expected_event
            ):
                continue
            if not locations.intersection(cap_locations):
                continue
            if abs(int(expires) - message.expires_at) <= CAP_EXPIRY_TOLERANCE_SECONDS:
                return dict(row)
        return None

    @staticmethod
    def _gate(
        message: SameMessage, cap: dict[str, Any] | None, now: int
    ) -> tuple[str, str, list[str]]:
        if message.is_test:
            return "log_only", "logged", ["test or demonstration event"]
        if not message.relevant:
            return "withheld", "logged", ["outside configured SAME counties"]
        if message.expires_at <= now:
            return "withheld", "expired", ["SAME message is expired"]
        if message.issued_at > now + 5 * 60:
            return "withheld", "logged", ["SAME issue time is in the future"]
        if cap is not None:
            state = "approved" if cap.get("linked_alert_id") is not None else "duplicate"
            return "duplicate", state, [f"matched NWS CAP record {cap['id']}"]
        return "accepted", "pending", []

    async def ingest(self, text: str) -> tuple[SameMessage, bool]:
        value = self.parse(text)
        now = int(self.clock.now().timestamp())
        cap = await self._matching_cap(value)
        decision, review_state, reasons = self._gate(value, cap, now)
        async with self.database.transaction() as transaction:
            existing = await transaction.read(
                "SELECT id FROM same_event WHERE header=?", (value.header,)
            )
            if not existing:
                await transaction.write(
                    """INSERT INTO same_event(header,originator,event_code,event_name,
                       location_codes,
                       purge_minutes,issued_day,issued_time,callsign,is_test,relevant,received_at,
                       expires_at,decision,gate_reasons,review_state,cap_alert_id,linked_alert_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                        value.expires_at,
                        decision,
                        json.dumps(reasons, separators=(",", ":")),
                        review_state,
                        cap["id"] if cap is not None else None,
                        cap.get("linked_alert_id") if cap is not None else None,
                    ),
                )
        self.last_decode_at = self.last_signal_at = self.last_audio_at = now
        return value, not existing

    async def list(self, *, include_expired: bool = False) -> list[dict[str, Any]]:
        now = int(self.clock.now().timestamp())
        await self.database.write(
            "UPDATE same_event SET review_state='expired' "
            "WHERE review_state='pending' AND expires_at<=?",
            (now,),
        )
        where = "" if include_expired else "WHERE review_state!='expired'"
        rows = await self.database.read(
            f"SELECT * FROM same_event {where} ORDER BY received_at DESC LIMIT 100"  # noqa: S608
        )
        values: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["location_codes"] = json.loads(item["location_codes"])
            item["gate_reasons"] = json.loads(item["gate_reasons"])
            values.append(item)
        return values

    async def dismiss(self, same_id: int) -> None:
        rows = await self.database.read(
            "SELECT id FROM same_event WHERE id=? AND review_state='pending'", (same_id,)
        )
        if not rows:
            raise ValueError("SAME event is not pending review.")
        await self.database.write(
            "UPDATE same_event SET review_state='dismissed' WHERE id=?", (same_id,)
        )

    async def approve(self, same_id: int, alerts: AlertService) -> dict[str, Any]:
        rows = await self.database.read(
            "SELECT * FROM same_event WHERE id=? AND decision='accepted' "
            "AND review_state='pending' AND is_test=0 AND relevant=1",
            (same_id,),
        )
        if not rows:
            raise ValueError("SAME event is not eligible for approval.")
        item = dict(rows[0])
        message = self.parse(str(item["header"]))
        cap = await self._matching_cap(message)
        if cap is not None and cap.get("linked_alert_id") is not None:
            linked_id = int(cap["linked_alert_id"])
            await self.database.write(
                "UPDATE same_event SET decision='duplicate',review_state='approved',"
                "cap_alert_id=?,linked_alert_id=?,gate_reasons=? WHERE id=?",
                (
                    cap["id"],
                    linked_id,
                    json.dumps([f"matched approved NWS CAP record {cap['id']}"]),
                    same_id,
                ),
            )
            alert = await alerts.by_id(linked_id)
            if alert is None:
                raise ValueError("Matched CAP alert no longer exists.")
            return alert.json()

        expiry = datetime.fromtimestamp(message.expires_at, UTC)
        location = ", ".join(message.location_codes)
        headline = f"NWR {message.event_name} · {location} · until {expiry:%H:%M} UTC"
        while len(headline.encode()) > 140:
            headline = headline[:-1]
        alert = await alerts.raise_alert(
            "critical" if message.event_code in CRITICAL_CODES else "urgent",
            headline,
            current_actor(),
            source="same",
            expires_at=message.expires_at,
        )
        async with self.database.transaction() as transaction:
            await transaction.write(
                "UPDATE same_event SET review_state='approved',linked_alert_id=?,cap_alert_id=? "
                "WHERE id=?",
                (alert.id, cap["id"] if cap is not None else None, same_id),
            )
            if cap is not None:
                await transaction.write(
                    "UPDATE cap_alert SET review_state='approved',linked_alert_id=?,"
                    "updated_at=unixepoch() WHERE id=? AND review_state='pending'",
                    (alert.id, cap["id"]),
                )
        return alert.json()

    async def reconcile_cap_duplicates(self) -> int:
        rows = await self.database.read(
            "SELECT * FROM same_event WHERE decision IN ('accepted','duplicate') "
            "AND review_state IN ('pending','approved','duplicate')"
        )
        reconciled = 0
        for row in rows:
            message = self.parse(str(row["header"]))
            cap = await self._matching_cap(message)
            if cap is None:
                continue
            same_alert = row["linked_alert_id"]
            cap_alert = cap.get("linked_alert_id")
            if same_alert is not None and cap_alert is None:
                await self.database.write(
                    "UPDATE cap_alert SET review_state='approved',linked_alert_id=?,"
                    "updated_at=unixepoch() WHERE id=? AND review_state='pending'",
                    (same_alert, cap["id"]),
                )
            elif cap_alert is not None and same_alert is None:
                await self.database.write(
                    "UPDATE same_event SET decision='duplicate',review_state='approved',"
                    "cap_alert_id=?,linked_alert_id=?,gate_reasons=? WHERE id=?",
                    (
                        cap["id"],
                        cap_alert,
                        json.dumps([f"matched approved NWS CAP record {cap['id']}"]),
                        row["id"],
                    ),
                )
            elif same_alert is None and cap_alert is None and row["review_state"] == "pending":
                await self.database.write(
                    "UPDATE same_event SET decision='duplicate',review_state='duplicate',"
                    "cap_alert_id=?,gate_reasons=? WHERE id=?",
                    (
                        cap["id"],
                        json.dumps([f"matched pending NWS CAP record {cap['id']}"]),
                        row["id"],
                    ),
                )
            else:
                continue
            reconciled += 1
        return reconciled

    def start_monitoring(self) -> None:
        self.monitor_started_at = int(self.clock.now().timestamp())

    def record_audio(self, rms: float) -> None:
        now = int(self.clock.now().timestamp())
        self.last_audio_at = now
        if rms >= self.config.signal_rms_threshold:
            self.last_signal_at = now

    def record_signal(self) -> None:
        now = int(self.clock.now().timestamp())
        self.last_signal_at = self.last_audio_at = now

    def health(self) -> dict[str, Any]:
        now = int(self.clock.now().timestamp())
        alarm_seconds = self.config.silence_alarm_minutes * 60
        reference = self.last_signal_at or self.monitor_started_at
        silent = reference is not None and now - reference > alarm_seconds
        status = (
            "disabled"
            if not self.config.enabled
            else "no_signal"
            if silent
            else "up"
            if self.last_signal_at is not None
            else "monitoring"
        )
        return {
            "enabled": self.config.enabled,
            "status": status,
            "last_decode_at": self.last_decode_at,
            "last_signal_at": self.last_signal_at,
            "last_audio_at": self.last_audio_at,
            "monitor_started_at": self.monitor_started_at,
            "silence_alarm_seconds": alarm_seconds,
        }
