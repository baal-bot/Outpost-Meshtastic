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
"$SMOKE_DIR/venv/bin/pip" install --upgrade pip
"$SMOKE_DIR/venv/bin/pip" install -c "$PROJECT_DIR/requirements.lock" "$WHEEL[radio]"
"$SMOKE_DIR/venv/bin/pip" check
"$SMOKE_DIR/venv/bin/python" "$PROJECT_DIR/tools/check_dependency_lock.py" --check-installed
"$SMOKE_DIR/venv/bin/python" - "$WHEEL" <<'PY'
import sys
import zipfile

wheel = sys.argv[1]
required = {
    "outpost/__main__.py",
    "outpost/diagnostics.py",
    "outpost/onboarding.py",
    "outpost/setup_token.py",
    "outpost/store/migrations/0000_core.sql",
    "outpost/store/migrations/0104_digests.sql",
    "outpost/store/migrations/0139_web_setup_secret.sql",
    "outpost/web/static/Figtree-Variable.ttf",
    "outpost/web/static/a11y.js",
    "outpost/web/static/app.js",
    "outpost/web/static/base.css",
    "outpost/web/static/components.css",
    "outpost/web/static/favicon.svg",
    "outpost/web/static/index.html",
    "outpost/web/static/layout.css",
    "outpost/web/static/nav.js",
    "outpost/web/static/radio.html",
    "outpost/web/static/theme-boot.js",
    "outpost/web/static/theme.js",
}
with zipfile.ZipFile(wheel) as archive:
    names = set(archive.namelist())
    entry_points_name = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
    entry_points = archive.read(entry_points_name).decode()
missing = sorted(required - names)
if missing:
    raise SystemExit(f"wheel is missing runtime files: {', '.join(missing)}")
for command in ("outpost-diagnostics", "outpost-onboarding", "outpost-setup-token"):
    if command not in entry_points:
        raise SystemExit(f"wheel is missing console command: {command}")
PY

echo "Package smoke test passed: $WHEEL"
