from __future__ import annotations

import json
import secrets
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from outpost.clock import Clock
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

    async def seal(
        self, peer_id: str, recipient: str, sender: str, subject: str, body: str
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
        plaintext = json.dumps(
            {"to": recipient, "from": sender[:80], "subject": subject[:120], "body": body},
            separators=(",", ":"),
        ).encode()
        secret = await self.peers.secret(peer_id)
        ciphertext = AESGCM(self._key(secret, self.peers.local_mesh_id, peer_id)).encrypt(
            nonce, plaintext, relay_id.encode()
        )
        await self.database.write(
            "INSERT INTO fed_mail_delivery(relay_id,peer_id,direction,recipient_handle,state,"
            "created_at,updated_at,expires_at) VALUES(?,?,'out',?,'queued',?,?,?)",
            (relay_id, peer.id, recipient, now, now, now + 86_400),
        )
        return {
            "relay_id": relay_id,
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
        if int(envelope["expires_at"]) <= now:
            raise ValueError("federation mail expired")
        seen = await self.database.read(
            "SELECT state FROM fed_mail_delivery WHERE relay_id=?", (relay_id,)
        )
        if seen:
            return relay_id, str(seen[0]["state"])
        secret = await self.peers.secret(peer_id)
        plaintext = AESGCM(self._key(secret, self.peers.local_mesh_id, peer_id)).decrypt(
            bytes(envelope["nonce"]), bytes(envelope["ciphertext"]), relay_id.encode()
        )
        message = json.loads(plaintext)
        recipient = self._handle(message["to"])
        if recipient == "operator":
            members = await self.database.read(
                "SELECT id,handle FROM member WHERE trust='operator' AND handle IS NOT NULL "
                "ORDER BY id LIMIT 1"
            )
            recipient_id = members[0]["id"] if members else None
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
        async with self.database.transaction() as transaction:
            concurrent = await transaction.read(
                "SELECT state FROM fed_mail_delivery WHERE relay_id=?", (relay_id,)
            )
            if concurrent:
                return relay_id, str(concurrent[0]["state"])
            mail_id = await transaction.write(
                "INSERT INTO mail(uid,from_label,to_id,to_label,subject,body,created_at,"
                "delivered_at,state,expires_at,reply_peer_mesh_id) "
                "VALUES(?,?,?,?,?,?,?,?,'delivered',?,?)",
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
        return relay_id, "delivered"
