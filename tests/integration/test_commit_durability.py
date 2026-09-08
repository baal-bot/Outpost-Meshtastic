"""Actual SQLite connection policy; no physical power-loss qualification."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import outpost.store.database as store_module
from outpost.store import Database, StoreError

pytestmark = pytest.mark.production_wiring


async def writer_policy(database):
    # Database.read uses another connection and cannot establish writer policy.
    async with database.transaction() as transaction:
        return {
            name: (await transaction.read(f"PRAGMA main.{name}"))[0][0]
            for name in ("synchronous", "journal_mode", "auto_vacuum")
        }


async def test_full_writer_policy_survives_fresh_migrations_and_reopen(tmp_path):
    path = tmp_path / "fresh.db"
    for opening in range(2):
        database = Database(path)
        await database.open()
        try:
            assert await writer_policy(database) == {
                "synchronous": 2,
                "journal_mode": "wal",
                "auto_vacuum": 2,
            }
            assert (await database.read("PRAGMA synchronous"))[0][0] == 2
            if opening == 0:
                await database.write(
                    "INSERT INTO runtime_setting VALUES('durability.fixture','true',1)"
                )
            else:
                assert (await database.read("SELECT value FROM runtime_setting"))[0][0] == "true"
        finally:
            await database.close()


async def test_upgrade_from_legacy_normal_writer_uses_full_before_migrating(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    migrations = sorted((Path(store_module.__file__).parent / "migrations").glob("[0-9]*.sql"))
    with closing(sqlite3.connect(path)) as legacy:
        legacy.execute("PRAGMA auto_vacuum=INCREMENTAL")
        legacy.execute("PRAGMA journal_mode=WAL")
        legacy.execute("PRAGMA synchronous=NORMAL")
        for migration in migrations:
            version = int(migration.name[:4])
            if version > 184:
                continue
            legacy.executescript(
                "BEGIN IMMEDIATE;\n"
                + migration.read_text()
                + f"\nINSERT INTO schema_version VALUES({version},1); COMMIT;"
            )
        assert legacy.execute("PRAGMA synchronous").fetchone()[0] == 1
    original = Database._migrate_sync
    observed = []

    def observe(database):
        observed.append(database._writer.execute("PRAGMA synchronous").fetchone()[0])
        original(database)

    monkeypatch.setattr(Database, "_migrate_sync", observe)
    database = Database(path)
    await database.open()
    try:
        assert observed == [2]
        assert (await writer_policy(database))["synchronous"] == 2
        assert (await database.read("SELECT MAX(version) FROM schema_version"))[0][0] == max(
            int(path.name[:4]) for path in migrations
        )
        assert await database.read("SELECT 1 FROM sqlite_schema WHERE name='recovery_fence'")
    finally:
        await database.close()


@pytest.mark.parametrize("stage", ["configure", "migrate"])
async def test_weaker_writer_policy_fails_closed_and_releases_connection(
    tmp_path, monkeypatch, stage
):
    database = Database(tmp_path / "rejected.db")
    connections = []
    if stage == "configure":
        original = Database._configure

        def weaken(connection):
            original(connection)
            connections.append(connection)
            connection.execute("PRAGMA synchronous=NORMAL")

        monkeypatch.setattr(Database, "_configure", staticmethod(weaken))
    else:
        original = Database._migrate_sync

        def weaken_after_migration(database):
            original(database)
            connections.append(database._writer)
            database._writer.execute("PRAGMA synchronous=NORMAL")

        monkeypatch.setattr(Database, "_migrate_sync", weaken_after_migration)
    try:
        with pytest.raises(StoreError, match="synchronous=FULL"):
            await database.open()
        assert database._writer is None
        with pytest.raises(StoreError, match="not open"):
            await database.write("INSERT INTO runtime_setting VALUES('must.not.ack','true',1)")
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connections[0].execute("SELECT 1")
    finally:
        await database.close()


async def test_backup_restore_preserves_target_writer_durability(tmp_path):
    database = Database(tmp_path / "target.db")
    await database.open()
    try:
        await database.write("INSERT INTO runtime_setting VALUES('durability.fixture','before',1)")
        backup = tmp_path / "backup.db"
        await database.backup(backup)
        with closing(sqlite3.connect(backup)) as source:
            source.execute("PRAGMA synchronous=NORMAL")
            source.execute("UPDATE runtime_setting SET value='restored'")
            source.commit()
        await database.restore_from(backup)
        assert (await writer_policy(database))["synchronous"] == 2
        assert (await database.read("SELECT value FROM runtime_setting"))[0][0] == "restored"
        await database.write("UPDATE runtime_setting SET value='new acknowledged write'")
        assert (await writer_policy(database))["synchronous"] == 2
        assert (await database.validate_current())["integrity"] == "ok"
    finally:
        await database.close()
