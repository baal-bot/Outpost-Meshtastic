#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RUFF=${OUTPOST_RUFF:-$PROJECT_DIR/.venv/bin/ruff}

[ -x "$RUFF" ] || {
  echo "Outpost pre-push: Ruff not found at $RUFF; set OUTPOST_RUFF if needed." >&2
  exit 1
}

cd "$PROJECT_DIR"
"$RUFF" format --check src tests \
  tools/build_release_metadata.py tools/check_capabilities.py tools/check_ci_evidence.py \
  tools/check_static_markup.py tools/verify_release.py deploy/configure.py deploy/render_avahi.py
echo "Outpost pre-push formatting gate passed."
python tools/check_static_markup.py
