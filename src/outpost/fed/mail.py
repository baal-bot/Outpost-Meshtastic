from __future__ import annotations

import json
import secrets
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from outpost.clock import Clock
from outpost.fed.framing import wire_bytes, wire_int
from outpost.fed.peers import FederationPeerService
from outpost.store import Database


class FederationMailService:
    def __init__(self, database: Database, peers: FederationPeerService, clock: Clock) -> None:
        self.database, self.peers, self.clock = database, peers, clock

    @staticmethod
    def _key(secret: bytes, first: str, second: str) -> bytes:
        context = b"outpost-mail-v1\0" + b"\0".join(sorted((first.encode(), second.encode())))
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=context).derive(secret)

    @staticmethod
    def _handle(value: object) -> str:
        return str(value).strip().removeprefix("@").strip().lower()

    @staticmethod
    def _validate_identity(recipient: str, kind: str, participant: str) -> None:
        if kind == "system" and (recipient != "operator" or participant != "operator"):
            raise ValueError("system mail must use the operator catch-all identity")
        if kind == "member" and participant == "operator":
            raise ValueError("member mail requires a named member participant")

    async def seal(
        self,
        peer_id: str,
        recipient: str,
        sender: str,
        subject: str,
        body: str,
        *,
        conversation_id: str | None = None,
        message_kind: str | None = None,
        participant_handle: str | None = None,
        reply_to: str | None = None,
        operator_actor: str = "web:operator",
    ) -> dict[str, Any]:
        peer = await self.peers.by_mesh_id(peer_id)
        if peer.state != "active" or not peer.relay_mail:
            raise ValueError("mail relay is not enabled for this peer")
        recipient = self._handle(recipient)
        if not recipient or len(recipient) > 40 or len(body.encode()) > 800:
            raise ValueError("invalid federation mail recipient or body size")
        now = int(self.clock.now().timestamp())
        recent = await self.database.read(
            "SELECT COUNT(*) count FROM fed_mail_delivery WHERE peer_id=? AND direction='out' "
            "AND created_at>?",
            (peer.id, now - 3600),
        )
        if int(recent[0]["count"]) >= peer.quota_mail_per_hour:
            raise ValueError("peer mail relay quota exceeded")
        relay_id, nonce = secrets.token_hex(16), secrets.token_bytes(12)
        conversation_id = str(conversation_id or relay_id).strip()
        if not conversation_id or len(conversation_id) > 64:
            raise ValueError("invalid federation mail conversation")
        kind = message_kind or ("system" if recipient == "operator" else "member")
        if kind not in {"member", "system"}:
            raise ValueError("invalid federation mail message kind")
        participant = self._handle(participant_handle or recipient)
        if not participant or len(participant) > 40:
            raise ValueError("invalid federation mail participant")
        self._validate_identity(recipient, kind, participant)
        reply_handle = self._handle(reply_to or sender.split("@", 1)[0])
        if not reply_handle or len(reply_handle) > 40:
            raise ValueError("invalid federation mail reply address")
        plaintext = json.dumps(
            {
                "to": recipient,
                "from": sender[:80],
                "subject": subject[:120],
                "body": body,
                "conversation_id": conversation_id,
                "message_kind": kind,
                "participant": participant,
                "reply_to": reply_handle,
                "operator_actor": operator_actor[:120],
            },
            separators=(",", ":"),
        ).encode()
        secret = await self.peers.secret(peer_id)
        ciphertext = AESGCM(self._key(secret, self.peers.local_mesh_id, peer_id)).encrypt(
            nonce, plaintext, relay_id.encode()
        )
        async with self.database.transaction() as transaction:
            mail_id = await transaction.write(
                "INSERT INTO mail(uid,from_label,to_label,to_node,subject,body,created_at,state,"
                "expires_at,conversation_key,federation_conversation_id,operator_read_at,"
                "message_kind,mail_direction,source_peer_mesh_id,reply_recipient_handle,"
                "participant_handle,operator_actor) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'out',?,?,?,?)",
                (
                    f"fed-out:{relay_id}",
                    sender[:80],
                    recipient,
                    peer_id,
                    subject[:120],
                    body,
                    now,
                    "queued",
                    now + 180 * 86_400,
                    f"fed:{peer_id}:{conversation_id}",
                    conversation_id,
                    now,
                    kind,
                    peer_id,
                    recipient,
                    participant,
                    operator_actor[:120],
                ),
            )
            await transaction.write(
                "INSERT INTO fed_mail_delivery(relay_id,peer_id,direction,mail_id,"
                "recipient_handle,state,created_at,updated_at,expires_at) "
                "VALUES(?,?,'out',?,?,'queued',?,?,?)",
                (relay_id, peer.id, mail_id, recipient, now, now, now + 86_400),
            )
        return {
            "relay_id": relay_id,
            "conversation_id": conversation_id,
            "nonce": nonce,
            "ciphertext": ciphertext,
            "expires_at": now + 86_400,
        }

    async def open(self, peer_id: str, envelope: dict[str, Any]) -> tuple[str, str]:
        peer = await self.peers.by_mesh_id(peer_id)
        if peer.state != "active" or not peer.relay_mail:
            raise ValueError("mail relay is not enabled for this peer")
        relay_id = str(envelope["relay_id"])
        now = int(self.clock.now().timestamp())
        if wire_int(envelope["expires_at"], "expires_at") <= now:
            raise ValueError("federation mail expired")
        seen = await self.database.read(
            "SELECT state FROM fed_mail_delivery WHERE relay_id=?", (relay_id,)
        )
        if seen:
            return relay_id, str(seen[0]["state"])
        secret = await self.peers.secret(peer_id)
        plaintext = AESGCM(self._key(secret, self.peers.local_mesh_id, peer_id)).decrypt(
            wire_bytes(envelope["nonce"], "nonce", length=12),
            wire_bytes(envelope["ciphertext"], "ciphertext"),
            relay_id.encode(),
        )
        message = json.loads(plaintext)
        recipient = self._handle(message["to"])
        if not recipient or len(recipient) > 40:
            raise ValueError("invalid federation mail recipient")
        conversation_id = str(message.get("conversation_id") or relay_id).strip()
        if not conversation_id or len(conversation_id) > 64:
            raise ValueError("invalid federation mail conversation")
        message_kind = str(
            message.get("message_kind") or ("system" if recipient == "operator" else "member")
        )
        if message_kind not in {"member", "system"}:
            raise ValueError("invalid federation mail message kind")
        participant = self._handle(message.get("participant") or recipient)
        reply_recipient = self._handle(
            message.get("reply_to") or str(message.get("from") or "").split("@", 1)[0]
        )
        if (
            not participant
            or len(participant) > 40
            or not reply_recipient
            or len(reply_recipient) > 40
        ):
            raise ValueError("invalid federation mail routing metadata")
        self._validate_identity(recipient, message_kind, participant)
        if recipient == "operator":
            recipient_id = None
            recipient_label = "operator"
        else:
            members = await self.database.read(
                "SELECT id,handle FROM member WHERE lower(handle)=lower(?) "
                "AND trust NOT IN ('blocked','guest')",
                (recipient,),
            )
            if not members:
                raise ValueError("local federation mail recipient was not found")
            recipient_id = members[0]["id"]
            recipient_label = members[0]["handle"]
        window = now - now % 3_600
        denial: str | None = None
        async with self.database.transaction() as transaction:
            concurrent = await transaction.read(
                "SELECT state FROM fed_mail_delivery WHERE relay_id=?", (relay_id,)
            )
            if concurrent:
                return relay_id, str(concurrent[0]["state"])
            usage = await transaction.read(
                "SELECT inbound_accepted FROM fed_mail_usage WHERE peer_id=? AND window_start=?",
                (peer.id, window),
            )
            recipient_usage = await transaction.read(
                "SELECT inbound_accepted FROM fed_mail_recipient_usage "
                "WHERE peer_id=? AND recipient_handle=? AND window_start=?",
                (peer.id, recipient, window),
            )
            accepted = int(usage[0]["inbound_accepted"]) if usage else 0
            recipient_accepted = (
                int(recipient_usage[0]["inbound_accepted"]) if recipient_usage else 0
            )
            if accepted >= peer.quota_mail_per_hour:
                denial = "peer inbound mail quota exceeded"
            elif recipient_accepted >= min(
                peer.quota_mail_per_hour, peer.quota_mail_per_recipient_per_hour
            ):
                denial = f"inbound mail quota exceeded for recipient @{recipient}"
            if denial is not None:
                await transaction.write(
                    "INSERT INTO fed_mail_usage(peer_id,window_start,inbound_rejected) "
                    "VALUES(?,?,1) ON CONFLICT(peer_id,window_start) DO UPDATE SET "
                    "inbound_rejected=inbound_rejected+1",
                    (peer.id, window),
                )
            else:
                mail_id = await transaction.write(
                    "INSERT INTO mail(uid,from_label,to_id,to_label,subject,body,created_at,"
                    "delivered_at,state,expires_at,reply_peer_mesh_id,conversation_key,"
                    "federation_conversation_id,message_kind,mail_direction,source_peer_mesh_id,"
                    "reply_recipient_handle,participant_handle,operator_actor) "
                    "VALUES(?,?,?,?,?,?,?,?,'delivered',?,?,?,?,?,'in',?,?,?,?)",
                    (
                        f"fed:{relay_id}",
                        str(message["from"]),
                        recipient_id,
                        recipient_label,
                        str(message.get("subject") or "")[:120],
                        str(message["body"]),
                        now,
                        now,
                        now + 180 * 86400,
                        peer_id,
                        f"fed:{peer_id}:{conversation_id}",
                        conversation_id,
                        message_kind,
                        peer_id,
                        reply_recipient[:40],
                        participant[:40],
                        str(message.get("operator_actor") or "")[:120] or None,
                    ),
                )
                await transaction.write(
                    "INSERT INTO fed_mail_delivery(relay_id,peer_id,direction,mail_id,"
                    "recipient_handle,state,created_at,updated_at,expires_at) "
                    "VALUES(?,?,'in',?,?,'delivered',?,?,?)",
                    (
                        relay_id,
                        peer.id,
                        mail_id,
                        recipient,
                        now,
                        now,
                        int(envelope["expires_at"]),
                    ),
                )
                await transaction.write(
                    "INSERT INTO fed_mail_usage(peer_id,window_start,inbound_accepted) "
                    "VALUES(?,?,1) ON CONFLICT(peer_id,window_start) DO UPDATE SET "
                    "inbound_accepted=inbound_accepted+1",
                    (peer.id, window),
                )
                await transaction.write(
                    "INSERT INTO fed_mail_recipient_usage(peer_id,recipient_handle,"
                    "window_start,inbound_accepted) VALUES(?,?,?,1) "
                    "ON CONFLICT(peer_id,recipient_handle,window_start) DO UPDATE SET "
                    "inbound_accepted=inbound_accepted+1",
                    (peer.id, recipient, window),
                )
        if denial is not None:
            raise ValueError(denial)
        return relay_id, "delivered"
