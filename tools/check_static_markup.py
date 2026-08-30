#!/usr/bin/env python3
"""Reject common unsafe dynamic-markup patterns in the static dashboard."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

STATIC_ROOT = Path(__file__).parents[1] / "src" / "outpost" / "web" / "static"
SINK = re.compile(r"(?:\.innerHTML\s*=|\.insertAdjacentHTML\s*\()")
DYNAMIC = re.compile(r"\$\{")
DIRECT_PROPERTY = re.compile(r"\$\{\s*([A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$][\w$]*)+)\s*\}")
UNQUOTED_ATTRIBUTE = re.compile(r"\b[\w:-]+\s*=\s*\$\{")
URL_ATTRIBUTE = re.compile(r'\b(?:href|src|action)\s*=\s*"([^"`]*\$\{[^"`]*)"')
ESCAPER_IMPORT = re.compile(
    r'import\s*\{[^}]*\bescapeHtml\b[^}]*\}\s*from\s*["\']/ui-primitives\.js["\']'
)
NUMERIC_FIELDS = {
    "ack_count",
    "broadcast_count",
    "byte_len",
    "checkins",
    "confirm_count",
    "count_24h",
    "delivered",
    "dispute_count",
    "escalation_stage",
    "frames_24h",
    "id",
    "incidents",
    "index",
    "incident_ref",
    "last_heard_snr",
    "local_ref",
    "mail",
    "message_count",
    "channel",
    "pending",
    "post_count",
    "recovered",
    "retries",
    "rx_counter",
    "seq",
    "slot",
    "stage_total",
    "status",
    "stored_bytes",
    "hidden",
    "rejected_24h",
    "thread_count",
    "tx_counter",
    "unread_count",
}


def _inside_escaper(region: str, position: int) -> bool:
    starts = [region.rfind(name, 0, position) for name in ("safe(", "escapeHtml(", "escapeMap(")]
    start = max(starts)
    if start < 0:
        return False
    opening = region.find("(", start)
    return region[opening:position].count("(") > region[opening:position].count(")")


def _markup_regions(source: str) -> list[str]:
    """Return conservative sink regions, ending at a top-level statement boundary."""
    regions: list[str] = []
    for match in SINK.finditer(source):
        start = match.start()
        index = match.end()
        stack: list[str] = []
        mode = "code"
        escaped = False
        while index < len(source):
            character = source[index]
            following = source[index : index + 2]
            if escaped:
                escaped = False
            elif character == "\\" and mode in {"single", "double", "template"}:
                escaped = True
            elif mode == "single":
                if character == "'":
                    mode = "code"
            elif mode == "double":
                if character == '"':
                    mode = "code"
            elif mode == "template":
                if following == "${":
                    stack.append("template-expression")
                    mode = "code"
                    index += 1
                elif character == "`":
                    mode = "code"
            elif following == "//":
                newline = source.find("\n", index + 2)
                index = len(source) if newline == -1 else newline
                continue
            elif following == "/*":
                close = source.find("*/", index + 2)
                index = len(source) if close == -1 else close + 1
            elif character == "'":
                mode = "single"
            elif character == '"':
                mode = "double"
            elif character == "`":
                mode = "template"
            elif character in "([":
                stack.append(character)
            elif character == "{":
                stack.append(character)
            elif character in ")]}":
                if stack:
                    opened = stack.pop()
                    if opened == "template-expression":
                        mode = "template"
            elif character == ";" and not stack:
                index += 1
                break
            index += 1
        regions.append(source[start:index])
    return regions


def markup_violations(source: str, *, name: str = "static.js") -> list[str]:
    violations: list[str] = []
    regions = [region for region in _markup_regions(source) if DYNAMIC.search(region)]
    if regions and not ESCAPER_IMPORT.search(source):
        violations.append(f"{name}: dynamic HTML sink does not import shared escapeHtml")
    for region in regions:
        for property_match in DIRECT_PROPERTY.finditer(region):
            property_name = property_match.group(1)
            field = property_name.rsplit(".", 1)[-1]
            if (
                field == "length"
                or field in NUMERIC_FIELDS
                or _inside_escaper(region, property_match.start())
            ):
                continue
            violations.append(
                f"{name}: raw property interpolation in dynamic HTML: {property_match.group(0)}"
            )
        if UNQUOTED_ATTRIBUTE.search(region):
            violations.append(f"{name}: unquoted dynamic HTML attribute")
        for url_match in URL_ATTRIBUTE.finditer(region):
            value = url_match.group(1)
            if "safeLocalHref(" in value or (value.startswith("/") and not value.startswith("//")):
                continue
            violations.append(f"{name}: dynamic URL attribute is not constrained to a local URL")
    return violations


def check_tree(root: Path = STATIC_ROOT) -> list[str]:
    violations: list[str] = []
    for path in sorted(root.glob("*.js")):
        violations.extend(markup_violations(path.read_text(), name=path.name))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=STATIC_ROOT)
    args = parser.parse_args()
    violations = check_tree(args.root)
    if violations:
        print("\n".join(violations))
        return 1
    print("Static markup safety check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
