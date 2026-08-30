from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def csv_safe(value: Any) -> Any:
    """Neutralize spreadsheet formulas without changing ordinary CSV values."""
    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def csv_safe_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: csv_safe(value) for key, value in row.items()}
