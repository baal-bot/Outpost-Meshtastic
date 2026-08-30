#!/usr/bin/env python3
"""Verified database snapshot helpers used by install and manual rollback."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class RecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatabaseState:
    integrity: str
    schema_version: int
    size_bytes: int


def inspect_database(path: str | Path) -> DatabaseState:
    database = Path(path)
    if not database.is_file():
        raise RecoveryError(f"database does not exist: {database}")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise RecoveryError(f"database integrity check failed: {integrity}")
        try:
            value = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        except sqlite3.DatabaseError as error:
            raise RecoveryError("database has no readable schema_version table") from error
        if value is None:
            raise RecoveryError("database schema version is empty")
        return DatabaseState(integrity, int(value), database.stat().st_size)
    finally:
        connection.close()


def snapshot_database(source: str | Path, destination: str | Path) -> DatabaseState:
    source_path, destination_path = Path(source), Path(destination)
    inspect_database(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists() and destination_path.stat().st_size:
        raise RecoveryError(f"refusing to overwrite snapshot: {destination_path}")
    source_connection = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    target_connection = sqlite3.connect(destination_path)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    except BaseException:
        target_connection.close()
        source_connection.close()
        destination_path.unlink(missing_ok=True)
        raise
    else:
        target_connection.close()
        source_connection.close()
    return inspect_database(destination_path)


def restore_database(
    source: str | Path, destination: str | Path, *, maximum_schema: int
) -> DatabaseState:
    source_path, destination_path = Path(source), Path(destination)
    candidate = inspect_database(source_path)
    if candidate.schema_version > maximum_schema:
        raise RecoveryError(
            f"snapshot schema {candidate.schema_version} exceeds target capacity {maximum_schema}"
        )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.restore-", dir=destination_path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    try:
        source_connection = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        target_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(target_connection)
            target_connection.commit()
        finally:
            target_connection.close()
            source_connection.close()
        restored = inspect_database(temporary)
        if restored.schema_version != candidate.schema_version:
            raise RecoveryError("restored schema does not match the selected snapshot")
        for suffix in ("-wal", "-shm"):
            Path(f"{destination_path}{suffix}").unlink(missing_ok=True)
        os.replace(temporary, destination_path)
        return inspect_database(destination_path)
    finally:
        temporary.unlink(missing_ok=True)


def write_metadata(
    output: str | Path,
    *,
    upgrade_release: str | Path,
    previous_release: str | Path,
    database: str | Path,
    backup: str | Path,
    pre_upgrade_schema: int,
    previous_schema_cap: int,
    upgrade_schema_cap: int,
) -> dict[str, Any]:
    if pre_upgrade_schema > previous_schema_cap:
        raise RecoveryError("pre-upgrade database is newer than the previous release")
    metadata: dict[str, Any] = {
        "format": 1,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "upgrade_release": str(Path(upgrade_release).resolve()),
        "previous_release": str(Path(previous_release).resolve()),
        "database": str(Path(database).resolve()),
        "backup": str(Path(backup).resolve()),
        "pre_upgrade_schema": pre_upgrade_schema,
        "previous_schema_cap": previous_schema_cap,
        "upgrade_schema_cap": upgrade_schema_cap,
    }
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.new")
    temporary.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, output_path)
    return metadata


def plan_rollback(
    metadata_path: str | Path,
    *,
    current_release: str | Path,
    target_release: str | Path,
) -> dict[str, Any]:
    try:
        metadata = json.loads(Path(metadata_path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RecoveryError(f"rollback metadata is unreadable: {error}") from error
    if metadata.get("format") != 1:
        raise RecoveryError("rollback metadata format is unsupported")
    current = str(Path(current_release).resolve())
    target = str(Path(target_release).resolve())
    if metadata.get("upgrade_release") != current:
        raise RecoveryError("rollback metadata does not match the current release")
    if metadata.get("previous_release") != target:
        raise RecoveryError("rollback metadata does not match the selected previous release")
    database = Path(str(metadata.get("database", "")))
    live = inspect_database(database)
    target_cap = int(metadata["previous_schema_cap"])
    action = "code-only"
    backup_state: DatabaseState | None = None
    if live.schema_version > target_cap:
        backup = Path(str(metadata.get("backup", "")))
        backup_state = inspect_database(backup)
        recorded_schema = int(metadata["pre_upgrade_schema"])
        if backup_state.schema_version != recorded_schema:
            raise RecoveryError("pre-upgrade snapshot does not match recorded schema metadata")
        if backup_state.schema_version > target_cap:
            raise RecoveryError("pre-upgrade snapshot is too new for the rollback target")
        action = "restore"
    return {
        "action": action,
        "database": str(database),
        "backup": str(metadata.get("backup", "")),
        "live": asdict(live),
        "candidate": asdict(backup_state) if backup_state else None,
        "target_schema_cap": target_cap,
        "current_schema_cap": int(metadata["upgrade_schema_cap"]),
        "snapshot_created_at": str(metadata["created_at"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--database", required=True)
    schema = commands.add_parser("schema")
    schema.add_argument("--database", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--source", required=True)
    snapshot.add_argument("--destination", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--source", required=True)
    restore.add_argument("--destination", required=True)
    restore.add_argument("--maximum-schema", required=True, type=int)
    record = commands.add_parser("record")
    record.add_argument("--output", required=True)
    record.add_argument("--upgrade-release", required=True)
    record.add_argument("--previous-release", required=True)
    record.add_argument("--database", required=True)
    record.add_argument("--backup", required=True)
    record.add_argument("--pre-upgrade-schema", required=True, type=int)
    record.add_argument("--previous-schema-cap", required=True, type=int)
    record.add_argument("--upgrade-schema-cap", required=True, type=int)
    plan = commands.add_parser("plan")
    plan.add_argument("--metadata", required=True)
    plan.add_argument("--current-release", required=True)
    plan.add_argument("--target-release", required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "inspect":
            result: Any = asdict(inspect_database(args.database))
        elif args.command == "schema":
            print(inspect_database(args.database).schema_version)
            return
        elif args.command == "snapshot":
            result = asdict(snapshot_database(args.source, args.destination))
        elif args.command == "restore":
            result = asdict(
                restore_database(args.source, args.destination, maximum_schema=args.maximum_schema)
            )
        elif args.command == "record":
            result = write_metadata(
                args.output,
                upgrade_release=args.upgrade_release,
                previous_release=args.previous_release,
                database=args.database,
                backup=args.backup,
                pre_upgrade_schema=args.pre_upgrade_schema,
                previous_schema_cap=args.previous_schema_cap,
                upgrade_schema_cap=args.upgrade_schema_cap,
            )
        else:
            result = plan_rollback(
                args.metadata,
                current_release=args.current_release,
                target_release=args.target_release,
            )
    except (KeyError, TypeError, ValueError, RecoveryError) as error:
        raise SystemExit(f"Outpost recovery: {error}") from error
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
