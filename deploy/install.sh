#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname -- "$SCRIPT_DIR")
PREFIX=${OUTPOST_PREFIX:-/opt/outpost}
STATE_DIR=${OUTPOST_STATE_DIR:-/var/lib/outpost}
CONFIG_DIR=${OUTPOST_CONFIG_DIR:-/etc/outpost}
SERVICE_NAME=${OUTPOST_SERVICE_NAME:-outpost.service}
HEALTH_URL=${OUTPOST_HEALTH_URL:-}
NONINTERACTIVE=${OUTPOST_NONINTERACTIVE:-0}

fail() { echo "Outpost install: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "run as root: sudo $0"
for command in python3 getent groupadd useradd usermod install systemctl ln mv readlink curl; do
  command -v "$command" >/dev/null 2>&1 || fail "missing required command: $command"
done
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' || fail "Python 3.12 or newer is required"
python3 -m venv --help >/dev/null 2>&1 || fail "Python venv support is missing; install python3-venv"

if command -v git >/dev/null 2>&1 && git -C "$PROJECT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  REVISION=$(git -C "$PROJECT_DIR" rev-parse --short=12 HEAD)
else
  REVISION=$(date -u +%Y%m%dT%H%M%SZ)
fi
RELEASE_ID=$(date -u +%Y%m%dT%H%M%SZ)-$REVISION
RELEASE_DIR=$PREFIX/releases/$RELEASE_ID
CURRENT_LINK=$PREFIX/current
PREVIOUS_LINK=$PREFIX/previous

getent group outpost >/dev/null 2>&1 || groupadd --system outpost
getent passwd outpost >/dev/null 2>&1 || useradd --system --gid outpost --home "$STATE_DIR" --shell /usr/sbin/nologin outpost
getent group dialout >/dev/null 2>&1 && usermod -a -G dialout outpost
install -d -m 0755 "$PREFIX" "$PREFIX/releases"
install -d -m 0750 -o outpost -g outpost "$STATE_DIR" "$STATE_DIR/.data" "$STATE_DIR/backups" /var/log/outpost
install -d -m 0750 -o root -g outpost "$CONFIG_DIR"

echo "Staging Outpost release $RELEASE_ID"
python3 -m venv "$RELEASE_DIR"
"$RELEASE_DIR/bin/pip" install --upgrade pip
"$RELEASE_DIR/bin/pip" install -c "$PROJECT_DIR/requirements.lock" "$PROJECT_DIR[radio]"
"$RELEASE_DIR/bin/pip" check
"$RELEASE_DIR/bin/python" -c 'import outpost; print("Validated Outpost", outpost.__version__)'

install -m 0640 -o root -g outpost "$PROJECT_DIR/config/config.example.yaml" "$CONFIG_DIR/config.yaml.dist"
install -m 0640 -o root -g outpost "$PROJECT_DIR/config/intents.yaml" "$CONFIG_DIR/intents.yaml.dist"
NEW_CONFIG=0
if [ ! -e "$CONFIG_DIR/config.yaml" ]; then
  install -m 0640 -o root -g outpost "$PROJECT_DIR/config/config.example.yaml" "$CONFIG_DIR/config.yaml"
  NEW_CONFIG=1
else
  echo "Preserved $CONFIG_DIR/config.yaml"
fi
if [ ! -e "$CONFIG_DIR/intents.yaml" ]; then
  install -m 0640 -o root -g outpost "$PROJECT_DIR/config/intents.yaml" "$CONFIG_DIR/intents.yaml"
fi
if [ "$NEW_CONFIG" -eq 1 ] && [ "$NONINTERACTIVE" != 1 ] && [ -t 0 ]; then
  "$RELEASE_DIR/bin/python" "$SCRIPT_DIR/configure.py" --config "$CONFIG_DIR/config.yaml"
  chown root:outpost "$CONFIG_DIR/config.yaml"
  chmod 0640 "$CONFIG_DIR/config.yaml"
else
  echo "First-run wizard skipped; edit $CONFIG_DIR/config.yaml before production use."
fi
OUTPOST_CONFIG="$CONFIG_DIR/config.yaml" "$RELEASE_DIR/bin/python" -c 'from outpost.config import load_config; load_config(); print("Configuration validated")'
if [ -z "$HEALTH_URL" ]; then
  HEALTH_URL=$(OUTPOST_CONFIG="$CONFIG_DIR/config.yaml" "$RELEASE_DIR/bin/python" - <<'PY'
from outpost.config import load_config
print(f"http://127.0.0.1:{load_config().web.port}/api/v1/health")
PY
  )
fi

if [ ! -f "$STATE_DIR/.data/tiles/manifest.json" ] && \
  "$RELEASE_DIR/bin/python" -c 'import sys,yaml; d=yaml.safe_load(open(sys.argv[1])) or {}; raise SystemExit(0 if d.get("node",{}).get("location") else 1)' "$CONFIG_DIR/config.yaml"; then
  echo "Installing bounded offline map pack for node.location"
  if "$RELEASE_DIR/bin/python" "$PROJECT_DIR/tools/build_tile_pack.py" \
    --config "$CONFIG_DIR/config.yaml" --output "$STATE_DIR/.data/tiles"; then
    chown -R outpost:outpost "$STATE_DIR/.data/tiles"
  else
    echo "Offline map download failed; installation will continue with online maps." >&2
  fi
else
  echo "Existing offline map preserved, or map setup deferred until node.location is configured."
fi

BACKUP_PATH=
DATABASE_PATH=$(OUTPOST_CONFIG="$CONFIG_DIR/config.yaml" "$RELEASE_DIR/bin/python" - <<'PY'
from outpost.config import load_config
print(load_config().store.path)
PY
)
if [ -f "$DATABASE_PATH" ]; then
  BACKUP_PATH="$STATE_DIR/backups/pre-upgrade-$RELEASE_ID.db"
  "$RELEASE_DIR/bin/python" - "$DATABASE_PATH" "$BACKUP_PATH" <<'PY'
import sqlite3, sys
source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
with target:
    source.backup(target)
assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
source.close(); target.close()
PY
  chown outpost:outpost "$BACKUP_PATH"
  chmod 0640 "$BACKUP_PATH"
  echo "Created verified pre-upgrade backup: $BACKUP_PATH"
fi

OLD_TARGET=$(readlink "$CURRENT_LINK" 2>/dev/null || true)
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK.next"
mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"
install -m 0644 "$SCRIPT_DIR/outpost.service" /etc/systemd/system/outpost.service
install -m 0755 "$SCRIPT_DIR/rollback.sh" /usr/local/sbin/outpost-rollback
printf '%s\n' "$PROJECT_DIR" > "$CONFIG_DIR/install-source"
chmod 0640 "$CONFIG_DIR/install-source"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

healthy=0
attempt=0
while [ "$attempt" -lt 30 ]; do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then healthy=1; break; fi
  attempt=$((attempt + 1)); sleep 2
done
if [ "$healthy" -ne 1 ]; then
  echo "New release failed health verification; rolling back." >&2
  systemctl stop "$SERVICE_NAME" || true
  if [ -n "$OLD_TARGET" ]; then
    ln -sfn "$OLD_TARGET" "$CURRENT_LINK.next"
    mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"
  fi
  if [ -n "$OLD_TARGET" ] && [ -n "$BACKUP_PATH" ] && [ -f "$BACKUP_PATH" ]; then
    cp -a "$DATABASE_PATH" "$DATABASE_PATH.failed-$RELEASE_ID" 2>/dev/null || true
    "$OLD_TARGET/bin/python" - "$BACKUP_PATH" "$DATABASE_PATH" <<'PY'
import os, sqlite3, sys
temporary = sys.argv[2] + ".restore"
if os.path.exists(temporary):
    os.unlink(temporary)
source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(temporary)
with target:
    source.backup(target)
assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
source.close(); target.close()
for suffix in ("-wal", "-shm"):
    try:
        os.unlink(sys.argv[2] + suffix)
    except FileNotFoundError:
        pass
os.replace(temporary, sys.argv[2])
PY
    chown outpost:outpost "$DATABASE_PATH"
  fi
  [ -n "$OLD_TARGET" ] && systemctl restart "$SERVICE_NAME" || true
  fail "release $RELEASE_ID did not become healthy; previous release restored"
fi
if [ -n "$OLD_TARGET" ]; then
  rm -f "$PREVIOUS_LINK"
  ln -s "$OLD_TARGET" "$PREVIOUS_LINK"
fi
printf '%s\n' "$RELEASE_ID" > "$PREFIX/installed-release"
echo "Outpost $RELEASE_ID is healthy at $HEALTH_URL"
echo "Rollback command: sudo outpost-rollback"
