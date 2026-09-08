from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from outpost.maps.packs import FORMAT, SCHEMA, active_manifest, atomic_json, publish_pack, read_json
from outpost.maps.pmtiles import Archive, RangeSource, tile_id
from outpost.maps.regions import MAX_DOWNLOAD_BYTES, MapError, Region, tiles_for_region
from outpost.maps.vector import validate_tile


def merged_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for offset, length in sorted(set(ranges)):
        if result:
            start, size = result[-1]
            end = max(start + size, offset + length)
            if offset <= start + size + 1024 and end - start <= 4 * 1024**2:
                result[-1] = start, end - start
                continue
        result.append((offset, length))
    return result


class MapSetupService:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.work = self.root / ".setup"
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="outpost-maps")
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.state: dict[str, Any] = {"state": "idle"}
        self._busy = False
        self._closed = False
        self._lease: int | None = None
        try:
            previous = read_json(self.work / "state.json")
            self.state = previous
            if previous.get("state") in {"planning", "downloading", "verifying"}:
                self.state = {
                    **previous,
                    "state": "paused",
                    "detail": "Setup interrupted. Resume the download or prepare a new plan.",
                }
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        self._closed = True
        self.cancel.set()
        self.executor.shutdown(wait=True, cancel_futures=True)

    def _save(self, **values: Any) -> None:
        with self.lock:
            self.state = {**self.state, **values, "updated_at": int(time.time())}
            snapshot = dict(self.state)
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.work, 0o700)
        atomic_json(self.work / "state.json", snapshot)

    def status(self) -> dict[str, Any]:
        if not self._busy:
            try:
                previous = read_json(self.work / "state.json")
                if previous.get("updated_at", 0) > self.state.get("updated_at", 0):
                    self.state = previous
            except (OSError, ValueError):
                pass
        with self.lock:
            state = dict(self.state)
            busy = self._busy
        try:
            manifest = active_manifest(self.root)
            error = None
        except (OSError, ValueError):
            manifest, error = (
                None,
                "Installed map is unreadable or changed; install a verified replacement.",
            )
        path = self.root
        while not path.exists():
            path = path.parent
        return {
            "job": state,
            "busy": busy,
            "installed": manifest,
            "installed_error": error,
            "free_bytes": shutil.disk_usage(path).free,
            "maximum_download_bytes": MAX_DOWNLOAD_BYTES,
        }

    def _claim(self) -> None:
        with self.lock:
            if self._closed or self._busy:
                raise MapError("Map setup is already running. Wait or pause the current download.")
            self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(self.work / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                os.close(descriptor)
                raise MapError(
                    "Another map setup process is running. Wait for it to finish."
                ) from error
            self._lease = descriptor
            self._busy = True
        self.cancel.clear()

    def _start(self, **values: Any) -> None:
        try:
            self._save(**values)
        except Exception:
            self._release()
            raise

    def _release(self) -> None:
        with self.lock:
            if self._lease is not None:
                os.close(self._lease)
                self._lease = None
            self._busy = False

    def _run(self, function: Any, *args: Any) -> None:
        try:
            function(*args)
        except MapError as error:
            self._save(state="paused" if self.cancel.is_set() else "failed", detail=str(error))
        except Exception:
            self._save(
                state="failed",
                detail="Map setup failed. Check storage permissions and connectivity, then retry.",
            )
        finally:
            self._release()

    def plan(self, region: Region | None, budget: int) -> dict[str, Any]:
        if type(budget) is not int or not 64 * 1024**2 <= budget <= MAX_DOWNLOAD_BYTES:
            raise MapError("Choose a download limit between 64 MiB and 1 GiB.")
        tiles_for_region(region)
        self._claim()
        identifier = uuid.uuid4().hex
        self._start(
            state="planning",
            id=identifier,
            detail="Checking regional coverage and download size…",
            plan=None,
            completed_tiles=0,
        )
        self.executor.submit(self._run, self._plan, identifier, region, budget)
        return {"id": identifier, "state": "planning"}

    def _source(self) -> RangeSource:
        for days in range(7):
            date = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y%m%d")
            source = RangeSource(
                f"https://build.protomaps.com/{date}.pmtiles",
                budget=32 * 1024**2,
                cancel=self.cancel,
            )
            try:
                source.read(0, 127)
                return source
            except MapError as error:
                source.close()
                if "no longer available" not in str(error):
                    raise
        raise MapError(
            "No recent map build is available. Check the date or import an offline pack."
        )

    def _plan(self, identifier: str, region: Region | None, budget: int) -> None:
        source = self._source()
        try:
            archive = Archive(source.read)
            version = archive.metadata.get("version", "")
            if not isinstance(version, str) or not version.startswith("4."):
                raise MapError(
                    "This map source needs a newer renderer; update Outpost before downloading it."
                )
            required_zoom = region.max_zoom if region is not None else 6
            if archive.min_zoom > 0 or archive.max_zoom < required_zoom:
                raise MapError("The map source does not cover the requested detail levels.")
            layers = archive.metadata.get("vector_layers", [])
            if not isinstance(layers, list) or not {"earth", "water", "roads", "places"} <= {
                i.get("id") for i in layers if isinstance(i, dict)
            }:
                raise MapError("The map source does not contain the required basemap layers.")
            planned = sorted(tiles_for_region(region), key=lambda row: tile_id(*row[1:]))
            records: list[list[Any]] = []
            for kind, zoom, x, y in planned:
                if self.cancel.is_set():
                    raise MapError("Map planning paused. Prepare a new plan when ready.")
                span = archive.locate(tile_id(zoom, x, y))
                records.append([kind, zoom, x, y, *(span or (0, 0))])
            groups = merged_ranges([(int(row[4]), int(row[5])) for row in records if row[5]])
            transfer = sum(length for _, length in groups)
            estimate = transfer + source.received + 128 * 1024
            if estimate > budget:
                raise MapError("Map plan exceeds the download limit; reduce the radius or detail.")
            output = sum(int(row[5]) for row in records) + len(records) * 512 + 2 * 1024**2
            if output > MAX_DOWNLOAD_BYTES:
                raise MapError(
                    "This region exceeds the installed pack limit; reduce radius or detail."
                )
            free = shutil.disk_usage(self.work).free
            reserve = max(1024**3, int(shutil.disk_usage(self.work).total * 0.05))
            if free < output * 2 + reserve:
                raise MapError(
                    "Not enough free storage for the map pack, staging space and station reserve."
                )
            summary = {
                "id": identifier,
                "region": region.document() if region else None,
                "overview_max_zoom": 6,
                "tile_count": len(records),
                "download_bytes": estimate,
                "installed_bytes_estimate": output,
                "budget_bytes": budget,
                "source": source.url,
                "source_version": version,
                "source_date": archive.metadata.get("planetiler:osm:osmosisreplicationtime"),
                "source_etag": source.etag,
                "tile_compression": archive.tile_compression,
            }
            atomic_json(
                self.work / f"{identifier}.plan.json",
                {"summary": summary, "tiles": records, "ranges": groups},
            )
            self._save(
                state="planned",
                detail="Coverage and size checked. Download when ready.",
                plan=summary,
            )
        finally:
            source.close()

    def download(self, identifier: str) -> dict[str, Any]:
        if len(identifier) != 32 or any(char not in "0123456789abcdef" for char in identifier):
            raise MapError("Invalid map plan identifier.")
        plan = read_json(self.work / f"{identifier}.plan.json", 8 * 1024**2)
        self._claim()
        self._start(
            state="downloading",
            id=identifier,
            plan=plan["summary"],
            detail="Downloading world overview and regional maps…",
        )
        self.executor.submit(self._run, self._download, identifier, plan)
        return {"id": identifier, "state": "downloading"}

    def pause(self) -> None:
        self.cancel.set()

    def _download(self, identifier: str, plan: dict[str, Any]) -> None:
        summary = plan["summary"]
        source = RangeSource(
            summary["source"],
            budget=summary["budget_bytes"],
            etag=summary["source_etag"],
            cancel=self.cancel,
        )
        staging = self.work / f"{identifier}.sqlite"
        try:
            # Recheck source identity even if all previously downloaded tiles are cached.
            source.read(0, 127)
            usage = shutil.disk_usage(self.work)
            if usage.free < summary["installed_bytes_estimate"] + max(
                1024**3, int(usage.total * 0.05)
            ):
                raise MapError("Free storage fell below the map download reserve.")
            with closing(sqlite3.connect(staging)) as database:
                database.execute("PRAGMA trusted_schema=OFF")
                database.execute("PRAGMA journal_mode=DELETE")
                database.execute("PRAGMA synchronous=FULL")
                database.execute("PRAGMA max_page_count=262144")
                if not database.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table'"
                ).fetchone():
                    database.executescript(SCHEMA)
                metadata = {"format": FORMAT, **summary}
                database.execute(
                    "INSERT OR REPLACE INTO metadata VALUES('manifest',?)", (json.dumps(metadata),)
                )
                cached: set[tuple[str, int, int, int]] = set()
                for kind, z, x, y, data, checksum in database.execute(
                    "SELECT kind,z,x,y,data,sha256 FROM tiles"
                ):
                    if hashlib.sha256(data).hexdigest() != checksum:
                        raise MapError("A staged tile is corrupt; prepare a new plan.")
                    cached.add((kind, z, x, y))
                missing: dict[tuple[int, int], list[tuple[str, int, int, int]]] = {}
                for kind, z, x, y, offset, length in plan["tiles"]:
                    key = (kind, z, x, y)
                    if key in cached:
                        continue
                    if length:
                        missing.setdefault((offset, length), []).append(key)
                    else:
                        database.execute(
                            "INSERT INTO tiles VALUES(?,?,?,?,?,?)",
                            (*key, b"", hashlib.sha256(b"").hexdigest()),
                        )
                        cached.add(key)
                database.commit()
                spans = sorted(missing)
                cursor = 0
                for start, size in merged_ranges(spans):
                    if self.cancel.is_set():
                        raise MapError("Map download paused; resume when ready.")
                    payload = source.read(start, size)
                    while (
                        cursor < len(spans) and spans[cursor][0] + spans[cursor][1] <= start + size
                    ):
                        offset, length = spans[cursor]
                        if offset < start:
                            raise MapError("The planned map ranges overlap incorrectly.")
                        content = payload[offset - start : offset - start + length]
                        validate_tile(content, summary["tile_compression"])
                        checksum = hashlib.sha256(content).hexdigest()
                        for key in missing[(offset, length)]:
                            database.execute(
                                "INSERT INTO tiles VALUES(?,?,?,?,?,?)", (*key, content, checksum)
                            )
                            cached.add(key)
                        cursor += 1
                    database.commit()
                    self._save(completed_tiles=len(cached), downloaded_bytes=source.received)
            self._save(
                state="verifying",
                detail="Verifying every tile and selecting the completed map pack…",
            )
            if self.cancel.is_set():
                raise MapError("Map download paused; resume when ready.")
            manifest = publish_pack(self.root, staging, identifier)
            self._save(
                state="complete",
                detail="World overview and regional maps are ready offline."
                if manifest.get("region")
                else "World overview is ready; add a regional map for local detail.",
                completed_tiles=manifest["tile_count"],
            )
        finally:
            source.close()

    def import_pack(self, source_path: Path) -> dict[str, Any]:
        self._claim()
        identifier = uuid.uuid4().hex
        self._start(state="verifying", id=identifier, detail="Verifying the imported map pack…")
        try:
            if (
                source_path.is_symlink()
                or not source_path.is_file()
                or source_path.stat().st_size > MAX_DOWNLOAD_BYTES
            ):
                raise MapError("Choose a regular regional map pack no larger than 1 GiB.")
            if shutil.disk_usage(self.work).free < source_path.stat().st_size + 1024**3:
                raise MapError("Not enough storage to import the map pack safely.")
            staged = self.work / f"{identifier}.sqlite"
            shutil.copyfile(source_path, staged)
            manifest = publish_pack(self.root, staged, identifier)
            self._save(
                state="complete", detail="Imported map pack verified and installed.", plan=None
            )
            return manifest
        except Exception:
            self._save(
                state="failed", detail="Map import failed; the previous map pack remains selected."
            )
            raise
        finally:
            self._release()
