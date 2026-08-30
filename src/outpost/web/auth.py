from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import string
import struct
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from outpost.audit import write_audit
from outpost.store import Database


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _initial_password() -> str:
    alphabet = string.ascii_letters + string.digits + "-_."
    return "".join(secrets.choice(alphabet) for _ in range(24))


def _normalize_username(username: str) -> str:
    value = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,31}", value):
        raise ValueError("Username must be 2-32 letters, numbers, dots, dashes, or underscores.")
    return value


def _totp(secret: str, at: int, *, period: int = 30, digits: int = 6) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", at // period), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % (10**digits)).zfill(digits)


def _normalize_recovery_code(code: str) -> str:
    return "".join(character for character in code.upper() if character.isalnum())


SETUP_SECRET_TTL_SECONDS = 3_600
SETUP_FILE_NAME = "setup-token"
STEP_UP_SECONDS = 600
RECOVERY_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
ROLES = {"administrator", "operator", "viewer"}


@dataclass(frozen=True)
class WebSession:
    csrf_token: str
    must_change: bool
    account_id: int
    username: str
    display_name: str
    role: str
    mfa_enabled: bool
    step_up_until: int | None
    recent_failed_attempts: int = 0


@dataclass(frozen=True)
class LoginAttemptState:
    source_failures: int
    account_failures: int
    global_failures: int
    delay_seconds: int


@dataclass(frozen=True)
class MfaChallenge:
    username: str
    mfa_required: bool = True


@dataclass(frozen=True)
class SetupSecret:
    path: Path
    expires_at: int


class WebAuthService:
    def __init__(
        self,
        database: Database,
        session_hours: int,
        *,
        setup_path: str | Path | None = None,
        setup_ttl_seconds: int = SETUP_SECRET_TTL_SECONDS,
        failure_window_seconds: int = 900,
        source_failure_limit: int = 5,
        account_failure_limit: int = 10,
        global_failure_limit: int = 50,
        throttle_base_seconds: int = 1,
        throttle_max_seconds: int = 16,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.database = database
        self.session_seconds = session_hours * 3_600
        self.setup_ttl_seconds = setup_ttl_seconds
        self.setup_path = Path(setup_path or database.path.parent / SETUP_FILE_NAME)
        self.failure_window_seconds = failure_window_seconds
        self.source_failure_limit = source_failure_limit
        self.account_failure_limit = account_failure_limit
        self.global_failure_limit = global_failure_limit
        self.throttle_base_seconds = throttle_base_seconds
        self.throttle_max_seconds = throttle_max_seconds
        self._sleep = sleep or asyncio.sleep
        self._throttle_lock = asyncio.Lock()
        self.hasher = PasswordHasher()
        self._dummy_hash = self.hasher.hash(secrets.token_urlsafe(24))

    def _remove_setup_file(self) -> None:
        self.setup_path.unlink(missing_ok=True)

    def _write_setup_file(self, token: str) -> None:
        self.setup_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.setup_path.with_name(f".{self.setup_path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(f"{token}\n")
            os.chmod(temporary, 0o600)
            if os.geteuid() == 0:
                owner = (
                    self.database.path if self.database.path.exists() else self.setup_path.parent
                )
                ownership = owner.stat()
                os.chown(temporary, ownership.st_uid, ownership.st_gid)
            os.replace(temporary, self.setup_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    async def _audit(
        self, actor: str, action: str, target: str | None, detail: object = None
    ) -> None:
        await write_audit(
            self.database,
            actor_kind="web",
            actor_ref=actor,
            action=action,
            target=target,
            detail=detail,
        )

    def _delay_for(self, source: int, account: int, global_count: int) -> int:
        overages = [
            count - limit
            for count, limit in (
                (source, self.source_failure_limit),
                (account, self.account_failure_limit),
                (global_count, self.global_failure_limit),
            )
            if count >= limit
        ]
        if not overages:
            return 0
        delay: int = min(
            self.throttle_max_seconds, self.throttle_base_seconds * 2 ** min(8, max(overages))
        )
        return delay

    async def _record_login_attempt(
        self, *, source: str, username: str, successful: bool, now: int
    ) -> LoginAttemptState:
        cutoff = now - self.failure_window_seconds
        async with self.database.transaction() as transaction:
            await transaction.write(
                "INSERT INTO web_login_attempt(source,successful,created_at,username) "
                "VALUES(?,?,?,?)",
                (source, int(successful), now, username),
            )
            rows = await transaction.read(
                "SELECT COALESCE(SUM(source=? AND username=?),0) source_failures,"
                "COALESCE(SUM(username=?),0) account_failures,COUNT(*) global_failures "
                "FROM web_login_attempt WHERE successful=0 AND created_at>?",
                (source, username, username, cutoff),
            )
            counts = rows[0]
            state = LoginAttemptState(
                int(counts["source_failures"]),
                int(counts["account_failures"]),
                int(counts["global_failures"]),
                0,
            )
            if successful:
                return state
            alerts: list[tuple[str, str, int]] = []
            if state.account_failures == self.account_failure_limit:
                alerts.append(
                    (f"account:{username}", f"account:{username}", state.account_failures)
                )
            if state.global_failures == self.global_failure_limit:
                alerts.append(("global", "global", state.global_failures))
            for scope, target, count in alerts:
                detail = {
                    "scope": scope,
                    "username": username,
                    "source": source[:128],
                    "failures": count,
                    "window_seconds": self.failure_window_seconds,
                }
                await write_audit(
                    transaction,
                    actor_kind="system",
                    actor_ref="authentication",
                    action="auth.login_throttled",
                    target=target,
                    detail=detail,
                    created_at=now,
                    outcome="denied",
                )
                conversation_key = f"system:auth-throttle:{scope}"
                title = (
                    f"Login attempts throttled for @{username}"
                    if scope != "global"
                    else "Login attempts throttled globally"
                )
                body = (
                    f"Outpost observed {count} failed sign-in attempts within "
                    f"{self.failure_window_seconds // 60} minutes. Failed attempts are now "
                    "delayed; valid credentials remain usable to prevent hostile account "
                    "lockout. Review Access and the audit log."
                )
                existing = await transaction.read(
                    "SELECT id FROM mail WHERE conversation_key=? LIMIT 1", (conversation_key,)
                )
                if existing:
                    await transaction.write(
                        "UPDATE mail SET subject=?,body=?,created_at=?,state='failed',"
                        "delivered_at=NULL,operator_read_at=NULL,archived_at=NULL,expires_at=? "
                        "WHERE id=?",
                        (title, body, now, now + 30 * 86_400, existing[0]["id"]),
                    )
                else:
                    await transaction.write(
                        "INSERT INTO mail(uid,from_label,to_label,subject,body,created_at,state,"
                        "expires_at,conversation_key,message_kind,mail_direction,"
                        "participant_handle,operator_actor) VALUES(?,?,?,?,?,?,'failed',?,?,"
                        "'system','local','outpost','system:authentication')",
                        (
                            str(uuid.uuid4()),
                            "outpost",
                            "operator",
                            title,
                            body,
                            now,
                            now + 30 * 86_400,
                            conversation_key,
                        ),
                    )
            return LoginAttemptState(
                state.source_failures,
                state.account_failures,
                state.global_failures,
                self._delay_for(
                    state.source_failures, state.account_failures, state.global_failures
                ),
            )

    async def issue_setup_secret(self) -> SetupSecret:
        token = secrets.token_urlsafe(24)
        password_hash = self.hasher.hash(token)
        now = int(time.time())
        expires_at = now + self.setup_ttl_seconds
        self._write_setup_file(token)
        try:
            async with self.database.transaction() as transaction:
                rows = await transaction.read("SELECT 1 FROM web_credential WHERE id=1")
                if rows:
                    await transaction.write(
                        "UPDATE web_credential SET password_hash=?,must_change=1,changed_at=NULL,"
                        "bootstrap_expires_at=?,bootstrap_consumed_at=NULL WHERE id=1",
                        (password_hash, expires_at),
                    )
                else:
                    await transaction.write(
                        "INSERT INTO web_credential("
                        "id,password_hash,must_change,created_at,bootstrap_expires_at"
                        ") VALUES(1,?,1,?,?)",
                        (password_hash, now, expires_at),
                    )
                accounts = await transaction.read("SELECT 1 FROM web_account WHERE id=1")
                if accounts:
                    await transaction.write(
                        "UPDATE web_account SET password_hash=?,must_change=1,enabled=1,"
                        "role='administrator',changed_at=NULL,bootstrap_expires_at=?,"
                        "bootstrap_consumed_at=NULL,totp_secret=NULL,"
                        "totp_pending_secret=NULL,totp_confirmed_at=NULL,"
                        "recovery_code_hashes='[]' "
                        "WHERE id=1",
                        (password_hash, expires_at),
                    )
                else:
                    await transaction.write(
                        "INSERT INTO web_account(id,username,display_name,role,password_hash,"
                        "must_change,enabled,bootstrap_expires_at,created_at,created_by) "
                        "VALUES(1,'operator','Operator','administrator',?,1,1,?,?,'local-setup')",
                        (password_hash, expires_at, now),
                    )
                await transaction.write("DELETE FROM web_session")
        except BaseException:
            self._remove_setup_file()
            raise
        return SetupSecret(self.setup_path, expires_at)

    async def ensure_credential(self) -> SetupSecret | None:
        rows = await self.database.read(
            "SELECT must_change,bootstrap_expires_at,bootstrap_consumed_at "
            "FROM web_account WHERE id=1"
        )
        if rows:
            row = rows[0]
            if not bool(row["must_change"]):
                self._remove_setup_file()
                return None
            expires_at = row["bootstrap_expires_at"]
            if expires_at is None:
                return await self.issue_setup_secret()
            if row["bootstrap_consumed_at"] is not None or int(expires_at) <= int(time.time()):
                self._remove_setup_file()
                return None
            if not self.setup_path.is_file():
                return await self.issue_setup_secret()
            os.chmod(self.setup_path, 0o600)
            return SetupSecret(self.setup_path, int(expires_at))
        return await self.issue_setup_secret()

    async def setup_status(self) -> dict[str, bool | int | None]:
        rows = await self.database.read(
            "SELECT must_change,bootstrap_expires_at,bootstrap_consumed_at "
            "FROM web_account WHERE id=1"
        )
        if not rows or not bool(rows[0]["must_change"]):
            self._remove_setup_file()
            return {"required": False, "available": False, "expires_at": None}
        expires_at = rows[0]["bootstrap_expires_at"]
        available = bool(
            expires_at is not None
            and int(expires_at) > int(time.time())
            and rows[0]["bootstrap_consumed_at"] is None
            and self.setup_path.is_file()
        )
        if not available:
            self._remove_setup_file()
        return {
            "required": True,
            "available": available,
            "expires_at": int(expires_at) if expires_at is not None else None,
        }

    def _password_valid(self, password_hash: str, password: str) -> bool:
        try:
            return self.hasher.verify(password_hash, password)
        except (InvalidHashError, VerificationError):
            return False

    def _totp_valid(self, secret: str, code: str, now: int) -> bool:
        clean = "".join(character for character in code if character.isdigit())
        return len(clean) == 6 and any(
            hmac.compare_digest(_totp(secret, now + offset * 30), clean) for offset in (-1, 0, 1)
        )

    async def _mfa_valid(self, account: Any, code: str, now: int) -> bool:
        secret = account["totp_secret"]
        if secret and self._totp_valid(str(secret), code, now):
            return True
        clean = _normalize_recovery_code(code)
        if not clean:
            return False
        match = _token_hash(f"{account['id']}:{clean}")
        async with self.database.transaction() as transaction:
            rows = await transaction.read(
                "SELECT recovery_code_hashes FROM web_account WHERE id=?", (account["id"],)
            )
            recovery = json.loads(rows[0]["recovery_code_hashes"] or "[]") if rows else []
            if match not in recovery:
                return False
            recovery.remove(match)
            await transaction.write(
                "UPDATE web_account SET recovery_code_hashes=? WHERE id=?",
                (json.dumps(recovery, separators=(",", ":")), account["id"]),
            )
        return True

    async def login(
        self,
        password: str,
        source: str,
        *,
        username: str = "operator",
        code: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[str, WebSession] | MfaChallenge | None:
        try:
            clean_username = _normalize_username(username)
        except ValueError:
            digest = hashlib.sha256(username.strip().lower().encode()).hexdigest()[:16]
            clean_username = f"invalid-{digest}"
        now = int(time.time())
        rows = await self.database.read(
            "SELECT * FROM web_account WHERE username=? COLLATE NOCASE", (clean_username,)
        )
        account = rows[0] if rows else None
        if account is None:
            password_hash = self._dummy_hash
            bootstrap = False
            active = False
        else:
            password_hash = str(account["password_hash"])
            bootstrap = bool(account["must_change"])
            active = bool(
                account["enabled"]
                and (
                    not bootstrap
                    or account["bootstrap_expires_at"] is None
                    or (
                        int(account["bootstrap_expires_at"]) > now
                        and account["bootstrap_consumed_at"] is None
                    )
                )
            )
        password_valid = self._password_valid(password_hash, password)
        valid = bool(password_valid and active)
        if valid and account is not None and account["totp_secret"]:
            if not code:
                return MfaChallenge(str(account["username"]))
            valid = await self._mfa_valid(account, code, now)
        recent_failures = 0
        if valid and account is not None:
            attempts = await self.database.read(
                "SELECT COUNT(*) count FROM web_login_attempt WHERE username=? "
                "AND successful=0 AND id>COALESCE((SELECT MAX(id) FROM web_login_attempt "
                "WHERE username=? AND successful=1),0)",
                (clean_username, clean_username),
            )
            recent_failures = int(attempts[0]["count"])
        attempt = await self._record_login_attempt(
            source=source,
            username=clean_username,
            successful=valid,
            now=now,
        )
        if not valid or account is None:
            if attempt.delay_seconds:
                async with self._throttle_lock:
                    await self._sleep(attempt.delay_seconds)
            return None
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        placeholder_hash = self.hasher.hash(_initial_password()) if bootstrap else None
        async with self.database.transaction() as transaction:
            if bootstrap:
                current = await transaction.read(
                    "SELECT bootstrap_expires_at,bootstrap_consumed_at FROM web_account WHERE id=?",
                    (account["id"],),
                )
                if not current or current[0]["bootstrap_consumed_at"] is not None:
                    return None
                expiry = current[0]["bootstrap_expires_at"]
                if expiry is not None and int(expiry) <= now:
                    return None
                await transaction.write(
                    "DELETE FROM web_session WHERE account_id=?", (account["id"],)
                )
                await transaction.write(
                    "UPDATE web_account SET password_hash=?,bootstrap_consumed_at=? WHERE id=?",
                    (placeholder_hash, now, account["id"]),
                )
                if int(account["id"]) == 1:
                    await transaction.write(
                        "UPDATE web_credential SET password_hash=?,bootstrap_consumed_at=? "
                        "WHERE id=1",
                        (placeholder_hash, now),
                    )
            await transaction.write(
                "INSERT INTO web_session(token_hash,csrf_token,created_at,expires_at,last_seen_at,"
                "account_id,source,user_agent,step_up_until) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    _token_hash(token),
                    csrf,
                    now,
                    now + self.session_seconds,
                    now,
                    account["id"],
                    source[:128],
                    (user_agent or "")[:300],
                    now + STEP_UP_SECONDS,
                ),
            )
            await transaction.write(
                "UPDATE web_account SET last_login_at=? WHERE id=?", (now, account["id"])
            )
        if bootstrap:
            self._remove_setup_file()
        return token, WebSession(
            csrf,
            bootstrap,
            int(account["id"]),
            str(account["username"]),
            str(account["display_name"]),
            str(account["role"]),
            bool(account["totp_secret"]),
            now + STEP_UP_SECONDS,
            recent_failures,
        )

    async def session(self, token: str | None) -> WebSession | None:
        if not token:
            return None
        now = int(time.time())
        rows = await self.database.read(
            "SELECT s.csrf_token,s.step_up_until,s.last_seen_at,a.id account_id,a.username,"
            "a.display_name,a.role,a.must_change,a.totp_secret FROM web_session s "
            "JOIN web_account a ON a.id=s.account_id "
            "WHERE s.token_hash=? AND s.expires_at>? AND a.enabled=1",
            (_token_hash(token), now),
        )
        if not rows:
            return None
        row = rows[0]
        if int(row["last_seen_at"]) < now - 60:
            await self.database.write(
                "UPDATE web_session SET last_seen_at=? WHERE token_hash=?",
                (now, _token_hash(token)),
            )
        return WebSession(
            str(row["csrf_token"]),
            bool(row["must_change"]),
            int(row["account_id"]),
            str(row["username"]),
            str(row["display_name"]),
            str(row["role"]),
            bool(row["totp_secret"]),
            int(row["step_up_until"]) if row["step_up_until"] is not None else None,
        )

    async def logout(self, token: str | None) -> None:
        if token:
            await self.database.write(
                "DELETE FROM web_session WHERE token_hash=?", (_token_hash(token),)
            )

    async def change_password(self, token: str, current: str, replacement: str) -> bool:
        if len(replacement) < 12:
            return False
        rows = await self.database.read(
            "SELECT a.id,a.username,a.password_hash,a.must_change FROM web_account a "
            "JOIN web_session s ON s.account_id=a.id "
            "WHERE s.token_hash=? AND s.expires_at>? AND a.enabled=1",
            (_token_hash(token), int(time.time())),
        )
        if not rows:
            return False
        account = rows[0]
        valid = bool(account["must_change"]) or self._password_valid(
            str(account["password_hash"]), current
        )
        if not valid:
            return False
        now = int(time.time())
        password_hash = self.hasher.hash(replacement)
        async with self.database.transaction() as transaction:
            await transaction.write(
                "UPDATE web_account SET password_hash=?,must_change=0,changed_at=?,"
                "bootstrap_expires_at=NULL,bootstrap_consumed_at=NULL WHERE id=?",
                (password_hash, now, account["id"]),
            )
            if int(account["id"]) == 1:
                await transaction.write(
                    "UPDATE web_credential SET password_hash=?,must_change=0,changed_at=?,"
                    "bootstrap_expires_at=NULL,bootstrap_consumed_at=NULL WHERE id=1",
                    (password_hash, now),
                )
            await transaction.write("DELETE FROM web_session WHERE account_id=?", (account["id"],))
        self._remove_setup_file()
        await self._audit(
            str(account["username"]), "auth.password_change", f"account:{account['id']}"
        )
        return True

    async def login_security_status(self) -> dict[str, object]:
        now = int(time.time())
        rows = await self.database.read(
            "SELECT l.username,l.source,l.created_at,a.id account_id "
            "FROM web_login_attempt l LEFT JOIN web_account a "
            "ON a.username=l.username COLLATE NOCASE "
            "WHERE l.successful=0 AND l.created_at>? "
            "ORDER BY l.created_at,l.id",
            (now - self.failure_window_seconds,),
        )
        grouped: dict[str, dict[str, object]] = {}
        global_times: list[int] = []
        for row in rows:
            username = str(row["username"])
            entry = grouped.setdefault(
                username,
                {
                    "username": username,
                    "known_account": row["account_id"] is not None,
                    "times": [],
                    "sources": set(),
                },
            )
            timestamp = int(row["created_at"])
            times = entry["times"]
            sources = entry["sources"]
            assert isinstance(times, list)
            assert isinstance(sources, set)
            times.append(timestamp)
            sources.add(str(row["source"]))
            global_times.append(timestamp)

        identities: list[dict[str, object]] = []
        for entry in grouped.values():
            times = entry.pop("times")
            sources = entry.pop("sources")
            assert isinstance(times, list)
            assert isinstance(sources, set)
            throttled = len(times) >= self.account_failure_limit
            identities.append(
                {
                    **entry,
                    "failures": len(times),
                    "source_count": len(sources),
                    "last_failure_at": times[-1],
                    "throttled": throttled,
                    "throttled_until": (
                        times[-self.account_failure_limit] + self.failure_window_seconds
                        if throttled
                        else None
                    ),
                }
            )

        def identity_order(item: dict[str, object]) -> tuple[int, str]:
            failures = item["failures"]
            assert isinstance(failures, int)
            return -failures, str(item["username"])

        identities.sort(key=identity_order)
        globally_throttled = len(global_times) >= self.global_failure_limit
        return {
            "window_seconds": self.failure_window_seconds,
            "total_failures": len(global_times),
            "global_failure_limit": self.global_failure_limit,
            "global_throttled": globally_throttled,
            "global_throttled_until": (
                global_times[-self.global_failure_limit] + self.failure_window_seconds
                if globally_throttled
                else None
            ),
            "identities": identities,
        }

    async def accounts(
        self, login_security: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        security = login_security or await self.login_security_status()
        raw_identities = security.get("identities", [])
        if not isinstance(raw_identities, list):
            raw_identities = []
        identity_security = {
            str(item["username"]).casefold(): item
            for item in raw_identities
            if isinstance(item, dict)
        }
        rows = await self.database.read(
            "SELECT a.id,a.username,a.display_name,a.role,a.must_change,a.enabled,"
            "a.totp_confirmed_at,a.created_at,a.changed_at,a.last_login_at,a.created_by,"
            "a.radio_linked_at,a.radio_linked_by,m.id radio_id,m.mesh_id radio_mesh_id,"
            "m.handle radio_handle,m.long_name radio_long_name,m.short_name radio_short_name,"
            "m.trust radio_trust,m.pki_state radio_pki_state "
            "FROM web_account a LEFT JOIN member m ON m.id=a.radio_member_id "
            "ORDER BY a.username"
        )
        items: list[dict[str, object]] = []
        for row in rows:
            attempt_state = identity_security.get(str(row["username"]).casefold(), {})
            items.append(
                {
                    "id": row["id"],
                    "username": row["username"],
                    "display_name": row["display_name"],
                    "role": row["role"],
                    "must_change": bool(row["must_change"]),
                    "enabled": bool(row["enabled"]),
                    "mfa_enabled": row["totp_confirmed_at"] is not None,
                    "created_at": row["created_at"],
                    "changed_at": row["changed_at"],
                    "last_login_at": row["last_login_at"],
                    "created_by": row["created_by"],
                    "failed_attempts_recent": int(attempt_state.get("failures", 0)),
                    "failed_attempt_sources": int(attempt_state.get("source_count", 0)),
                    "last_failed_attempt_at": attempt_state.get("last_failure_at"),
                    "login_throttled": bool(attempt_state.get("throttled", False)),
                    "login_throttled_until": attempt_state.get("throttled_until"),
                    "operator_radio": (
                        {
                            "id": row["radio_id"],
                            "mesh_id": row["radio_mesh_id"],
                            "handle": row["radio_handle"],
                            "long_name": row["radio_long_name"],
                            "short_name": row["radio_short_name"],
                            "trust": row["radio_trust"],
                            "pki_state": row["radio_pki_state"],
                            "linked_at": row["radio_linked_at"],
                            "linked_by": row["radio_linked_by"],
                        }
                        if row["radio_id"] is not None
                        else None
                    ),
                }
            )
        return items

    async def operator_radios(self) -> list[dict[str, object]]:
        """Return mesh operators plus any linked radio whose trust later changed."""
        rows = await self.database.read(
            "SELECT m.id,m.mesh_id,m.handle,m.long_name,m.short_name,m.trust,m.pki_state,"
            "m.last_seen,a.id account_id,a.username account_username,"
            "a.display_name account_display_name,a.role account_role,a.enabled account_enabled "
            "FROM member m LEFT JOIN web_account a ON a.radio_member_id=m.id "
            "WHERE m.trust='operator' OR a.id IS NOT NULL "
            "ORDER BY (m.trust='operator') DESC,m.last_seen DESC,m.mesh_id"
        )
        return [
            {
                **dict(row),
                "account_enabled": (
                    bool(row["account_enabled"]) if row["account_enabled"] is not None else None
                ),
            }
            for row in rows
        ]

    async def link_operator_radio(
        self, account_id: int, member_id: int | None, actor: str
    ) -> dict[str, object]:
        async with self.database.transaction() as transaction:
            accounts = await transaction.read(
                "SELECT username,role,radio_member_id FROM web_account WHERE id=?",
                (account_id,),
            )
            if not accounts:
                raise ValueError("Account not found.")
            account = accounts[0]
            if account["role"] not in {"administrator", "operator"}:
                raise ValueError("Only Administrator or Operator accounts can own a radio.")
            old_member_id = account["radio_member_id"]
            old_mesh_id = None
            if old_member_id is not None:
                old_rows = await transaction.read(
                    "SELECT mesh_id FROM member WHERE id=?", (old_member_id,)
                )
                old_mesh_id = old_rows[0]["mesh_id"] if old_rows else None
            mesh_id = None
            if member_id is not None:
                members = await transaction.read(
                    "SELECT mesh_id,trust FROM member WHERE id=?", (member_id,)
                )
                if not members:
                    raise ValueError("Radio identity not found.")
                if members[0]["trust"] != "operator":
                    raise ValueError("Promote this radio to mesh Operator before linking it.")
                mesh_id = str(members[0]["mesh_id"])
                linked = await transaction.read(
                    "SELECT username FROM web_account WHERE radio_member_id=? AND id<>?",
                    (member_id, account_id),
                )
                if linked:
                    raise ValueError(f"That radio is already linked to @{linked[0]['username']}.")
            await transaction.write(
                "UPDATE web_account SET radio_member_id=?,radio_linked_at=?,radio_linked_by=? "
                "WHERE id=?",
                (
                    member_id,
                    int(time.time()) if member_id is not None else None,
                    actor if member_id is not None else None,
                    account_id,
                ),
            )
        await self._audit(
            actor,
            "auth.operator_radio_link" if member_id is not None else "auth.operator_radio_unlink",
            f"account:{account_id}",
            {
                "account": str(account["username"]),
                "radio_before": old_mesh_id,
                "radio_after": mesh_id,
            },
        )
        return next(item for item in await self.accounts() if item["id"] == account_id)

    async def create_account(
        self, username: str, display_name: str, role: str, password: str, actor: str
    ) -> dict[str, object]:
        clean = _normalize_username(username)
        label = display_name.strip()
        if not 1 <= len(label) <= 80:
            raise ValueError("Display name must be 1-80 characters.")
        if role not in ROLES:
            raise ValueError("Unknown account role.")
        if len(password) < 12:
            raise ValueError("Initial password must contain at least 12 characters.")
        now = int(time.time())
        try:
            account_id = await self.database.write(
                "INSERT INTO web_account(username,display_name,role,password_hash,must_change,"
                "enabled,created_at,created_by) VALUES(?,?,?,?,1,1,?,?)",
                (clean, label, role, self.hasher.hash(password), now, actor),
            )
        except Exception as error:
            if "UNIQUE" in str(error):
                raise ValueError("That username is already in use.") from error
            raise
        await self._audit(actor, "auth.account_create", f"account:{account_id}", {"role": role})
        return next(item for item in await self.accounts() if item["id"] == account_id)

    async def update_account(
        self,
        account_id: int,
        *,
        display_name: str | None,
        role: str | None,
        enabled: bool | None,
        actor: str,
    ) -> dict[str, object]:
        rows = await self.database.read("SELECT * FROM web_account WHERE id=?", (account_id,))
        if not rows:
            raise ValueError("Account not found.")
        current = rows[0]
        new_role = role or str(current["role"])
        new_enabled = bool(current["enabled"]) if enabled is None else enabled
        label = str(current["display_name"]) if display_name is None else display_name.strip()
        if new_role not in ROLES or not 1 <= len(label) <= 80:
            raise ValueError("Account role or display name is invalid.")
        removing_admin = current["role"] == "administrator" and (
            new_role != "administrator" or not new_enabled
        )
        if removing_admin:
            count = await self.database.read(
                "SELECT COUNT(*) count FROM web_account "
                "WHERE role='administrator' AND enabled=1 AND id<>?",
                (account_id,),
            )
            if int(count[0]["count"]) == 0:
                raise ValueError("At least one enabled administrator is required.")
        radio_unlinked = new_role == "viewer" and current["radio_member_id"] is not None
        await self.database.write(
            "UPDATE web_account SET display_name=?,role=?,enabled=?,"
            "radio_member_id=CASE WHEN ?='viewer' THEN NULL ELSE radio_member_id END,"
            "radio_linked_at=CASE WHEN ?='viewer' THEN NULL ELSE radio_linked_at END,"
            "radio_linked_by=CASE WHEN ?='viewer' THEN NULL ELSE radio_linked_by END WHERE id=?",
            (label, new_role, int(new_enabled), new_role, new_role, new_role, account_id),
        )
        if not new_enabled:
            await self.database.write("DELETE FROM web_session WHERE account_id=?", (account_id,))
        await self._audit(
            actor,
            "auth.account_update",
            f"account:{account_id}",
            {"role": new_role, "enabled": new_enabled, "radio_unlinked": radio_unlinked},
        )
        return next(item for item in await self.accounts() if item["id"] == account_id)

    async def reset_password(self, account_id: int, password: str, actor: str) -> None:
        if len(password) < 12:
            raise ValueError("Temporary password must contain at least 12 characters.")
        rows = await self.database.read(
            "SELECT username FROM web_account WHERE id=?", (account_id,)
        )
        if not rows:
            raise ValueError("Account not found.")
        await self.database.write(
            "UPDATE web_account SET password_hash=?,must_change=1,changed_at=unixepoch(),"
            "bootstrap_expires_at=NULL,bootstrap_consumed_at=NULL WHERE id=?",
            (self.hasher.hash(password), account_id),
        )
        await self.database.write("DELETE FROM web_session WHERE account_id=?", (account_id,))
        await self._audit(actor, "auth.password_reset", f"account:{account_id}")

    async def sessions(self, account_id: int, current_token: str) -> list[dict[str, object]]:
        now = int(time.time())
        current_hash = _token_hash(current_token)
        rows = await self.database.read(
            "SELECT token_hash,source,user_agent,created_at,expires_at,last_seen_at,step_up_until "
            "FROM web_session WHERE account_id=? AND expires_at>? ORDER BY last_seen_at DESC",
            (account_id, now),
        )
        return [
            {
                "id": str(row["token_hash"])[:16],
                "source": row["source"] or "unknown",
                "user_agent": row["user_agent"] or "unknown client",
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "last_activity_at": row["last_seen_at"],
                "step_up_until": row["step_up_until"],
                "current": hmac.compare_digest(str(row["token_hash"]), current_hash),
            }
            for row in rows
        ]

    async def revoke_session(self, account_id: int, session_id: str, actor: str) -> bool:
        rows = await self.database.read(
            "SELECT token_hash FROM web_session WHERE account_id=?", (account_id,)
        )
        matches = [
            str(row["token_hash"]) for row in rows if str(row["token_hash"]).startswith(session_id)
        ]
        if len(matches) != 1:
            return False
        await self.database.write("DELETE FROM web_session WHERE token_hash=?", (matches[0],))
        await self._audit(actor, "auth.session_revoke", f"session:{session_id}")
        return True

    async def revoke_all_sessions(self, account_id: int, actor: str) -> int:
        rows = await self.database.read(
            "SELECT COUNT(*) count FROM web_session WHERE account_id=?", (account_id,)
        )
        count = int(rows[0]["count"])
        await self.database.write("DELETE FROM web_session WHERE account_id=?", (account_id,))
        await self._audit(actor, "auth.sessions_revoke", f"account:{account_id}", {"count": count})
        return count

    async def begin_mfa(self, account_id: int, username: str) -> dict[str, str]:
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        await self.database.write(
            "UPDATE web_account SET totp_pending_secret=? WHERE id=?", (secret, account_id)
        )
        label = quote(f"Outpost:{username}")
        return {
            "secret": secret,
            "otpauth_uri": f"otpauth://totp/{label}?secret={secret}&issuer=Outpost&digits=6&period=30",
        }

    async def confirm_mfa(self, account_id: int, username: str, code: str) -> list[str]:
        rows = await self.database.read(
            "SELECT totp_pending_secret FROM web_account WHERE id=?", (account_id,)
        )
        secret = str(rows[0]["totp_pending_secret"] or "") if rows else ""
        if not secret or not self._totp_valid(secret, code, int(time.time())):
            raise ValueError("The authenticator code is invalid or expired.")
        codes = [
            "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(4))
            + "-"
            + "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(4))
            + "-"
            + "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(4))
            for _ in range(8)
        ]
        hashes = [_token_hash(f"{account_id}:{_normalize_recovery_code(code)}") for code in codes]
        await self.database.write(
            "UPDATE web_account SET totp_secret=?,totp_pending_secret=NULL,"
            "totp_confirmed_at=unixepoch(),recovery_code_hashes=? WHERE id=?",
            (secret, json.dumps(hashes, separators=(",", ":")), account_id),
        )
        await self._audit(username, "auth.mfa_enable", f"account:{account_id}")
        return codes

    async def disable_mfa(self, account_id: int, username: str) -> None:
        await self.database.write(
            "UPDATE web_account SET totp_secret=NULL,totp_pending_secret=NULL,"
            "totp_confirmed_at=NULL,recovery_code_hashes='[]' WHERE id=?",
            (account_id,),
        )
        await self._audit(username, "auth.mfa_disable", f"account:{account_id}")

    async def step_up(
        self, token: str, password: str, code: str | None, *, source: str = "step-up"
    ) -> WebSession | None:
        session = await self.session(token)
        if session is None or session.must_change:
            return None
        now = int(time.time())
        failures = await self.database.read(
            "SELECT COUNT(*) count FROM web_login_attempt WHERE source=? AND username=? "
            "AND successful=0 AND created_at>?",
            (source, session.username, now - 900),
        )
        if int(failures[0]["count"]) >= 5:
            return None
        rows = await self.database.read(
            "SELECT * FROM web_account WHERE id=?", (session.account_id,)
        )
        valid = bool(rows and self._password_valid(str(rows[0]["password_hash"]), password))
        if valid and rows[0]["totp_secret"]:
            valid = bool(code and await self._mfa_valid(rows[0], code, now))
        await self.database.write(
            "INSERT INTO web_login_attempt(source,successful,created_at,username) VALUES(?,?,?,?)",
            (source, int(valid), now, session.username),
        )
        if not valid:
            return None
        until = now + STEP_UP_SECONDS
        await self.database.write(
            "UPDATE web_session SET step_up_until=? WHERE token_hash=?",
            (until, _token_hash(token)),
        )
        await self._audit(session.username, "auth.step_up", f"account:{session.account_id}")
        return WebSession(**{**session.__dict__, "step_up_until": until})
