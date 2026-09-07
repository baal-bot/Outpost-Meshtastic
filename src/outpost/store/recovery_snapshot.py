"""Validated in-memory SQLite backup shared by the writer and recovery command."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from outpost.recovery_format import MAX_DATABASE, RecoveryError


def migrations() -> list[Path]:
    return sorted((Path(__file__).parent / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql"))


def schema_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in migrations():
        digest.update(path.name.encode() + b"\x00" + path.read_bytes() + b"\x00")
    return digest.hexdigest()


def _schema(connection: sqlite3.Connection) -> list[tuple[str, str, str, str | None]]:
    return connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()


@lru_cache(maxsize=1)
def expected_schema() -> list[tuple[str, str, str, str | None]]:
    memory = sqlite3.connect(":memory:")
    try:
        memory.execute("PRAGMA temp_store=MEMORY")
        for path in migrations():
            memory.executescript(path.read_text())
        return _schema(memory)
    finally:
        memory.close()


def validate(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA trusted_schema=OFF")
    if _schema(connection) != expected_schema():
        raise RecoveryError("Recovery database schema objects differ from the compatible runtime")
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise RecoveryError("Recovery database failed integrity verification")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RecoveryError("Recovery database has inconsistent references")
    versions = {row[0] for row in connection.execute("SELECT version FROM schema_version")}
    if versions != {int(path.name[:4]) for path in migrations()}:
        raise RecoveryError("Recovery database requires its original compatible schema")
    if connection.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
        raise RecoveryError("Recovery database has incompatible storage settings")


def snapshot(connection: sqlite3.Connection) -> bytes:
    pages = connection.execute("PRAGMA page_count").fetchone()[0]
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    if pages * page_size > MAX_DATABASE:
        raise RecoveryError("Database exceeds the 256 MiB memory-only recovery limit")
    deadline = time.monotonic() + 60

    def progress(_status: int, _remaining: int, total: int) -> None:
        if total * page_size > MAX_DATABASE or time.monotonic() > deadline:
            raise RecoveryError("Recovery snapshot exceeded its size or time bound")

    memory = sqlite3.connect(":memory:")
    try:
        memory.execute("PRAGMA temp_store=MEMORY")
        connection.backup(memory, pages=128, progress=progress, sleep=0.01)
        validate(memory)
        # A WAL backup serialized directly retains WAL header bytes and cannot
        # be deserialized without sidecars. Memory-only VACUUM normalizes them
        # and removes free-list content; it does not alter the source database.
        memory.execute("VACUUM")
        result = memory.serialize()
        if len(result) > MAX_DATABASE:
            raise RecoveryError("Recovery snapshot exceeds the supported bound")
        return result
    except sqlite3.Error as error:
        raise RecoveryError("Recovery database could not be verified") from error
    finally:
        memory.close()


def open_image(value: bytes) -> sqlite3.Connection:
    if not 100 <= len(value) <= MAX_DATABASE or not value.startswith(b"SQLite format 3\x00"):
        raise RecoveryError("Invalid recovery database image")
    memory = sqlite3.connect(":memory:")
    try:
        memory.execute("PRAGMA temp_store=MEMORY")
        memory.deserialize(value)
        validate(memory)
        return memory
    except (sqlite3.Error, RecoveryError) as error:
        memory.close()
        raise RecoveryError("Recovery database could not be verified") from error
