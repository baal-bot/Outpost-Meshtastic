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
for command in python3 getent groupadd useradd usermod install systemctl udevadm ln mv readlink curl; do
  command -v "$command" >/dev/null 2>&1 || fail "missing required command: $command"
done
python3 -c 'import sys; raise SystemExit(not ((3, 12) <= sys.version_info < (3, 14)))' || \
  fail "Python 3.12 or 3.13 is required"
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
OLD_TARGET=$(readlink "$CURRENT_LINK" 2>/dev/null || true)

getent group outpost >/dev/null 2>&1 || groupadd --system outpost
getent group outpost-sdr >/dev/null 2>&1 || groupadd --system outpost-sdr
getent passwd outpost >/dev/null 2>&1 || useradd --system --gid outpost --home "$STATE_DIR" --shell /usr/sbin/nologin outpost
getent group dialout >/dev/null 2>&1 && usermod -a -G dialout outpost
usermod -a -G outpost-sdr outpost
install -d -m 0755 "$PREFIX" "$PREFIX/releases"
install -d -m 0750 -o outpost -g outpost "$STATE_DIR" "$STATE_DIR/.data" "$STATE_DIR/backups" /var/log/outpost
install -d -m 0750 -o root -g outpost "$CONFIG_DIR"
install -m 0644 "$SCRIPT_DIR/70-outpost-rtl-sdr.rules" /etc/udev/rules.d/70-outpost-rtl-sdr.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --action=change

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
SAME_ENABLED=$(OUTPOST_CONFIG="$CONFIG_DIR/config.yaml" "$RELEASE_DIR/bin/python" - <<'PY'
from outpost.config import load_config
print("1" if load_config().modules.env.enabled and load_config().env.same.enabled else "0")
PY
)
AI_PROVIDER=$(OUTPOST_CONFIG="$CONFIG_DIR/config.yaml" "$RELEASE_DIR/bin/python" - <<'PY'
from outpost.config import load_config
config = load_config()
print(config.ai.provider if config.modules.ai.enabled else "disabled")
PY
)
if [ "$SAME_ENABLED" -eq 1 ]; then
  echo "Installing the receive-only RTL-SDR/SAME toolchain"
  for command in apt-get sha256sum uname mktemp; do
    command -v "$command" >/dev/null 2>&1 || fail "SAME receiver requires: $command"
  done
  if ! command -v rtl_fm >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends rtl-sdr
  fi
  SAMEDEC_VERSION=0.4.2
  case "$(uname -m)" in
    aarch64|arm64)
      SAMEDEC_TARGET=aarch64-unknown-linux-gnu
      SAMEDEC_SHA256=1f4fabefac5e246bbe26671fb7dcf9e3b677651f1eab2cbbfdbc37c014f499ed
      ;;
    armv7l|armv7*)
      SAMEDEC_TARGET=armv7-unknown-linux-gnueabihf
      SAMEDEC_SHA256=2a2a7108b633fa4c8afbead1674416f08ab70ed1c7fcdf17b167a228d2734140
      ;;
    x86_64|amd64)
      SAMEDEC_TARGET=x86_64-unknown-linux-gnu
      SAMEDEC_SHA256=355168cf3658d73c4363d94d8652da7821f0f282f67ec7ec3cb0cf24a36e206e
      ;;
    *) fail "samedec $SAMEDEC_VERSION has no supported build for $(uname -m)" ;;
  esac
  SAMEDEC_TEMP=$(mktemp /tmp/outpost-samedec.XXXXXX)
  trap 'rm -f "$SAMEDEC_TEMP"' EXIT HUP INT TERM
  curl -fL --proto '=https' --tlsv1.2 \
    "https://github.com/cbs228/sameold/releases/download/samedec-$SAMEDEC_VERSION/samedec-$SAMEDEC_TARGET" \
    -o "$SAMEDEC_TEMP"
  printf '%s  %s\n' "$SAMEDEC_SHA256" "$SAMEDEC_TEMP" | sha256sum -c -
  install -m 0755 "$SAMEDEC_TEMP" /usr/local/bin/samedec
  rm -f "$SAMEDEC_TEMP"
  trap - EXIT HUP INT TERM
  rtl_fm -h >/dev/null 2>&1 || true
  samedec --version
fi
if [ "$AI_PROVIDER" = hailo ]; then
  command -v hailo-ollama >/dev/null 2>&1 || fail \
    "Hailo AI requires hailo-h10-all and hailo-gen-ai-model-zoo; see docs/INSTALLATION.md"
  [ -e /dev/hailo0 ] || fail \
    "Hailo-10H is not ready at /dev/hailo0; install its runtime and reboot first"
  install -m 0644 "$SCRIPT_DIR/hailo-ollama.service" \
    /etc/systemd/system/hailo-ollama.service
  chown -R outpost:outpost /usr/share/hailo-ollama/models
fi
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
PRE_UPGRADE_SCHEMA=
PREVIOUS_SCHEMA_CAP=
UPGRADE_SCHEMA_CAP=$("$RELEASE_DIR/bin/python" - <<'PY'
from pathlib import Path
import outpost.store
migrations = Path(outpost.store.__file__).parent / "migrations"
print(max(int(path.name[:4]) for path in migrations.glob("[0-9][0-9][0-9][0-9]_*.sql")))
PY
)
DATABASE_PATH=$(OUTPOST_CONFIG="$CONFIG_DIR/config.yaml" "$RELEASE_DIR/bin/python" - <<'PY'
from outpost.config import load_config
print(load_config().store.path)
PY
)
if [ -f "$DATABASE_PATH" ]; then
  BACKUP_PATH="$STATE_DIR/backups/pre-upgrade-$RELEASE_ID.db"
  python3 "$SCRIPT_DIR/release_recovery.py" snapshot \
    --source "$DATABASE_PATH" --destination "$BACKUP_PATH" >/dev/null
  PRE_UPGRADE_SCHEMA=$(python3 "$SCRIPT_DIR/release_recovery.py" schema \
    --database "$BACKUP_PATH")
  chown outpost:outpost "$BACKUP_PATH"
  chmod 0640 "$BACKUP_PATH"
  echo "Created verified pre-upgrade backup: $BACKUP_PATH"
fi

if [ -n "$OLD_TARGET" ] && [ -n "$BACKUP_PATH" ]; then
  PREVIOUS_SCHEMA_CAP=$("$OLD_TARGET/bin/python" - <<'PY'
from pathlib import Path
import outpost.store
migrations = Path(outpost.store.__file__).parent / "migrations"
print(max(int(path.name[:4]) for path in migrations.glob("[0-9][0-9][0-9][0-9]_*.sql")))
PY
  )
  python3 "$SCRIPT_DIR/release_recovery.py" record \
    --output "$RELEASE_DIR/rollback.json" \
    --upgrade-release "$RELEASE_DIR" --previous-release "$OLD_TARGET" \
    --database "$DATABASE_PATH" --backup "$BACKUP_PATH" \
    --pre-upgrade-schema "$PRE_UPGRADE_SCHEMA" \
    --previous-schema-cap "$PREVIOUS_SCHEMA_CAP" \
    --upgrade-schema-cap "$UPGRADE_SCHEMA_CAP" >/dev/null
  chmod 0644 "$RELEASE_DIR/rollback.json"
fi
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK.next"
mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"
install -m 0644 "$SCRIPT_DIR/outpost.service" /etc/systemd/system/outpost.service
install -m 0755 "$SCRIPT_DIR/rollback.sh" /usr/local/sbin/outpost-rollback
ln -sfn "$CURRENT_LINK/bin/outpost-setup-token" /usr/local/sbin/outpost-setup-token
ln -sfn "$CURRENT_LINK/bin/outpost-diagnostics" /usr/local/sbin/outpost-diagnostics
install -d -m 0755 /usr/local/lib/outpost
install -m 0755 "$SCRIPT_DIR/release_recovery.py" /usr/local/lib/outpost/release_recovery.py
printf '%s\n' "$PROJECT_DIR" > "$CONFIG_DIR/install-source"
chmod 0640 "$CONFIG_DIR/install-source"
systemctl daemon-reload
if [ "$AI_PROVIDER" = hailo ]; then
  systemctl enable hailo-ollama.service
  systemctl restart hailo-ollama.service
fi
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
echo "Dashboard setup token (first install): sudo outpost-setup-token show"
echo "Dashboard setup recovery (local root only): sudo outpost-setup-token reset"
echo "Rollback command: sudo outpost-rollback"
