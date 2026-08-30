from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import time
from typing import TYPE_CHECKING

from outpost.clock import Clock
from outpost.config import AirtimeConfig
from outpost.store.outbox import OutboxRejected, OutboxStore
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
    TOA_SECONDS,
)
from .models import LinkState, RadioLink, SendResult, Severity, TrafficClass
from .toa import MAX_PAYLOAD_BYTES, toa

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


class AirtimeGovernor:
    """Deterministic sole-egress scheduler with a rolling one-hour budget."""

    def __init__(
        self,
        link: RadioLink,
        config: AirtimeConfig,
        clock: Clock,
        *,
        preset: str = "LONG_FAST",
        regional_ceiling_percent: float | None = None,
        outbox: OutboxStore | None = None,
    ) -> None:
        self.link, self.config, self.clock, self.preset = link, config, clock, preset
        self.outbox = outbox
        configured_total = config.budget_percent + config.emergency_reserve_percent
        if regional_ceiling_percent is not None and configured_total > regional_ceiling_percent:
            scale = regional_ceiling_percent / configured_total
            self.budget_percent = config.budget_percent * scale
            self.reserve_percent = config.emergency_reserve_percent * scale
        else:
            self.budget_percent = config.budget_percent
            self.reserve_percent = config.emergency_reserve_percent
        self.queues: dict[TrafficClass, deque[OutboundItem]] = {
            cls: deque() for cls in TrafficClass
        }
        self.history: deque[tuple[float, float, TrafficClass, Severity]] = deque()
        self._recent: dict[tuple[str, int, str], float] = {}
        self._held_ids: set[int] = set()
        self._next_id = 1
        self._next_tx_at = 0.0
        self._last_toa = 0.0
        self._next_outbox_sweep_at = 0.0
        self._rr = deque(cls for cls in TrafficClass if cls != TrafficClass.ALERT)
        self.metrics = GovernorMetrics()

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
        if self.outbox is None:
            return self.enqueue_many(items, hold=hold)
        if not items:
            return []
        oversized = [item for item in items if item.payload_size > MAX_PAYLOAD_BYTES]
        if oversized:
            for item in items:
                self.metrics.dropped[(item.traffic_class, "payload_too_large")] += 1
                OUTBOUND_DROPPED.labels(item.traffic_class.value, "payload_too_large").inc()
            return None
        now_mono = self.clock.monotonic()
        now_epoch = self.clock.now().timestamp()
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
                }
            )
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
            return None

        superseded = set(result.superseded_ids)
        if superseded:
            self._remove_ids(superseded)
        for item, item_id in zip(items, result.ids, strict=True):
            item.item_id = item_id
            self.queues[item.traffic_class].append(item)
            if hold:
                self._held_ids.add(item_id)
            self.metrics.enqueued[item.traffic_class] += 1
            OUTBOUND_ENQUEUED.labels(item.traffic_class.value).inc()
            QUEUE_DEPTH.labels(item.traffic_class.value).set(len(self.queues[item.traffic_class]))
        return result.ids

    async def recover(self) -> int:
        if self.outbox is None:
            return 0
        now_epoch = self.clock.now().timestamp()
        now_mono = self.clock.monotonic()
        rows = await self.outbox.recover(now_epoch)
        self.queues = {cls: deque() for cls in TrafficClass}
        self._held_ids.clear()
        self.history.clear()
        for record in await self.outbox.recent_airtime(now_epoch):
            traffic_class = TrafficClass(str(record["airtime_class"]))
            severity = Severity(str(record["severity"]))
            sent_at = now_mono - max(0.0, now_epoch - float(record["created_at"]))
            self.history.append((sent_at, float(record["toa_ms"]) / 1_000, traffic_class, severity))
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
        return len(rows)

    def enqueue_many(self, items: list[OutboundItem], *, hold: bool = False) -> list[int] | None:
        """Atomically admit a complete multi-part response (REQ-TRANSPORT-035)."""
        if self.outbox is not None:
            raise RuntimeError("durable governors require await governor.admit_many()")
        if any(item.payload_size > MAX_PAYLOAD_BYTES for item in items):
            for item in items:
                self.metrics.dropped[(item.traffic_class, "payload_too_large")] += 1
                OUTBOUND_DROPPED.labels(item.traffic_class.value, "payload_too_large").inc()
            return None
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
            return None
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
                return None
            batch_keys.add(key)
        ids = [self.enqueue(item) for item in items]
        if any(item_id is None for item_id in ids):
            raise AssertionError("atomic enqueue preflight diverged")
        admitted = [item_id for item_id in ids if item_id is not None]
        if hold:
            self._held_ids.update(admitted)
        return admitted

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
        cancelled = await self.outbox.cancel(item_id, self.clock.now().timestamp())
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
            await self.outbox.retract_many(item_ids, self.clock.now().timestamp())

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
        current = self.clock.now().time().replace(tzinfo=None)
        start = time.fromisoformat(self.config.quiet_hours.start)
        end = time.fromisoformat(self.config.quiet_hours.end)
        return start <= current < end if start < end else current >= start or current < end

    def _available(self, item: OutboundItem, now: float) -> bool:
        return item.item_id not in self._held_ids and item.next_attempt_at <= now

    def _eligible_class(
        self, *, only_critical: bool, high_util: bool, now: float
    ) -> TrafficClass | None:
        alerts = self.queues[TrafficClass.ALERT]
        available_alerts = [item for item in alerts if self._available(item, now)]
        if available_alerts:
            if not only_critical or any(
                item.severity == Severity.CRITICAL for item in available_alerts
            ):
                return TrafficClass.ALERT
        if only_critical or high_util:
            return None
        for _ in range(len(self._rr)):
            cls = self._rr[0]
            self._rr.rotate(-1)
            if any(self._available(item, now) for item in self.queues[cls]) and not self._quiet(
                cls
            ):
                return cls
        return None

    def _pop_alert(self, queue: deque[OutboundItem], now: float) -> OutboundItem:
        for severity in ALERT_SEVERITY_ORDER:
            candidates = [
                queued
                for queued in queue
                if queued.severity == severity and self._available(queued, now)
            ]
            item = max(candidates, key=lambda value: value.priority, default=None)
            if item is not None:
                queue.remove(item)
                return item
        raise AssertionError("non-empty alert queue has no severity")

    def _pop_unheld(self, queue: deque[OutboundItem], now: float) -> OutboundItem:
        candidates = [queued for queued in queue if self._available(queued, now)]
        item = max(candidates, key=lambda value: value.priority)
        queue.remove(item)
        return item

    async def tick(self) -> OutboundItem | None:
        now = self.clock.monotonic()
        now_epoch = self.clock.now().timestamp()
        self._prune_history(now)
        for traffic_class, pending in self.queues.items():
            for expired in tuple(item for item in pending if item.expires_at <= now):
                pending.remove(expired)
                self._held_ids.discard(expired.item_id)
                self.metrics.dropped[(traffic_class, "expired")] += 1
                OUTBOUND_DROPPED.labels(traffic_class.value, "expired").inc()
                if self.outbox is not None:
                    await self.outbox.expire(expired.item_id, now_epoch)
            QUEUE_DEPTH.labels(traffic_class.value).set(len(pending))
        if self.outbox is not None and now >= self._next_outbox_sweep_at:
            await self.outbox.expire_ack_waits(now_epoch)
            self._next_outbox_sweep_at = now + 30
        if self.link.state != LinkState.UP or now < self._next_tx_at:
            return None
        telemetry = await self.link.local_telemetry()
        CHANNEL_UTIL.set(telemetry.channel_utilisation / 100)
        AIR_UTIL_TX.set(telemetry.air_util_tx / 100)
        budget_s = 3_600 * self.budget_percent / 100
        total_s = 3_600 * (self.budget_percent + self.reserve_percent) / 100
        if self.used_airtime >= total_s:
            self.metrics.hard_stops += 1
            return None
        only_critical = self.noncritical_airtime >= budget_s or self.used_airtime >= budget_s
        high_util = telemetry.channel_utilisation >= self.config.utilisation_ceiling
        cls = self._eligible_class(only_critical=only_critical, high_util=high_util, now=now)
        if cls is None:
            if any(self.queues.values()):
                self.metrics.throttled["budget" if only_critical else "utilisation"] += 1
            return None
        queue = self.queues[cls]
        if cls == TrafficClass.ALERT:
            item = self._pop_alert(queue, now)
        else:
            item = self._pop_unheld(queue, now)
        try:
            cost = toa(item.payload_size, self.preset)
        except (KeyError, ValueError) as error:
            self.metrics.dropped[(cls, "invalid_payload")] += 1
            OUTBOUND_DROPPED.labels(cls.value, "invalid_payload").inc()
            QUEUE_DEPTH.labels(cls.value).set(len(queue))
            if self.outbox is not None:
                await self.outbox.fail_unstarted(
                    item.item_id,
                    now_epoch,
                    f"{type(error).__name__}: {error}",
                )
            return None
        item.estimated_toa = cost
        # Preflight prevents a packet from crossing either rolling ceiling.
        critical = cls == TrafficClass.ALERT and item.severity == Severity.CRITICAL
        ceiling = total_s if critical else budget_s
        if self.used_airtime + cost > ceiling:
            queue.appendleft(item)
            self.metrics.throttled[cls] += 1
            return None
        class_ceiling = budget_s * self.config.class_shares[cls.value]
        if not critical and self.class_airtime(cls) + cost > class_ceiling:
            queue.appendleft(item)
            self.metrics.throttled[cls] += 1
            return None
        if self.outbox is not None:
            if not await self.outbox.start_attempt(
                item.item_id, now_epoch, round(item.estimated_toa * 1_000)
            ):
                return None
            item.attempts += 1
        try:
            if item.binary_payload is None:
                item.send_result = await self.link._send_text(
                    item.text,
                    dest=item.dest,
                    channel=item.channel,
                    want_ack=item.want_ack if item.dest != "^all" else False,
                    priority=item.priority,
                )
            else:
                item.send_result = await self.link._send_data(
                    item.binary_payload,
                    dest=item.dest,
                    channel=item.channel,
                    portnum=item.portnum or 260,
                    want_ack=item.want_ack,
                )
        except Exception as error:
            if self.outbox is None:
                queue.appendleft(item)
                raise
            # The radio call may have crossed the physical transmit boundary before raising.
            # Count it conservatively so retries cannot bypass the rolling airtime ceiling.
            self.history.append((now, cost, cls, item.severity))
            AIRTIME_USED.set(self.used_airtime / 3_600)
            state, retry_epoch, attempts = await self.outbox.fail_attempt(
                item.item_id, now_epoch, f"{type(error).__name__}: {error}"
            )
            item.attempts = attempts
            if state == "pending" and retry_epoch is not None:
                item.next_attempt_at = now + max(0.0, retry_epoch - now_epoch)
                queue.append(item)
            else:
                self.metrics.dropped[(cls, "send_failed")] += 1
                OUTBOUND_DROPPED.labels(cls.value, "send_failed").inc()
            QUEUE_DEPTH.labels(cls.value).set(len(queue))
            return None
        self.history.append((now, cost, cls, item.severity))
        self.metrics.sent[cls] += 1
        TOA_SECONDS.observe(cost)
        AIRTIME_USED.set(self.used_airtime / 3_600)
        OUTBOUND_SENT.labels(cls.value, "broadcast" if item.dest == "^all" else "direct").inc()
        QUEUE_DEPTH.labels(cls.value).set(len(queue))
        gap = max(self.config.min_gap_s, 4 * cost)
        if item.multipart:
            gap = max(gap, self.config.interpart_delay_s)
        self._last_toa, self._next_tx_at = cost, now + gap
        if self.outbox is not None:
            result = item.send_result
            await self.outbox.complete_attempt(
                item.item_id,
                now=now_epoch,
                packet_id=result.packet_id if result else None,
                outcome=result.outcome if result else "timeout",
                peer_mesh_id=item.dest,
                channel=item.channel,
                portnum=item.portnum or 1,
                text=item.text if item.binary_payload is None else None,
                byte_len=item.payload_size,
                toa_ms=round(item.estimated_toa * 1_000),
                airtime_class=item.traffic_class.value,
                is_direct=item.dest != "^all",
                wait_for_ack=item.want_ack and item.dest != "^all",
            )
        return item

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
