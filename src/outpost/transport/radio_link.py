from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import secrets
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
        self._config_lock = asyncio.Lock()

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

    @staticmethod
    def _enum_options(message: Any, field: str) -> list[str]:
        descriptor = message.DESCRIPTOR.fields_by_name[field]
        return [str(value.name) for value in descriptor.enum_type.values]

    @staticmethod
    def _set_enum(message: Any, field: str, value: str) -> None:
        descriptor = message.DESCRIPTOR.fields_by_name[field]
        option = descriptor.enum_type.values_by_name.get(value.upper())
        if option is None:
            raise ValueError(f"unsupported {field.replace('_', ' ')}: {value}")
        setattr(message, field, option.number)

    def _local_node(self) -> Any:
        if self._interface is None:
            raise ConnectionError("radio is down")
        local = getattr(self._interface, "localNode", None)
        if local is None:
            raise ConnectionError("radio local node is unavailable")
        return local

    @staticmethod
    def _psk_kind(value: Any) -> str:
        size = len(bytes(value or b""))
        return {0: "open", 1: "default", 16: "AES-128", 32: "AES-256"}.get(
            size, f"{size}-byte key"
        )

    async def configuration_status(self) -> dict[str, Any]:
        if self._interface is None:
            return {"available": False, "connection": self.config.transport}
        local = self._local_node()
        local_config = getattr(local, "localConfig", None)
        module_config = getattr(local, "moduleConfig", None)
        if local_config is None or module_config is None:
            return {"available": False, "connection": self.config.transport}
        device = local_config.device
        lora = local_config.lora
        position = local_config.position
        node_info: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            node_info = self._interface.getMyNodeInfo() or {}
        user = node_info.get("user", {}) if isinstance(node_info, dict) else {}
        raw_position = node_info.get("position", {}) if isinstance(node_info, dict) else {}
        node_position = raw_position if isinstance(raw_position, dict) else {}
        channels = getattr(local, "channels", []) or []
        mqtt = await self.mqtt_status()
        role = self._enum_name(device, "role")
        region = self._enum_name(lora, "region")
        preset = self._enum_name(lora, "modem_preset")
        warnings: list[str] = []
        if region == "UNSET":
            warnings.append("A legal LoRa region must be selected before transmitting.")
        if role not in {"CLIENT", "CLIENT_BASE"}:
            warnings.append(f"{role} is not an Outpost-recommended connected-radio role.")
        if bool(getattr(lora, "override_duty_cycle", False)):
            warnings.append("The radio is overriding its regional duty-cycle limit.")
        if float(getattr(lora, "override_frequency", 0)) != 0:
            warnings.append("The radio is using an advanced frequency override.")
        return {
            "available": True,
            "connection": self.config.transport,
            "node_id": self._local_id,
            "identity": {
                "long_name": str(user.get("longName", user.get("long_name", ""))),
                "short_name": str(user.get("shortName", user.get("short_name", ""))),
            },
            "device": {
                "role": role,
                "rebroadcast_mode": self._enum_name(device, "rebroadcast_mode"),
                "node_info_broadcast_secs": int(
                    getattr(device, "node_info_broadcast_secs", 0)
                ),
            },
            "lora": {
                "region": region,
                "modem_preset": preset,
                "frequency_slot": int(getattr(lora, "channel_num", 0)),
                "hop_limit": int(getattr(lora, "hop_limit", 3)),
                "tx_power": int(getattr(lora, "tx_power", 0)),
                "tx_enabled": bool(getattr(lora, "tx_enabled", True)),
            },
            "position": {
                "fixed_position": bool(getattr(position, "fixed_position", False)),
                "gps_mode": self._enum_name(position, "gps_mode"),
                "smart_broadcast": bool(
                    getattr(position, "position_broadcast_smart_enabled", True)
                ),
                "broadcast_secs": int(getattr(position, "position_broadcast_secs", 0)),
                "latitude": node_position.get("latitude", self._snapshot.latitude),
                "longitude": node_position.get("longitude", self._snapshot.longitude),
                "altitude": int(node_position.get("altitude", 0)),
            },
            "channels": [
                {
                    "index": index,
                    "role": self._enum_name(channel, "role"),
                    "name": str(getattr(channel.settings, "name", "")),
                    "psk": self._psk_kind(getattr(channel.settings, "psk", b"")),
                    "uplink_enabled": bool(
                        getattr(channel.settings, "uplink_enabled", False)
                    ),
                    "downlink_enabled": bool(
                        getattr(channel.settings, "downlink_enabled", False)
                    ),
                    "position_precision": int(
                        getattr(channel.settings.module_settings, "position_precision", 0)
                    ),
                    "muted": bool(
                        getattr(channel.settings.module_settings, "is_muted", False)
                    ),
                }
                for index, channel in enumerate(channels)
            ],
            "mqtt": mqtt,
            "options": {
                "roles": ["CLIENT", "CLIENT_BASE"],
                "rebroadcast_modes": [
                    value
                    for value in self._enum_options(device, "rebroadcast_mode")
                    if value in {"ALL", "LOCAL_ONLY", "CORE_PORTNUMS_ONLY"}
                ],
                "regions": [
                    value for value in self._enum_options(lora, "region") if value != "UNSET"
                ],
                "modem_presets": [
                    value
                    for value in self._enum_options(lora, "modem_preset")
                    if value != "UNSET"
                ],
                "gps_modes": self._enum_options(position, "gps_mode"),
            },
            "recommendations": {
                "role": "CLIENT",
                "modem_preset": "LONG_FAST",
                "hop_limit": 3,
                "tx_power": 0,
                "mqtt_encryption": True,
                "mqtt_tls": True,
            },
            "warnings": warnings,
        }

    async def configure(self, section: str, values: dict[str, Any]) -> dict[str, Any]:
        if section == "mqtt":
            return await self.configure_mqtt(**values)
        local = self._local_node()
        generated_psk: str | None = None
        async with self._config_lock:
            if section == "identity":
                long_name = str(values["long_name"]).strip()
                short_name = str(values["short_name"]).strip()
                if not long_name or len(long_name.encode()) > 40:
                    raise ValueError("long name must be 1 to 40 UTF-8 bytes")
                if not 1 <= len(short_name.encode()) <= 4:
                    raise ValueError("short name must be 1 to 4 UTF-8 bytes")
                await asyncio.to_thread(local.setOwner, long_name, short_name)
            elif section == "device":
                role = str(values["role"]).upper()
                if role not in {"CLIENT", "CLIENT_BASE"}:
                    raise ValueError("Outpost-connected radios must use CLIENT or CLIENT_BASE")
                device = local.localConfig.device
                self._set_enum(device, "role", role)
                self._set_enum(device, "rebroadcast_mode", str(values["rebroadcast_mode"]))
                device.node_info_broadcast_secs = int(values["node_info_broadcast_secs"])
                if hasattr(device, "serial_enabled"):
                    device.serial_enabled = True
                await asyncio.to_thread(local.writeConfig, "device")
            elif section == "lora":
                lora = local.localConfig.lora
                region = str(values["region"]).upper()
                if region == "UNSET":
                    raise ValueError("select the legal radio region for this installation")
                self._set_enum(lora, "region", region)
                self._set_enum(lora, "modem_preset", str(values["modem_preset"]))
                lora.use_preset = True
                frequency_slot = int(values["frequency_slot"])
                if not 0 <= frequency_slot <= 65_535:
                    raise ValueError("frequency slot must be between 0 and 65535")
                lora.channel_num = frequency_slot
                lora.hop_limit = int(values["hop_limit"])
                lora.tx_power = int(values["tx_power"])
                lora.tx_enabled = bool(values["tx_enabled"])
                lora.override_duty_cycle = False
                lora.override_frequency = 0
                await asyncio.to_thread(local.writeConfig, "lora")
            elif section == "position":
                position = local.localConfig.position
                fixed = bool(values["fixed_position"])
                position.fixed_position = fixed
                self._set_enum(position, "gps_mode", str(values["gps_mode"]))
                position.position_broadcast_smart_enabled = bool(values["smart_broadcast"])
                position.position_broadcast_secs = int(values["broadcast_secs"])
                await asyncio.to_thread(local.writeConfig, "position")
                if fixed:
                    await asyncio.to_thread(
                        local.setFixedPosition,
                        float(values["latitude"]),
                        float(values["longitude"]),
                        int(values["altitude"]),
                    )
                else:
                    await asyncio.to_thread(local.removeFixedPosition)
            elif section == "channel":
                index = int(values["index"])
                channels = getattr(local, "channels", []) or []
                if not 0 <= index < len(channels):
                    raise ValueError("channel slot is unavailable")
                role = str(values["role"]).upper()
                if (index == 0 and role != "PRIMARY") or (
                    index > 0 and role not in {"SECONDARY", "DISABLED"}
                ):
                    raise ValueError(
                        "slot 0 must be primary; later slots are secondary or disabled"
                    )
                planned_roles = [int(channel.role) for channel in channels]
                role_descriptor = channels[index].DESCRIPTOR.fields_by_name["role"].enum_type
                planned_roles[index] = role_descriptor.values_by_name[role].number
                active = [slot for slot, value in enumerate(planned_roles) if value != 0]
                if active != list(range(len(active))):
                    raise ValueError("active channel slots must be consecutive")
                channel = channels[index]
                self._set_enum(channel, "role", role)
                name = str(values["name"]).strip()
                if len(name.encode()) > 12:
                    raise ValueError("channel name must be no more than 12 UTF-8 bytes")
                channel.settings.name = name
                if bool(values.get("generate_psk")):
                    key = secrets.token_bytes(32)
                    channel.settings.psk = key
                    generated_psk = base64.b64encode(key).decode()
                elif values.get("psk"):
                    try:
                        key = base64.b64decode(str(values["psk"]), validate=True)
                    except (binascii.Error, ValueError) as error:
                        raise ValueError("channel key must be valid base64") from error
                    if len(key) not in {1, 16, 32}:
                        raise ValueError("channel key must decode to 1, 16, or 32 bytes")
                    channel.settings.psk = key
                channel.settings.uplink_enabled = bool(values["uplink_enabled"])
                channel.settings.downlink_enabled = bool(values["downlink_enabled"])
                channel.settings.module_settings.position_precision = int(
                    values["position_precision"]
                )
                channel.settings.module_settings.is_muted = bool(values["muted"])
                await asyncio.to_thread(local.writeChannel, index)
            else:
                raise ValueError("unsupported radio configuration section")
        result = await self.configuration_status()
        if section == "position" and bool(values["fixed_position"]):
            result["position"].update(
                {
                    "latitude": float(values["latitude"]),
                    "longitude": float(values["longitude"]),
                    "altitude": int(values["altitude"]),
                }
            )
        if generated_psk is not None:
            result["generated_psk"] = generated_psk
        return result

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
            "username_configured": bool(str(getattr(module, "username", ""))),
            "password_configured": bool(str(getattr(module, "password", ""))),
            "json_enabled": bool(getattr(module, "json_enabled", False)),
            "proxy_to_client_enabled": bool(getattr(module, "proxy_to_client_enabled", False)),
            "map_reporting_enabled": bool(
                getattr(module, "map_reporting_enabled", False)
            ),
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
        username: str | None = None,
        password: str | None = None,
        json_enabled: bool | None = None,
        proxy_to_client_enabled: bool | None = None,
        map_reporting_enabled: bool | None = None,
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
        async with self._config_lock:
            module.enabled = enabled
            module.address = address.strip()
            module.tls_enabled = tls_enabled
            # Outpost never permits cleartext channel payloads over MQTT.
            module.encryption_enabled = True
            module.root = root.strip() or "msh"
            if username is not None:
                module.username = username.strip()
            if password is not None:
                module.password = password
            if json_enabled is not None:
                module.json_enabled = json_enabled
            if proxy_to_client_enabled is not None:
                module.proxy_to_client_enabled = proxy_to_client_enabled
            if map_reporting_enabled is not None:
                module.map_reporting_enabled = map_reporting_enabled
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
