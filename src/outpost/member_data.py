from __future__ import annotations

import secrets
from typing import Any, Literal

from outpost.audit import write_audit
from outpost.clock import Clock
from outpost.config import RetentionConfig
from outpost.store import Database, Transaction
from outpost.store.members import Member

DAY = 86_400

REMOVAL_POLICY = {
    "deleted": (
        "Exact and pending positions, mesh packet content and keys, AI question/answer content, "
        "mail content, subscriptions, and read cursors are removed immediately."
    ),
    "pseudonymized": (
        "The member directory identity and author labels on retained board, welfare, and incident "
        "records are replaced with a non-identifying former-member label."
    ),
    "preserved": (
        "Published incident facts, safety history, trust/PKI events, data-request decisions, and "
        "append-only audit evidence remain for safety and accountability. Their free text may "
        "still contain information the member chose to publish."
    ),
}


def retention_statement(retention: RetentionConfig) -> dict[str, Any]:
    """Build the public retention statement directly from validated runtime configuration."""
    categories = [
        {
            "key": "positions",
            "label": "Exact member positions",
            "window": f"up to {retention.member_positions_hours} hours",
            "removal": "A verified member may delete their current exact position immediately.",
        },
        {
            "key": "messages",
            "label": "Packet and command telemetry",
            "window": (
                f"up to {retention.message_log_days} days or "
                f"{retention.message_log_max_rows:,} rows"
            ),
            "removal": "Content and radio identity material are redacted on approved removal.",
        },
        {
            "key": "mail",
            "label": "Private and operator mail",
            "window": f"up to {retention.mail_days} days or its earlier delivery expiry",
            "removal": "Content and participant labels are redacted on approved removal.",
        },
        {
            "key": "posts",
            "label": "Board posts",
            "window": f"normally {retention.posts_days} days; a board may override this",
            "removal": "Retained public content keeps a pseudonymous author label.",
        },
        {
            "key": "welfare",
            "label": "Welfare check-ins",
            "window": f"up to {retention.watch_history_days} days",
            "removal": (
                "Safety history remains pseudonymous; retained exact check-in coordinates are "
                "cleared."
            ),
        },
        {
            "key": "incidents",
            "label": "Incident reports and updates",
            "window": f"at least {retention.incident_history_days} days after conclusion",
            "removal": (
                "Published safety facts remain with pseudonymous reporter and author labels."
            ),
        },
        {
            "key": "ai",
            "label": "Assistant interactions",
            "window": (
                f"content up to {retention.ai_interaction_content_days} days; de-identified "
                "metrics "
                f"up to {retention.ai_interaction_metrics_days} days"
            ),
            "removal": "Member link, question, and answer are redacted on approved removal.",
        },
        {
            "key": "security",
            "label": "Identity, trust, PKI, and audit evidence",
            "window": (
                "preserved until an explicit lifecycle decision; audit evidence is not aged out"
            ),
            "removal": (
                "Operational identity is pseudonymized; protected security and audit evidence "
                "remains."
            ),
        },
    ]
    summary = (
        "Outpost stores mesh activity in plaintext on the local appliance. Retention is bounded by "
        "the running configuration, except protected safety, identity, and audit evidence. Members "
        "can inspect category counts, immediately delete an exact position with verified radio "
        "identity, and submit a verified removal request for operator review."
    )
    return {
        "generated_from": "validated store.retention runtime configuration",
        "summary": summary,
        "categories": categories,
        "removal_policy": dict(REMOVAL_POLICY),
    }


class MemberDataService:
    def __init__(
        self, database: Database, clock: Clock, retention: RetentionConfig | None = None
    ) -> None:
        self.database = database
        self.clock = clock
        self.retention = retention or RetentionConfig()

    async def summary(self, member: Member) -> dict[str, Any]:
        rows = await self.database.read(
            """
            SELECT
              (SELECT COUNT(*) FROM member_position WHERE member_id=?) positions,
              (SELECT MAX(expires_at) FROM member_position WHERE member_id=?) position_expires_at,
              (SELECT COUNT(*) FROM message_log WHERE member_id=?) messages,
              (SELECT COUNT(*) FROM mail WHERE from_id=? OR to_id=?) mail,
              (SELECT COUNT(*) FROM post WHERE author_id=?) posts,
              (SELECT COUNT(*) FROM checkin WHERE member_id=?) welfare,
              ((SELECT COUNT(*) FROM incident WHERE reporter_id=?) +
               (SELECT COUNT(*) FROM incident_update WHERE author_id=?)) incidents,
              (SELECT COUNT(*) FROM ai_interaction WHERE member_id=?) ai,
              ((SELECT COUNT(*) FROM member_trust_history WHERE member_id=?) +
               (SELECT COUNT(*) FROM member_pki_event WHERE member_id=?)) security
            """,
            (
                member.id,
                member.id,
                member.id,
                member.id,
                member.id,
                member.id,
                member.id,
                member.id,
                member.id,
                member.id,
                member.id,
                member.id,
            ),
        )
        value = dict(rows[0])
        value["position_expires_at"] = (
            int(value["position_expires_at"]) if value["position_expires_at"] is not None else None
        )
        value["retention"] = {
            "positions_hours": self.retention.member_positions_hours,
            "messages_days": self.retention.message_log_days,
            "mail_days": self.retention.mail_days,
            "posts_days": self.retention.posts_days,
            "welfare_days": self.retention.watch_history_days,
            "incidents_days": self.retention.incident_history_days,
            "ai_content_days": self.retention.ai_interaction_content_days,
        }
        return value

    async def delete_position(
        self,
        member_id: int,
        *,
        actor_kind: Literal["mesh", "web"],
        actor_ref: str,
    ) -> dict[str, int]:
        now = int(self.clock.now().timestamp())
        async with self.database.transaction() as transaction:
            members = await transaction.read("SELECT 1 FROM member WHERE id=?", (member_id,))
            if not members:
                raise ValueError("Member not found.")
            current = await transaction.read(
                "SELECT COUNT(*) count FROM member_position WHERE member_id=?", (member_id,)
            )
            pending = await transaction.read(
                "SELECT COUNT(*) count FROM pending_incident_location WHERE member_id=?",
                (member_id,),
            )
            await transaction.write("DELETE FROM member_position WHERE member_id=?", (member_id,))
            await transaction.write(
                "DELETE FROM pending_incident_location WHERE member_id=?", (member_id,)
            )
            result = {
                "positions": int(current[0]["count"]),
                "pending_positions": int(pending[0]["count"]),
            }
            await write_audit(
                transaction,
                actor_kind=actor_kind,
                actor_ref=actor_ref,
                action="member.position_delete",
                target=f"member:{member_id}",
                detail=result,
                created_at=now,
            )
        return result

    async def request_removal(self, member: Member) -> tuple[dict[str, Any], bool]:
        now = int(self.clock.now().timestamp())
        async with self.database.transaction() as transaction:
            existing = await transaction.read(
                "SELECT * FROM member_data_request WHERE member_id=? AND request_type='removal' "
                "AND state='pending'",
                (member.id,),
            )
            if existing:
                return dict(existing[0]), False
            request_id = await transaction.write(
                "INSERT INTO member_data_request(member_id,request_type,requested_at) "
                "VALUES(?,'removal',?)",
                (member.id, now),
            )
            conversation_key = f"member-data:{request_id}"
            await transaction.write(
                "UPDATE member_data_request SET conversation_key=? WHERE id=?",
                (conversation_key, request_id),
            )
            label = member.handle or member.mesh_id
            uid = f"local:data-request:{request_id}"
            await transaction.write(
                """
                INSERT INTO mail(
                  uid,from_id,from_label,to_label,subject,body,created_at,delivered_at,state,
                  expires_at,conversation_key,message_kind,mail_direction,participant_handle,
                  operator_actor
                ) VALUES(?,?,?,'operator','Member removal request',?,?,?,'delivered',?,?,'member',
                         'local',?,?)
                """,
                (
                    uid,
                    member.id,
                    label,
                    "A verified member asked to leave Outpost. Review the retained-record policy "
                    "before approving or rejecting this request.",
                    now,
                    now,
                    now + self.retention.mail_days * DAY,
                    conversation_key,
                    label,
                    f"member:{member.mesh_id}",
                ),
            )
            await write_audit(
                transaction,
                actor_kind="mesh",
                actor_ref=member.mesh_id,
                action="member.data_removal_requested",
                target=f"request:{request_id}",
                detail={"request_type": "removal"},
                created_at=now,
            )
            created = await transaction.read(
                "SELECT * FROM member_data_request WHERE id=?", (request_id,)
            )
        return dict(created[0]), True

    async def list_requests(
        self, state: Literal["pending", "approved", "rejected", "all"] = "pending"
    ) -> list[dict[str, Any]]:
        rows = await self.database.read(
            """
            SELECT r.*,COALESCE(m.handle,m.mesh_id) member_label,m.directory_state
            FROM member_data_request r JOIN member m ON m.id=r.member_id
            WHERE (?='all' OR r.state=?)
            ORDER BY CASE r.state WHEN 'pending' THEN 0 ELSE 1 END,r.requested_at DESC,r.id DESC
            LIMIT 200
            """,
            (state, state),
        )
        return [dict(row) for row in rows]

    async def pending_count(self) -> int:
        rows = await self.database.read(
            "SELECT COUNT(*) count FROM member_data_request WHERE state='pending'"
        )
        return int(rows[0]["count"])

    @staticmethod
    async def _unused_pseudonym(transaction: Transaction) -> tuple[str, int, str]:
        for _ in range(32):
            mesh_num = secrets.randbits(32)
            mesh_id = f"!{mesh_num:08x}"
            label = f"former-{secrets.token_hex(3)}"
            collision = await transaction.read(
                "SELECT 1 FROM member WHERE mesh_id=? OR mesh_num=?", (mesh_id, mesh_num)
            )
            if not collision:
                return mesh_id, mesh_num, label
        raise RuntimeError("Could not allocate a pseudonymous member identity.")

    async def _approve(
        self, transaction: Transaction, request: dict[str, Any], actor: str, reason: str, now: int
    ) -> dict[str, Any]:
        member_id = int(request["member_id"])
        rows = await transaction.read("SELECT mesh_id,handle FROM member WHERE id=?", (member_id,))
        if not rows:
            raise ValueError("Member not found.")
        old_mesh_id = str(rows[0]["mesh_id"])
        old_handle = str(rows[0]["handle"] or "")
        mesh_id, mesh_num, pseudonym = await self._unused_pseudonym(transaction)

        await transaction.write("DELETE FROM member_position WHERE member_id=?", (member_id,))
        await transaction.write(
            "DELETE FROM pending_incident_location WHERE member_id=?", (member_id,)
        )
        await transaction.write("DELETE FROM read_marker WHERE member_id=?", (member_id,))
        await transaction.write("DELETE FROM subscription WHERE member_id=?", (member_id,))
        await transaction.write("DELETE FROM digest_state WHERE member_id=?", (member_id,))
        await transaction.write("DELETE FROM member_pki_replay WHERE member_id=?", (member_id,))
        await transaction.write(
            "UPDATE checkin SET lat=NULL,lon=NULL WHERE member_id=?", (member_id,)
        )
        await transaction.write(
            "UPDATE message_log SET text=NULL,payload=NULL,pki_public_key=NULL,latitude=NULL,"
            "longitude=NULL,peer_mesh_id=CASE WHEN peer_mesh_id=? THEN ? ELSE peer_mesh_id END,"
            "to_mesh_id=CASE WHEN to_mesh_id=? THEN ? ELSE to_mesh_id END "
            "WHERE member_id=? OR peer_mesh_id=? OR to_mesh_id=?",
            (old_mesh_id, mesh_id, old_mesh_id, mesh_id, member_id, old_mesh_id, old_mesh_id),
        )
        await transaction.write(
            "UPDATE ai_interaction SET member_id=NULL,question='',answer=NULL,content_purged_at=? "
            "WHERE member_id=?",
            (now, member_id),
        )
        await transaction.write(
            "UPDATE post SET author_label=? WHERE author_id=?", (pseudonym, member_id)
        )
        await transaction.write(
            "UPDATE incident SET reporter_label=? WHERE reporter_id=?", (pseudonym, member_id)
        )
        await transaction.write(
            "UPDATE incident_update SET author_label=? WHERE author_id=?", (pseudonym, member_id)
        )
        await transaction.write(
            """
            UPDATE mail SET
              from_label=CASE WHEN from_id=? THEN ? ELSE from_label END,
              to_label=CASE WHEN to_id=? THEN ? ELSE to_label END,
              participant_handle=CASE WHEN from_id=? OR to_id=? THEN ? ELSE participant_handle END,
              operator_actor=CASE WHEN operator_actor IN (?,?) THEN ? ELSE operator_actor END,
              reply_recipient_handle=CASE WHEN from_id=? OR to_id=? THEN NULL
                                           ELSE reply_recipient_handle END,
              body='[content removed after approved member removal request]'
            WHERE from_id=? OR to_id=?
            """,
            (
                member_id,
                pseudonym,
                member_id,
                pseudonym,
                member_id,
                member_id,
                pseudonym,
                f"member:@{old_handle}" if old_handle else "",
                f"member:{old_mesh_id}",
                f"former-member:{pseudonym}",
                member_id,
                member_id,
                member_id,
                member_id,
            ),
        )
        await transaction.write(
            "UPDATE web_account SET radio_member_id=NULL,radio_linked_at=NULL,radio_linked_by=NULL "
            "WHERE radio_member_id=?",
            (member_id,),
        )
        await transaction.write(
            """
            UPDATE member SET mesh_id=?,mesh_num=?,handle=NULL,long_name=NULL,short_name=NULL,
              hw_model=NULL,public_key=NULL,pending_public_key=NULL,pki_state='unknown',
              pki_verified_at=NULL,pki_last_seen_at=NULL,trust='guest',last_heard_snr=NULL,
              hops_away=NULL,muted_until=NULL,blocked_until=NULL,unreachable_since=NULL,
              prefs='{"position":"off"}',notes=NULL,handle_changed_at=NULL,prior_handle=NULL,
              prior_handle_until=NULL,directory_state='ignored',directory_state_at=?,
              directory_state_by=?,reviewed_at=?,reviewed_by=? WHERE id=?
            """,
            (mesh_id, mesh_num, now, actor, now, actor, member_id),
        )
        await transaction.write(
            "UPDATE member_data_request SET state='approved',reviewed_at=?,reviewed_by=?,"
            "review_reason=?,pseudonym=? WHERE id=?",
            (now, actor, reason, pseudonym, request["id"]),
        )
        return {"pseudonym": pseudonym, "member_id": member_id}

    async def review(
        self,
        request_id: int,
        action: Literal["approve", "reject"],
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        clean_reason = reason.strip()
        if len(clean_reason) < 3:
            raise ValueError("A review reason of at least 3 characters is required.")
        now = int(self.clock.now().timestamp())
        async with self.database.transaction() as transaction:
            rows = await transaction.read(
                "SELECT * FROM member_data_request WHERE id=?", (request_id,)
            )
            if not rows:
                raise ValueError("Data request not found.")
            request = dict(rows[0])
            if request["state"] != "pending":
                raise ValueError("Data request was already reviewed.")
            detail: dict[str, Any] = {"member_id": int(request["member_id"])}
            if action == "approve":
                detail.update(await self._approve(transaction, request, actor, clean_reason, now))
            else:
                await transaction.write(
                    "UPDATE member_data_request SET state='rejected',reviewed_at=?,reviewed_by=?,"
                    "review_reason=? WHERE id=?",
                    (now, actor, clean_reason, request_id),
                )
            await write_audit(
                transaction,
                actor_kind="web",
                actor_ref=actor,
                action=f"member.data_removal_{'approved' if action == 'approve' else 'rejected'}",
                target=f"request:{request_id}",
                detail=detail,
                created_at=now,
            )
            reviewed = await transaction.read(
                "SELECT * FROM member_data_request WHERE id=?", (request_id,)
            )
        return dict(reviewed[0])
