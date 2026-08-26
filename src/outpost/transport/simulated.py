from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from .models import InboundMessage, LinkState, LocalTelemetry, SendResult


@dataclass(frozen=True)
class SentPacket:
    text: str | None
    payload: bytes | None
    dest: str
    channel: int
    want_ack: bool


class SimulatedRadioLink:
    def __init__(self) -> None:
        self._state = LinkState.DOWN
        self.sent: list[SentPacket] = []
        self.telemetry = LocalTelemetry()
        self.received: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._last_activity = 0.0

    @property
    def state(self) -> LinkState:
        return self._state

    @property
    def last_activity(self) -> float:
        return self._last_activity

    async def connect(self) -> None:
        self._state = LinkState.UP

    async def close(self) -> None:
        self._state = LinkState.DOWN

    async def inbound(self) -> AsyncIterator[InboundMessage]:
        while True:
            yield await self.received.get()

    def inbound_status(self) -> dict[str, int | None]:
        return {
            "depth": self.received.qsize(),
            "capacity": self.received.maxsize,
            "received": None,
            "dropped": 0,
            "last_drop_at": None,
        }

    async def _send_text(
        self, text: str, *, dest: str, channel: int, want_ack: bool, priority: int
    ) -> SendResult:
        self.sent.append(SentPacket(text, None, dest, channel, want_ack))
        return SendResult(len(self.sent))

    async def _send_data(
        self, payload: bytes, *, dest: str, channel: int, portnum: int, want_ack: bool
    ) -> SendResult:
        self.sent.append(SentPacket(None, payload, dest, channel, want_ack))
        return SendResult(len(self.sent))

    async def local_telemetry(self) -> LocalTelemetry:
        return self.telemetry
