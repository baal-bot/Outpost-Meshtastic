from __future__ import annotations

import hashlib
import os
import secrets
import string
import time
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from outpost.store import Database


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _initial_password() -> str:
    alphabet = string.ascii_letters + string.digits + "-_."
    return "".join(secrets.choice(alphabet) for _ in range(24))


SETUP_SECRET_TTL_SECONDS = 3_600
SETUP_FILE_NAME = "setup-token"


@dataclass(frozen=True)
class WebSession:
    csrf_token: str
    must_change: bool


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
    ) -> None:
        self.database = database
        self.session_seconds = session_hours * 3_600
        self.setup_ttl_seconds = setup_ttl_seconds
        self.setup_path = Path(setup_path or database.path.parent / SETUP_FILE_NAME)
        self.hasher = PasswordHasher()

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

    async def issue_setup_secret(self) -> SetupSecret:
        token = secrets.token_urlsafe(24)
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
                        (self.hasher.hash(token), expires_at),
                    )
                else:
                    await transaction.write(
                        "INSERT INTO web_credential("
                        "id,password_hash,must_change,created_at,bootstrap_expires_at"
                        ") VALUES(1,?,1,?,?)",
                        (self.hasher.hash(token), now, expires_at),
                    )
                await transaction.write("DELETE FROM web_session")
        except BaseException:
            self._remove_setup_file()
            raise
        return SetupSecret(self.setup_path, expires_at)

    async def ensure_credential(self) -> SetupSecret | None:
        rows = await self.database.read(
            "SELECT must_change,bootstrap_expires_at,bootstrap_consumed_at "
            "FROM web_credential WHERE id=1"
        )
        if rows:
            row = rows[0]
            if not bool(row["must_change"]):
                self._remove_setup_file()
                return None
            expires_at = row["bootstrap_expires_at"]
            consumed_at = row["bootstrap_consumed_at"]
            if expires_at is None:
                return await self.issue_setup_secret()
            if consumed_at is not None or int(expires_at) <= int(time.time()):
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
            "FROM web_credential WHERE id=1"
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

    async def login(self, password: str, source: str) -> tuple[str, WebSession] | None:
        now = int(time.time())
        failures = await self.database.read(
            """
            SELECT COUNT(*) AS count FROM web_login_attempt
            WHERE source=? AND successful=0 AND created_at>?
            """,
            (source, now - 900),
        )
        if int(failures[0]["count"]) >= 5:
            return None
        rows = await self.database.read(
            "SELECT password_hash,must_change,bootstrap_expires_at,bootstrap_consumed_at "
            "FROM web_credential WHERE id=1"
        )
        valid = False
        bootstrap = bool(rows and rows[0]["must_change"])
        if rows:
            active = not bootstrap or bool(
                rows[0]["bootstrap_expires_at"] is not None
                and int(rows[0]["bootstrap_expires_at"]) > now
                and rows[0]["bootstrap_consumed_at"] is None
            )
            if active:
                try:
                    valid = self.hasher.verify(rows[0]["password_hash"], password)
                except VerifyMismatchError:
                    pass
        await self.database.write(
            "INSERT INTO web_login_attempt(source,successful,created_at) VALUES(?,?,?)",
            (source, int(valid), now),
        )
        if not valid:
            return None
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        async with self.database.transaction() as transaction:
            if bootstrap:
                credential = await transaction.read(
                    "SELECT bootstrap_expires_at,bootstrap_consumed_at "
                    "FROM web_credential WHERE id=1"
                )
                if (
                    not credential
                    or credential[0]["bootstrap_consumed_at"] is not None
                    or credential[0]["bootstrap_expires_at"] is None
                    or int(credential[0]["bootstrap_expires_at"]) <= now
                ):
                    return None
                await transaction.write("DELETE FROM web_session")
                await transaction.write(
                    "UPDATE web_credential SET password_hash=?,bootstrap_consumed_at=? WHERE id=1",
                    (self.hasher.hash(_initial_password()), now),
                )
            await transaction.write(
                """
                INSERT INTO web_session(token_hash,csrf_token,created_at,expires_at,last_seen_at)
                VALUES(?,?,?,?,?)
                """,
                (_token_hash(token), csrf, now, now + self.session_seconds, now),
            )
        if bootstrap:
            self._remove_setup_file()
        return token, WebSession(csrf, bool(rows[0]["must_change"]))

    async def session(self, token: str | None) -> WebSession | None:
        if not token:
            return None
        now = int(time.time())
        rows = await self.database.read(
            """
            SELECT s.csrf_token,c.must_change FROM web_session s CROSS JOIN web_credential c
            WHERE s.token_hash=? AND s.expires_at>?
            """,
            (_token_hash(token), now),
        )
        return WebSession(rows[0]["csrf_token"], bool(rows[0]["must_change"])) if rows else None

    async def logout(self, token: str | None) -> None:
        if token:
            await self.database.write(
                "DELETE FROM web_session WHERE token_hash=?", (_token_hash(token),)
            )

    async def change_password(self, token: str, current: str, replacement: str) -> bool:
        if len(replacement) < 12:
            return False
        rows = await self.database.read(
            "SELECT password_hash,must_change FROM web_credential WHERE id=1"
        )
        if not rows:
            return False
        if bool(rows[0]["must_change"]):
            sessions = await self.database.read(
                "SELECT 1 FROM web_session WHERE token_hash=? AND expires_at>?",
                (_token_hash(token), int(time.time())),
            )
            valid = bool(sessions)
        else:
            try:
                valid = self.hasher.verify(rows[0]["password_hash"], current)
            except VerifyMismatchError:
                valid = False
        if not valid:
            return False
        now = int(time.time())
        async with self.database.transaction() as transaction:
            await transaction.write(
                "UPDATE web_credential SET password_hash=?,must_change=0,changed_at=?,"
                "bootstrap_expires_at=NULL,bootstrap_consumed_at=NULL WHERE id=1",
                (self.hasher.hash(replacement), now),
            )
            await transaction.write("DELETE FROM web_session")
        self._remove_setup_file()
        return True
