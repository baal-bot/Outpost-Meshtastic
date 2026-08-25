from __future__ import annotations

import hashlib
import hmac
import time
import zlib
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import cbor2

MAGIC = 0x4F
VERSION = 1
HEADER_SIZE = 18
# Live two-node acceptance found that nominally valid 225-233 byte application
# payloads were not reliably delivered by the test radios. Keep the complete
# private-app frame at the proven 188-byte ceiling (18-byte header + body).
FRAGMENT_BODY_SIZE = 170
FLAG_MORE = 0x01
FLAG_COMPRESSED = 0x02


class MessageType(IntEnum):
    HELLO = 0x01
    PAIR_REQ = 0x02
    PAIR_ACK = 0x03
    PAIR_NAK = 0x04
    PAIR_CONFIRM = 0x05
    SYNC_REQ = 0x10
    SYNC_MANIFEST = 0x11
    ITEM_REQ = 0x12
    ITEM = 0x13
    SYNC_DONE = 0x14
    SYNC_NOTIFY = 0x15
    MAIL_RELAY = 0x20
    MAIL_RECEIPT = 0x21
    INCIDENT = 0x30
    ALERT_RELAY = 0x31
    PING = 0x40
    PONG = 0x41
    SERVICE_QUERY = 0x50
    SERVICE_RESPONSE = 0x51


class FrameError(ValueError):
    pass


@dataclass(frozen=True)
class Fragment:
    msg_type: MessageType
    flags: int
    index: int
    total: int
    counter: int
    body: bytes


class FrameCodec:
    def __init__(self, max_fragments: int = 8) -> None:
        self.max_fragments = max_fragments

    @staticmethod
    def _signature(secret: bytes, prefix: bytes, body: bytes) -> bytes:
        return hmac.new(secret, prefix + body, hashlib.sha256).digest()[:8]

    def encode(
        self,
        msg_type: MessageType,
        value: Any,
        counter: int,
        secret: bytes | None,
    ) -> list[bytes]:
        if not 0 <= counter <= 0xFFFFFFFF:
            raise FrameError("counter is outside uint32 range")
        if secret is None and msg_type not in {
            MessageType.HELLO,
            MessageType.PAIR_REQ,
            MessageType.PAIR_ACK,
        }:
            raise FrameError("authenticated federation frame requires a peer secret")
        encoded = cbor2.dumps(value, canonical=True)
        compressed = zlib.compress(encoded, level=6)
        flags = FLAG_COMPRESSED if len(compressed) < len(encoded) else 0
        payload = compressed if flags else encoded
        total = max(1, (len(payload) + FRAGMENT_BODY_SIZE - 1) // FRAGMENT_BODY_SIZE)
        if total > self.max_fragments:
            raise FrameError("message exceeds federation fragment ceiling")
        frames = []
        for index in range(total):
            body = payload[index * FRAGMENT_BODY_SIZE : (index + 1) * FRAGMENT_BODY_SIZE]
            fragment_flags = flags | (FLAG_MORE if index < total - 1 else 0)
            prefix = bytes((MAGIC, VERSION, int(msg_type), fragment_flags, index, total))
            prefix += counter.to_bytes(4, "big")
            signature = b"\0" * 8 if secret is None else self._signature(secret, prefix, body)
            frames.append(prefix + signature + body)
        return frames

    def decode_fragment(self, frame: bytes, secret: bytes | None) -> Fragment:
        if len(frame) < HEADER_SIZE:
            raise FrameError("frame is shorter than federation header")
        if frame[0] != MAGIC or frame[1] != VERSION:
            raise FrameError("wrong federation magic or version")
        if frame[3] & ~(FLAG_MORE | FLAG_COMPRESSED):
            raise FrameError("reserved federation flags are set")
        try:
            msg_type = MessageType(frame[2])
        except ValueError as error:
            raise FrameError("unknown federation message type") from error
        index, total = frame[4], frame[5]
        if total < 1 or total > self.max_fragments or index >= total:
            raise FrameError("invalid federation fragment index")
        prefix, signature, body = frame[:10], frame[10:18], frame[18:]
        unsigned_types = {MessageType.HELLO, MessageType.PAIR_REQ, MessageType.PAIR_ACK}
        if msg_type in unsigned_types and secret is None:
            if signature != b"\0" * 8:
                raise FrameError("discovery frame has unexpected authentication data")
        else:
            if secret is None:
                raise FrameError("peer secret is unavailable")
            expected = self._signature(secret, prefix, body)
            if not hmac.compare_digest(signature, expected):
                raise FrameError("federation HMAC failed")
        return Fragment(
            msg_type,
            frame[3],
            index,
            total,
            int.from_bytes(frame[6:10], "big"),
            body,
        )

    @staticmethod
    def decode_body(payload: bytes, compressed: bool) -> Any:
        try:
            return cbor2.loads(zlib.decompress(payload) if compressed else payload)
        except (ValueError, zlib.error) as error:
            raise FrameError("invalid federation body") from error


class Reassembler:
    def __init__(self, timeout_s: int = 300) -> None:
        self.timeout_s = timeout_s
        self._pending: dict[tuple[str, int, int], tuple[float, int, int, dict[int, bytes]]] = {}

    def add(self, sender: str, fragment: Fragment, now: float | None = None) -> Any | None:
        stamp = time.monotonic() if now is None else now
        self.expire(stamp)
        key = (sender, fragment.counter, int(fragment.msg_type))
        created, total, flags, pieces = self._pending.get(
            key, (stamp, fragment.total, fragment.flags, {})
        )
        if total != fragment.total or bool(flags & FLAG_COMPRESSED) != bool(
            fragment.flags & FLAG_COMPRESSED
        ):
            self._pending.pop(key, None)
            raise FrameError("inconsistent federation fragments")
        pieces[fragment.index] = fragment.body
        self._pending[key] = (created, total, flags, pieces)
        if len(pieces) != total:
            return None
        self._pending.pop(key, None)
        payload = b"".join(pieces[index] for index in range(total))
        return FrameCodec.decode_body(payload, bool(flags & FLAG_COMPRESSED))

    def expire(self, now: float | None = None) -> int:
        stamp = time.monotonic() if now is None else now
        expired = [key for key, value in self._pending.items() if stamp - value[0] > self.timeout_s]
        for key in expired:
            self._pending.pop(key, None)
        return len(expired)
