from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from outpost.store import Database, ownership
from outpost.store.database import StoreError

pytestmark = pytest.mark.production_wiring


def marker(database: Path, current: Path) -> Path:
    path = database.with_name(database.name + ".deployment.json")
    path.write_text(json.dumps({"format": 1, "database": str(database), "current": str(current)}))
    return path


@pytest.mark.parametrize("alias", [False, True])
async def test_checkout_cannot_migrate_a_marked_installed_store(
    tmp_path: Path, alias: bool
) -> None:
    database = tmp_path / "installed.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(
            "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at INTEGER);"
            "INSERT INTO schema_version VALUES(172,1);"
            "CREATE TABLE acknowledged(id TEXT PRIMARY KEY);"
            "INSERT INTO acknowledged VALUES('retained-critical-work');"
        )
    before = database.read_bytes()
    wal = Path(str(database) + "-wal")
    marker(database, tmp_path / "missing-current")
    selected = database
    if alias:
        selected = tmp_path / "alias.db"
        selected.symlink_to(database)
    store = Database(selected)
    try:
        with pytest.raises(StoreError, match="belongs to the installed Outpost release"):
            await store.open()
        assert store._writer is None
        assert database.read_bytes() == before
        assert not await asyncio.to_thread(wal.exists)
    finally:
        await store.close()


async def test_standard_state_is_reserved_before_a_database_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ownership, "STATE", tmp_path / "state")
    monkeypatch.setattr(ownership, "CURRENT", tmp_path / "missing-current")
    store = Database(tmp_path / "state" / "new.db")
    try:
        with pytest.raises(StoreError, match="belongs to the installed Outpost release"):
            await store.open()
        assert not store.path.parent.exists()
    finally:
        await store.close()


@pytest.mark.parametrize("corrupt", ["{", "x" * 4097, '{"format": 2}'])
async def test_invalid_ownership_record_fails_before_database_creation(
    tmp_path: Path, corrupt: str
) -> None:
    database = tmp_path / "installed.db"
    record = marker(database, tmp_path / "current")
    record.write_text(corrupt)
    store = Database(database)
    try:
        with pytest.raises(StoreError, match="ownership is unreadable"):
            await store.open()
        assert not database.exists()
    finally:
        await store.close()


async def test_selected_packaged_owner_can_migrate_reopen_and_retain_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    current = tmp_path / "current"
    current.symlink_to(release)
    monkeypatch.setattr(ownership, "PACKAGE", release / "lib" / "python" / "outpost")
    monkeypatch.setattr(sys, "prefix", str(release))
    database = tmp_path / "installed.db"
    marker(database, current)
    store = Database(database)
    await store.open()
    try:
        await store.write("CREATE TABLE owner_probe(id TEXT PRIMARY KEY)")
        await store.write("INSERT INTO owner_probe VALUES('retained-critical-work')")
    finally:
        await store.close()
    reopened = Database(database)
    await reopened.open()
    try:
        assert (await reopened.read("SELECT id FROM owner_probe"))[0]["id"] == (
            "retained-critical-work"
        )
    finally:
        await reopened.close()
    # Even the selected interpreter cannot use checkout code on this database.
    monkeypatch.setattr(ownership, "PACKAGE", tmp_path / "checkout" / "outpost")
    rejected = Database(database)
    try:
        with pytest.raises(StoreError, match="belongs to the installed Outpost release"):
            await rejected.open()
    finally:
        await rejected.close()


async def test_isolated_development_store_still_migrates(tmp_path: Path) -> None:
    store = Database(tmp_path / "development.db")
    await store.open()
    try:
        assert (await store.read("SELECT MAX(version) FROM schema_version"))[0][0] >= 185
    finally:
        await store.close()
