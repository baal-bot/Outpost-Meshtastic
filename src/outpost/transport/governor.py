from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from outpost.clock import Clock
from outpost.config import AirtimeConfig, RadioPowerConfig
from outpost.radio_power import normalize_battery_level
from outpost.store.database import StoreError
from outpost.store.outbox import OutboxDeferred, OutboxRejected, OutboxStore
from outpost.timekeeping import ElapsedTime, TimeUncertain, require_time, time_status
from outpost.transport.chunker import truncate_utf8

if TYPE_CHECKING:
    from outpost.store.database import Transaction

from .metrics import (
    AIR_UTIL_TX,
    AIRTIME_USED,
    CHANNEL_UTIL,
    OUTBOUND_DROPPED,
    OUTBOUND_ENQUEUED,
    OUTBOUND_SENT,
    QUEUE_DEPTH,
    RADIO_BATTERY_LEVEL,
    RADIO_BATTERY_REPORTED,
    RADIO_POWER_OBSERVATION_FAILURES,
    TOA_SECONDS,
)
from .models import LinkState, RadioLink, SendResult, Severity, TrafficClass
from .radio_frequency import regional_duty_cycle_percent
from .toa import MAX_PAYLOAD_BYTES, Preset, resolve_preset, toa

TTL_SECONDS = {
    TrafficClass.ALERT: 86_400,
    TrafficClass.REPLY: 300,
    TrafficClass.AI: 180,
    TrafficClass.BULLETIN: 7_200,
    TrafficClass.DIGEST: 3_600,
    TrafficClass.FEDERATION: 1_800,
}
ALERT_SEVERITY_ORDER = (
    Severity.CRITICAL,
    Severity.URGENT,
    Severity.CAUTION,
    Severity.INFO,
)
DISCRETIONARY_POWER_CLASSES = frozenset(
    {TrafficClass.AI, TrafficClass.BULLETIN, TrafficClass.DIGEST}
)


class GovernorConfigurationError(ValueError):
    """A runtime configuration fault that the egress loop may safely contain."""


@dataclass
class OutboundItem:
    text: str
    dest: str
    channel: int
    traffic_class: TrafficClass
    severity: Severity = Severity.INFO
    want_ack: bool = True
    priority: int = 0
    created_at: float = 0.0
    expires_at: float = 0.0
    supersedes: str | None = None
    queue_key: str | None = None
    dedupe_token: str | None = None
    item_id: int = 0
    binary_payload: bytes | None = None
    portnum: int | None = None
    multipart: bool = False
    send_result: SendResult | None = None
    estimated_toa: float = 0.0
    uid: str = ""
    created_at_epoch: float = 0.0
    expires_at_epoch: float = 0.0
    attempts: int = 0
    next_attempt_at: float = 0.0
    guard_kind: str | None = None

    def __post_init__(self) -> None:
        if self.binary_payload is None:
            self.text = truncate_utf8(self.text, MAX_PAYLOAD_BYTES)

    @property
    def payload_size(self) -> int:
        return len(self.binary_payload if self.binary_payload is not None else self.text.encode())


@dataclass
class GovernorMetrics:
    enqueued: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    sent: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    dropped: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    throttled: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    hard_stops: int = 0


@dataclass(frozen=True)
class AdmissionResult:
    """Admission outcome; caller-transaction IDs remain provisional until commit."""

    item_ids: tuple[int, ...] = ()
    rejection_reason: str | None = None

    @property
    def admitted(self) -> int:
        return len(self.item_ids)


class AirtimeGovernor:
    """Deterministic sole-egress scheduler with a rolling one-hour budget."""

    def __init__(
        self,
        link: RadioLink,
        config: AirtimeConfig,
        clock: Clock,
        *,
        preset: str = "LONG_FAST",
        region: str | None = None,
        regional_ceiling_percent: float | None = None,
        outbox: OutboxStore | None = None,
        power_config: RadioPowerConfig | None = None,
        power_observer: Callable[[int | None], Awaitable[None]] | None = None,
        timezone: str = "UTC",
    ) -> None:
        self.link, self.config, self.clock = link, config, clock
        self._elapsed = ElapsedTime(clock)
        self.timezone = ZoneInfo(timezone)
        self.outbox = outbox
        self.power_config = power_config or RadioPowerConfig()
        self.power_observer = power_observer
        self.battery_level: int | None = None
        self.channel_utilisation: float | None = None
        self.reported_preset = ""
        self.preset = ""
        self.region = "unknown"
        self.regional_ceiling_percent: float | None = None
        self.profile_warnings: tuple[str, ...] = ()
        self._toa_preset = Preset(12, 62_500, 4)
        self.sync_radio_profile(
            preset,
            region,
            regional_ceiling_percent=regional_ceiling_percent,
        )
        self.queues: dict[TrafficClass, deque[OutboundItem]] = {
            cls: deque() for cls in TrafficClass
        }
        self.history: deque[tuple[float, float, TrafficClass, Severity]] = deque()
        self._time_airtime: deque[tuple[float, float, TrafficClass, Severity]] = deque()
        self._recent: dict[tuple[str, int, str], float] = {}
        self._held_ids: set[int] = set()
        self._publication_failed = False
        self._time_recovery_pending = False
        self._time_started_safe = time_status(clock).timestamp_safe
        self.time_recovery_guard: Callable[[OutboundItem], bool] = lambda item: False
        self._recovery_loaded = False
        self._startup_not_before = 0.0
        self._recovery_silence_until = 0.0
        self._dispatch_lock = asyncio.Lock()
        self._next_id = 1
        self._next_tx_at = 0.0
        self._last_toa = 0.0
        self._next_outbox_sweep_at = 0.0
        self._rr = deque(cls for cls in TrafficClass if cls != TrafficClass.ALERT)
        self.metrics = GovernorMetrics()

    def sync_radio_profile(
        self,
        preset: str,
        region: str | None,
        *,
        regional_ceiling_percent: float | None = None,
    ) -> None:
        """Atomically refresh costing and legal ceilings from the live radio snapshot."""
        reported = preset.strip().upper() or "UNKNOWN"
        normalized_region = region.strip().upper() if region is not None else "UNKNOWN"
        costing, toa_preset, supported = resolve_preset(
            reported, wide_lora=normalized_region == "LORA_24"
        )
        warnings: list[str] = []
        if not supported:
            warnings.append(
                f"Radio reports unsupported modem preset {reported}; governor is using "
                f"conservative {costing} costing."
            )
        ceiling = regional_ceiling_percent
        if ceiling is None and region is not None:
            ceiling = regional_duty_cycle_percent(normalized_region)
            if ceiling is None:
                ceiling = 0.0
                warnings.append(
                    f"Radio region {normalized_region} has no known duty-cycle ceiling; "
                    "outbound transmission is paused."
                )
        if ceiling is not None and not 0 <= ceiling <= 100:
            raise ValueError("regional airtime ceiling must be between 0 and 100 percent")
        self.reported_preset = reported
        self.preset = costing
        self.region = normalized_region
        self.regional_ceiling_percent = ceiling
        self.profile_warnings = tuple(warnings)
        self._toa_preset = toa_preset
        self._apply_budget(ceiling)

    def _apply_budget(self, regional_ceiling_percent: float | None) -> None:
        config = self.config
        configured_total = config.budget_percent + config.emergency_reserve_percent
        if regional_ceiling_percent is not None and configured_total > regional_ceiling_percent:
            scale = regional_ceiling_percent / configured_total
            self.budget_percent = config.budget_percent * scale
            self.reserve_percent = config.emergency_reserve_percent * scale
        else:
            self.budget_percent = config.budget_percent
            self.reserve_percent = config.emergency_reserve_percent

    def estimate_toa(self, payload_bytes: int, *, portnum: int = 1) -> float:
        return toa(payload_bytes, self._toa_preset, portnum=portnum)

    def estimate_payloads(
        self,
        payload_bytes: list[int],
        *,
        traffic_class: TrafficClass,
        severity: Severity = Severity.INFO,
        copies: int = 1,
        portnum: int = 1,
    ) -> dict[str, object]:
        """Forecast a batch using the exact model and rolling state used at dispatch."""
        if not payload_bytes or any(size < 1 or size > MAX_PAYLOAD_BYTES for size in payload_bytes):
            raise ValueError(f"Each message part must be 1-{MAX_PAYLOAD_BYTES} UTF-8 bytes.")
        if copies < 0:
            raise ValueError("Transmission copies cannot be negative.")
        part_seconds = [self.estimate_toa(size, portnum=portnum) for size in payload_bytes]
        per_copy_seconds = sum(part_seconds)
        total_seconds = per_copy_seconds * copies
        budget_seconds = 3_600 * self.budget_percent / 100
        reserve_seconds = 3_600 * self.reserve_percent / 100
        total_budget_seconds = budget_seconds + reserve_seconds
        used_seconds = self.used_airtime
        class_used_seconds = self.class_airtime(traffic_class)
        class_ceiling_seconds = budget_seconds * self.config.class_shares.get(
            traffic_class.value, 0.0
        )
        projected_seconds = used_seconds + total_seconds
        projected_class_seconds = class_used_seconds + total_seconds
        critical = traffic_class == TrafficClass.ALERT and severity == Severity.CRITICAL
        breaches: list[dict[str, object]] = []
        if total_seconds > 0 and projected_seconds > budget_seconds:
            detail = (
                "This critical alert will consume emergency reserve."
                if critical and projected_seconds <= total_budget_seconds
                else "The batch exceeds the rolling one-hour airtime allowance."
            )
            breaches.append(
                {
                    "code": "hourly_budget",
                    "label": "One-hour budget",
                    "detail": detail,
                    "ceiling_seconds": total_budget_seconds if critical else budget_seconds,
                    "projected_seconds": projected_seconds,
                }
            )
        if total_seconds > 0 and not critical and projected_class_seconds > class_ceiling_seconds:
            breaches.append(
                {
                    "code": "class_share",
                    "label": f"{traffic_class.value.title()} class share",
                    "detail": (
                        "This work will wait behind traffic already using this class share."
                    ),
                    "ceiling_seconds": class_ceiling_seconds,
                    "projected_seconds": projected_class_seconds,
                }
            )
        projected_utilisation = None
        if self.channel_utilisation is not None:
            projected_utilisation = self.channel_utilisation + total_seconds / 36
            if total_seconds > 0 and projected_utilisation > self.config.utilisation_ceiling:
                breaches.append(
                    {
                        "code": "utilisation_ceiling",
                        "label": "Channel utilisation ceiling",
                        "detail": (
                            "The governor will pause non-alert traffic while the channel is busy."
                        ),
                        "ceiling_percent": self.config.utilisation_ceiling,
                        "projected_percent": projected_utilisation,
                    }
                )
        reserve_used_before = max(0.0, used_seconds - budget_seconds)
        reserve_used_after = max(0.0, projected_seconds - budget_seconds)
        breach_codes = [str(breach["code"]) for breach in breaches]
        return {
            "payload_bytes": sum(payload_bytes),
            "part_count": len(payload_bytes),
            "parts": [
                {"number": index, "payload_bytes": size, "seconds": seconds}
                for index, (size, seconds) in enumerate(
                    zip(payload_bytes, part_seconds, strict=True), 1
                )
            ],
            "copies": copies,
            "transmission_count": len(payload_bytes) * copies,
            "per_copy_seconds": per_copy_seconds,
            "total_seconds": total_seconds,
            "traffic_class": traffic_class.value,
            "severity": severity.value,
            "reported_preset": self.reported_preset,
            "costing_preset": self.preset,
            "region": self.region,
            "budget": {
                "used_seconds": used_seconds,
                "projected_seconds": projected_seconds,
                "normal_ceiling_seconds": budget_seconds,
                "reserve_seconds": reserve_seconds,
                "total_ceiling_seconds": total_budget_seconds,
                "remaining_before_seconds": max(0.0, budget_seconds - used_seconds),
                "remaining_after_seconds": max(0.0, budget_seconds - projected_seconds),
                "reserve_used_before_seconds": reserve_used_before,
                "reserve_used_after_seconds": reserve_used_after,
            },
            "class_budget": {
                "used_seconds": class_used_seconds,
                "projected_seconds": projected_class_seconds,
                "ceiling_seconds": class_ceiling_seconds,
                "exempt": critical,
            },
            "utilisation": {
                "current_percent": self.channel_utilisation,
                "projected_percent": projected_utilisation,
                "ceiling_percent": self.config.utilisation_ceiling,
            },
            "breach_codes": breach_codes,
            "breaches": breaches,
            "requires_confirmation": bool(breaches),
            "displacement": " ".join(str(breach["detail"]) for breach in breaches),
        }

    def estimate_text(
        self,
        text: str,
        *,
        traffic_class: TrafficClass,
        severity: Severity = Severity.INFO,
        copies: int = 1,
    ) -> dict[str, object]:
        payload = text.strip().encode()
        return self.estimate_payloads(
            [len(payload)], traffic_class=traffic_class, severity=severity, copies=copies
        )

    @property
    def durable(self) -> bool:
        return self.outbox is not None

    @staticmethod
    def _payload(item: OutboundItem) -> bytes:
        return (
            item.dedupe_token.encode()
            if item.dedupe_token is not None
            else item.binary_payload
            if item.binary_payload is not None
            else item.text.encode()
        )

    @classmethod
    def _digest(cls, item: OutboundItem) -> str:
        return hashlib.sha256(cls._payload(item)).hexdigest()

    def enqueue(self, item: OutboundItem) -> int | None:
        now = self.clock.monotonic()
        if self.outbox is not None:
            raise RuntimeError("durable governors require await governor.admit()")
        if item.payload_size > MAX_PAYLOAD_BYTES:
            self.metrics.dropped[(item.traffic_class, "payload_too_large")] += 1
            OUTBOUND_DROPPED.labels(item.traffic_class.value, "payload_too_large").inc()
            return None
        digest = self._digest(item)
        dedupe_key = (item.dest, item.channel, digest)
        if self._recent.get(dedupe_key, float("-inf")) + self.config.dedupe_window_s > now:
            self.metrics.dropped[(item.traffic_class, "duplicate")] += 1
            OUTBOUND_DROPPED.labels(item.traffic_class.value, "duplicate").inc()
            return None
        if item.supersedes:
            for queue in self.queues.values():
                retained = deque(
                    existing for existing in queue if existing.queue_key != item.supersedes
                )
                queue.clear()
                queue.extend(retained)
        if sum(map(len, self.queues.values())) >= self.config.queue_max_items:
            self.metrics.dropped[(item.traffic_class, "queue_full")] += 1
            OUTBOUND_DROPPED.labels(item.traffic_class.value, "queue_full").inc()
            return None
        item.item_id = self._next_id
        self._next_id += 1
        item.created_at = now
        item.expires_at = now + TTL_SECONDS[item.traffic_class]
        self.queues[item.traffic_class].append(item)
        self._recent[dedupe_key] = now
        self.metrics.enqueued[item.traffic_class] += 1
        OUTBOUND_ENQUEUED.labels(item.traffic_class.value).inc()
        QUEUE_DEPTH.labels(item.traffic_class.value).set(len(self.queues[item.traffic_class]))
        return item.item_id

    async def admit(self, item: OutboundItem, *, hold: bool = False) -> int | None:
        admitted = await self.admit_many([item], hold=hold)
        return admitted[0] if admitted else None

    async def admit_many(
        self,
        items: list[OutboundItem],
        *,
        hold: bool = False,
        transaction: Transaction | None = None,
    ) -> list[int] | None:
        """Persist a complete batch before making any item eligible to transmit."""
        result = await self.admit_many_result(items, hold=hold, transaction=transaction)
        return list(result.item_ids) if result.rejection_reason is None else None

    async def admit_many_result(
        self,
        items: list[OutboundItem],
        *,
        hold: bool = False,
        transaction: Transaction | None = None,
    ) -> AdmissionResult:
        """Persist a batch and retain the reason when queue policy rejects it."""
        if self.outbox is None:
            return self._enqueue_many_result(items, hold=hold)
        if transaction is None:
            async with self.outbox.database.transaction() as owned:
                return await self.admit_many_result(items, hold=hold, transaction=owned)
        transaction.check_owner(self.outbox.database)
        if not items:
            return AdmissionResult()
        if any(self._time_blocked(item) for item in items):
            return AdmissionResult(rejection_reason="time_uncertain")
        oversized = [item for item in items if item.payload_size > MAX_PAYLOAD_BYTES]
        if oversized:
            for item in items:
                self.metrics.dropped[(item.traffic_class, "payload_too_large")] += 1
                OUTBOUND_DROPPED.labels(item.traffic_class.value, "payload_too_large").inc()
            return AdmissionResult(rejection_reason="payload_too_large")
        now_mono = self.clock.monotonic()
        now_epoch = self._elapsed.now()
        batch_uid = str(uuid.uuid4()) if len(items) > 1 else None
        records: list[dict[str, object]] = []
        for item in items:
            item.uid = item.uid or str(uuid.uuid4())
            item.created_at = now_mono
            item.expires_at = now_mono + TTL_SECONDS[item.traffic_class]
            item.created_at_epoch = now_epoch
            item.expires_at_epoch = now_epoch + TTL_SECONDS[item.traffic_class]
            records.append(
                {
                    "uid": item.uid,
                    "batch_uid": batch_uid,
                    "state": "held" if hold else "pending",
                    "text": item.text,
                    "binary_payload": item.binary_payload,
                    "destination": item.dest,
                    "channel": item.channel,
                    "traffic_class": item.traffic_class.value,
                    "severity": item.severity.value,
                    "want_ack": item.want_ack,
                    "priority": item.priority,
                    "created_at": item.created_at_epoch,
                    "expires_at": item.expires_at_epoch,
                    "supersedes": item.supersedes,
                    "queue_key": item.queue_key,
                    "dedupe_token": item.dedupe_token,
                    "dedupe_hash": self._digest(item),
                    "portnum": item.portnum,
                    "multipart": item.multipart,
                    "byte_len": item.payload_size,
                    "guard_kind": item.guard_kind,
                }
            )
        # Callers can retain/mutate their objects while the writer yields. The
        # committed queue must use the same immutable field snapshot as SQLite.
        prepared = tuple((item, replace(item)) for item in items)
        try:
            result = await self.outbox.admit_many(
                records,
                queue_max_items=self.config.queue_max_items,
                dedupe_window_s=self.config.dedupe_window_s,
                transaction=transaction,
            )
        except OutboxRejected as error:
            for item in items:
                self.metrics.dropped[(item.traffic_class, error.reason)] += 1
                OUTBOUND_DROPPED.labels(item.traffic_class.value, error.reason).inc()
            return AdmissionResult(rejection_reason=error.reason)

        for (original, snapshot), item_id in zip(prepared, result.ids, strict=True):
            original.item_id = snapshot.item_id = item_id

        def publish() -> None:
            try:
                self._publish_committed(prepared, tuple(result.superseded_ids), hold=hold)
            except BaseException:
                # A partial mirror is unsafe. The core egress task must fail
                # visibly and recover only after it has been quiesced/restarted.
                self._publication_failed = True
                raise

        transaction.after_commit(publish)
        return AdmissionResult(tuple(result.ids))

    def _publish_committed(
        self,
        items: tuple[tuple[OutboundItem, OutboundItem], ...],
        superseded_ids: tuple[int, ...],
        *,
        hold: bool,
    ) -> None:
        """Non-yielding local mirror publication, called only after SQLite commit."""
        superseded = set(superseded_ids)
        if superseded:
            self._remove_ids(superseded)
        for item, snapshot in items:
            # Keep the existing item identity/status API, but publish the field
            # values actually admitted. After publication the governor owns it.
            vars(item).update(vars(snapshot))
            self.queues[item.traffic_class].append(item)
            if hold:
                self._held_ids.add(item.item_id)
            self.metrics.enqueued[item.traffic_class] += 1
            OUTBOUND_ENQUEUED.labels(item.traffic_class.value).inc()
            QUEUE_DEPTH.labels(item.traffic_class.value).set(len(self.queues[item.traffic_class]))

    def _check_publication(self) -> None:
        if self._publication_failed:
            raise StoreError("outbox publication failed; quiesce egress and recover before sending")

    async def recover(self) -> int:
        """Rebuild the committed mirror with egress and admissions quiesced."""
        if self.outbox is None:
            return 0
        await self._load_time_recovery()
        if not time_status(self.clock).timestamp_safe:
            self._time_recovery_pending = True
            return 0
        if not self._recovery_silence_until:
            self._startup_not_before = 0
        self._publication_failed = True
        self._elapsed.reset()
        now_epoch = self._elapsed.now()
        try:
            rows = await self.outbox.recover(now_epoch, time_guard=lambda: require_time(self.clock))
        except TimeUncertain:
            self._time_recovery_pending = True
            self._publication_failed = False
            return 0
        self.queues = {cls: deque() for cls in TrafficClass}
        self._held_ids.clear()
        # UTC can have changed while recovery probes were in flight. Preserve
        # their elapsed-time costs even if the durable UTC history no longer agrees.
        retained_history = list(self._time_airtime)
        self.history.clear()
        airtime = await self.outbox.recent_airtime(self._elapsed.now())
        now_epoch = self._elapsed.now()
        now_mono = self.clock.monotonic()
        self._next_tx_at = 0.0
        for record in airtime:
            traffic_class = TrafficClass(str(record["airtime_class"]))
            severity = Severity(str(record["severity"]))
            sent_at = now_mono - max(0.0, now_epoch - float(record["created_at"]))
            cost = float(record["toa_ms"]) / 1_000
            self.history.append((sent_at, cost, traffic_class, severity))
            self._next_tx_at = max(
                self._next_tx_at, sent_at + self._attempt_gap(cost, bool(record["multipart"]))
            )
        self.history.extend(retained_history)
        self.history = deque(sorted(self.history))
        for sent_at, cost, _, _ in retained_history:
            self._next_tx_at = max(self._next_tx_at, sent_at + self._attempt_gap(cost, True))
        for row in rows:
            item = OutboundItem(
                text=str(row["text"]),
                binary_payload=(
                    bytes(row["binary_payload"]) if row["binary_payload"] is not None else None
                ),
                dest=str(row["destination"]),
                channel=int(row["channel"]),
                traffic_class=TrafficClass(str(row["traffic_class"])),
                severity=Severity(str(row["severity"])),
                want_ack=bool(row["want_ack"]),
                priority=int(row["priority"]),
                supersedes=row["supersedes"],
                queue_key=row["queue_key"],
                dedupe_token=row["dedupe_token"],
                item_id=int(row["id"]),
                portnum=row["portnum"],
                multipart=bool(row["multipart"]),
                uid=str(row["uid"]),
                created_at_epoch=float(row["created_at"]),
                expires_at_epoch=float(row["expires_at"]),
                attempts=int(row["attempts"]),
                guard_kind=row["guard_kind"],
            )
            age = max(0.0, now_epoch - item.created_at_epoch)
            item.created_at = now_mono - age
            item.expires_at = now_mono + max(0.0, item.expires_at_epoch - now_epoch)
            retry_epoch = float(row["next_attempt_at"] or now_epoch)
            item.next_attempt_at = now_mono + max(0.0, retry_epoch - now_epoch)
            self.queues[item.traffic_class].append(item)
            self.metrics.enqueued[item.traffic_class] += 1
            QUEUE_DEPTH.labels(item.traffic_class.value).set(len(self.queues[item.traffic_class]))
        if rows:
            self._next_id = max(int(row["id"]) for row in rows) + 1
        self._publication_failed = False
        self._time_recovery_pending = False
        self._time_started_safe = True
        return len(rows)

    def enqueue_many(self, items: list[OutboundItem], *, hold: bool = False) -> list[int] | None:
        """Atomically admit a complete multi-part response (REQ-TRANSPORT-035)."""
        result = self._enqueue_many_result(items, hold=hold)
        return list(result.item_ids) if result.rejection_reason is None else None

    def _enqueue_many_result(
        self, items: list[OutboundItem], *, hold: bool = False
    ) -> AdmissionResult:
        if self.outbox is not None:
            raise RuntimeError("durable governors require await governor.admit_many()")
        if not items:
            return AdmissionResult()
        if any(item.payload_size > MAX_PAYLOAD_BYTES for item in items):
            for item in items:
                self.metrics.dropped[(item.traffic_class, "payload_too_large")] += 1
                OUTBOUND_DROPPED.labels(item.traffic_class.value, "payload_too_large").inc()
            return AdmissionResult(rejection_reason="payload_too_large")
        superseded = {item.supersedes for item in items if item.supersedes is not None}
        retained = sum(
            existing.queue_key not in superseded
            for queue in self.queues.values()
            for existing in queue
        )
        available = self.config.queue_max_items - retained
        if len(items) > available:
            for item in items:
                self.metrics.dropped[(item.traffic_class, "queue_full")] += 1
                OUTBOUND_DROPPED.labels(item.traffic_class.value, "queue_full").inc()
            return AdmissionResult(rejection_reason="queue_full")
        # Preflight duplicate keys so enqueue cannot partially reject the batch.
        now = self.clock.monotonic()
        batch_keys: set[tuple[str, int, str]] = set()
        for item in items:
            payload = (
                item.dedupe_token.encode()
                if item.dedupe_token is not None
                else item.binary_payload
                if item.binary_payload is not None
                else item.text.encode()
            )
            digest = hashlib.sha256(payload).hexdigest()
            key = (item.dest, item.channel, digest)
            if key in batch_keys or (
                self._recent.get(key, float("-inf")) + self.config.dedupe_window_s > now
            ):
                for rejected in items:
                    self.metrics.dropped[(rejected.traffic_class, "duplicate")] += 1
                    OUTBOUND_DROPPED.labels(rejected.traffic_class.value, "duplicate").inc()
                return AdmissionResult(rejection_reason="duplicate")
            batch_keys.add(key)
        ids = [self.enqueue(item) for item in items]
        if any(item_id is None for item_id in ids):
            raise AssertionError("atomic enqueue preflight diverged")
        admitted = [item_id for item_id in ids if item_id is not None]
        if hold:
            self._held_ids.update(admitted)
        return AdmissionResult(tuple(admitted))

    def queued_items(self) -> list[OutboundItem]:
        return sorted(
            (item for queue in self.queues.values() for item in queue),
            key=lambda item: item.item_id,
        )

    def cancel(self, item_id: int) -> bool:
        if self.outbox is not None:
            raise RuntimeError("durable governors require await governor.cancel_work()")
        for traffic_class, queue in self.queues.items():
            for item in queue:
                if item.item_id != item_id:
                    continue
                queue.remove(item)
                QUEUE_DEPTH.labels(traffic_class.value).set(len(queue))
                self.metrics.dropped[(traffic_class.value, "operator_cancel")] += 1
                OUTBOUND_DROPPED.labels(traffic_class.value, "operator_cancel").inc()
                return True
        return False

    def _remove_ids(self, item_ids: set[int]) -> None:
        self._held_ids.difference_update(item_ids)
        for traffic_class, queue in self.queues.items():
            retained = deque(item for item in queue if item.item_id not in item_ids)
            queue.clear()
            queue.extend(retained)
            QUEUE_DEPTH.labels(traffic_class.value).set(len(queue))

    async def cancel_work(self, item_id: int) -> bool:
        if self.outbox is None:
            return self.cancel(item_id)
        cancelled = await self.outbox.cancel(item_id, self._elapsed.now())
        if cancelled:
            self._remove_ids({item_id})
        return cancelled

    def retract_many(self, item_ids: list[int]) -> None:
        """Undo an admitted batch when its associated database transaction rolls back."""
        remaining = set(item_ids)
        self._held_ids.difference_update(remaining)
        for traffic_class, queue in self.queues.items():
            for item in tuple(queue):
                if item.item_id not in remaining:
                    continue
                queue.remove(item)
                remaining.remove(item.item_id)
                payload = (
                    item.dedupe_token.encode()
                    if item.dedupe_token is not None
                    else item.binary_payload
                    if item.binary_payload is not None
                    else item.text.encode()
                )
                digest = hashlib.sha256(payload).hexdigest()
                self._recent.pop((item.dest, item.channel, digest), None)
                self.metrics.enqueued[traffic_class] = max(
                    0, self.metrics.enqueued[traffic_class] - 1
                )
                self.metrics.dropped[(traffic_class, "transaction_rollback")] += 1
                OUTBOUND_DROPPED.labels(traffic_class.value, "transaction_rollback").inc()
            QUEUE_DEPTH.labels(traffic_class.value).set(len(queue))
        if remaining:
            raise ValueError("cannot retract queue items that are no longer pending")

    async def retract_work(self, item_ids: list[int], *, persisted: bool = True) -> None:
        if self.outbox is None:
            self.retract_many(item_ids)
            return
        missing = set(item_ids) - {item.item_id for item in self.queued_items()}
        if missing:
            raise ValueError("cannot retract queue items that are no longer pending")
        self._remove_ids(set(item_ids))
        if persisted:
            await self.outbox.retract_many(item_ids, self._elapsed.now())

    def release_many(self, item_ids: list[int]) -> None:
        item_set = set(item_ids)
        if not item_set <= self._held_ids:
            raise ValueError("cannot release queue items that are not held")
        self._held_ids.difference_update(item_set)

    async def release_work(self, item_ids: list[int]) -> None:
        if self.outbox is None:
            self.release_many(item_ids)
            return
        item_set = set(item_ids)
        if not item_set <= self._held_ids:
            raise ValueError("cannot release queue items that are not held")
        assert self.outbox is not None
        await self.outbox.release_many(item_ids)
        self._held_ids.difference_update(item_set)

    def airtime_breakdown(self) -> dict[str, float]:
        self._prune_history(self.clock.monotonic())
        return {
            traffic_class.value: self.class_airtime(traffic_class) for traffic_class in TrafficClass
        }

    def _prune_history(self, now: float) -> None:
        while self.history and self.history[0][0] <= now - 3_600:
            self.history.popleft()
        while self._time_airtime and self._time_airtime[0][0] <= now - 3_600:
            self._time_airtime.popleft()
        self._recent = {
            key: timestamp
            for key, timestamp in self._recent.items()
            if timestamp + self.config.dedupe_window_s > now
        }

    @property
    def used_airtime(self) -> float:
        self._prune_history(self.clock.monotonic())
        return sum(entry[1] for entry in self.history)

    @property
    def noncritical_airtime(self) -> float:
        self._prune_history(self.clock.monotonic())
        return sum(
            seconds
            for _, seconds, cls, severity in self.history
            if not (cls == TrafficClass.ALERT and severity == Severity.CRITICAL)
        )

    def class_airtime(self, traffic_class: TrafficClass) -> float:
        self._prune_history(self.clock.monotonic())
        return sum(seconds for _, seconds, cls, _ in self.history if cls == traffic_class)

    def _quiet(self, cls: TrafficClass) -> bool:
        if cls.value not in self.config.quiet_hours.classes or cls == TrafficClass.ALERT:
            return False
        current = self.clock.now().astimezone(self.timezone).time().replace(tzinfo=None)
        # QuietHours validates these before startup; retaining a safe fallback here keeps a
        # post-startup mutation from taking down the dispatch loop.
        try:
            start = time.fromisoformat(self.config.quiet_hours.start)
            end = time.fromisoformat(self.config.quiet_hours.end)
        except ValueError:
            return False
        return start <= current < end if start < end else current >= start or current < end

    def _available(self, item: OutboundItem, now: float) -> bool:
        return item.item_id not in self._held_ids and item.next_attempt_at <= now

    def _dispatch_candidates(self) -> Iterator[OutboundItem]:
        """Visit each available candidate once, without reordering deferred work.

        Airtime preflight may reject a candidate without blocking later work. Stable
        sorting preserves FIFO among equal priorities within an alert severity/class.
        Round-robin advances only as far as the class visited by the caller.
        """
        yield from sorted(
            (item for item in self.queues[TrafficClass.ALERT]),
            key=lambda item: (ALERT_SEVERITY_ORDER.index(item.severity), -item.priority),
        )
        for _ in range(len(self._rr)):
            cls = self._rr[0]
            self._rr.rotate(-1)
            yield from sorted(
                self.queues[cls],
                key=lambda item: -item.priority,
            )

    def _expired(self, item: OutboundItem) -> bool:
        if item.guard_kind == "federation-time-v1":
            return self.clock.monotonic() > item.created_at + 30
        if self._time_blocked(item):
            return False
        return item.expires_at <= self.clock.monotonic() or (
            self.outbox is not None and item.expires_at_epoch <= self._elapsed.now()
        )

    def _time_blocked(self, item: OutboundItem) -> bool:
        if self.time_recovery_guard(item):
            return False
        if time_status(self.clock).timestamp_safe:
            self._time_started_safe = True
            return self._time_recovery_pending
        return (
            not self._time_started_safe
            or self._time_recovery_pending
            or item.traffic_class not in {TrafficClass.REPLY, TrafficClass.ALERT}
        )

    def _dispatch_policy(self, item: OutboundItem, cost: float) -> str | None:
        """Current, synchronous eligibility evidence at selection/reservation."""
        self._check_publication()
        if self._time_blocked(item):
            return "time_uncertain"
        now = self.clock.monotonic()
        if now < self._startup_not_before:
            return "time_recovery_cooldown"
        if self._expired(item):
            return "expired"
        if (
            self.link.state != LinkState.UP
            or now < self._next_tx_at
            or not self._available(item, now)
        ):
            return "unavailable"
        cls = item.traffic_class
        critical = cls == TrafficClass.ALERT and item.severity == Severity.CRITICAL
        budget = 3_600 * self.budget_percent / 100
        total = 3_600 * (self.budget_percent + self.reserve_percent) / 100
        if self.used_airtime + cost > (total if critical else budget) or (
            not critical
            and self.class_airtime(cls) + cost > budget * self.config.class_shares.get(cls.value, 0)
        ):
            return "budget"
        if (
            cls != TrafficClass.ALERT
            and self.channel_utilisation is not None
            and self.channel_utilisation >= self.config.utilisation_ceiling
        ):
            return "utilisation"
        if (
            cls in DISCRETIONARY_POWER_CLASSES
            and self.power_config.shed_discretionary
            and self.battery_level is not None
            and self.battery_level <= self.power_config.shed_below_percent
        ):
            return "low_power"
        if self._quiet(cls):
            return "unavailable"
        portnum = item.portnum or (1 if item.binary_payload is None else 260)
        if cost != self.estimate_toa(item.payload_size, portnum=portnum):
            return "profile_changed"
        return None

    def _account_attempt(self, item: OutboundItem, cost: float) -> None:
        # RF may happen anywhere inside the awaited call, even if it raises or is
        # cancelled. Retain the cost for a full hour after the latest known end.
        now = self.clock.monotonic()
        if item.guard_kind == "federation-time-v1":
            self._recovery_silence_until = now + 3_600
            self._time_airtime.append((now, cost, item.traffic_class, item.severity))
        self.history.append((now, cost, item.traffic_class, item.severity))
        AIRTIME_USED.set(self.used_airtime / 3_600)
        self._last_toa, self._next_tx_at = cost, now + self._attempt_gap(cost, item.multipart)

    def _attempt_gap(self, cost: float, multipart: bool) -> float:
        gap = max(self.config.min_gap_s, 4 * cost)
        if multipart:
            gap = max(gap, self.config.interpart_delay_s)
        return gap

    async def tick(self) -> OutboundItem | None:
        # Preserve the sole-egress contract across all awaits, including callers
        # outside the normal supervised loop. Admission and cancellation stay free.
        async with self._dispatch_lock:
            return await self._tick()

    async def _tick(self) -> OutboundItem | None:
        self._check_publication()
        await self._load_time_recovery()
        if self._time_recovery_pending and time_status(self.clock).timestamp_safe:
            await self.recover()
        if self.clock.monotonic() < self._startup_not_before:
            self.metrics.throttled["time_recovery_cooldown"] += 1
            return None
        now = self.clock.monotonic()
        if (
            self.outbox is not None
            and self._recovery_silence_until
            and now >= self._recovery_silence_until
        ):
            await self.outbox.database.write(
                "DELETE FROM runtime_setting WHERE key='airtime.time_recovery'"
            )
            self._recovery_silence_until = 0
        self._prune_history(now)
        for traffic_class, pending in self.queues.items():
            for expired in tuple(item for item in pending if self._expired(item)):
                if expired not in pending:
                    continue  # Cancellation/supersession can run during an earlier expiry write.
                pending.remove(expired)
                self._held_ids.discard(expired.item_id)
                self.metrics.dropped[(traffic_class, "expired")] += 1
                OUTBOUND_DROPPED.labels(traffic_class.value, "expired").inc()
                if self.outbox is not None:
                    await self.outbox.expire(expired.item_id, self._elapsed.now())
            QUEUE_DEPTH.labels(traffic_class.value).set(len(pending))
        if (
            self.outbox is not None
            and not self._time_recovery_pending
            and now >= self._next_outbox_sweep_at
        ):
            await self.outbox.expire_ack_waits(
                self._elapsed.now(),
                current_time=lambda: self._elapsed.now(),
            )
            self._next_outbox_sweep_at = self.clock.monotonic() + 30
        if self.link.state != LinkState.UP or self.clock.monotonic() < self._next_tx_at:
            return None
        telemetry = await self.link.local_telemetry()
        self._check_publication()
        self.channel_utilisation = telemetry.channel_utilisation
        CHANNEL_UTIL.set(telemetry.channel_utilisation / 100)
        AIR_UTIL_TX.set(telemetry.air_util_tx / 100)
        self.battery_level = normalize_battery_level(telemetry.battery_level)
        RADIO_BATTERY_REPORTED.set(int(self.battery_level is not None))
        RADIO_BATTERY_LEVEL.set(
            float(self.battery_level) if self.battery_level is not None else float("nan")
        )
        if self.power_observer is not None:
            try:
                await self.power_observer(telemetry.battery_level)
            except Exception:
                # Power history must never become a new failure boundary for alert egress.
                RADIO_POWER_OBSERVATION_FAILURES.inc()
        total_s = 3_600 * (self.budget_percent + self.reserve_percent) / 100
        if self.used_airtime >= total_s:
            self.metrics.hard_stops += 1
            return None
        throttled_classes: set[TrafficClass] = set()
        reason = "unavailable"
        item = None
        for candidate in self._dispatch_candidates():
            cls = candidate.traffic_class
            # Persisting a rejected payload below yields to other work. A later
            # candidate in the snapshot may have been cancelled or superseded.
            if candidate not in self.queues[cls]:
                continue
            if self._expired(candidate):
                self._remove_ids({candidate.item_id})
                self.metrics.dropped[(cls, "expired")] += 1
                OUTBOUND_DROPPED.labels(cls.value, "expired").inc()
                if self.outbox is not None:
                    await self.outbox.expire(candidate.item_id, self._elapsed.now())
                continue
            try:
                portnum = candidate.portnum or (1 if candidate.binary_payload is None else 260)
                cost = self.estimate_toa(candidate.payload_size, portnum=portnum)
            except (KeyError, ValueError) as error:
                self.queues[cls].remove(candidate)
                self.metrics.dropped[(cls, "invalid_payload")] += 1
                OUTBOUND_DROPPED.labels(cls.value, "invalid_payload").inc()
                QUEUE_DEPTH.labels(cls.value).set(len(self.queues[cls]))
                if self.outbox is not None:
                    await self.outbox.fail_unstarted(
                        candidate.item_id,
                        self._elapsed.now(),
                        f"{type(error).__name__}: {error}",
                    )
                continue
            # Both ceilings are admission-to-transmit checks, not reasons to stop
            # looking. Another class or a smaller packet may still fit this tick.
            current_reason = self._dispatch_policy(candidate, cost)
            if current_reason is not None:
                reason = current_reason
                if reason == "budget":
                    throttled_classes.add(cls)
                continue
            candidate.estimated_toa = cost
            item = candidate
            break
        for throttled_class in throttled_classes:
            self.metrics.throttled[throttled_class] += 1
        if item is None:
            if any(self.queues.values()) and not throttled_classes:
                self.metrics.throttled[reason] += 1
            return None
        cls = item.traffic_class
        self._check_publication()
        queue = self.queues[cls]
        cost = item.estimated_toa
        dispatch = replace(item)
        if self.outbox is not None:
            expected = {
                "text": dispatch.text,
                "binary_payload": dispatch.binary_payload,
                "destination": dispatch.dest,
                "channel": dispatch.channel,
                "portnum": dispatch.portnum,
                "want_ack": dispatch.want_ack,
                "priority": dispatch.priority,
                "traffic_class": dispatch.traffic_class.value,
                "severity": dispatch.severity.value,
                "guard_kind": dispatch.guard_kind,
            }
            try:
                started = await self.outbox.start_attempt(
                    item.item_id,
                    self._elapsed.now(),
                    round(cost * 1_000),
                    current_time=lambda: self._elapsed.now(),
                    dispatch_policy=lambda: self._dispatch_policy(dispatch, cost),
                    **({"expected": expected} if dispatch.guard_kind is not None else {}),
                )
            except OutboxDeferred:
                return None
            if not started:
                self._remove_ids({item.item_id})
                return None
            item.attempts += 1
        self._remove_ids({item.item_id})
        self._check_publication()
        try:
            if dispatch.binary_payload is None:
                item.send_result = await self.link._send_text(
                    dispatch.text,
                    dest=dispatch.dest,
                    channel=dispatch.channel,
                    want_ack=dispatch.want_ack if dispatch.dest != "^all" else False,
                    priority=dispatch.priority,
                )
            else:
                item.send_result = await self.link._send_data(
                    dispatch.binary_payload,
                    dest=dispatch.dest,
                    channel=dispatch.channel,
                    portnum=dispatch.portnum or 260,
                    want_ack=dispatch.want_ack,
                )
        except BaseException as error:
            self._account_attempt(dispatch, cost)
            if not isinstance(error, Exception):
                # Leave the durable started attempt for fail-closed recovery.
                raise
            if self.outbox is None:
                queue.appendleft(item)
                raise
            # The radio call may have crossed the physical transmit boundary before raising.
            # Count it conservatively so retries cannot bypass the rolling airtime ceiling.
            state, retry_epoch, attempts = await self.outbox.fail_attempt(
                item.item_id,
                self._elapsed.now(),
                f"{type(error).__name__}: {error}",
                current_time=lambda: self._elapsed.now(),
            )
            item.attempts = attempts
            if state == "pending" and retry_epoch is not None:
                item.next_attempt_at = self.clock.monotonic() + max(
                    0.0, retry_epoch - self._elapsed.now()
                )
                queue.append(item)
            else:
                self.metrics.dropped[(cls, "send_failed")] += 1
                OUTBOUND_DROPPED.labels(cls.value, "send_failed").inc()
            QUEUE_DEPTH.labels(cls.value).set(len(queue))
            return None
        self._account_attempt(dispatch, cost)
        self.metrics.sent[cls] += 1
        TOA_SECONDS.observe(cost)
        AIRTIME_USED.set(self.used_airtime / 3_600)
        OUTBOUND_SENT.labels(cls.value, "broadcast" if item.dest == "^all" else "direct").inc()
        QUEUE_DEPTH.labels(cls.value).set(len(queue))
        if self.outbox is not None:
            result = item.send_result
            await self.outbox.complete_attempt(
                item.item_id,
                now=self._elapsed.now(),
                packet_id=result.packet_id if result else None,
                outcome=result.outcome if result else "timeout",
                peer_mesh_id=dispatch.dest,
                channel=dispatch.channel,
                portnum=dispatch.portnum or (1 if dispatch.binary_payload is None else 260),
                text=dispatch.text if dispatch.binary_payload is None else None,
                byte_len=dispatch.payload_size,
                toa_ms=round(cost * 1_000),
                airtime_class=dispatch.traffic_class.value,
                is_direct=dispatch.dest != "^all",
                wait_for_ack=dispatch.want_ack and dispatch.dest != "^all",
            )
        return item

    async def _load_time_recovery(self) -> None:
        if self._recovery_loaded or self.outbox is None:
            return
        rows = await self.outbox.database.read(
            "SELECT value FROM runtime_setting WHERE key='airtime.time_recovery'"
        )
        now = self.clock.monotonic()
        if not self._time_started_safe:
            self._startup_not_before = self.clock.monotonic() + 3_600
        if rows:
            # Unknown completion time after a crash: charge every reserved probe
            # for a full hour from this process. No UTC labels are needed.
            try:
                costs = json.loads(rows[0]["value"])
                valid = (
                    isinstance(costs, list)
                    and 0 < len(costs) <= 512
                    and all(type(c) in {int, float} and 0 < c <= 3_600 for c in costs)
                    and sum(costs) <= 3_600
                )
            except (TypeError, ValueError):
                valid = False
            if valid:
                for cost in costs:
                    entry = (now, float(cost), TrafficClass.FEDERATION, Severity.INFO)
                    self._time_airtime.append(entry)
                    self.history.append(entry)
            else:
                # An older marker or corrupt accounting cannot waive unknown RF.
                self._startup_not_before = now + 3_600
            self._recovery_silence_until = now + 3_600
        self._recovery_loaded = True

    async def reserve_time_airtime(self, transaction: Transaction, cost: float) -> None:
        """Persist conservative probe costs in the same transaction as reservation."""
        self._prune_history(self.clock.monotonic())
        costs = [entry[1] for entry in self._time_airtime] + [cost]
        await transaction.write(
            "INSERT INTO runtime_setting(key,value,updated_at) VALUES('airtime.time_recovery',?,0) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(costs),),
        )

    @property
    def time_recovery_wait(self) -> float:
        return max(0, self._startup_not_before - self.clock.monotonic())

    def queue_depths(self) -> dict[str, int]:
        return {cls.value: len(queue) for cls, queue in self.queues.items()}

    def alert_delivery_status(self) -> dict[str, int]:
        dropped = sum(
            count
            for (traffic_class, _), count in self.metrics.dropped.items()
            if traffic_class in {TrafficClass.ALERT, TrafficClass.ALERT.value}
        )
        return {
            "queued": len(self.queues[TrafficClass.ALERT]),
            "enqueued": self.metrics.enqueued[TrafficClass.ALERT],
            "sent": self.metrics.sent[TrafficClass.ALERT],
            "throttled": self.metrics.throttled[TrafficClass.ALERT],
            "budget_delays": self.metrics.throttled["budget"],
            "utilisation_delays": self.metrics.throttled["utilisation"],
            "hard_stops": self.metrics.hard_stops,
            "dropped": dropped,
        }
