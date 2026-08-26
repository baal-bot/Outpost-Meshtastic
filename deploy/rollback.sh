#!/bin/sh
set -eu

PREFIX=${OUTPOST_PREFIX:-/opt/outpost}
CONFIG_DIR=${OUTPOST_CONFIG_DIR:-/etc/outpost}
SERVICE_NAME=${OUTPOST_SERVICE_NAME:-outpost.service}
HEALTH_URL=${OUTPOST_HEALTH_URL:-}
CURRENT=$PREFIX/current
PREVIOUS=$PREFIX/previous

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo $0" >&2; exit 1; }
[ -L "$CURRENT" ] && [ -L "$PREVIOUS" ] || { echo "No previous versioned Outpost release is available." >&2; exit 1; }
old=$(readlink "$CURRENT")
target=$(readlink "$PREVIOUS")
if [ -z "$HEALTH_URL" ]; then
  HEALTH_URL=$(OUTPOST_CONFIG="$CONFIG_DIR/config.yaml" "$target/bin/python" - <<'PY'
from outpost.config import load_config
print(f"http://127.0.0.1:{load_config().web.port}/api/v1/health")
PY
  )
fi
echo "Rolling back code from $old to $target"
systemctl stop "$SERVICE_NAME"
ln -sfn "$target" "$CURRENT.next"
mv -Tf "$CURRENT.next" "$CURRENT"
rm -f "$PREVIOUS"
ln -s "$old" "$PREVIOUS"
systemctl start "$SERVICE_NAME"
attempt=0
while [ "$attempt" -lt 30 ]; do
  curl -fsS "$HEALTH_URL" >/dev/null 2>&1 && { echo "Rollback is healthy. Database was not changed."; exit 0; }
  attempt=$((attempt + 1)); sleep 2
done
echo "Rolled-back code did not become healthy. Restore a schema-compatible pre-upgrade backup." >&2
exit 1
