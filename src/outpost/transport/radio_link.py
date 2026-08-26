from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from outpost.clock import Clock
from outpost.config import RadioConfig

from .metrics import INBOUND_DROPPED, INBOUND_QUEUE_DEPTH
from .models import InboundMessage, LinkState, LocalTelemetry, RadioSnapshot, SendResult


class MeshtasticRadioLink:
    """Thread-safe asyncio bridge around Meshtastic's synchronous pubsub client."""

    def __init__(self, config: RadioConfig, clock: Clock, *, queue_size: int = 512) -> None:
        self.config, self.clock = config, clock
        self._state = LinkState.DOWN
        self._interface: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(queue_size)
        self._local_id = ""
        self._last_rx = 0.0
        self._snapshot = RadioSnapshot()
        self._inbound_received = 0
        self._inbound_dropped = 0
        self._last_inbound_drop_at: int | None = None

    @property
    def state(self) -> LinkState:
        return self._state

    @property
    def last_activity(self) -> float:
        return self._last_rx

    @property
    def local_node_id(self) -> str:
        return self._local_id

    @property
    def snapshot(self) -> RadioSnapshot:
        return self._snapshot

    def _construct_interface(self) -> Any:
        if self.config.transport == "serial":
            from meshtastic.serial_interface import SerialInterface  # type: ignore[import-untyped]

            return SerialInterface(devPath=self.config.serial.port)
        if self.config.transport == "tcp":
            from meshtastic.tcp_interface import TCPInterface  # type: ignore[import-untyped]

            return TCPInterface(hostname=self.config.tcp.host)
        from meshtastic.ble_interface import BLEInterface  # type: ignore[import-untyped]

        return BLEInterface(self.config.ble.address)

    @staticmethod
    def _enum_name(message: Any, field: str) -> str:
        descriptor = message.DESCRIPTOR.fields_by_name[field]
        value = int(getattr(message, field))
        return str(descriptor.enum_type.values_by_number[value].name)

    async def connect(self) -> None:
        if self._state in {LinkState.CONNECTING, LinkState.UP}:
            return
        self._state = LinkState.CONNECTING
        self._loop = asyncio.get_running_loop()
        try:
            self._interface = await asyncio.to_thread(self._construct_interface)
            from pubsub import pub

            pub.subscribe(self._on_receive, "meshtastic.receive")
            pub.subscribe(self._on_lost, "meshtastic.connection.lost")
            node_info = getattr(self._interface, "myInfo", None)
            node_num = getattr(node_info, "my_node_num", 0)
            self._local_id = f"!{node_num:08x}" if node_num else ""
            local_node = getattr(self._interface, "localNode", None)
            lora = getattr(getattr(local_node, "localConfig", None), "lora", None)
            region = "unknown"
            preset = "unknown"
            if lora is not None:
                with contextlib.suppress(Exception):
                    region = self._enum_name(lora, "region")
                with contextlib.suppress(Exception):
                    preset = self._enum_name(lora, "modem_preset")
            channels = getattr(local_node, "channels", []) or []
            active_channels = frozenset(
                index for index, channel in enumerate(channels) if int(channel.role) != 0
            )
            position: dict[str, Any] = {}
            with contextlib.suppress(Exception):
                position = (self._interface.getMyNodeInfo() or {}).get("position", {})
            latitude = position.get("latitude")
            longitude = position.get("longitude")
            if latitude is None and position.get("latitudeI") is not None:
                latitude = float(position["latitudeI"]) / 10_000_000
            if longitude is None and position.get("longitudeI") is not None:
                longitude = float(position["longitudeI"]) / 10_000_000
            self._snapshot = RadioSnapshot(
                node_id=self._local_id,
                region=region,
                preset=preset,
                channels=active_channels,
                latitude=float(latitude) if latitude is not None else None,
                longitude=float(longitude) if longitude is not None else None,
            )
            self._last_rx = self.clock.monotonic()
            self._state = LinkState.UP
        except Exception:
            self._state = LinkState.DOWN
            await self.close()
            raise

    async def close(self) -> None:
        interface, self._interface = self._interface, None
        if interface is not None:
            await asyncio.to_thread(interface.close)
        self._state = LinkState.DOWN

    def _on_lost(self, interface: Any = None, **_: Any) -> None:
        self._state = LinkState.DOWN

    def _put_from_thread(self, message: InboundMessage) -> None:
        self._last_rx = self.clock.monotonic()
        self._inbound_received += 1
        if self._inbound.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._inbound.get_nowait()
                self._inbound_dropped += 1
                self._last_inbound_drop_at = int(self.clock.now().timestamp())
                INBOUND_DROPPED.labels("radio_queue_full").inc()
        self._inbound.put_nowait(message)
        INBOUND_QUEUE_DEPTH.labels("radio").set(self._inbound.qsize())

    def inbound_status(self) -> dict[str, int | None]:
        return {
            "depth": self._inbound.qsize(),
            "capacity": self._inbound.maxsize,
            "received": self._inbound_received,
            "dropped": self._inbound_dropped,
            "last_drop_at": self._last_inbound_drop_at,
        }

    @staticmethod
    def _portnum(value: object) -> int:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            from meshtastic.protobuf import portnums_pb2  # type: ignore[import-untyped]

            return int(portnums_pb2.PortNum.Value(value))
        return 0

    def _on_receive(self, packet: dict[str, Any], interface: Any = None, **_: Any) -> None:
        """Pubsub callback: construct and hand off only; never parse, log, or perform I/O."""
        if self._loop is None:
            return
        decoded = packet.get("decoded", {})
        sender_num = int(packet.get("from", 0))
        recipient_num = int(packet.get("to", 0xFFFFFFFF))
        sender = packet.get("fromId") or f"!{sender_num:08x}"
        recipient = packet.get("toId") or (
            "^all" if recipient_num == 0xFFFFFFFF else f"!{recipient_num:08x}"
        )
        payload = decoded.get("payload")
        if isinstance(payload, str):
            payload = payload.encode()
        position = decoded.get("position") or {}
        latitude = position.get("latitude")
        longitude = position.get("longitude")
        if latitude is None and position.get("latitudeI") is not None:
            latitude = float(position["latitudeI"]) / 10_000_000
        if longitude is None and position.get("longitudeI") is not None:
            longitude = float(position["longitudeI"]) / 10_000_000
        message = InboundMessage(
            packet_id=int(packet.get("id", 0)),
            from_id=sender,
            to_id=recipient,
            channel=int(packet.get("channel", 0)),
            portnum=self._portnum(decoded.get("portnum", 0)),
            is_direct=recipient == self._local_id,
            text=decoded.get("text"),
            payload=payload,
            rx_time=self.clock.now(),
            rx_snr=packet.get("rxSnr"),
            rx_rssi=packet.get("rxRssi"),
            hops_away=packet.get("hopsAway"),
            want_ack=bool(packet.get("wantAck", False)),
            pki_encrypted=bool(packet.get("pkiEncrypted", False)),
            via_mqtt=bool(packet.get("viaMqtt", packet.get("via_mqtt", False))),
            request_id=decoded.get("requestId"),
            routing_error=(decoded.get("routing") or {}).get("errorReason"),
            latitude=float(latitude) if latitude is not None else None,
            longitude=float(longitude) if longitude is not None else None,
        )
        self._loop.call_soon_threadsafe(self._put_from_thread, message)

    async def inbound(self) -> AsyncIterator[InboundMessage]:
        while True:
            message = await self._inbound.get()
            INBOUND_QUEUE_DEPTH.labels("radio").set(self._inbound.qsize())
            yield message

    async def _send_text(
        self, text: str, *, dest: str, channel: int, want_ack: bool, priority: int
    ) -> SendResult:
        if self._interface is None:
            raise ConnectionError("radio is down")
        destination = "^all" if dest == "^all" else dest
        packet = await asyncio.to_thread(
            self._interface.sendText,
            text,
            destinationId=destination,
            channelIndex=channel,
            wantAck=want_ack,
        )
        return SendResult(getattr(packet, "id", None), "pending" if want_ack else "not_requested")

    async def _send_data(
        self, payload: bytes, *, dest: str, channel: int, portnum: int, want_ack: bool
    ) -> SendResult:
        if self._interface is None:
            raise ConnectionError("radio is down")
        packet = await asyncio.to_thread(
            self._interface.sendData,
            payload,
            destinationId=dest,
            portNum=portnum,
            channelIndex=channel,
            wantAck=want_ack,
        )
        return SendResult(getattr(packet, "id", None), "pending" if want_ack else "not_requested")

    async def mqtt_status(self) -> dict[str, Any]:
        if self._interface is None:
            return {"available": False, "enabled": False, "channels": []}
        local = getattr(self._interface, "localNode", None)
        module = getattr(getattr(local, "moduleConfig", None), "mqtt", None)
        channels = getattr(local, "channels", []) or []
        return {
            "available": module is not None,
            "enabled": bool(getattr(module, "enabled", False)),
            "address": str(getattr(module, "address", "")),
            "tls_enabled": bool(getattr(module, "tls_enabled", False)),
            "encryption_enabled": bool(getattr(module, "encryption_enabled", True)),
            "root": str(getattr(module, "root", "") or "msh"),
            "proxy_to_client_enabled": bool(getattr(module, "proxy_to_client_enabled", False)),
            "channels": [
                {
                    "index": index,
                    "name": str(getattr(channel.settings, "name", "") or f"Channel {index}"),
                    "uplink_enabled": bool(getattr(channel.settings, "uplink_enabled", False)),
                    "downlink_enabled": bool(getattr(channel.settings, "downlink_enabled", False)),
                }
                for index, channel in enumerate(channels)
                if int(getattr(channel, "role", 0)) != 0
            ],
        }

    async def configure_mqtt(
        self,
        *,
        enabled: bool,
        address: str,
        tls_enabled: bool,
        root: str,
        channel: int,
        uplink_enabled: bool,
        downlink_enabled: bool,
    ) -> dict[str, Any]:
        if self._interface is None:
            raise ConnectionError("radio is down")
        local = getattr(self._interface, "localNode", None)
        if local is None:
            raise ConnectionError("radio local node is unavailable")
        module = getattr(getattr(local, "moduleConfig", None), "mqtt", None)
        channels = getattr(local, "channels", []) or []
        if module is None or not 0 <= channel < len(channels):
            raise ValueError("MQTT module or selected channel is unavailable")
        module.enabled = enabled
        module.address = address.strip()
        module.tls_enabled = tls_enabled
        module.encryption_enabled = True
        module.root = root.strip() or "msh"
        module.proxy_to_client_enabled = False
        settings = channels[channel].settings
        settings.uplink_enabled = uplink_enabled
        settings.downlink_enabled = downlink_enabled
        await asyncio.to_thread(local.writeConfig, "mqtt")
        await asyncio.to_thread(local.writeChannel, channel)
        return await self.mqtt_status()

    async def local_telemetry(self) -> LocalTelemetry:
        if self._interface is None:
            return LocalTelemetry()
        node = getattr(self._interface, "nodes", {}).get(self._local_id, {})
        metrics = node.get("deviceMetrics", {}) if isinstance(node, dict) else {}
        return LocalTelemetry(
            channel_utilisation=float(metrics.get("channelUtilization", 0.0)),
            air_util_tx=float(metrics.get("airUtilTx", 0.0)),
            battery_level=metrics.get("batteryLevel"),
        )
