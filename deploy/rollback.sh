#!/bin/sh
set -eu

PREFIX=${OUTPOST_PREFIX:-/opt/outpost}
CONFIG_DIR=${OUTPOST_CONFIG_DIR:-/etc/outpost}
SERVICE_NAME=${OUTPOST_SERVICE_NAME:-outpost.service}
HEALTH_URL=${OUTPOST_HEALTH_URL:-}
RECOVERY_HELPER=${OUTPOST_RECOVERY_HELPER:-/usr/local/lib/outpost/release_recovery.py}
HEALTH_HELPER=${OUTPOST_HEALTH_HELPER:-/usr/local/lib/outpost/health_probe.sh}
CURRENT=$PREFIX/current
PREVIOUS=$PREFIX/previous

fail() { echo "Outpost rollback: $*" >&2; exit 1; }
json_field() {
  printf '%s' "$1" | python3 -c "import json,sys; print(json.load(sys.stdin)[$2])"
}
select_release() {
  destination=$1
  ln -sfn "$destination" "$CURRENT.next" || return 1
  mv -Tf "$CURRENT.next" "$CURRENT"
}

[ "$(id -u)" -eq 0 ] || fail "run as root: sudo $0"
for command in python3 systemctl ln mv readlink curl dirname chown chmod mktemp; do
  command -v "$command" >/dev/null 2>&1 || fail "missing required command: $command"
done
[ -f "$RECOVERY_HELPER" ] || fail "recovery helper is missing: $RECOVERY_HELPER"
[ -f "$HEALTH_HELPER" ] || fail "health helper is missing: $HEALTH_HELPER"
. "$HEALTH_HELPER"
[ -L "$CURRENT" ] && [ -L "$PREVIOUS" ] || fail "no previous versioned release is available"

old=$(readlink "$CURRENT")
target=$(readlink "$PREVIOUS")
[ -x "$old/bin/python" ] || fail "current release is incomplete: $old"
[ -x "$target/bin/python" ] || fail "previous release is incomplete: $target"
metadata=$old/rollback.json
[ -f "$metadata" ] || fail "current release has no matching rollback metadata"

# Verify live data, the target schema capacity, release pair, and backup before downtime.
if ! PLAN=$(python3 "$RECOVERY_HELPER" plan \
  --metadata "$metadata" --current-release "$old" --target-release "$target"); then
  fail "compatibility dry-run failed; the running release was not changed"
fi
ACTION=$(json_field "$PLAN" '"action"')
DATABASE_PATH=$(json_field "$PLAN" '"database"')
BACKUP_PATH=$(json_field "$PLAN" '"backup"')
TARGET_SCHEMA_CAP=$(json_field "$PLAN" '"target_schema_cap"')
CURRENT_SCHEMA_CAP=$(json_field "$PLAN" '"current_schema_cap"')

outpost_configure_health "$target/bin/python" "$CONFIG_DIR/config.yaml"

# Catch malformed URLs, unavailable curl features, and TLS-mode mistakes while
# the current release is still running and before any code or data changes.
if outpost_probe_url "$HEALTH_URL" >/dev/null 2>&1; then
  preflight_status=0
else
  preflight_status=$?
fi
if [ "$preflight_status" -ne 0 ] && \
  ! outpost_probe_status_is_retryable "$preflight_status"; then
  fail "health probe configuration failed before rollback (curl exit $preflight_status)"
fi

echo "Rollback dry-run passed: $ACTION from $old to $target"
systemctl stop "$SERVICE_NAME" || fail "could not stop $SERVICE_NAME; no changes were made"

SAFETY_PATH=$(mktemp "$(dirname -- "$BACKUP_PATH")/pre-manual-rollback-XXXXXXXX.db")
if ! python3 "$RECOVERY_HELPER" snapshot \
  --source "$DATABASE_PATH" --destination "$SAFETY_PATH" >/dev/null; then
  systemctl start "$SERVICE_NAME" || true
  fail "could not create the pre-rollback safety snapshot; current release restarted"
fi
chown outpost:outpost "$SAFETY_PATH"
chmod 0640 "$SAFETY_PATH"

if [ "$ACTION" = restore ]; then
  if ! python3 "$RECOVERY_HELPER" restore --source "$BACKUP_PATH" \
    --destination "$DATABASE_PATH" --maximum-schema "$TARGET_SCHEMA_CAP" >/dev/null; then
    systemctl start "$SERVICE_NAME" || true
    fail "could not restore the compatible snapshot; current release restarted"
  fi
  chown outpost:outpost "$DATABASE_PATH"
  chmod 0640 "$DATABASE_PATH"
  echo "Restored verified schema-compatible snapshot; safety copy: $SAFETY_PATH"
fi

if ! select_release "$target"; then
  python3 "$RECOVERY_HELPER" restore --source "$SAFETY_PATH" \
    --destination "$DATABASE_PATH" --maximum-schema "$CURRENT_SCHEMA_CAP" >/dev/null || true
  chown outpost:outpost "$DATABASE_PATH" || true
  chmod 0640 "$DATABASE_PATH" || true
  systemctl start "$SERVICE_NAME" || true
  fail "could not select the previous release; original code and data were retained"
fi
rm -f "$PREVIOUS"
ln -s "$old" "$PREVIOUS"
systemctl start "$SERVICE_NAME" || true
if outpost_wait_for_health; then
  if ! OUTPOST_CONFIG="$CONFIG_DIR/config.yaml" "$old/bin/python" - \
    "$PREFIX/releases" "$CURRENT" "$PREVIOUS" <<'PY'
import sys
from pathlib import Path

from outpost.config import load_config
from outpost.store import Database
from outpost.store.backups import BackupService, prune_release_directories

config = load_config()
BackupService(Database(config.store.path), config.store.backup).rotate()
prune_release_directories(
    Path(sys.argv[1]),
    Path(sys.argv[2]),
    Path(sys.argv[3]),
    config.store.backup.superseded_release_keep,
)
PY
  then
    echo "Rollback recovery retention could not be completed." >&2
  fi
  echo "Rollback is healthy. Active release: $target"
  exit 0
else
  rollback_health_status=$?
fi

echo "Rollback health check failed; restoring the original known-good state." >&2
systemctl stop "$SERVICE_NAME" || true
select_release "$old" || fail "could not reselect original release $old"
rm -f "$PREVIOUS"
ln -s "$target" "$PREVIOUS"
if [ "$ACTION" = restore ]; then
  python3 "$RECOVERY_HELPER" restore --source "$SAFETY_PATH" \
    --destination "$DATABASE_PATH" --maximum-schema "$CURRENT_SCHEMA_CAP" >/dev/null \
    || fail "original code restored, but automatic data recovery failed; use $SAFETY_PATH"
  chown outpost:outpost "$DATABASE_PATH"
  chmod 0640 "$DATABASE_PATH"
fi
systemctl start "$SERVICE_NAME" || fail "original state restored but service did not start"
if [ "$rollback_health_status" -eq 2 ]; then
  fail "rollback probe could not be performed; original state is running but health is unverified"
fi
if outpost_wait_for_health; then
  fail "rollback failed health verification; original code and data are running"
fi
fail "rollback and original-state health verification both failed"
