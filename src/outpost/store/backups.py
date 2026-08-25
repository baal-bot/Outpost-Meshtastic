from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .database import Database


class BackupService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.directory = database.path.parent / "backups"

    async def create(self) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = self.directory / f"outpost-{stamp}.db"
        await self.database.backup(destination)
        return destination

    def list(self) -> list[dict[str, object]]:
        if not self.directory.exists():
            return []
        return [
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "created_at": datetime.fromtimestamp(path.stat().st_mtime, UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            for path in sorted(self.directory.glob("outpost-*.db"), reverse=True)
            if path.is_file()
        ]

    def resolve(self, name: str) -> Path | None:
        if Path(name).name != name:
            return None
        candidate = self.directory / name
        return candidate if candidate.is_file() and candidate.name.startswith("outpost-") else None

    def rotate(self, keep: int) -> int:
        paths = sorted(self.directory.glob("outpost-*.db"), reverse=True)
        removed = 0
        for path in paths[keep:]:
            if path.is_file():
                path.unlink()
                removed += 1
        return removed

    async def validate(self, name: str) -> dict[str, object]:
        path = self.resolve(name)
        if path is None:
            raise ValueError("Backup not found.")
        result = await self.database.validate_backup(path)
        return {"name": name, **result}

    async def restore(self, name: str, confirmation: str) -> dict[str, object]:
        path = self.resolve(name)
        if path is None:
            raise ValueError("Backup not found.")
        if confirmation != f"RESTORE {name}":
            raise ValueError("Confirmation phrase does not match.")
        validation = await self.database.validate_backup(path)
        safety = await self.create()
        await self.database.restore_from(path)
        await self.database.write(
            """
            INSERT INTO audit_log(actor_kind,actor_ref,action,target,detail,created_at)
            VALUES('web','operator','backup.restore',?,?,unixepoch())
            """,
            (name, f"pre_restore={safety.name}"),
        )
        return {"restored": name, "safety_backup": safety.name, **validation}
