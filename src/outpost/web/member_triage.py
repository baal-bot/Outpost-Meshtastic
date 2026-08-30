from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from collections.abc import Sequence
from typing import Any

from outpost.store import Database

ADMITTED_TRUST = ("member", "trusted", "responder", "operator")
DISCOVERED_SQL = "handle IS NULL AND trust IN ('guest','blocked')"
APPROVED_SQL = "(handle IS NOT NULL OR trust IN ('member','trusted','responder','operator'))"
REVIEW_ELIGIBLE_SQL = """(
  member.handle IS NOT NULL
  OR member.trust IN ('member','trusted','responder','operator')
  OR EXISTS(
    SELECT 1 FROM member_trust_history review_history
    WHERE review_history.member_id=member.id
      AND review_history.to_trust IN ('member','trusted','responder','operator')
  )
)"""
NEEDS_REVIEW_SQL = f"""(
  {REVIEW_ELIGIBLE_SQL} AND (
    (member.trust IN ('guest','blocked') AND member.reviewed_at IS NULL)
    OR member.pki_state IN ('pending','conflict')
  )
)"""
STATE_ACTIONS = {"archive": "archived", "ignore": "ignored", "restore": "active"}

SAVED_FILTERS = (
    (
        "new",
        "New discoveries",
        "Active discovered radios first heard in the past 24 hours.",
        f"directory_state='active' AND {DISCOVERED_SQL} AND first_seen>=unixepoch()-86400",
    ),
    (
        "recent",
        "Recently heard",
        "Active identities heard in the past 24 hours.",
        "directory_state='active' AND last_seen>=unixepoch()-86400",
    ),
    (
        "stale",
        "Stale discoveries",
        "Unreviewed active radios not heard for 30 days.",
        f"directory_state='active' AND {DISCOVERED_SQL} "
        "AND reviewed_at IS NULL AND last_seen<unixepoch()-2592000",
    ),
    (
        "member",
        "Members",
        "All identities with a claimed username or operator-admitted trust.",
        f"directory_state='active' AND {APPROVED_SQL}",
    ),
    (
        "responder",
        "Responders",
        "Members admitted to responder-level workflows.",
        "directory_state='active' AND trust='responder'",
    ),
    (
        "review",
        "Needs review",
        "Claimed identities and previously admitted radio keys awaiting an operator decision.",
        f"member.directory_state='active' AND {NEEDS_REVIEW_SQL}",
    ),
)


class MemberTriageError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class MemberTriageService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _classification(item: dict[str, Any]) -> dict[str, Any]:
        public_key = item.pop("public_key", None)
        pending_public_key = item.pop("pending_public_key", None)
        item["pki_fingerprint"] = (
            hashlib.sha256(bytes(public_key)).hexdigest() if public_key is not None else None
        )
        item["pki_pending_fingerprint"] = (
            hashlib.sha256(bytes(pending_public_key)).hexdigest()
            if pending_public_key is not None
            else None
        )
        item["pki_elevated_eligible"] = bool(
            item.get("pki_state") == "verified" and public_key is not None
        )
        item["needs_review"] = bool(item.get("needs_review"))
        state = str(item.get("directory_state") or "active")
        trust = str(item.get("trust") or "guest")
        handle = item.get("handle")
        if state == "archived":
            category = "archived"
            reason = "Archived by an operator; retained evidence is hidden from active triage."
        elif state == "ignored":
            category = "ignored"
            reason = "Ignored by an operator; future traffic is logged but does not reopen review."
        elif handle is not None or trust in ADMITTED_TRUST:
            category = "approved"
            if trust == "blocked":
                reason = "Enrolled identity retained for accountability; mesh commands are blocked."
            elif handle is not None:
                reason = "Enrolled identity with a claimed handle; included in member counts."
            else:
                reason = f"Operator-assigned {trust} trust admits this identity as a member."
        elif trust == "blocked":
            category = "blocked"
            reason = "Discovered radio explicitly blocked from command handling."
        else:
            category = "discovered"
            reason = "Heard on the mesh only; no handle or admitted trust has been granted."
        item["category"] = category
        item["category_reason"] = reason
        item["member_eligible"] = (
            state == "active"
            and (handle is not None or trust in ADMITTED_TRUST)
            and trust != "blocked"
        )
        item["promotion_effects"] = {
            "guest": (
                "Discovery-only without a handle. Enrolled handles stay identifiable, but guest "
                "command limits apply."
            ),
            "member": (
                "Enables member counts, member BBS access, welfare rosters, and map display "
                "after an approved POS share."
            ),
            "trusted": (
                "Includes member access plus workflows restricted to trusted community members."
            ),
            "responder": (
                "Includes member access and eligibility for responder alerts and welfare "
                "operations. Requires a reviewed Meshtastic PKI key for mesh actions."
            ),
            "operator": (
                "Grants the highest mesh-command trust after PKI review; it does not create a "
                "web operator account."
            ),
            "blocked": "Keeps identity evidence while suppressing mesh command responses.",
        }
        return item

    @staticmethod
    def _condition(view: str, saved: str | None) -> str:
        if saved:
            match = next((item[3] for item in SAVED_FILTERS if item[0] == saved), None)
            if match is None:
                raise MemberTriageError("invalid_filter", "Unknown saved member filter.")
            return match
        views = {
            "approved": f"directory_state='active' AND {APPROVED_SQL}",
            "discovered": f"directory_state='active' AND {DISCOVERED_SQL}",
            "archived": "directory_state IN ('archived','ignored')",
            "all": "1=1",
        }
        try:
            return views[view]
        except KeyError as error:
            raise MemberTriageError("invalid_view", "Unknown member directory view.") from error

    async def list(
        self,
        *,
        view: str,
        saved: str | None,
        query: str,
        cursor: int,
        limit: int,
    ) -> dict[str, Any]:
        conditions = [self._condition(view, saved)]
        params: list[Any] = []
        if query.strip():
            conditions.append(
                "(mesh_id LIKE ? OR COALESCE(handle,'') LIKE ? OR "
                "COALESCE(long_name,'') LIKE ? OR COALESCE(short_name,'') LIKE ? OR "
                "COALESCE(notes,'') LIKE ?)"
            )
            term = f"%{query.strip()}%"
            params.extend([term] * 5)
        params.extend((limit + 1, cursor))
        rows = await self.database.read(
            f"""
            SELECT id,mesh_id,handle,long_name,short_name,hw_model,trust,first_seen,last_seen,
                   last_heard_snr,hops_away,notes,directory_state,directory_state_at,
                   directory_state_by,reviewed_at,reviewed_by,
                   public_key,pending_public_key,pki_state,pki_verified_at,pki_last_seen_at,
                   {NEEDS_REVIEW_SQL} needs_review,
                   COALESCE(json_extract(prefs,'$.position'),'coarse') position_consent,
                   EXISTS(SELECT 1 FROM member_position p
                          WHERE p.member_id=member.id AND p.expires_at>unixepoch()) active_position,
                   (SELECT p.received_at FROM member_position p
                    WHERE p.member_id=member.id) position_received_at,
                   (SELECT p.expires_at FROM member_position p
                    WHERE p.member_id=member.id) position_expires_at,
                   (SELECT COUNT(*) FROM message_log ml
                    WHERE (ml.member_id=member.id OR ml.peer_mesh_id=member.mesh_id)
                      AND ml.created_at>=unixepoch()-86400) activity_24h
            FROM member WHERE {" AND ".join(conditions)}
            ORDER BY CASE directory_state WHEN 'active' THEN 0 WHEN 'archived' THEN 1 ELSE 2 END,
                     last_seen DESC,id
            LIMIT ? OFFSET ?
            """,  # noqa: S608 - conditions are fixed expressions plus parameterized search.
            tuple(params),
        )
        count_rows = await self.database.read(
            f"SELECT COUNT(*) count FROM member WHERE {' AND '.join(conditions)}",  # noqa: S608
            tuple(params[:-2]),
        )
        summary = await self.database.read(
            f"""SELECT
              SUM(directory_state='active' AND
                  (handle IS NOT NULL OR
                   trust IN ('member','trusted','responder','operator'))) approved_count,
              SUM(directory_state='active' AND handle IS NULL AND
                  trust IN ('guest','blocked')) discovered_count,
              SUM(directory_state='active' AND
                  {NEEDS_REVIEW_SQL})
                review_count,
              SUM(directory_state='archived') archived_count,
              SUM(directory_state='ignored') ignored_count,
              SUM(directory_state='active' AND trust IN ('trusted','responder','operator'))
                trusted_count,
              SUM(directory_state='active' AND trust IN ('responder','operator'))
                responder_count
            FROM member"""  # noqa: S608 - review expression is a fixed application constant.
        )
        filter_counts = await self.database.read(
            f"""SELECT
              SUM(directory_state='active' AND handle IS NULL AND
                  trust IN ('guest','blocked') AND first_seen>=unixepoch()-86400) new,
              SUM(directory_state='active' AND last_seen>=unixepoch()-86400) recent,
              SUM(directory_state='active' AND handle IS NULL AND
                  trust IN ('guest','blocked') AND reviewed_at IS NULL AND
                  last_seen<unixepoch()-2592000) stale,
              SUM(directory_state='active' AND
                  (handle IS NOT NULL OR
                   trust IN ('member','trusted','responder','operator'))) member,
              SUM(directory_state='active' AND trust='responder') responder,
              SUM(directory_state='active' AND
                  {NEEDS_REVIEW_SQL}) review
            FROM member"""  # noqa: S608 - review expression is a fixed application constant.
        )
        items = [self._classification(dict(row)) for row in rows[:limit]]
        counts = {key: int(value or 0) for key, value in dict(summary[0]).items()}
        return {
            "items": items,
            **counts,
            "total": int(count_rows[0]["count"]),
            "next_cursor": cursor + limit if len(rows) > limit else None,
            "saved_filters": [
                {
                    "key": key,
                    "label": label,
                    "description": detail,
                    "count": int(filter_counts[0][key] or 0),
                }
                for key, label, detail, _condition in SAVED_FILTERS
            ],
        }

    async def detail(self, member_id: int) -> dict[str, Any] | None:
        rows = await self.database.read(
            f"""
            SELECT id,mesh_id,mesh_num,handle,long_name,short_name,hw_model,trust,first_seen,
                   last_seen,last_heard_snr,hops_away,notes,directory_state,directory_state_at,
                   directory_state_by,reviewed_at,reviewed_by,
                   public_key,pending_public_key,pki_state,pki_verified_at,pki_last_seen_at,
                   {NEEDS_REVIEW_SQL} needs_review,
                   COALESCE(json_extract(prefs,'$.position'),'coarse') position_consent,
                   p.lat position_lat,p.lon position_lon,p.received_at position_received_at,
                   p.source position_source,p.expires_at position_expires_at
            FROM member LEFT JOIN member_position p ON p.member_id=member.id
            WHERE member.id=?
            """,  # noqa: S608 - review expression is a fixed application constant.
            (member_id,),
        )
        if not rows:
            return None
        member = self._classification(dict(rows[0]))
        activity = await self.database.read(
            """
            SELECT direction,channel,portnum,is_direct,airtime_class,command,outcome,drop_reason,
                   byte_len,rx_snr,rx_rssi,hops,transport,created_at
            FROM message_log
            WHERE member_id=? OR peer_mesh_id=?
            ORDER BY created_at DESC,id DESC LIMIT 12
            """,
            (member_id, member["mesh_id"]),
        )
        history = await self.database.read(
            """
            SELECT from_trust,to_trust,changed_by,reason,created_at
            FROM member_trust_history WHERE member_id=? ORDER BY created_at DESC,id DESC LIMIT 20
            """,
            (member_id,),
        )
        pki_events = await self.database.read(
            """
            SELECT event,fingerprint,prior_fingerprint,actor,detail,created_at
            FROM member_pki_event WHERE member_id=? ORDER BY created_at DESC,id DESC LIMIT 20
            """,
            (member_id,),
        )
        stats = await self.database.read(
            """
            SELECT
              (SELECT COUNT(*) FROM message_log
               WHERE member_id=? OR peer_mesh_id=?) messages,
              (SELECT COUNT(*) FROM incident WHERE reporter_id=?) incidents,
              (SELECT COUNT(*) FROM checkin WHERE member_id=?) checkins,
              (SELECT COUNT(*) FROM mail WHERE from_id=? OR to_id=?) mail
            """,
            (member_id, member["mesh_id"], member_id, member_id, member_id, member_id),
        )
        member["position_state"] = (
            "active"
            if member.get("position_expires_at") is not None
            and int(member["position_expires_at"]) > int(time.time())
            else "expired"
            if member.get("position_expires_at") is not None
            else "not_shared"
        )
        return {
            "member": member,
            "recent_activity": [dict(row) for row in activity],
            "trust_history": [dict(row) for row in history],
            "pki_events": [dict(row) for row in pki_events],
            "stats": {key: int(value or 0) for key, value in dict(stats[0]).items()},
        }

    async def update(
        self,
        member_id: int,
        *,
        trust: str | None,
        notes: str | None,
        notes_supplied: bool,
        reason: str | None,
        actor: str = "web:operator",
    ) -> dict[str, bool]:
        now_reason = (reason or "").strip()
        async with self.database.transaction() as transaction:
            rows = await transaction.read(
                "SELECT mesh_id,trust,notes,directory_state,public_key,pki_state "
                "FROM member WHERE id=?",
                (member_id,),
            )
            if not rows:
                raise MemberTriageError("not_found", "Member not found.")
            before = rows[0]
            trust_changed = trust is not None and trust != before["trust"]
            if trust_changed and len(now_reason) < 3:
                raise MemberTriageError(
                    "reason_required", "Record a reason before changing member trust."
                )
            if trust in {"trusted", "responder", "operator"} and (
                before["public_key"] is None or before["pki_state"] != "verified"
            ):
                raise MemberTriageError(
                    "pki_required",
                    "Observe and approve this radio's Meshtastic PKI key before elevating trust.",
                )
            assignments = ["reviewed_at=?", "reviewed_by=?"]
            now = int(time.time())
            params: list[Any] = [now, actor]
            if trust is not None:
                assignments.append("trust=?")
                params.append(trust)
                if trust in ADMITTED_TRUST:
                    assignments.extend(
                        ["directory_state='active'", "directory_state_at=?", "directory_state_by=?"]
                    )
                    params.extend((now, actor))
            if notes_supplied:
                assignments.append("notes=?")
                params.append(notes or None)
            params.append(member_id)
            await transaction.write(
                f"UPDATE member SET {','.join(assignments)} WHERE id=?",  # noqa: S608
                tuple(params),
            )
            if trust_changed:
                await transaction.write(
                    """
                    INSERT INTO member_trust_history(
                      member_id,from_trust,to_trust,changed_by,reason,created_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (member_id, before["trust"], trust, actor, now_reason, now),
                )
            detail = json.dumps(
                {
                    "fields": [
                        *(["trust"] if trust is not None else []),
                        *(["notes"] if notes_supplied else []),
                    ],
                    "trust_before": before["trust"],
                    "trust_after": trust if trust is not None else before["trust"],
                    "reason": now_reason or None,
                },
                separators=(",", ":"),
            )
            await transaction.write(
                """
                INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
                VALUES('web',?,'member.update',?,?,?)
                """,
                (actor.removeprefix("web:"), before["mesh_id"], detail, now),
            )
        return {"ok": True}

    async def review_pki(
        self,
        member_id: int,
        action: str,
        reason: str,
        *,
        actor: str = "web:operator",
    ) -> dict[str, Any]:
        if action not in {"approve", "reject"}:
            raise MemberTriageError("invalid_action", "Unknown PKI review action.")
        clean_reason = reason.strip()
        if len(clean_reason) < 3:
            raise MemberTriageError("reason_required", "Record a reason for the PKI review.")
        now = int(time.time())
        async with self.database.transaction() as transaction:
            rows = await transaction.read(
                "SELECT mesh_id,public_key,pending_public_key,pki_state FROM member WHERE id=?",
                (member_id,),
            )
            if not rows:
                raise MemberTriageError("not_found", "Member not found.")
            row = rows[0]
            if row["pending_public_key"] is None:
                raise MemberTriageError("no_pending_key", "No authenticated PKI key awaits review.")
            current = bytes(row["public_key"]) if row["public_key"] is not None else None
            pending = bytes(row["pending_public_key"])
            current_fingerprint = (
                hashlib.sha256(current).hexdigest() if current is not None else None
            )
            pending_fingerprint = hashlib.sha256(pending).hexdigest()
            if action == "approve":
                await transaction.write(
                    "UPDATE member SET public_key=pending_public_key,pending_public_key=NULL,"
                    "pki_state='verified',pki_verified_at=?,pki_last_seen_at=COALESCE("
                    "pki_last_seen_at,?) WHERE id=?",
                    (now, now, member_id),
                )
                state = "verified"
                event = "verified"
            else:
                state = "verified" if current is not None else "unknown"
                await transaction.write(
                    "UPDATE member SET pending_public_key=NULL,pki_state=? WHERE id=?",
                    (state, member_id),
                )
                event = "rejected"
            await transaction.write(
                "INSERT INTO member_pki_event(member_id,event,fingerprint,prior_fingerprint,"
                "actor,detail,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    member_id,
                    event,
                    pending_fingerprint,
                    current_fingerprint,
                    actor,
                    json.dumps({"reason": clean_reason}, separators=(",", ":")),
                    now,
                ),
            )
            await transaction.write(
                "INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at) "
                "VALUES('web',?,?,?,?,?)",
                (
                    actor.removeprefix("web:"),
                    f"member.pki.{action}",
                    row["mesh_id"],
                    json.dumps(
                        {
                            "fingerprint": pending_fingerprint,
                            "prior_fingerprint": current_fingerprint,
                            "reason": clean_reason,
                        },
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )
        return {"ok": True, "state": state, "fingerprint": pending_fingerprint}

    async def set_state(
        self, member_id: int, action: str, reason: str, *, actor: str = "web:operator"
    ) -> dict[str, Any]:
        if action not in STATE_ACTIONS:
            raise MemberTriageError("invalid_action", "Unknown directory action.")
        clean_reason = reason.strip()
        if action != "restore" and len(clean_reason) < 3:
            raise MemberTriageError("reason_required", "Record a reason for this directory action.")
        async with self.database.transaction() as transaction:
            rows = await transaction.read(
                "SELECT mesh_id,handle,trust,directory_state FROM member WHERE id=?", (member_id,)
            )
            if not rows:
                raise MemberTriageError("not_found", "Member not found.")
            row = rows[0]
            if action in {"archive", "ignore"} and row["directory_state"] != "active":
                raise MemberTriageError(
                    "inactive_identity", "Restore this identity before changing its inactive state."
                )
            if action in {"archive", "ignore"} and not (
                row["handle"] is None and row["trust"] in {"guest", "blocked"}
            ):
                raise MemberTriageError(
                    "not_discovered", "Only discovered radios can be archived or ignored."
                )
            if action == "restore" and row["directory_state"] == "active":
                raise MemberTriageError("already_active", "This identity is already active.")
            now = int(time.time())
            state = STATE_ACTIONS[action]
            await transaction.write(
                "UPDATE member SET directory_state=?,directory_state_at=?,directory_state_by=?,"
                "reviewed_at=?,reviewed_by=? WHERE id=?",
                (state, now, actor, now, actor, member_id),
            )
            await transaction.write(
                """
                INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
                VALUES('web',?,?,?,?,?)
                """,
                (
                    actor.removeprefix("web:"),
                    f"member.{action}",
                    row["mesh_id"],
                    json.dumps(
                        {"from": row["directory_state"], "to": state, "reason": clean_reason},
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )
        return {"ok": True, "state": state}

    async def bulk(
        self, member_ids: Sequence[int], action: str, reason: str, *, actor: str = "web:operator"
    ) -> dict[str, Any]:
        ids: tuple[int, ...] = tuple(dict.fromkeys(member_ids))
        if not ids or len(ids) > 200 or any(member_id <= 0 for member_id in ids):
            raise MemberTriageError("invalid_selection", "Select between 1 and 200 identities.")
        if action not in STATE_ACTIONS:
            raise MemberTriageError("invalid_action", "Unknown directory action.")
        clean_reason = reason.strip()
        if action != "restore" and len(clean_reason) < 3:
            raise MemberTriageError("reason_required", "Record a reason for this bulk action.")
        placeholders = ",".join("?" for _ in ids)
        async with self.database.transaction() as transaction:
            rows = await transaction.read(
                f"SELECT id,mesh_id,handle,trust,directory_state FROM member "  # noqa: S608
                f"WHERE id IN ({placeholders})",
                ids,
            )
            eligible = [
                row
                for row in rows
                if (
                    action in {"archive", "ignore"}
                    and row["directory_state"] == "active"
                    and row["handle"] is None
                    and row["trust"] in {"guest", "blocked"}
                )
                or (action == "restore" and row["directory_state"] != "active")
            ]
            eligible_ids = tuple(int(row["id"]) for row in eligible)
            now = int(time.time())
            state = STATE_ACTIONS[action]
            if eligible_ids:
                eligible_placeholders = ",".join("?" for _ in eligible_ids)
                await transaction.write(
                    f"UPDATE member SET directory_state=?,directory_state_at=?,"  # noqa: S608
                    f"directory_state_by=?,reviewed_at=?,reviewed_by=? "
                    f"WHERE id IN ({eligible_placeholders})",
                    (state, now, actor, now, actor, *eligible_ids),
                )
            await transaction.write(
                """
                INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
                VALUES('web',?,?,'member:bulk',?,?)
                """,
                (
                    actor.removeprefix("web:"),
                    f"member.bulk_{action}",
                    json.dumps(
                        {
                            "selected": len(ids),
                            "changed": len(eligible_ids),
                            "skipped": len(ids) - len(eligible_ids),
                            "mesh_ids": [row["mesh_id"] for row in eligible],
                            "reason": clean_reason,
                        },
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )
        return {
            "ok": True,
            "action": action,
            "changed": len(eligible_ids),
            "skipped": len(ids) - len(eligible_ids),
        }

    @staticmethod
    def _csv_safe(value: Any) -> Any:
        if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
            return f"'{value}"
        return value

    async def export(
        self, member_ids: Sequence[int], *, actor: str = "web:operator"
    ) -> tuple[str, int]:
        ids: tuple[int, ...] = tuple(dict.fromkeys(member_ids))
        if not ids or len(ids) > 200 or any(member_id <= 0 for member_id in ids):
            raise MemberTriageError("invalid_selection", "Select between 1 and 200 identities.")
        placeholders = ",".join("?" for _ in ids)
        rows = await self.database.read(
            f"""SELECT mesh_id,handle,long_name,short_name,hw_model,trust,directory_state,
                       first_seen,last_seen,last_heard_snr,hops_away,
                       COALESCE(json_extract(prefs,'$.position'),'coarse') position_consent,notes
                FROM member WHERE id IN ({placeholders}) ORDER BY last_seen DESC""",  # noqa: S608
            ids,
        )
        output = io.StringIO(newline="")
        fields = [
            "mesh_id",
            "handle",
            "long_name",
            "short_name",
            "hw_model",
            "trust",
            "directory_state",
            "first_seen",
            "last_seen",
            "last_heard_snr",
            "hops_away",
            "position_consent",
            "notes",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: self._csv_safe(row[key]) for key in fields})
        await self.database.write(
            """
            INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
            VALUES('web',?,'member.export','member:bulk',?,unixepoch())
            """,
            (
                actor.removeprefix("web:"),
                json.dumps({"count": len(rows), "ids": list(ids)}, separators=(",", ":")),
            ),
        )
        return output.getvalue(), len(rows)
