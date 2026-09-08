"""Reserve installed databases for their selected packaged interpreter."""

from __future__ import annotations

import json
import sys
from pathlib import Path

STATE = Path("/var/lib/outpost")
CURRENT = Path("/opt/outpost/current")
PACKAGE = Path(__file__).parents[1]


def require_store_owner(database: Path) -> None:
    """Check before SQLite can create, migrate or write the installed store.

    Standard state paths are protected even before their first ownership marker.
    The installer also marks custom database paths. This is an accidental-use
    guard, not a security boundary against an administrator changing the files.
    """
    path = database.resolve()
    marker = path.with_name(path.name + ".deployment.json")
    current: Path | None = CURRENT if path.is_relative_to(STATE.resolve()) else None
    try:
        with marker.open("rb") as stream:
            content = stream.read(4097)
        if len(content) > 4096:
            raise ValueError("oversized ownership record")
        record = json.loads(content)
        if (
            not isinstance(record, dict)
            or record.get("format") != 1
            or record.get("database") != str(path)
            or not isinstance(record.get("current"), str)
            or not Path(record["current"]).is_absolute()
        ):
            raise ValueError("invalid ownership record")
        current = Path(record["current"])
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        raise RuntimeError(
            "Installed database ownership is unreadable; repair the deployment record "
            "before opening this store."
        ) from None
    if current is None:
        return
    try:
        release = current.resolve(strict=True)
        owned = (
            current.is_symlink()
            and Path(sys.prefix).resolve() == release
            and PACKAGE.resolve().is_relative_to(release / "lib")
        )
    except (OSError, RuntimeError):
        owned = False
    if not owned:
        raise RuntimeError(
            "This database belongs to the installed Outpost release. Use deploy/update.sh "
            "to update the service; use a separate store.path outside /var/lib/outpost "
            "for development. No database migration was performed."
        )
