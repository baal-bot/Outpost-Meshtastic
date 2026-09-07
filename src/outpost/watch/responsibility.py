"""Version-bound local incident responsibility, independent of alerts and federation.

One existing SQLite writer owns authorization, compare-and-swap, history and audit.
No network effect or in-memory publication is needed to commit a decision.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

from outpost.audit import write_audit
from outpost.clock import Clock
from outpost.store import Database, Transaction

Action = Literal["offer", "accept", "update", "cancel", "release", "complete"]
TargetKind = Literal["member", "group", "account"]
STALE_SECONDS = 30 * 60
MAX_ACTION_BYTES = 160


class ResponsibilityConflict(ValueError):
    """A current view is required; never silently retry a decision."""


class ResponsibilityDenied(ValueError):
    """Current identity/role does not authorize this action."""


@dataclass(frozen=True)
class ResponsibilityActor:
    # Mesh callers supply the authenticated packet key, not just the cached member.
    member_id: int | None = None
    account_id: int | None = None
    public_key: bytes | None = None
    session_hash: str | None = None


@dataclass(frozen=True)
class _Authorized:
    member_id: int | None
    account_id: int | None
    operator: bool
    audit_kind: Literal["mesh", "web"]
    audit_ref: str


class IncidentResponsibilityService:
    def __init__(self, database: Database, clock: Clock) -> None:
        self.database, self.clock = database, clock
        self._epoch = secrets.token_hex(16)
        self._baseline: tuple[float, float] | None = None

    def _time(self) -> tuple[int, str]:
        wall, mono = self.clock.now().timestamp(), self.clock.monotonic()
        if self._baseline is not None:
            prior_wall, prior_mono = self._baseline
            if mono < prior_mono or abs((wall - prior_wall) - (mono - prior_mono)) > 5:
                # Ownership survives a clock step; its freshness and old decisions do not.
                self._epoch = secrets.token_hex(16)
        self._baseline = (wall, mono)
        return int(wall), self._epoch

    @staticmethod
    async def _authorize(store: Transaction, actor: ResponsibilityActor) -> _Authorized:
        if (actor.member_id is None) == (actor.account_id is None):
            raise ResponsibilityDenied("An authenticated responder or operator is required.")
        if actor.account_id is not None:
            sessions = await store.read(
                "SELECT 1 FROM web_session WHERE token_hash=? AND account_id=? AND expires_at>?",
                (actor.session_hash, actor.account_id, int(time.time())),
            )
            if not sessions:
                raise ResponsibilityDenied(
                    "The operator session expired or was revoked; sign in again."
                )
            rows = await store.read("SELECT * FROM web_account WHERE id=?", (actor.account_id,))
            if (
                not rows
                or rows[0]["role"] not in {"administrator", "operator"}
                or not rows[0]["enabled"]
                or rows[0]["must_change"]
            ):
                raise ResponsibilityDenied("A current operator account is required.")
            account = rows[0]
            linked = await store.read(
                "SELECT id FROM member WHERE id=? AND trust IN ('responder','operator') "
                "AND pki_state='verified'",
                (account["radio_member_id"],),
            )
            return _Authorized(
                int(linked[0]["id"]) if linked else None,
                actor.account_id,
                True,
                "web",
                str(account["username"]),
            )
        rows = await store.read("SELECT * FROM member WHERE id=?", (actor.member_id,))
        if (
            not rows
            or rows[0]["trust"] not in {"responder", "operator"}
            or rows[0]["pki_state"] != "verified"
            or actor.public_key is None
            or rows[0]["public_key"] != actor.public_key
        ):
            raise ResponsibilityDenied("Use a currently verified responder PKI direct message.")
        return _Authorized(
            actor.member_id, None, rows[0]["trust"] == "operator", "mesh", rows[0]["mesh_id"]
        )

    @staticmethod
    async def _target(
        store: Database | Transaction, target_id: int | None
    ) -> dict[str, Any] | None:
        if target_id is None:
            return None
        rows = await store.read(
            "SELECT t.*,m.handle,m.trust,m.pki_state,g.name,a.username,"
            "a.role,a.enabled,a.must_change "
            "FROM incident_responsibility_target t "
            "LEFT JOIN member m ON m.id=t.member_id "
            "LEFT JOIN responder_group g ON g.id=t.group_id "
            "LEFT JOIN web_account a ON a.id=t.account_id WHERE t.id=?",
            (target_id,),
        )
        if not rows:
            return None
        value = dict(rows[0])
        if value["kind"] == "member":
            available = (
                value["trust"] in {"responder", "operator"} and value["pki_state"] == "verified"
            )
            label = value["handle"] or "Unnamed responder"
        elif value["kind"] == "account":
            available = bool(
                value["role"] in {"administrator", "operator"}
                and value["enabled"]
                and not value["must_change"]
            )
            label = value["username"] or "Removed account"
        else:
            eligible = await store.read(
                "SELECT 1 FROM responder_group_member gm JOIN member m ON m.id=gm.member_id "
                "WHERE gm.group_id=? AND m.trust IN ('responder','operator') "
                "AND m.pki_state='verified' LIMIT 1",
                (value["group_id"],),
            )
            available = value["name"] is not None and bool(eligible)
            label = value["name"] or "Removed team"
        return {
            "id": target_id,
            "kind": value["kind"],
            "reference": value[f"{value['kind']}_id"],
            "label": label,
            "available": available,
        }

    @staticmethod
    async def _represents(
        store: Transaction, actor: _Authorized, target: dict[str, Any] | None
    ) -> bool:
        if target is None or not target["available"]:
            return False
        reference = target["reference"]
        if target["kind"] == "account":
            return bool(actor.account_id == reference)
        if target["kind"] == "member":
            return bool(actor.member_id == reference)
        return bool(
            actor.member_id is not None
            and await store.read(
                "SELECT 1 FROM responder_group_member WHERE group_id=? AND member_id=?",
                (reference, actor.member_id),
            )
        )

    async def _state(self, store: Database | Transaction, incident_id: int) -> dict[str, Any]:
        if type(incident_id) is not int or not 0 < incident_id < 2**63:
            raise ValueError("Invalid incident identity.")
        incidents = await store.read(
            "SELECT id,uid,local_ref,status,merged_into_id FROM incident WHERE id=?", (incident_id,)
        )
        if not incidents:
            raise ValueError("Incident not found.")
        incident = dict(incidents[0])
        rows = await store.read(
            "SELECT * FROM incident_responsibility WHERE incident_id=?", (incident_id,)
        )
        current: dict[str, Any] = (
            dict(rows[0])
            if rows
            else {
                "incident_id": incident_id,
                "version": 0,
                "owner_id": None,
                "offer_id": None,
                "accepted_at": None,
                "verified_at": None,
                "verification_epoch": None,
                "next_action": "",
                "offer_action": "",
                "closed_state": "unassigned",
                "updated_at": None,
            }
        )
        return {
            "incident": incident,
            "current": current,
            "owner": await self._target(store, current["owner_id"]),
            "offer": await self._target(store, current["offer_id"]),
        }

    @staticmethod
    def _token(state: dict[str, Any], epoch: str) -> str:
        # A 96-bit comparison token fits a bounded handheld command. It is not authorization.
        payload = json.dumps([epoch, state], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    def _view(self, state: dict[str, Any], now: int, epoch: str) -> dict[str, Any]:
        current = state["current"]
        verified = current["verified_at"]
        fresh = (
            verified is not None
            and 0 <= now - verified <= STALE_SECONDS
            and current["verification_epoch"] == epoch
        )
        return {
            "incident_uid": state["incident"]["uid"],
            "local_ref": state["incident"]["local_ref"],
            "scope": "This Outpost only; not globally exclusive during a partition.",
            "state": "accepted" if state["owner"] else current["closed_state"],
            "owner": state["owner"],
            "offer": state["offer"],
            "acceptance_pending": state["offer"] is not None,
            "next_action": current["next_action"],
            "offer_action": current["offer_action"],
            "accepted_at": current["accepted_at"],
            "verified_at": verified,
            "verification": "fresh" if fresh else "stale_or_unverified",
            "stale_after_seconds": STALE_SECONDS,
            "version": current["version"],
            "review_token": self._token(state, epoch),
            "updated_at": current["updated_at"],
        }

    async def snapshot(
        self,
        incident_id: int,
        actor: ResponsibilityActor | None = None,
    ) -> dict[str, Any]:
        async with self.database.transaction() as transaction:
            authorized = await self._authorize(transaction, actor) if actor is not None else None
            state = await self._state(transaction, incident_id)
            now, epoch = self._time()
            view = self._view(state, now, epoch)
            if authorized is not None:
                owner = await self._represents(transaction, authorized, state["owner"])
                actions = []
                if state["incident"]["merged_into_id"] is None:
                    if (authorized.operator or owner) and state["incident"]["status"] in {
                        "open",
                        "monitoring",
                    }:
                        actions.append("offer")
                    if await self._represents(transaction, authorized, state["offer"]):
                        actions.append("accept")
                    if owner:
                        actions.extend(("update", "complete"))
                    if (authorized.operator or owner) and state["owner"]:
                        actions.append("release")
                    if (authorized.operator or owner) and state["offer"]:
                        actions.append("cancel")
                view["allowed_actions"] = actions
            return view

    async def targets(
        self,
        kind: TargetKind,
        *,
        after: int = 0,
        query: str = "",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        if (
            kind not in {"member", "group", "account"}
            or not 0 <= after < 2**63
            or not 1 <= limit <= 50
            or len(query) > 50
        ):
            raise ValueError("Invalid target page.")
        statements = {
            "member": "SELECT id,COALESCE(handle,'Unnamed responder') label FROM member "
            "WHERE trust IN ('responder','operator') AND pki_state='verified'",
            "account": "SELECT id,username label FROM web_account "
            "WHERE role IN ('operator','administrator') "
            "AND enabled=1 AND must_change=0",
            "group": "SELECT id,name label FROM responder_group g WHERE EXISTS ("
            "SELECT 1 FROM responder_group_member gm JOIN member m ON m.id=gm.member_id "
            "WHERE gm.group_id=g.id AND m.trust IN ('responder','operator') "
            "AND m.pki_state='verified')",
        }
        rows = await self.database.read(
            "SELECT t.id,eligible.label FROM ("  # noqa: S608 - fixed query selected by validated enum
            + statements[kind]
            + f") eligible JOIN incident_responsibility_target t ON t.{kind}_id=eligible.id "
            "WHERE t.id>? AND instr(lower(label),lower(?))>0 ORDER BY t.id LIMIT ?",
            (after, query, limit),
        )
        return [{"kind": kind, "reference": int(row["id"]), "label": row["label"]} for row in rows]

    async def history(
        self,
        incident_id: int,
        *,
        after: int = 0,
        through: int = 2**63 - 1,
    ) -> list[dict[str, Any]]:
        rows = await self.database.read(
            "SELECT e.*,m.handle,a.username FROM incident_responsibility_event e "
            "LEFT JOIN member m ON m.id=e.actor_member_id "
            "LEFT JOIN web_account a ON a.id=e.actor_account_id "
            "WHERE e.incident_id=? AND e.version>? AND e.version<=? ORDER BY e.version LIMIT 100",
            (incident_id, after, through),
        )
        return [
            {
                "version": int(row["version"]),
                "action": row["action"],
                "actor": row["username"] or row["handle"] or "Unnamed/former responder",
                "target": await self._target(self.database, row["target_id"]),
                "owner": await self._target(self.database, row["owner_id"]),
                "next_action": row["next_action"],
                "created_at": int(row["created_at"]),
            }
            for row in rows
        ]

    @staticmethod
    def summary(view: dict[str, Any]) -> str:
        def label(target: dict[str, Any] | None) -> str:
            if target is None:
                return "none"
            return str(target["label"]) + (" (unavailable)" if not target["available"] else "")

        return (
            f"Local responsibility: {view['state']}; owner {label(view['owner'])}; "
            f"pending acceptance {label(view['offer'])}; {view['verification']}; "
            f"next action: {view['next_action'] or 'none'}. "
            "This Outpost only; not globally exclusive. ACK is not acceptance or completion."
        )

    async def apply(
        self,
        incident_id: int,
        actor: ResponsibilityActor,
        action: Action,
        token: str,
        *,
        target_kind: TargetKind | None = None,
        target_ref: int | None = None,
        next_action: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"offer", "accept", "update", "cancel", "release", "complete"}:
            raise ValueError("Unknown responsibility action.")
        if not re.fullmatch(r"[0-9a-f]{24}", token):
            raise ResponsibilityConflict("Refresh responsibility and review the current decision.")
        if next_action is not None:
            next_action = next_action.strip()
            if len(next_action.encode()) > MAX_ACTION_BYTES or any(
                ord(c) < 32 for c in next_action
            ):
                raise ValueError("Next action must be one line, at most 160 UTF-8 bytes.")
        if action in {"offer", "update"} and not next_action:
            raise ValueError("A next action is required.")
        if action != "offer" and (target_kind is not None or target_ref is not None):
            raise ValueError("Only an offer specifies a target.")
        if action not in {"offer", "update"} and next_action is not None:
            raise ValueError("Only offer or update changes the next action.")
        async with self.database.transaction() as transaction:
            authorized = await self._authorize(transaction, actor)
            state = await self._state(transaction, incident_id)
            now, epoch = self._time()
            if not hmac.compare_digest(token, self._token(state, epoch)):
                raise ResponsibilityConflict(
                    "Responsibility changed. Refresh and review before acting."
                )
            if state["incident"]["merged_into_id"] is not None:
                raise ResponsibilityConflict(
                    "This incident was merged. Review its canonical record."
                )
            current = dict(state["current"])
            represents_owner = await self._represents(transaction, authorized, state["owner"])
            target_id = (
                current["offer_id"]
                if action in {"offer", "accept", "cancel"}
                else current["owner_id"]
            )
            if action == "accept":
                if not current["offer_action"]:
                    raise ResponsibilityConflict(
                        "No current offered next action; ask an operator to renew the offer."
                    )
                if not await self._represents(transaction, authorized, state["offer"]):
                    raise ResponsibilityDenied(
                        "Only the offered person or a current team member may accept."
                    )
                current.update(
                    owner_id=target_id,
                    offer_id=None,
                    accepted_at=now,
                    verified_at=now,
                    verification_epoch=epoch,
                    next_action=current["offer_action"],
                    offer_action="",
                )
            elif action == "offer":
                if not authorized.operator and not represents_owner:
                    raise ResponsibilityDenied(
                        "Only an operator or accepted owner may offer responsibility."
                    )
                if state["incident"]["status"] not in {"open", "monitoring"}:
                    raise ValueError("Reopen the incident before offering new responsibility.")
                if (
                    target_kind not in {"member", "group", "account"}
                    or type(target_ref) is not int
                    or not 0 < target_ref < 2**63
                ):
                    raise ValueError("Choose a current responder, team or operator account.")
                # Non-reusable catalog identity, not a potentially reused member/group row ID.
                target_id = target_ref
                target = await self._target(transaction, target_id)
                if (
                    target is None
                    or target["kind"] != target_kind
                    or not target["available"]
                    or target_id == current["owner_id"]
                ):
                    raise ValueError("Target is unavailable or already owns this responsibility.")
                current.update(offer_id=target_id, offer_action=next_action)
            elif action == "update":
                if not represents_owner:
                    raise ResponsibilityDenied(
                        "Only the accepted owner may verify the next action."
                    )
                current.update(next_action=next_action, verified_at=now, verification_epoch=epoch)
            else:
                if not authorized.operator and not represents_owner:
                    raise ResponsibilityDenied(
                        "Only an operator or accepted owner may end this decision."
                    )
                if action == "cancel":
                    if state["offer"] is None:
                        raise ValueError("No offer is pending.")
                    current.update(offer_id=None, offer_action="")
                else:
                    if state["owner"] is None:
                        raise ValueError("No accepted responsibility to release or complete.")
                    if action == "complete" and not represents_owner:
                        raise ResponsibilityDenied(
                            "Only the accepted owner may mark responsibility complete."
                        )
                    current.update(
                        owner_id=None,
                        offer_id=None,
                        next_action="",
                        offer_action="",
                        accepted_at=None,
                        verified_at=None,
                        verification_epoch=None,
                        closed_state="completed" if action == "complete" else "released",
                    )
            current.update(version=current["version"] + 1, updated_at=now)
            await transaction.write(
                "INSERT INTO incident_responsibility(incident_id,version,owner_id,offer_id,"
                "accepted_at,verified_at,verification_epoch,next_action,offer_action,"
                "closed_state,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(incident_id) DO UPDATE SET "
                "version=excluded.version,owner_id=excluded.owner_id,offer_id=excluded.offer_id,"
                "accepted_at=excluded.accepted_at,verified_at=excluded.verified_at,"
                "verification_epoch=excluded.verification_epoch,next_action=excluded.next_action,"
                "offer_action=excluded.offer_action,closed_state=excluded.closed_state,updated_at=excluded.updated_at",
                tuple(
                    current[key]
                    for key in (
                        "incident_id",
                        "version",
                        "owner_id",
                        "offer_id",
                        "accepted_at",
                        "verified_at",
                        "verification_epoch",
                        "next_action",
                        "offer_action",
                        "closed_state",
                        "updated_at",
                    )
                ),
            )
            await transaction.write(
                "INSERT INTO incident_responsibility_event(incident_id,version,action,"
                "actor_member_id,actor_account_id,target_id,owner_id,next_action,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    incident_id,
                    current["version"],
                    action,
                    authorized.member_id,
                    authorized.account_id,
                    target_id,
                    current["owner_id"],
                    next_action or current["next_action"],
                    now,
                ),
            )
            await write_audit(
                transaction,
                actor_kind=authorized.audit_kind,
                actor_ref=authorized.audit_ref,
                action=f"incident.responsibility.{action}",
                target=state["incident"]["uid"],
                detail={
                    "version": current["version"],
                    "target_id": target_id,
                    "owner_id": current["owner_id"],
                },
                created_at=now,
            )
            updated = await self._state(transaction, incident_id)
            return self._view(updated, now, epoch)
