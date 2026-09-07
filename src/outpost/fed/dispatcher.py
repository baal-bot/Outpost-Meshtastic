"""Authenticated federation ingress, borrowing the existing domain owners.

This stateless coordinator neither starts tasks nor creates another database,
reassembler, counter, receipt store or radio sender. All egress callbacks retain
the application's governed admission and current-policy checks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from outpost.clock import Clock
from outpost.fed import item_failures
from outpost.fed.framing import (
    FrameCodec,
    FrameError,
    FrameTooLarge,
    MessageType,
    Reassembler,
    wire_bytes,
    wire_int,
)
from outpost.fed.incident_receipts import IncidentReceipts
from outpost.fed.incident_sender import IncidentSender
from outpost.fed.mail import FederationMailService
from outpost.fed.peers import FederationPeerService
from outpost.fed.reconciliation import Reconciliation
from outpost.fed.relay import FederationRelayService
from outpost.fed.revisions import CAPABILITY as RECONCILIATION_CAPABILITY
from outpost.fed.revisions import MODE as RECONCILIATION_MODE
from outpost.fed.revisions import RevisionReset
from outpost.fed.sync import FederationSyncService
from outpost.fed.topology import FederationTopologyService
from outpost.store import Database


class RadioIdentity(Protocol):
    @property
    def local_node_id(self) -> str: ...


class QueueHello(Protocol):
    async def __call__(self, destination: str, *, target_mesh_id: str | None = None) -> bool: ...


class QueueFrames(Protocol):
    async def __call__(
        self, frames: list[bytes], destination: str, *, want_ack: bool
    ) -> list[int]: ...


class RelayReceipt(Protocol):
    async def __call__(
        self, peer_id: str, envelope_id: str, state: str, reason: str | None = None
    ) -> None: ...


HandleValue = Callable[[str, dict[str, object]], Awaitable[None]]
QueueControl = Callable[[str, MessageType, dict[str, object]], Awaitable[bool]]
SendValue = Callable[[str, MessageType, dict[str, object]], Awaitable[int]]


@dataclass(frozen=True, kw_only=True)
class FederationDispatcher:
    database: Database
    clock: Clock
    radio: RadioIdentity
    federation: FederationPeerService
    federation_codec: FrameCodec
    federation_reassembler: Reassembler
    federation_sync: FederationSyncService
    federation_reconciliation: Reconciliation
    federation_mail: FederationMailService
    federation_relay: FederationRelayService
    federation_topology: FederationTopologyService
    incident_sender: IncidentSender
    incident_receipts: IncidentReceipts
    _queue_federation_hello: QueueHello
    _queue_federation_frames: QueueFrames
    _queue_federation_control: QueueControl
    _send_federation_value: SendValue
    _send_relay_receipt: RelayReceipt
    _handle_service_query: HandleValue
    _handle_service_response: HandleValue
    _handle_sync_manifest: HandleValue

    async def receive(self, message: object) -> None:
        payload = getattr(message, "payload", None)
        sender = getattr(message, "from_id", "")
        if not isinstance(payload, bytes) or not sender:
            return
        await self.database.write(
            "UPDATE message_log SET airtime_class='federation' "
            "WHERE direction='in' AND packet_id=? AND peer_mesh_id=?",
            (getattr(message, "packet_id", None), sender),
        )
        if self.radio.local_node_id:
            self.federation_sync.local_mesh_id = self.radio.local_node_id
        try:
            if len(payload) < 3:
                raise FrameError("frame is shorter than federation header")
            msg_type = MessageType(payload[2])
            secret = None
            if msg_type is MessageType.PAIR_CONFIRM:
                try:
                    secret = await self.federation.pairing_secret(sender)
                except ValueError:
                    secret = await self.federation.secret(sender)
            elif msg_type in {
                MessageType.SERVICE_QUERY,
                MessageType.SERVICE_RESPONSE,
                MessageType.SYNC_REQ,
                MessageType.SYNC_MANIFEST,
                MessageType.ITEM_REQ,
                MessageType.ITEM,
                MessageType.SYNC_DONE,
                MessageType.SYNC_NOTIFY,
                MessageType.ITEM_RECEIPT,
                MessageType.INCIDENT,
                MessageType.INCIDENT_RECEIPT,
                MessageType.MAIL_RELAY,
                MessageType.MAIL_RECEIPT,
                MessageType.RELAY_PUT,
                MessageType.RELAY_ACK,
                MessageType.TOPOLOGY_UPDATE,
            }:
                secret = await self.federation.secret(sender)
            fragment = self.federation_codec.decode_fragment(payload, secret)
            value = self.federation_reassembler.add(sender, fragment)
            if value is None:
                return
            authenticated = msg_type not in {
                MessageType.HELLO,
                MessageType.PAIR_REQ,
                MessageType.PAIR_ACK,
                MessageType.PAIR_CONFIRM,
            }
            replayed_item = False
            if authenticated and not (
                await self.federation.accept_counter(sender, fragment.counter)
            ):
                if msg_type is MessageType.ITEM:
                    replayed_item = True
                else:
                    raise FrameError("replayed federation service frame")
            if not isinstance(value, dict) or value.get("mesh_id") != sender:
                raise FrameError("federation identity does not match packet sender")
            target = value.get("target_mesh_id")
            if target is not None and target != self.radio.local_node_id:
                return
            if authenticated:
                await self.federation.touch(sender)
            self.federation.local_mesh_id = self.radio.local_node_id
            self.federation_sync.local_mesh_id = self.radio.local_node_id
            await self.federation_sync.import_approved_replies(
                "federation:auto-thread", int(self.clock.now().timestamp())
            )
            if msg_type is MessageType.HELLO:
                capabilities = value.get("capabilities", {})
                if not isinstance(capabilities, dict):
                    raise FrameError("HELLO capabilities must be a map")
                await self.federation.discover(
                    sender,
                    str(value.get("name", sender)),
                    wire_int(value.get("protocol", 1), "protocol", minimum=1, maximum=255),
                    capabilities,
                    "mqtt" if getattr(message, "via_mqtt", False) else "radio",
                )
                # A node may have joined just after our infrequent broadcast HELLO.
                # Answer broadcasts directly so both peer directories converge without
                # another broadcast or an endless HELLO response loop.
                if not getattr(message, "is_direct", False) and target is None:
                    if getattr(message, "via_mqtt", False):
                        await self._queue_federation_hello("^all", target_mesh_id=sender)
                    else:
                        await self._queue_federation_hello(sender)
            elif msg_type is MessageType.PAIR_REQ:
                _, acknowledgement, _ = await self.federation.accept_pairing_request(
                    sender,
                    wire_bytes(value.get("public_key"), "public_key", length=32),
                    wire_bytes(value.get("nonce"), "nonce", length=16),
                )
                frames = self.federation_codec.encode(
                    MessageType.PAIR_ACK, acknowledgement, 0, None
                )
                await self._queue_federation_frames(
                    frames,
                    sender if getattr(message, "is_direct", False) else "^all",
                    want_ack=bool(getattr(message, "is_direct", False)),
                )
            elif msg_type is MessageType.PAIR_ACK:
                await self.federation.accept_pairing_ack(
                    sender,
                    wire_bytes(value.get("public_key"), "public_key", length=32),
                    wire_bytes(value.get("nonce"), "nonce", length=16),
                )
            elif msg_type is MessageType.PAIR_CONFIRM and value.get("approved") is True:
                peer = await self.federation.confirm_remote(sender)
                if peer.local_approved and not value.get("receipt", False):
                    if secret is None:
                        raise FrameError("pairing confirmation secret is unavailable")
                    confirmation = self.federation_codec.encode(
                        MessageType.PAIR_CONFIRM,
                        {
                            "mesh_id": self.federation.local_mesh_id,
                            "target_mesh_id": sender,
                            "approved": True,
                            "receipt": True,
                        },
                        0,
                        secret,
                    )
                    await self._queue_federation_frames(confirmation, "^all", want_ack=False)
            elif msg_type is MessageType.SERVICE_QUERY:
                await self._handle_service_query(sender, value)
            elif msg_type is MessageType.SERVICE_RESPONSE:
                await self._handle_service_response(sender, value)
            elif msg_type is MessageType.SYNC_REQ:
                peer = await self.federation.by_mesh_id(sender)
                if value.get("mode") == RECONCILIATION_MODE:
                    page_value = await self.federation_sync.revisions.page(peer, value)
                    await self._queue_federation_control(
                        sender, MessageType.SYNC_MANIFEST, page_value
                    )
                    return
                limit = wire_int(value.get("limit", 8), "limit", minimum=1, maximum=8)
                budget = wire_int(value.get("budget", limit), "budget", minimum=1, maximum=100)
                page_size = min(limit, budget)
                snapshot = wire_int(
                    value.get("snapshot", int(self.clock.now().timestamp())), "snapshot"
                )
                raw_before = value.get("before")
                before = None
                if raw_before is not None:
                    if not isinstance(raw_before, list) or len(raw_before) != 3:
                        raise ValueError("invalid federation reconciliation cursor")
                    before = (
                        wire_int(raw_before[0], "before version"),
                        str(raw_before[1]),
                        str(raw_before[2]),
                    )
                page = await self.federation_sync.manifest(
                    peer, page_size + 1, snapshot=snapshot, before=before
                )
                items = page[:page_size]
                has_more = len(page) > page_size
                next_before = None
                if has_more and items:
                    last = items[-1]
                    next_before = [last.version, last.stream, last.uid]
                await self._queue_federation_control(
                    sender,
                    MessageType.SYNC_MANIFEST,
                    {
                        "items": [item.json() for item in items],
                        "snapshot": snapshot,
                        "next_before": next_before,
                        "remaining": max(0, budget - len(items)),
                    },
                )
            elif msg_type is MessageType.SYNC_NOTIFY:
                stream = str(value.get("stream", ""))
                peer = await self.federation.by_mesh_id(sender)
                if not stream.startswith("board:") or stream[6:] not in peer.boards:
                    raise ValueError("federation change notification is outside peer policy")
                if (
                    peer.reconciliation_version == RECONCILIATION_MODE
                    or peer.capabilities.get(RECONCILIATION_CAPABILITY) == RECONCILIATION_MODE
                ):
                    await self.federation_reconciliation.tick(peer)
                else:
                    await self._queue_federation_control(sender, MessageType.SYNC_REQ, {"limit": 8})
            elif msg_type is MessageType.SYNC_MANIFEST:
                await self._handle_sync_manifest(sender, value)
            elif msg_type is MessageType.ITEM_REQ:
                requests = value.get("items", [])
                if not isinstance(requests, list):
                    raise ValueError("invalid federation item request")
                peer = await self.federation.by_mesh_id(sender)
                try:
                    exported = (
                        await self.federation_sync.revisions.export(peer, value)
                        if value.get("mode") == RECONCILIATION_MODE
                        else await self.federation_sync.export_items(peer, requests)
                    )
                except RevisionReset as reset:
                    await self._queue_federation_control(
                        sender, MessageType.SYNC_MANIFEST, reset.manifest
                    )
                    return
                sent = 0
                for item in exported:
                    if value.get("mode") == RECONCILIATION_MODE:
                        peer = await self.federation.by_mesh_id(sender)
                        if self.federation_sync.revisions.scope(peer) != value.get("scope"):
                            raise ValueError("item is outside peer sync policy after export")
                    try:
                        await self._send_federation_value(
                            sender,
                            MessageType.ITEM,
                            {"mesh_id": self.federation.local_mesh_id, "item": item},
                        )
                        sent += 1
                        if value.get("mode") == RECONCILIATION_MODE:
                            await self.federation_sync.item_failures.admitted(
                                peer, item, int(self.clock.now().timestamp())
                            )
                    except FrameTooLarge:
                        if value.get("mode") == RECONCILIATION_MODE and "payload" in item:
                            failure = self.federation_sync.item_failures.envelope(item)
                            await self.federation_sync.item_failures.record(
                                peer, failure, int(self.clock.now().timestamp())
                            )
                            current_peer = await self.federation.by_mesh_id(sender)
                            if item_failures.supported(
                                current_peer
                            ) and self.federation_sync.revisions.scope(current_peer) == value.get(
                                "scope"
                            ):
                                await self._send_federation_value(
                                    sender, MessageType.ITEM, {"item": failure}
                                )
                    except FrameError:
                        continue
                if value.get("mode") != RECONCILIATION_MODE:
                    await self._queue_federation_control(
                        sender,
                        MessageType.SYNC_DONE,
                        {"mesh_id": self.federation.local_mesh_id, "sent": sent},
                    )
            elif msg_type is MessageType.INCIDENT:
                peer = await self.federation.by_mesh_id(sender)
                event_receipt = await self.federation_sync.incident_events.receive_wire(
                    peer, value, int(self.clock.now().timestamp())
                )
                reply_ids = await self.incident_receipts.admit(peer.id, event_receipt)
                if not reply_ids:
                    raise ValueError("incident receipt reply has no governed frames")
            elif msg_type is MessageType.INCIDENT_RECEIPT:
                peer = await self.federation.by_mesh_id(sender)
                await self.incident_sender.receive(
                    peer,
                    value,
                    int(self.clock.now().timestamp()),
                    authenticated_secret=secret,
                )
            elif msg_type is MessageType.ITEM:
                incoming_item = value.get("item")
                if not isinstance(incoming_item, dict):
                    raise ValueError("invalid federation item")
                peer = await self.federation.by_mesh_id(sender)
                received = False
                if "revision" in incoming_item or "epoch" in incoming_item:
                    received = await self.federation_reconciliation.receive(peer, incoming_item)
                    if incoming_item.get("unavailable") is True or "failure" in incoming_item:
                        return
                elif not replayed_item:
                    received = await self.federation_sync.quarantine(
                        peer, incoming_item, int(self.clock.now().timestamp())
                    )
                if received and str(incoming_item.get("stream", "")).startswith("board:"):
                    payload = incoming_item.get("payload")
                    if isinstance(payload, dict):
                        slug = str(incoming_item["stream"])[6:]
                        approved = await self.federation_sync.approved_thread(
                            slug, str(payload.get("thread_uid", ""))
                        )
                        if approved or int(payload.get("seq", 0)) == 1:
                            inbox = await self.database.read(
                                "SELECT id FROM fed_inbox_item WHERE peer_id=? AND stream=? "
                                "AND uid=? AND state='pending'",
                                (
                                    peer.id,
                                    str(incoming_item["stream"]),
                                    str(incoming_item.get("uid", "")),
                                ),
                            )
                            if inbox:
                                await self.federation_sync.import_inbox(
                                    int(inbox[0]["id"]),
                                    "federation:auto-thread",
                                    int(self.clock.now().timestamp()),
                                )
                receipt = await self.database.read(
                    "SELECT state FROM fed_inbox_item WHERE peer_id=? AND stream=? AND uid=?",
                    (
                        peer.id,
                        str(incoming_item.get("stream", "")),
                        str(incoming_item.get("uid", "")),
                    ),
                )
                if receipt:
                    await self._send_federation_value(
                        sender,
                        MessageType.ITEM_RECEIPT,
                        {
                            "uid": str(incoming_item.get("uid", "")),
                            "state": str(receipt[0]["state"]),
                        },
                    )
            elif msg_type is MessageType.ITEM_RECEIPT:
                state = str(value.get("state", ""))
                if state not in {"pending", "imported", "rejected"}:
                    raise ValueError("invalid federation item receipt state")
                peer = await self.federation.by_mesh_id(sender)
                await self.database.write(
                    "UPDATE fed_post_delivery SET state='delivered',delivered_at=unixepoch(),"
                    "updated_at=unixepoch(),error=NULL WHERE peer_id=? AND uid=?",
                    (peer.id, str(value.get("uid", ""))),
                )
            elif msg_type is MessageType.SYNC_DONE:
                peer = await self.federation.by_mesh_id(sender)
                if (
                    peer.reconciliation_version != RECONCILIATION_MODE
                    and peer.capabilities.get(RECONCILIATION_CAPABILITY) != RECONCILIATION_MODE
                ):
                    await self.database.write(
                        "UPDATE fed_peer SET last_sync_at=unixepoch() WHERE mesh_id=?",
                        (sender,),
                    )
            elif msg_type is MessageType.MAIL_RELAY:
                try:
                    relay_id, state = await self.federation_mail.open(sender, value)
                except (KeyError, TypeError, ValueError) as error:
                    relay_id = str(value.get("relay_id", ""))
                    if relay_id and len(relay_id) <= 64:
                        await self._send_federation_value(
                            sender,
                            MessageType.MAIL_RECEIPT,
                            {
                                "mesh_id": self.federation.local_mesh_id,
                                "relay_id": relay_id,
                                "state": "failed",
                                "error": str(error)[:80],
                            },
                        )
                    raise
                await self._send_federation_value(
                    sender,
                    MessageType.MAIL_RECEIPT,
                    {
                        "mesh_id": self.federation.local_mesh_id,
                        "relay_id": relay_id,
                        "state": state,
                    },
                )
            elif msg_type is MessageType.MAIL_RECEIPT:
                state = str(value.get("state", ""))
                if state in {"delivered", "failed"}:
                    relay_id = str(value["relay_id"])
                    async with self.database.transaction() as transaction:
                        await transaction.write(
                            "UPDATE fed_mail_delivery SET state=?,error=?,updated_at=unixepoch() "
                            "WHERE relay_id=? AND direction='out'",
                            (
                                state,
                                str(value.get("error") or "")[:120] or None,
                                relay_id,
                            ),
                        )
                        await transaction.write(
                            "UPDATE mail SET state=?,delivered_at=CASE WHEN ?='delivered' "
                            "THEN unixepoch() ELSE delivered_at END WHERE id=(SELECT mail_id "
                            "FROM fed_mail_delivery WHERE relay_id=? AND direction='out')",
                            (state, state, relay_id),
                        )
            elif msg_type is MessageType.RELAY_PUT:
                envelope = value.get("envelope")
                if not isinstance(envelope, dict):
                    raise ValueError("invalid relay envelope")
                try:
                    envelope_id, state = await self.federation_relay.accept(
                        sender,
                        envelope,
                        transport=("mqtt" if getattr(message, "via_mqtt", False) else "radio"),
                    )
                except ValueError as error:
                    rejected_id = envelope.get("envelope_id")
                    if (
                        isinstance(rejected_id, str)
                        and len(rejected_id) == 32
                        and all(character in "0123456789abcdef" for character in rejected_id)
                    ):
                        try:
                            await self._send_relay_receipt(
                                sender, rejected_id, "rejected", str(error)
                            )
                        except (FrameError, ValueError):
                            pass
                    raise
                try:
                    await self._send_relay_receipt(sender, envelope_id, state)
                except (FrameError, ValueError):
                    pass
                else:
                    if state == "delivered":
                        await self.federation_relay.mark_receipt_sent(envelope_id)
            elif msg_type is MessageType.RELAY_ACK:
                envelope_id = str(value.get("envelope_id", ""))
                state = str(value.get("state", ""))
                reason = value.get("reason")
                if reason is not None and not isinstance(reason, str):
                    raise ValueError("invalid relay acknowledgement reason")
                previous = await self.federation_relay.acknowledge(
                    sender, envelope_id, state, reason
                )
                if previous is not None:
                    try:
                        await self._send_relay_receipt(previous, envelope_id, "delivered")
                    except (FrameError, ValueError):
                        pass
                    else:
                        await self.federation_relay.mark_receipt_sent(envelope_id)
            elif msg_type is MessageType.TOPOLOGY_UPDATE:
                topology = value.get("topology")
                if not isinstance(topology, dict):
                    raise ValueError("invalid federation topology update")
                await self.federation_topology.accept(sender, topology)
        except (FrameError, KeyError, OverflowError, TypeError, ValueError) as error:
            packet_id = getattr(message, "packet_id", None)
            if packet_id is not None:
                await self.database.write(
                    "UPDATE message_log SET outcome='rejected',drop_reason=? "
                    "WHERE direction='in' AND packet_id=?",
                    (self._federation_rejection_reason(error), packet_id),
                )
            return

    @staticmethod
    def _federation_rejection_reason(error: Exception) -> str:
        reason = str(error).lower()
        if "hmac" in reason or "authentication" in reason:
            return "authentication failed"
        if "secret" in reason or "paired peer" in reason:
            return "unauthenticated peer"
        if "replay" in reason:
            return "replay detected"
        if "expired" in reason:
            return "expired message"
        if "identity" in reason:
            return "identity mismatch"
        if "outside peer" in reason or "outside peer sync policy" in reason:
            return "policy denied"
        if "reconciliation" in reason:
            return "reconciliation protocol violation"
        return "invalid federation frame"
