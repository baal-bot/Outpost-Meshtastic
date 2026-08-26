from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")
MIN_SQLITE = (3, 43, 0)


class StoreError(RuntimeError):
    pass


class Transaction:
    """Operations executed on the serialized writer inside one SQLite transaction."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def write(self, sql: str, params: Sequence[Any] = ()) -> int:
        return await self._database._writer_write(sql, params)

    async def read(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return await self._database._writer_read(sql, params)


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._writer: sqlite3.Connection | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="outpost-db-writer")
        self._transaction_lock = asyncio.Lock()

    @staticmethod
    def _configure(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-16000")
        connection.execute("PRAGMA mmap_size=67108864")

    def _open_sync(self) -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise StoreError("REQ-DATA-002b requires SQLite >= 3.43")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        if fresh:
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            connection.execute("PRAGMA journal_mode=WAL")
        self._configure(connection)
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        auto_vacuum = connection.execute("PRAGMA auto_vacuum").fetchone()[0]
        if str(journal).lower() != "wal" or auto_vacuum != 2:
            connection.close()
            raise StoreError(
                "REQ-DATA-002a: database must use journal_mode=wal and auto_vacuum=incremental"
            )
        self._writer = connection
        self._migrate_sync()

    def _migrate_sync(self) -> None:
        assert self._writer is not None
        migrations = sorted(
            (Path(__file__).parent / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql")
        )
        known = {int(path.name[:4]) for path in migrations}
        try:
            current = {row[0] for row in self._writer.execute("SELECT version FROM schema_version")}
        except sqlite3.OperationalError:
            current = set()
        if current and max(current) > max(known):
            raise StoreError("database schema is newer than this Outpost binary")
        for path in migrations:
            version = int(path.name[:4])
            if version in current:
                continue
            script = path.read_text()
            # executescript provides its own transaction boundary for the entire migration.
            version_sql = (
                "\nINSERT INTO schema_version(version, applied_at) "
                f"VALUES ({version}, unixepoch());\nCOMMIT;"
            )
            self._writer.executescript("BEGIN IMMEDIATE;\n" + script + version_sql)

    async def open(self) -> None:
        await asyncio.get_running_loop().run_in_executor(self._executor, self._open_sync)

    async def close(self) -> None:
        if self._writer is not None:
            connection, self._writer = self._writer, None
            await asyncio.get_running_loop().run_in_executor(self._executor, connection.close)
        self._executor.shutdown(wait=True)

    async def _writer_call(self, operation: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._executor, operation)

    async def _writer_write(self, sql: str, params: Sequence[Any] = ()) -> int:
        def operation() -> int:
            if self._writer is None:
                raise StoreError("database is not open")
            cursor = self._writer.execute(sql, params)
            if cursor.lastrowid is None:
                raise StoreError("write did not return a row id")
            return cursor.lastrowid

        return await self._writer_call(operation)

    async def _writer_read(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        def operation() -> list[sqlite3.Row]:
            if self._writer is None:
                raise StoreError("database is not open")
            return list(self._writer.execute(sql, params))

        return await self._writer_call(operation)

    async def write(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self._transaction_lock:
            try:
                row_id = await self._writer_write(sql, params)
                await self._writer_call(self._commit)
                return row_id
            except BaseException:
                await asyncio.shield(self._writer_call(self._rollback))
                raise

    def _commit(self) -> None:
        if self._writer is None:
            raise StoreError("database is not open")
        self._writer.commit()

    def _rollback(self) -> None:
        if self._writer is not None:
            self._writer.rollback()

    def _begin(self) -> None:
        if self._writer is None:
            raise StoreError("database is not open")
        self._writer.execute("BEGIN IMMEDIATE")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        """Hold the sole writer and commit all enclosed operations as one unit."""
        async with self._transaction_lock:
            await self._writer_call(self._begin)
            try:
                yield Transaction(self)
            except BaseException:
                await asyncio.shield(self._writer_call(self._rollback))
                raise
            else:
                try:
                    await asyncio.shield(self._writer_call(self._commit))
                except BaseException:
                    await asyncio.shield(self._writer_call(self._rollback))
                    raise

    async def read(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        def operation() -> list[sqlite3.Row]:
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            self._configure(connection)
            try:
                return list(connection.execute(sql, params))
            finally:
                connection.close()

        return await asyncio.to_thread(operation)

    async def backup(self, destination: str | Path) -> None:
        target_path = Path(destination)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        def operation() -> None:
            if self._writer is None:
                raise StoreError("database is not open")
            target = sqlite3.connect(target_path)
            try:
                self._writer.backup(target)
                result = target.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise StoreError(f"backup integrity_check failed: {result}")
            finally:
                target.close()

        async with self._transaction_lock:
            await self._writer_call(operation)

    async def validate_backup(self, source: str | Path) -> dict[str, int | str]:
        source_path = Path(source)

        def operation() -> dict[str, int | str]:
            candidate = sqlite3.connect(source_path)
            try:
                integrity = str(candidate.execute("PRAGMA integrity_check").fetchone()[0])
                if integrity != "ok":
                    raise StoreError(f"backup integrity check failed: {integrity}")
                source_version = int(
                    candidate.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                )
                if self._writer is None:
                    raise StoreError("database is not open")
                current_version = int(
                    self._writer.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                )
                if source_version != current_version:
                    raise StoreError(
                        f"backup schema {source_version} does not match "
                        f"current schema {current_version}"
                    )
                return {
                    "integrity": integrity,
                    "schema_version": source_version,
                    "size_bytes": source_path.stat().st_size,
                }
            finally:
                candidate.close()

        async with self._transaction_lock:
            return await self._writer_call(operation)

    async def restore_from(self, source: str | Path) -> None:
        source_path = Path(source)

        def operation() -> None:
            if self._writer is None:
                raise StoreError("database is not open")
            candidate = sqlite3.connect(source_path)
            try:
                if candidate.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise StoreError("refusing to restore a corrupt backup")
                candidate.backup(self._writer)
                self._writer.commit()
                if self._writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise StoreError("restored database failed integrity check")
            finally:
                candidate.close()

        async with self._transaction_lock:
            await self._writer_call(operation)
