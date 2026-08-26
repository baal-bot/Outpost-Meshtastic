#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname -- "$SCRIPT_DIR")
TEST_ENV=${OUTPOST_TEST_VENV:-$PROJECT_DIR/.venv}
WITH_BROWSER=0

fail() { echo "Outpost test-host setup: $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: ./deploy/install-test-host.sh [--with-browser]

Creates or updates a checkout-local acceptance-test environment. Run the
production installer separately with sudo; this helper must run as the normal
checkout owner and never changes /opt/outpost or restarts the service.

  --with-browser  install the Chromium runtime used by Playwright UI tests
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --with-browser) WITH_BROWSER=1 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
  shift
done

[ "$(id -u)" -ne 0 ] || fail "run as the normal checkout user, not root or sudo"
command -v python3 >/dev/null 2>&1 || fail "missing required command: python3"
python3 -c 'import sys; raise SystemExit(not ((3, 12) <= sys.version_info < (3, 14)))' || \
  fail "Python 3.12 or 3.13 is required"
python3 -m venv --help >/dev/null 2>&1 || \
  fail "Python venv support is missing; install python3-venv"
[ -f "$PROJECT_DIR/pyproject.toml" ] || fail "run this helper from an Outpost checkout"

if [ -e "$TEST_ENV" ] && [ ! -f "$TEST_ENV/pyvenv.cfg" ]; then
  fail "$TEST_ENV exists but is not a Python virtual environment"
fi

echo "Preparing checkout-local acceptance environment: $TEST_ENV"
python3 -m venv "$TEST_ENV"
"$TEST_ENV/bin/pip" install --upgrade pip
"$TEST_ENV/bin/pip" install -e "$PROJECT_DIR[dev,radio]"
"$TEST_ENV/bin/pip" check
"$TEST_ENV/bin/python" -c \
  'import meshtastic, pytest, outpost; print("Validated Outpost acceptance environment", outpost.__version__)'

if [ "$WITH_BROWSER" -eq 1 ]; then
  "$TEST_ENV/bin/python" -m playwright install chromium
fi

cat <<EOF

Acceptance host is ready.
  Tests:      $TEST_ENV/bin/pytest
  Lint:       $TEST_ENV/bin/ruff check src tests
  Production: /opt/outpost/current (unchanged)

After pulling changes, rerun this helper for test tools and run
sudo ./deploy/install.sh separately when you intend to update the service.
EOF
