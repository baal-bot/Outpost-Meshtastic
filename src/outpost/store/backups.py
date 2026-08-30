from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from outpost.audit import write_audit
from outpost.config import BackupConfig
from outpost.operator_context import current_actor_ref

from .database import Database


class RestoreRecoveredError(RuntimeError):
    """The selected restore failed, but the pre-restore snapshot was recovered."""

    def __init__(self, message: str, safety_backup: str) -> None:
        super().__init__(message)
        self.safety_backup = safety_backup


class BackupService:
    def __init__(self, database: Database, config: BackupConfig | None = None) -> None:
        self.database = database
        self.config = config or BackupConfig()
        self.directory = database.path.parent / "backups"

    def _kind(self, path: Path) -> tuple[str, str]:
        relative = path.relative_to(self.directory)
        if relative.parent.name == "restore-jobs" and path.suffix == ".json":
            return "restore_metadata", "Restore job metadata"
        if relative.parent != Path("."):
            return "unmanaged", "Unmanaged backup artifact"
        if path.name.startswith("outpost-") and path.suffix == ".db":
            return "scheduled", "Verified backup"
        if path.name.startswith("pre-upgrade-") and path.suffix == ".db":
            return "pre_upgrade", "Pre-upgrade recovery"
        if path.name.startswith("pre-manual-rollback-") and path.suffix == ".db":
            return "pre_rollback", "Pre-rollback recovery"
        if path.name.endswith((".db-wal", ".db-shm", ".partial")):
            return "auxiliary", "Auxiliary backup data"
        return "unmanaged", "Unmanaged backup artifact"

    def _snapshot_paths(self, prefix: str) -> list[Path]:
        return sorted(
            (
                path
                for path in self.directory.glob(f"{prefix}*.db")
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )

    async def create(self) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / f"outpost-{stamp}.db"
        partial = destination.with_suffix(".db.partial")
        try:
            await self.database.backup(partial)
            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)
        return destination

    def list(self) -> list[dict[str, object]]:
        if not self.directory.exists():
            return []
        protected = set(self._snapshot_paths("pre-upgrade-")[: self.config.pre_upgrade_keep])
        protected.update(self._snapshot_paths("pre-manual-rollback-")[:1])
        items: list[dict[str, object]] = []
        for path in self.directory.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                status = path.stat()
            except OSError:
                continue
            kind, label = self._kind(path)
            recovery = kind in {"pre_upgrade", "pre_rollback"}
            items.append(
                {
                    "name": str(path.relative_to(self.directory)),
                    "kind": kind,
                    "kind_label": label,
                    "size_bytes": status.st_size,
                    "created_at": datetime.fromtimestamp(status.st_mtime, UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "downloadable": kind in {"scheduled", "pre_upgrade", "pre_rollback"},
                    "restorable": kind == "scheduled",
                    "removable": recovery and path not in protected,
                    "protected": recovery and path in protected,
                }
            )
        return sorted(items, key=lambda item: str(item["created_at"]), reverse=True)

    def resolve(self, name: str) -> Path | None:
        if Path(name).name != name:
            return None
        candidate = self.directory / name
        return (
            candidate
            if candidate.is_file()
            and not candidate.is_symlink()
            and candidate.name.startswith("outpost-")
            and candidate.suffix == ".db"
            else None
        )

    def resolve_download(self, name: str) -> Path | None:
        if Path(name).name != name:
            return None
        candidate = self.directory / name
        kind, _ = self._kind(candidate)
        return (
            candidate
            if candidate.is_file()
            and not candidate.is_symlink()
            and kind in {"scheduled", "pre_upgrade", "pre_rollback"}
            else None
        )

    @staticmethod
    def _unlink_snapshot(path: Path) -> int:
        removed = 0
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            if candidate.is_file() and not candidate.is_symlink():
                candidate.unlink()
                removed += 1
        return removed

    def rotate(self, keep: int | None = None, *, now: datetime | None = None) -> int:
        keep = keep if keep is not None else self.config.keep
        now = now or datetime.now(UTC)
        paths = self._snapshot_paths("outpost-")
        removed = 0
        for path in paths[keep:]:
            removed += self._unlink_snapshot(path)
        for path in self._snapshot_paths("pre-upgrade-")[self.config.pre_upgrade_keep :]:
            removed += self._unlink_snapshot(path)
        rollback_paths = self._snapshot_paths("pre-manual-rollback-")
        rollback_cutoff = now.timestamp() - self.config.pre_rollback_days * 86_400
        for path in rollback_paths[1:]:
            if path.stat().st_mtime < rollback_cutoff:
                removed += self._unlink_snapshot(path)
        return removed

    def remove_recovery(self, name: str, confirmation: str) -> Path:
        path = self.resolve_download(name)
        if path is None or self._kind(path)[0] not in {"pre_upgrade", "pre_rollback"}:
            raise ValueError("Recovery snapshot not found.")
        if confirmation != f"DELETE {name}":
            raise ValueError("Confirmation phrase does not match.")
        prefix = "pre-upgrade-" if path.name.startswith("pre-upgrade-") else "pre-manual-rollback-"
        paths = self._snapshot_paths(prefix)
        protected_count = self.config.pre_upgrade_keep if prefix == "pre-upgrade-" else 1
        if path in paths[:protected_count]:
            raise ValueError("This recovery snapshot is protected by the active retention policy.")
        self._unlink_snapshot(path)
        return path

    async def validate(self, name: str) -> dict[str, object]:
        path = self.resolve(name)
        if path is None:
            raise ValueError("Backup not found.")
        result = await self.database.validate_backup(path)
        return {"name": name, **result}

    async def prepare_restore(self, name: str, confirmation: str) -> dict[str, object]:
        if self.resolve(name) is None:
            raise ValueError("Backup not found.")
        if confirmation != f"RESTORE {name}":
            raise ValueError("Confirmation phrase does not match.")
        return await self.validate(name)

    async def restore_quiesced(self, name: str) -> dict[str, object]:
        """Restore after all external writers have been stopped by the application."""
        path = self.resolve(name)
        if path is None:
            raise ValueError("Backup not found.")
        candidate = await self.database.validate_backup(path)
        before = await self.database.validate_current()
        safety = await self.create()
        safety_validation = await self.database.validate_backup(safety)
        if safety_validation["schema_version"] != before["schema_version"]:
            raise RuntimeError("Pre-restore safety snapshot schema does not match live state.")
        try:
            await self.database.restore_from(path)
            restored = await self.database.validate_current()
            if restored["schema_version"] != candidate["schema_version"]:
                raise RuntimeError("Restored database schema does not match the candidate.")
            await write_audit(
                self.database,
                actor_kind="web",
                actor_ref=current_actor_ref(),
                action="backup.restore",
                target=name,
                detail={"pre_restore": safety.name},
            )
            restored = await self.database.validate_current()
        except BaseException as error:
            try:
                await self.database.restore_from(safety)
                recovered = await self.database.validate_current()
                if recovered["schema_version"] != before["schema_version"]:
                    raise RuntimeError(
                        "Recovered database schema does not match pre-restore state."
                    )
            except BaseException as recovery_error:
                raise RuntimeError(
                    f"Restore failed and automatic recovery failed: {recovery_error}"
                ) from error
            raise RestoreRecoveredError(str(error), safety.name) from error
        return {
            "restored": name,
            "safety_backup": safety.name,
            "candidate": candidate,
            "safety": safety_validation,
            "database": restored,
        }


def directory_file_bytes(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def release_inventory(directory: Path) -> dict[str, int]:
    if not directory.is_dir():
        return {"count": 0, "size_bytes": 0}
    releases = [path for path in directory.iterdir() if path.is_dir() and not path.is_symlink()]
    return {"count": len(releases), "size_bytes": directory_file_bytes(directory)}


def plan_release_pruning(
    directory: Path,
    current: Path,
    previous: Path,
    keep_prior: int,
) -> list[Path]:
    root = directory.resolve(strict=False)
    if not root.is_absolute() or not root.is_dir():
        return []
    protected = {
        link.resolve(strict=False)
        for link in (current, previous)
        if link.exists() or link.is_symlink()
    }
    candidates = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and path.resolve(strict=False).parent == root
            and path.resolve(strict=False) not in protected
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    return candidates[keep_prior:]


def prune_release_directories(
    directory: Path,
    current: Path,
    previous: Path,
    keep_prior: int,
) -> list[str]:
    removed = []
    for path in plan_release_pruning(directory, current, previous, keep_prior):
        shutil.rmtree(path)
        removed.append(path.name)
    return removed


class RestoreCoordinator:
    """Maintenance gate and durable, session-independent restore progress."""

    _JOB_ID = re.compile(r"^[A-Za-z0-9_-]{32}$")
    _TERMINAL = {"completed", "failed_recovered", "failed", "interrupted"}

    def __init__(
        self,
        backups: BackupService,
        restore: Callable[[str], Awaitable[dict[str, object]]],
        request_restart: Callable[[], None],
    ) -> None:
        self.backups = backups
        self._restore = restore
        self._request_restart = request_restart
        self.directory = backups.directory / "restore-jobs"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._condition = asyncio.Condition()
        self._maintenance = False
        self._active_mutations = 0
        self._current_job: str | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._mark_interrupted_jobs()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def _path(self, job_id: str) -> Path | None:
        if not self._JOB_ID.fullmatch(job_id):
            return None
        return self.directory / f"{job_id}.json"

    def _persist(self, job: dict[str, Any]) -> None:
        path = self._path(str(job["job_id"]))
        if path is None:
            raise RuntimeError("Invalid restore job identifier.")
        job["updated_at"] = self._now()
        temporary = path.with_suffix(".json.new")
        temporary.write_text(json.dumps(job, indent=2, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def _mark_interrupted_jobs(self) -> None:
        for path in self.directory.glob("*.json"):
            try:
                job = json.loads(path.read_text())
                if job.get("state") not in self._TERMINAL:
                    job.update(
                        {
                            "state": "interrupted",
                            "message": (
                                "The process stopped before restore completion. "
                                "Verify the active database before retrying."
                            ),
                        }
                    )
                    self._persist(job)
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def status(self, job_id: str) -> dict[str, Any] | None:
        path = self._path(job_id)
        if path is None or not path.is_file():
            return None
        try:
            return dict(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def maintenance_status(self) -> dict[str, object]:
        job = self.status(self._current_job) if self._current_job else None
        return {
            "active": self._maintenance,
            "job_id": self._current_job,
            "state": job.get("state") if job else None,
            "message": job.get("message") if job else None,
        }

    async def enter_mutation(self) -> bool:
        async with self._condition:
            if self._maintenance:
                return False
            self._active_mutations += 1
            return True

    async def leave_mutation(self) -> None:
        async with self._condition:
            self._active_mutations = max(0, self._active_mutations - 1)
            self._condition.notify_all()

    async def schedule(self, name: str, confirmation: str) -> dict[str, Any]:
        validation = await self.backups.prepare_restore(name, confirmation)
        async with self._condition:
            if self._maintenance:
                raise ValueError("Another restore is already in progress.")
            self._maintenance = True
            job_id = secrets.token_urlsafe(24)
            self._current_job = job_id
            job: dict[str, Any] = {
                "job_id": job_id,
                "state": "queued",
                "message": "Restore accepted; waiting for active mutations to drain.",
                "backup": name,
                "validation": validation,
                "requested_at": self._now(),
                "updated_at": self._now(),
                "status_url": f"/api/v1/recovery/restores/{job_id}",
            }
            self._persist(job)
        task = asyncio.create_task(self._run(job_id, name), name=f"restore-{job_id[:8]}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job_id: str, name: str) -> None:
        # Give the initiating response a chance to reach the operator before quiescing.
        await asyncio.sleep(0.1)
        job = self.status(job_id)
        if job is None:
            return
        async with self._condition:
            while self._active_mutations:
                await self._condition.wait()
        job.update(
            {
                "state": "quiescing",
                "message": "Mutations are blocked; draining radio and background writers.",
            }
        )
        self._persist(job)
        try:
            result = await self._restore(name)
        except RestoreRecoveredError as error:
            job.update(
                {
                    "state": "failed_recovered",
                    "message": (
                        "Restore failed; the verified pre-restore state was recovered. "
                        "Outpost is restarting."
                    ),
                    "error": str(error),
                    "safety_backup": error.safety_backup,
                }
            )
        except BaseException as error:
            job.update(
                {
                    "state": "failed",
                    "message": "Restore could not complete safely; operator review is required.",
                    "error": f"{type(error).__name__}: {error}",
                }
            )
        else:
            job.update(
                {
                    "state": "completed",
                    "message": "Restore verified; Outpost is restarting with restored state.",
                    **result,
                }
            )
        self._persist(job)
        self._request_restart()
