from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class LinkState(StrEnum):
    DOWN = "down"
    CONNECTING = "connecting"
    UP = "up"
    DEGRADED = "degraded"


class TrafficClass(StrEnum):
    ALERT = "alert"
    REPLY = "reply"
    AI = "ai"
    BULLETIN = "bulletin"
    DIGEST = "digest"
    FEDERATION = "federation"


class Severity(StrEnum):
    INFO = "info"
    CAUTION = "caution"
    URGENT = "urgent"
    CRITICAL = "critical"


@dataclass(frozen=True)
class InboundMessage:
    packet_id: int
    from_id: str
    to_id: str
    channel: int
    portnum: int
    is_direct: bool
    text: str | None
    payload: bytes | None
    rx_time: datetime
    rx_snr: float | None = None
    rx_rssi: int | None = None
    hops_away: int | None = None
    want_ack: bool = False
    pki_encrypted: bool = False
    pki_public_key: bytes | None = None
    via_mqtt: bool = False
    no_reply: bool = False
    request_id: int | None = None
    routing_error: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True)
class SendResult:
    packet_id: int | None
    outcome: str = "pending"


@dataclass(frozen=True)
class LocalTelemetry:
    channel_utilisation: float = 0.0
    air_util_tx: float = 0.0
    battery_level: int | None = None


@dataclass(frozen=True)
class RadioSnapshot:
    node_id: str = ""
    region: str = "unknown"
    preset: str = "unknown"
    channels: frozenset[int] = frozenset()
    latitude: float | None = None
    longitude: float | None = None


class RadioLink(Protocol):
    @property
    def state(self) -> LinkState: ...
    @property
    def last_activity(self) -> float: ...
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    def inbound(self) -> AsyncIterator[InboundMessage]: ...
    def inbound_status(self) -> dict[str, int | None]: ...
    async def _send_text(
        self, text: str, *, dest: str, channel: int, want_ack: bool, priority: int
    ) -> SendResult: ...
    async def _send_data(
        self, payload: bytes, *, dest: str, channel: int, portnum: int, want_ack: bool
    ) -> SendResult: ...
    async def local_telemetry(self) -> LocalTelemetry: ...
