from __future__ import annotations

import hashlib
import secrets
import string
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from outpost.store import Database


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _initial_password() -> str:
    alphabet = string.ascii_letters + string.digits + "-_."
    return "".join(secrets.choice(alphabet) for _ in range(24))


@dataclass(frozen=True)
class WebSession:
    csrf_token: str
    must_change: bool


class WebAuthService:
    def __init__(self, database: Database, session_hours: int) -> None:
        self.database = database
        self.session_seconds = session_hours * 3_600
        self.hasher = PasswordHasher()

    async def ensure_credential(self) -> str | None:
        rows = await self.database.read("SELECT 1 FROM web_credential WHERE id=1")
        if rows:
            return None
        password = _initial_password()
        await self.database.write(
            "INSERT INTO web_credential(id,password_hash,must_change,created_at) VALUES(1,?,1,?)",
            (self.hasher.hash(password), int(time.time())),
        )
        return password

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
            "SELECT password_hash,must_change FROM web_credential WHERE id=1"
        )
        valid = False
        if rows:
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
        await self.database.write(
            """
            INSERT INTO web_session(token_hash,csrf_token,created_at,expires_at,last_seen_at)
            VALUES(?,?,?,?,?)
            """,
            (_token_hash(token), csrf, now, now + self.session_seconds, now),
        )
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
        rows = await self.database.read("SELECT password_hash FROM web_credential WHERE id=1")
        try:
            valid = bool(rows) and self.hasher.verify(rows[0]["password_hash"], current)
        except VerifyMismatchError:
            valid = False
        if not valid:
            return False
        now = int(time.time())
        await self.database.write(
            "UPDATE web_credential SET password_hash=?,must_change=0,changed_at=? WHERE id=1",
            (self.hasher.hash(replacement), now),
        )
        await self.database.write(
            "DELETE FROM web_session WHERE token_hash<>?", (_token_hash(token),)
        )
        return True
