#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RUFF=${OUTPOST_RUFF:-$PROJECT_DIR/.venv/bin/ruff}
PYTHON=${OUTPOST_PYTHON:-$PROJECT_DIR/.venv/bin/python}

[ -x "$RUFF" ] || {
  echo "Outpost pre-push: Ruff not found at $RUFF; set OUTPOST_RUFF if needed." >&2
  exit 1
}
[ -x "$PYTHON" ] || {
  echo "Outpost pre-push: Python not found at $PYTHON; set OUTPOST_PYTHON if needed." >&2
  exit 1
}

cd "$PROJECT_DIR"
"$RUFF" format --check src tests \
  tools/build_release_metadata.py tools/check_capabilities.py tools/check_commands.py \
  tools/check_ci_evidence.py tools/check_dependency_lock.py tools/check_mypy_ratchet.py \
  tools/check_static_markup.py \
  tools/pytest_evidence_plugin.py tools/verify_release.py deploy/configure.py \
  deploy/render_avahi.py
echo "Outpost pre-push formatting gate passed."
"$PYTHON" tools/check_mypy_ratchet.py
"$PYTHON" tools/check_commands.py --check
"$PYTHON" tools/check_static_markup.py
