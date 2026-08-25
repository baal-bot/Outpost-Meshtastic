import sqlite3

import pytest

from outpost.store import Database
from outpost.store.backups import BackupService


@pytest.mark.asyncio
async def test_backup_is_verified_listed_and_safely_resolved(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = BackupService(database)
    path = await service.create()

    assert service.list()[0]["name"] == path.name
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
    assert service.resolve(path.name) == path
    assert service.resolve("../outpost.db") is None
    validation = await service.validate(path.name)
    assert validation["integrity"] == "ok"
    await database.write(
        "INSERT INTO runtime_setting(key,value,updated_at) VALUES('test.restore','true',1)"
    )
    with pytest.raises(ValueError, match="Confirmation"):
        await service.restore(path.name, "wrong")
    restored = await service.restore(path.name, f"RESTORE {path.name}")
    assert restored["restored"] == path.name
    assert not await database.read("SELECT 1 FROM runtime_setting WHERE key='test.restore'")
    assert await database.read("SELECT 1 FROM audit_log WHERE action='backup.restore'")
    await database.close()
