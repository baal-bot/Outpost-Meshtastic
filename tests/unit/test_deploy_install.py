import runpy
import sqlite3
from contextlib import closing
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

RECOVERY = runpy.run_path(
    str(Path(__file__).parents[2] / "deploy" / "release_recovery.py"),
    run_name="outpost_release_recovery",
)
inspect_database = cast(Callable[..., Any], RECOVERY["inspect_database"])
plan_rollback = cast(Callable[..., dict[str, Any]], RECOVERY["plan_rollback"])
restore_database = cast(Callable[..., Any], RECOVERY["restore_database"])
snapshot_database = cast(Callable[..., Any], RECOVERY["snapshot_database"])
write_metadata = cast(Callable[..., dict[str, Any]], RECOVERY["write_metadata"])


def test_installer_stages_health_checked_release_with_rollback() -> None:
    script = (Path(__file__).parents[2] / "deploy" / "install.sh").read_text()

    assert "useradd --system --gid outpost" in script
    assert 'pip" install -c "$PROJECT_DIR/requirements.lock"' in script
    assert "pre-upgrade-$RELEASE_ID.db" in script
    assert 'release_recovery.py" record' in script
    assert '"$RELEASE_DIR/rollback.json"' in script
    assert 'mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"' in script
    assert 'curl -fsS "$HEALTH_URL"' in script
    assert 'ln -sfn "$CURRENT_LINK/bin/outpost-setup-token"' in script
    assert 'ln -sfn "$CURRENT_LINK/bin/outpost-diagnostics"' in script
    assert "sudo outpost-setup-token show" in script
    assert "sudo outpost-setup-token reset" in script
    assert "New release failed health verification; rolling back." in script


def test_service_uses_atomic_current_release() -> None:
    unit = (Path(__file__).parents[2] / "deploy" / "outpost.service").read_text()

    assert "ExecStart=/opt/outpost/current/bin/python -m outpost" in unit
    assert "ExecStartPre=/opt/outpost/current/bin/python" in unit


def test_service_startup_never_prints_reusable_dashboard_credentials() -> None:
    application = (Path(__file__).parents[2] / "src" / "outpost" / "app.py").read_text()

    assert "OUTPOST INITIAL OPERATOR PASSWORD" not in application
    assert "setup_path.read_text" not in application


def _database(path: Path, schema: int, value: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at INTEGER);"
        "CREATE TABLE sample(value TEXT);"
    )
    connection.execute("INSERT INTO schema_version VALUES(?,1)", (schema,))
    connection.execute("INSERT INTO sample VALUES(?)", (value,))
    connection.commit()
    connection.close()


def test_release_recovery_restores_snapshot_across_schema_migration(tmp_path: Path) -> None:
    old_release, new_release = tmp_path / "old", tmp_path / "new"
    old_release.mkdir()
    new_release.mkdir()
    live, backup = tmp_path / "outpost.db", tmp_path / "pre-upgrade.db"
    _database(live, 1, "before")
    snapshot_database(live, backup)
    metadata = new_release / "rollback.json"
    write_metadata(
        metadata,
        upgrade_release=new_release,
        previous_release=old_release,
        database=live,
        backup=backup,
        pre_upgrade_schema=1,
        previous_schema_cap=1,
        upgrade_schema_cap=2,
    )
    connection = sqlite3.connect(live)
    connection.execute("INSERT INTO schema_version VALUES(2,2)")
    connection.execute("UPDATE sample SET value='after'")
    connection.commit()
    connection.close()

    plan = plan_rollback(metadata, current_release=new_release, target_release=old_release)
    assert plan["action"] == "restore"
    safety = tmp_path / "safety.db"
    snapshot_database(live, safety)
    restore_database(backup, live, maximum_schema=1)
    assert inspect_database(live).schema_version == 1
    with closing(sqlite3.connect(live)) as restored:
        assert restored.execute("SELECT value FROM sample").fetchone()[0] == "before"

    # A failed code rollback can put the exact pre-attempt data back.
    restore_database(safety, live, maximum_schema=2)
    assert inspect_database(live).schema_version == 2
    with closing(sqlite3.connect(live)) as recovered:
        assert recovered.execute("SELECT value FROM sample").fetchone()[0] == "after"


def test_release_recovery_skips_data_restore_without_schema_change(tmp_path: Path) -> None:
    old_release, new_release = tmp_path / "old", tmp_path / "new"
    old_release.mkdir()
    new_release.mkdir()
    live, backup = tmp_path / "outpost.db", tmp_path / "pre-upgrade.db"
    _database(live, 1, "current")
    snapshot_database(live, backup)
    metadata = new_release / "rollback.json"
    write_metadata(
        metadata,
        upgrade_release=new_release,
        previous_release=old_release,
        database=live,
        backup=backup,
        pre_upgrade_schema=1,
        previous_schema_cap=1,
        upgrade_schema_cap=1,
    )

    plan = plan_rollback(metadata, current_release=new_release, target_release=old_release)
    assert plan["action"] == "code-only"
    assert plan["candidate"] is None
