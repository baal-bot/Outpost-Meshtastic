from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import time

from outpost.clock import Clock
from outpost.config import AirtimeConfig

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
from .toa import toa

TTL_SECONDS = {
    TrafficClass.ALERT: 86_400,
    TrafficClass.REPLY: 300,
    TrafficClass.AI: 180,
    TrafficClass.BULLETIN: 7_200,
    TrafficClass.DIGEST: 3_600,
    TrafficClass.FEDERATION: 1_800,
}


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
    item_id: int = 0
    binary_payload: bytes | None = None
    portnum: int | None = None
    multipart: bool = False
    send_result: SendResult | None = None
    estimated_toa: float = 0.0

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
    ) -> None:
        self.link, self.config, self.clock, self.preset = link, config, clock, preset
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
        self._next_id = 1
        self._next_tx_at = 0.0
        self._last_toa = 0.0
        self._rr = deque(cls for cls in TrafficClass if cls != TrafficClass.ALERT)
        self.metrics = GovernorMetrics()

    def enqueue(self, item: OutboundItem) -> int | None:
        now = self.clock.monotonic()
        if sum(map(len, self.queues.values())) >= self.config.queue_max_items:
            self.metrics.dropped[(item.traffic_class, "queue_full")] += 1
            OUTBOUND_DROPPED.labels(item.traffic_class.value, "queue_full").inc()
            return None
        payload = item.binary_payload if item.binary_payload is not None else item.text.encode()
        digest = hashlib.sha256(payload).hexdigest()
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

    def enqueue_many(self, items: list[OutboundItem]) -> list[int] | None:
        """Atomically admit a complete multi-part response (REQ-TRANSPORT-035)."""
        available = self.config.queue_max_items - sum(map(len, self.queues.values()))
        if len(items) > available:
            for item in items:
                self.metrics.dropped[(item.traffic_class, "queue_full")] += 1
            return None
        # Preflight duplicate keys so enqueue cannot partially reject the batch.
        now = self.clock.monotonic()
        batch_keys: set[tuple[str, int, str]] = set()
        for item in items:
            payload = item.binary_payload if item.binary_payload is not None else item.text.encode()
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
        return [item_id for item_id in ids if item_id is not None]

    def queued_items(self) -> list[OutboundItem]:
        return sorted(
            (item for queue in self.queues.values() for item in queue),
            key=lambda item: item.item_id,
        )

    def cancel(self, item_id: int) -> bool:
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

    def _eligible_class(self, *, only_critical: bool, high_util: bool) -> TrafficClass | None:
        alerts = self.queues[TrafficClass.ALERT]
        if alerts:
            if not only_critical or any(item.severity == Severity.CRITICAL for item in alerts):
                return TrafficClass.ALERT
        if only_critical or high_util:
            return None
        for _ in range(len(self._rr)):
            cls = self._rr[0]
            self._rr.rotate(-1)
            if self.queues[cls] and not self._quiet(cls):
                return cls
        return None

    async def tick(self) -> OutboundItem | None:
        now = self.clock.monotonic()
        self._prune_history(now)
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
        cls = self._eligible_class(only_critical=only_critical, high_util=high_util)
        if cls is None:
            if any(self.queues.values()):
                self.metrics.throttled["budget" if only_critical else "utilisation"] += 1
            return None
        queue = self.queues[cls]
        if cls == TrafficClass.ALERT and only_critical:
            item = next(item for item in queue if item.severity == Severity.CRITICAL)
            queue.remove(item)
        else:
            item = queue.popleft()
        if item.expires_at <= now:
            self.metrics.dropped[(cls, "expired")] += 1
            OUTBOUND_DROPPED.labels(cls.value, "expired").inc()
            return None
        cost = toa(item.payload_size, self.preset)
        item.estimated_toa = cost
        # Preflight prevents a packet from crossing either rolling ceiling.
        critical = cls == TrafficClass.ALERT and item.severity == Severity.CRITICAL
        ceiling = total_s if critical else budget_s
        if (
            self.used_airtime + cost > ceiling
            and cls == TrafficClass.ALERT
            and not critical
            and any(queued.severity == Severity.CRITICAL for queued in queue)
        ):
            queue.appendleft(item)
            item = next(queued for queued in queue if queued.severity == Severity.CRITICAL)
            queue.remove(item)
            cost = toa(item.payload_size, self.preset)
            item.estimated_toa = cost
            critical = True
            ceiling = total_s
        if self.used_airtime + cost > ceiling:
            queue.appendleft(item)
            self.metrics.throttled[cls] += 1
            return None
        class_ceiling = budget_s * self.config.class_shares[cls.value]
        if not critical and self.class_airtime(cls) + cost > class_ceiling:
            queue.appendleft(item)
            self.metrics.throttled[cls] += 1
            return None
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
