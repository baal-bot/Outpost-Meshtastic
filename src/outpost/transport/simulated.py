from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from outpost.clock import Clock

from .models import InboundMessage, LinkState, LocalTelemetry, RadioSnapshot, SendResult


@dataclass(frozen=True)
class SentPacket:
    text: str | None
    payload: bytes | None
    dest: str
    channel: int
    want_ack: bool


class SimulatedRadioLink:
    def __init__(
        self,
        clock: Clock | None = None,
        *,
        node_id: str = "!00000001",
        region: str = "US",
        preset: str = "LONG_FAST",
        channels: frozenset[int] = frozenset(range(8)),
    ) -> None:
        self.clock = clock
        self._state = LinkState.DOWN
        self.sent: list[SentPacket] = []
        self.telemetry = LocalTelemetry()
        self.received: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._last_activity = 0.0
        self._local_id = node_id
        self._snapshot = RadioSnapshot(
            node_id=node_id,
            region=region,
            preset=preset,
            channels=channels,
        )
        self._connection_generation = 0

    @property
    def state(self) -> LinkState:
        return self._state

    @property
    def last_activity(self) -> float:
        return self._last_activity

    @property
    def local_node_id(self) -> str:
        return self._local_id

    @property
    def snapshot(self) -> RadioSnapshot:
        return self._snapshot

    @property
    def connection_generation(self) -> int:
        return self._connection_generation

    async def connect(self) -> None:
        self._state = LinkState.UP
        self._connection_generation += 1
        self._touch()

    async def close(self) -> None:
        self._state = LinkState.DOWN

    def _touch(self) -> None:
        if self.clock is not None:
            self._last_activity = self.clock.monotonic()

    async def inject(self, message: InboundMessage) -> None:
        if self._state is not LinkState.UP:
            raise ConnectionError("simulated radio is down")
        self._touch()
        await self.received.put(message)

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
        if self._state is not LinkState.UP:
            raise ConnectionError("simulated radio is down")
        self._touch()
        self.sent.append(SentPacket(text, None, dest, channel, want_ack))
        return SendResult(len(self.sent), "pending" if want_ack else "not_requested")

    async def _send_data(
        self, payload: bytes, *, dest: str, channel: int, portnum: int, want_ack: bool
    ) -> SendResult:
        if self._state is not LinkState.UP:
            raise ConnectionError("simulated radio is down")
        self._touch()
        self.sent.append(SentPacket(None, payload, dest, channel, want_ack))
        return SendResult(len(self.sent), "pending" if want_ack else "not_requested")

    async def local_telemetry(self) -> LocalTelemetry:
        return self.telemetry

    async def configuration_status(self) -> dict[str, Any]:
        return {
            "available": True,
            "stale": False,
            "verified_at": (int(self.clock.now().timestamp()) if self.clock is not None else None),
            "connection": "simulated",
            "node_id": self._local_id,
            "identity": {"long_name": "Replay radio", "short_name": "DRIL"},
            "device": {"role": "CLIENT", "rebroadcast_mode": "LOCAL_ONLY"},
            "lora": {
                "region": self._snapshot.region,
                "modem_preset": self._snapshot.preset,
                "frequency_slot": 0,
                "hop_limit": 3,
                "tx_power": 0,
                "tx_enabled": False,
            },
            "position": {
                "fixed_position": False,
                "gps_mode": "DISABLED",
                "smart_broadcast": False,
                "broadcast_secs": 0,
                "latitude": self._snapshot.latitude,
                "longitude": self._snapshot.longitude,
                "altitude": 0,
            },
            "channels": [
                {
                    "index": index,
                    "role": "PRIMARY" if index == 0 else "SECONDARY",
                    "name": f"Replay {index}",
                    "psk": "redacted",
                    "uplink_enabled": False,
                    "downlink_enabled": False,
                    "position_precision": 0,
                    "muted": False,
                }
                for index in sorted(self._snapshot.channels)
            ],
            "mqtt": await self.mqtt_status(),
            "options": {},
            "recommendations": {},
            "warnings": ["Drill mode uses a simulated radio. No packet can reach RF or MQTT."],
        }

    async def refresh_configuration(self) -> dict[str, Any]:
        return await self.configuration_status()

    async def mqtt_status(self) -> dict[str, Any]:
        return {
            "available": False,
            "enabled": False,
            "connection": "simulated",
            "channels": [],
        }

    async def configure_mqtt(self, **_values: Any) -> dict[str, Any]:
        raise RuntimeError("radio configuration is disabled in drill mode")

    async def capture_configuration(self, _section: str) -> dict[str, Any]:
        raise RuntimeError("radio configuration is disabled in drill mode")

    async def configure(self, _section: str, _values: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("radio configuration is disabled in drill mode")

    async def restore_configuration(
        self, _section: str, _snapshot: dict[str, Any], *, channel_index: int | None = None
    ) -> None:
        del channel_index
        raise RuntimeError("radio configuration is disabled in drill mode")
