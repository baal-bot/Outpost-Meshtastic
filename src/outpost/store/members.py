from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from outpost.clock import Clock

from .database import Database


@dataclass(frozen=True)
class Member:
    id: int
    mesh_id: str
    mesh_num: int
    handle: str | None
    trust: str
    first_seen: int
    last_seen: int
    public_key: bytes | None = None
    pending_public_key: bytes | None = None
    pki_state: str = "unknown"
    pki_verified_at: int | None = None
    pki_last_seen_at: int | None = None


class MemberRepo:
    def __init__(self, database: Database, clock: Clock) -> None:
        self.database, self.clock = database, clock

    async def resolve(
        self,
        mesh_id: str,
        *,
        last_heard_snr: float | None = None,
        hops_away: int | None = None,
        authenticated_pki_key: bytes | None = None,
    ) -> Member:
        select_member = (
            "SELECT id,mesh_id,mesh_num,handle,trust,first_seen,last_seen,public_key,"
            "pending_public_key,pki_state,pki_verified_at,pki_last_seen_at "
            "FROM member WHERE mesh_id=?"
        )
        rows = await self.database.read(
            select_member,
            (mesh_id,),
        )
        now = int(self.clock.now().timestamp())
        if not rows:
            mesh_num = int(mesh_id.removeprefix("!"), 16)
            await self.database.write(
                "INSERT INTO member(mesh_id,mesh_num,first_seen,last_seen,"
                "last_heard_snr,hops_away) "
                "VALUES(?,?,?,?,?,?)",
                (mesh_id, mesh_num, now, now, last_heard_snr, hops_away),
            )
            rows = await self.database.read(
                select_member,
                (mesh_id,),
            )
        else:
            await self.database.write(
                "UPDATE member SET last_seen=?,last_heard_snr=COALESCE(?,last_heard_snr),"
                "hops_away=COALESCE(?,hops_away) WHERE mesh_id=?",
                (now, last_heard_snr, hops_away, mesh_id),
            )
        if authenticated_pki_key is not None:
            if len(authenticated_pki_key) != 32:
                raise ValueError("Meshtastic PKI public keys must be 32 bytes")
            await self._observe_authenticated_key(mesh_id, authenticated_pki_key, now)
            rows = await self.database.read(select_member, (mesh_id,))
        row = rows[0]
        return Member(**dict(row))

    @staticmethod
    def fingerprint(public_key: bytes | None) -> str | None:
        if public_key is None:
            return None
        return hashlib.sha256(public_key).hexdigest()

    async def _observe_authenticated_key(self, mesh_id: str, key: bytes, now: int) -> None:
        fingerprint = self.fingerprint(key)
        assert fingerprint is not None
        async with self.database.transaction() as transaction:
            rows = await transaction.read(
                "SELECT id,trust,public_key,pending_public_key,pki_state FROM member "
                "WHERE mesh_id=?",
                (mesh_id,),
            )
            if not rows:
                return
            row = rows[0]
            pinned = bytes(row["public_key"]) if row["public_key"] is not None else None
            pending = (
                bytes(row["pending_public_key"]) if row["pending_public_key"] is not None else None
            )
            if pinned == key and row["pki_state"] in {"verified", "conflict"}:
                await transaction.write(
                    "UPDATE member SET pki_last_seen_at=? WHERE id=?", (now, row["id"])
                )
                return
            if (
                pinned is None
                and pending == key
                and row["pki_state"]
                in {
                    "pending",
                    "conflict",
                }
            ):
                await transaction.write(
                    "UPDATE member SET pki_last_seen_at=? WHERE id=?", (now, row["id"])
                )
                return

            prior = self.fingerprint(pinned or pending)
            event = (
                "observed"
                if (pinned is None and pending is None)
                or (pinned == key and row["pki_state"] == "unknown")
                else "conflict"
            )
            state = "pending" if event == "observed" else "conflict"
            await transaction.write(
                "UPDATE member SET pending_public_key=?,pki_state=?,pki_last_seen_at=? WHERE id=?",
                (key, state, now, row["id"]),
            )
            await transaction.write(
                "INSERT INTO member_pki_event(member_id,event,fingerprint,prior_fingerprint,"
                "actor,detail,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    row["id"],
                    event,
                    fingerprint,
                    prior,
                    "mesh:firmware-pki",
                    json.dumps({"mesh_id": mesh_id}, separators=(",", ":")),
                    now,
                ),
            )
            if event == "conflict":
                if row["trust"] in {"member", "trusted", "responder", "operator"}:
                    await transaction.write(
                        "UPDATE member SET trust='guest' WHERE id=?", (row["id"],)
                    )
                    await transaction.write(
                        "INSERT INTO member_trust_history(member_id,from_trust,to_trust,"
                        "changed_by,reason,created_at) VALUES(?,?,'guest',?,?,?)",
                        (
                            row["id"],
                            row["trust"],
                            "system:pki",
                            "Meshtastic PKI key conflict; operator review required",
                            now,
                        ),
                    )
                await transaction.write(
                    "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at,"
                    "outcome) VALUES('mesh',?,'member.pki.conflict',?,?,?,'denied')",
                    (
                        mesh_id,
                        mesh_id,
                        json.dumps(
                            {"fingerprint": fingerprint, "prior_fingerprint": prior},
                            separators=(",", ":"),
                        ),
                        now,
                    ),
                )

    async def authorize_elevated(
        self, member: Member, message: Any, command: str
    ) -> tuple[bool, str]:
        fingerprint = self.fingerprint(message.pki_public_key)
        reason: str | None = None
        if not message.is_direct:
            reason = "direct_message_required"
        elif not message.pki_encrypted or fingerprint is None:
            reason = "pki_required"
        elif member.pki_state != "verified" or member.public_key is None:
            reason = "reviewed_key_required"
        elif bytes(member.public_key) != bytes(message.pki_public_key):
            reason = "key_mismatch"
        elif int(message.packet_id) <= 0:
            reason = "packet_id_required"

        now = int(self.clock.now().timestamp())
        if reason is None:
            async with self.database.transaction() as transaction:
                await transaction.write(
                    "DELETE FROM member_pki_replay WHERE received_at<?", (now - 90 * 86_400,)
                )
                seen = await transaction.read(
                    "SELECT 1 FROM member_pki_replay WHERE member_id=? AND packet_id=? "
                    "AND fingerprint=?",
                    (member.id, message.packet_id, fingerprint),
                )
                if not seen:
                    await transaction.write(
                        "INSERT INTO member_pki_replay(member_id,packet_id,fingerprint,command,"
                        "received_at) VALUES(?,?,?,?,?)",
                        (member.id, message.packet_id, fingerprint, command.upper(), now),
                    )
                    return True, "verified"
            reason = "replay"

        event = "replay_denied" if reason == "replay" else "elevated_denied"
        async with self.database.transaction() as transaction:
            await transaction.write(
                "INSERT INTO member_pki_event(member_id,event,fingerprint,prior_fingerprint,"
                "actor,detail,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    member.id,
                    event,
                    fingerprint,
                    self.fingerprint(member.public_key),
                    f"mesh:{member.mesh_id}",
                    json.dumps(
                        {"command": command.upper(), "reason": reason}, separators=(",", ":")
                    ),
                    now,
                ),
            )
            await transaction.write(
                "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at,"
                "outcome) VALUES('mesh',?,'mesh.elevated_auth',?,?,?,'denied')",
                (
                    member.mesh_id,
                    command.upper(),
                    json.dumps({"reason": reason}, separators=(",", ":")),
                    now,
                ),
            )
        return False, reason

    async def claim_handle(self, mesh_id: str, handle: str, *, approve: bool = True) -> Member:
        normalized = handle.lower()
        current = await self.database.read(
            "SELECT pki_state FROM member WHERE mesh_id=?", (mesh_id,)
        )
        if current and current[0]["pki_state"] == "conflict":
            raise ValueError("identity key conflict requires operator review")
        existing = await self.database.read(
            "SELECT mesh_id FROM member WHERE handle=? AND mesh_id<>?",
            (normalized, mesh_id),
        )
        if existing:
            raise ValueError("handle is already claimed")
        now = int(self.clock.now().timestamp())
        trust = "member" if approve else "guest"
        await self.database.write(
            """
            UPDATE member SET handle=?,trust=?,handle_changed_at=?,last_seen=?,
              directory_state='active',directory_state_at=?,directory_state_by='mesh:enrollment'
            WHERE mesh_id=?
            """,
            (normalized, trust, now, now, now, mesh_id),
        )
        return await self.resolve(mesh_id)

    async def by_handle(self, handle: str) -> Member | None:
        rows = await self.database.read(
            """
            SELECT id,mesh_id,mesh_num,handle,trust,first_seen,last_seen,public_key,
                   pending_public_key,pki_state,pki_verified_at,pki_last_seen_at
            FROM member WHERE handle=?
            """,
            (handle.lower(),),
        )
        return Member(**dict(rows[0])) if rows else None

    async def recent(self, limit: int = 8) -> list[Member]:
        rows = await self.database.read(
            """
            SELECT id,mesh_id,mesh_num,handle,trust,first_seen,last_seen,public_key,
                   pending_public_key,pki_state,pki_verified_at,pki_last_seen_at
            FROM member ORDER BY last_seen DESC LIMIT ?
            """,
            (limit,),
        )
        return [Member(**dict(row)) for row in rows]
