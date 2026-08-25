#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname -- "$SCRIPT_DIR")
SMOKE_DIR=$(mktemp -d)
trap 'rm -rf "$SMOKE_DIR"' EXIT HUP INT TERM

python3 -m pip wheel --no-deps --wheel-dir "$SMOKE_DIR" "$PROJECT_DIR"
WHEEL=$(find "$SMOKE_DIR" -maxdepth 1 -name 'outpost-*.whl' -print -quit)
if [ -z "$WHEEL" ]; then
  echo "Outpost wheel was not created" >&2
  exit 1
fi

python3 -m venv "$SMOKE_DIR/venv"
"$SMOKE_DIR/venv/bin/pip" install --no-deps "$WHEEL"
"$SMOKE_DIR/venv/bin/python" - "$WHEEL" <<'PY'
import sys
import zipfile

wheel = sys.argv[1]
required = {
    "outpost/__main__.py",
    "outpost/store/migrations/0000_core.sql",
    "outpost/store/migrations/0104_digests.sql",
    "outpost/web/static/Figtree-Variable.ttf",
    "outpost/web/static/app.js",
    "outpost/web/static/favicon.svg",
    "outpost/web/static/index.html",
    "outpost/web/static/nav.js",
    "outpost/web/static/radio.html",
    "outpost/web/static/theme-corrections.css",
    "outpost/web/static/theme.js",
}
with zipfile.ZipFile(wheel) as archive:
    names = set(archive.namelist())
missing = sorted(required - names)
if missing:
    raise SystemExit(f"wheel is missing runtime files: {', '.join(missing)}")
PY

echo "Package smoke test passed: $WHEEL"
