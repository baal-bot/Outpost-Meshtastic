from __future__ import annotations

import asyncio
import inspect
import sqlite3
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar, cast

from prometheus_client import Counter, Gauge

T = TypeVar("T")
MIN_SQLITE = (3, 43, 0)
DB_READ_POOL_SIZE = 2
DB_READ_CONNECTIONS_OPENED = Counter(
    "outpost_db_read_connections_opened_total", "SQLite read connections opened"
)
DB_READ_CONNECTIONS_ACTIVE = Gauge(
    "outpost_db_read_connections_active", "Active bounded SQLite read connections"
)
DB_READ_QUERIES = Counter("outpost_db_read_queries_total", "SQLite read queries")


class StoreError(RuntimeError):
    pass


class PostCommitError(StoreError):
    """SQLite committed, but a local publication failed; do not report rollback."""


class Transaction:
    """Operations executed on the serialized writer inside one SQLite transaction."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._owner = asyncio.current_task()
        self._active = True
        self._publications: list[Callable[[], None]] = []

    def check_owner(self, database: Database) -> None:
        if not self._active or asyncio.current_task() is not self._owner:
            raise StoreError("transaction is closed or used outside its owning task")
        if database is not self._database:
            raise StoreError("transaction belongs to a different database")
        if database._active_transaction is not self:
            raise StoreError("transaction is not the active owned writer")

    def after_commit(self, publication: Callable[[], None]) -> None:
        """Register synchronous local publication; never SQL, radio or async I/O."""
        self.check_owner(self._database)
        if (
            not callable(publication)
            or inspect.iscoroutinefunction(publication)
            or inspect.iscoroutinefunction(type(publication).__call__)
        ):
            raise TypeError("post-commit publication must be synchronous")
        self._publications.append(publication)

    def _publish(self) -> None:
        publications, self._publications = self._publications, []
        errors: list[BaseException] = []
        for publication in publications:
            try:
                result = cast(Callable[[], object], publication)()
                if inspect.iscoroutine(result):
                    result.close()
                if result is not None:
                    raise TypeError("post-commit publication must return None, not async work")
            except (Exception, asyncio.CancelledError) as error:
                # One broken local consumer must not suppress the others. SQLite
                # is already committed; rollback would be a misleading promise.
                errors.append(error)
        if errors:
            raise PostCommitError("transaction committed; local publication failed") from errors[0]

    async def write(self, sql: str, params: Sequence[Any] = ()) -> int:
        self.check_owner(self._database)
        return await self._database._writer_write(sql, params)

    async def read(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        self.check_owner(self._database)
        return await self._database._writer_read(sql, params)


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._writer: sqlite3.Connection | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="outpost-db-writer")
        self._read_executor = ThreadPoolExecutor(
            max_workers=DB_READ_POOL_SIZE, thread_name_prefix="outpost-db-reader"
        )
        self._read_local = threading.local()
        self._read_connections: list[sqlite3.Connection] = []
        self._read_connections_lock = threading.Lock()
        self._read_connection_opens = 0
        self._read_queries = 0
        self._transaction_lock = asyncio.Lock()
        self._active_transaction: Transaction | None = None

    @staticmethod
    def _configure(connection: sqlite3.Connection) -> None:
        # WAL commits must request their durability barrier before callers can
        # acknowledge authoritative data. Storage must still honor SQLite's sync.
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-16000")
        connection.execute("PRAGMA mmap_size=67108864")

    @staticmethod
    def _verify_writer_durability(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA main.synchronous").fetchone()[0] != 2:
            raise StoreError("REQ-DATA-002b: writer must use synchronous=FULL")

    def _open_sync(self) -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise StoreError("REQ-DATA-002b requires SQLite >= 3.43")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            if fresh:
                connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
                connection.execute("PRAGMA journal_mode=WAL")
            self._configure(connection)
            journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
            auto_vacuum = connection.execute("PRAGMA auto_vacuum").fetchone()[0]
            if str(journal).lower() != "wal" or auto_vacuum != 2:
                raise StoreError(
                    "REQ-DATA-002a: database must use journal_mode=wal and auto_vacuum=incremental"
                )
            self._verify_writer_durability(connection)
            self._writer = connection
            self._migrate_sync()
            self._verify_writer_durability(connection)
        except BaseException:
            self._writer = None
            connection.close()
            raise

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
        self._read_executor.shutdown(wait=True)
        with self._read_connections_lock:
            for reader in self._read_connections:
                reader.close()
                DB_READ_CONNECTIONS_ACTIVE.dec()
            self._read_connections.clear()
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
                await self._settle(self._writer_call(self._commit))
                return row_id
            except BaseException:
                await self._settle(self._writer_call(self._rollback))
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

    async def _settle(self, operation: Awaitable[None]) -> None:
        """Keep writer ownership until cleanup/publication finishes, then cancel.

        Shield alone returns early on cancellation. Repeated cancellation must not
        let the next writer overtake commit/rollback or post-commit publication.
        """
        task = asyncio.ensure_future(operation)
        cancellation = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = error
        task.result()
        if cancellation is not None:
            raise cancellation

    async def _commit_transaction(self, transaction: Transaction) -> None:
        try:
            await self._writer_call(self._commit)
        except BaseException:
            transaction._publications.clear()
            await self._writer_call(self._rollback)
            raise
        transaction._publish()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        """Hold the sole writer and commit all enclosed operations as one unit."""
        async with self._transaction_lock:
            transaction = Transaction(self)
            self._active_transaction = transaction
            try:
                # Cancelling the await cannot stop a BEGIN already running on the
                # writer thread. Roll it back before another task gets the lock.
                await self._writer_call(self._begin)
                yield transaction
            except BaseException:
                transaction._active = False
                self._active_transaction = None
                transaction._publications.clear()
                await self._settle(self._writer_call(self._rollback))
                raise
            else:
                transaction._active = False
                self._active_transaction = None
                await self._settle(self._commit_transaction(transaction))

    async def read(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        if self._writer is None:
            raise StoreError("database is not open")

        def operation() -> list[sqlite3.Row]:
            connection = getattr(self._read_local, "connection", None)
            if connection is None:
                connection = sqlite3.connect(self.path, check_same_thread=False)
                connection.row_factory = sqlite3.Row
                self._configure(connection)
                self._read_local.connection = connection
                with self._read_connections_lock:
                    self._read_connections.append(connection)
                    self._read_connection_opens += 1
                DB_READ_CONNECTIONS_OPENED.inc()
                DB_READ_CONNECTIONS_ACTIVE.inc()
            with self._read_connections_lock:
                self._read_queries += 1
            DB_READ_QUERIES.inc()
            return list(connection.execute(sql, params))

        return await asyncio.get_running_loop().run_in_executor(self._read_executor, operation)

    def read_pool_status(self) -> dict[str, int]:
        with self._read_connections_lock:
            return {
                "capacity": DB_READ_POOL_SIZE,
                "opened": self._read_connection_opens,
                "active": len(self._read_connections),
                "queries": self._read_queries,
            }

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

    async def recovery_snapshot(self) -> bytes:
        """Capture the same serialized owner without any plaintext export file."""
        from .recovery_snapshot import snapshot

        result: list[bytes] = []

        def operation() -> None:
            if self._writer is None:
                raise StoreError("database is not open")
            result.append(snapshot(self._writer))

        async with self._transaction_lock:
            # Cancellation must not release the writer while SQLite still owns it.
            await self._settle(self._writer_call(operation))
        return result[0]

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

    async def validate_current(self) -> dict[str, int | str]:
        def operation() -> dict[str, int | str]:
            if self._writer is None:
                raise StoreError("database is not open")
            integrity = str(self._writer.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise StoreError(f"database integrity check failed: {integrity}")
            value = self._writer.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            if value is None:
                raise StoreError("database schema version is empty")
            return {
                "integrity": integrity,
                "schema_version": int(value),
                "size_bytes": self.path.stat().st_size,
            }

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
