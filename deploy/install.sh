#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=${OUTPOST_PROJECT_DIR:-$(dirname -- "$SCRIPT_DIR")}
. "$SCRIPT_DIR/health_probe.sh"
PREFIX=${OUTPOST_PREFIX:-/opt/outpost}
STATE_DIR=${OUTPOST_STATE_DIR:-/var/lib/outpost}
CONFIG_DIR=${OUTPOST_CONFIG_DIR:-/etc/outpost}
SYSTEM_ROOT=${OUTPOST_SYSTEM_ROOT:-}
SYSTEMD_DIR=${OUTPOST_SYSTEMD_DIR:-$SYSTEM_ROOT/etc/systemd/system}
UDEV_RULES_DIR=${OUTPOST_UDEV_RULES_DIR:-$SYSTEM_ROOT/etc/udev/rules.d}
AVAHI_SERVICES_DIR=${OUTPOST_AVAHI_SERVICES_DIR:-$SYSTEM_ROOT/etc/avahi/services}
SBIN_DIR=${OUTPOST_SBIN_DIR:-$SYSTEM_ROOT/usr/local/sbin}
LIB_DIR=${OUTPOST_LIB_DIR:-$SYSTEM_ROOT/usr/local/lib/outpost}
LOG_DIR=${OUTPOST_LOG_DIR:-$SYSTEM_ROOT/var/log/outpost}
SERVICE_NAME=${OUTPOST_SERVICE_NAME:-outpost.service}
HEALTH_URL=${OUTPOST_HEALTH_URL:-}
METRICS_URL=
NONINTERACTIVE=${OUTPOST_NONINTERACTIVE:-0}
HAILORT_WHEEL=${OUTPOST_HAILORT_WHEEL:-}
HAILO_VLM_MODEL_SOURCE=${OUTPOST_HAILO_VLM_MODEL:-}
MDNS_ENABLED=${OUTPOST_MDNS:-1}
HAILO_RELEASE_GRACE_SECONDS=${OUTPOST_HAILO_RELEASE_GRACE_SECONDS:-5}
ALLOW_UNVERIFIED_CI=${OUTPOST_ALLOW_UNVERIFIED_CI:-0}
CI_VERIFIED_REVISION=${OUTPOST_CI_VERIFIED_REVISION:-}
CI_EVIDENCE=${OUTPOST_CI_EVIDENCE:-}
WEB_TRANSPORT_MODE=trusted_http
OUTPOST_HEALTH_ATTEMPTS=${OUTPOST_HEALTH_ATTEMPTS:-60}
OUTPOST_HEALTH_DELAY_SECONDS=${OUTPOST_HEALTH_DELAY_SECONDS:-2}

fail() { echo "Outpost install: $*" >&2; exit 1; }
json_field() {
  printf '%s' "$1" | python3 -c "import json,sys; print(json.load(sys.stdin)[$2])"
}

wait_for_hailo_release() {
  if [ "$AI_PROVIDER" = hailo_vlm ] && [ "$HAILO_RELEASE_GRACE_SECONDS" -gt 0 ]; then
    echo "Waiting ${HAILO_RELEASE_GRACE_SECONDS}s for the Hailo device to be released"
    sleep "$HAILO_RELEASE_GRACE_SECONDS"
  fi
}

metrics_probe() {
  outpost_probe_url "$METRICS_URL"
}

[ "$(id -u)" -eq 0 ] || fail "run as root: sudo $0"
for command in python3 getent groupadd useradd usermod install systemctl udevadm ln mv readlink curl grep; do
  command -v "$command" >/dev/null 2>&1 || fail "missing required command: $command"
done
python3 -c 'import sys; raise SystemExit(not ((3, 12) <= sys.version_info < (3, 14)))' || \
  fail "Python 3.12 or 3.13 is required"
python3 -c 'from zoneinfo import ZoneInfo; ZoneInfo("UTC")' || \
  fail "IANA timezone data is required; install the tzdata operating-system package"
python3 -m venv --help >/dev/null 2>&1 || fail "Python venv support is missing; install python3-venv"
case "$MDNS_ENABLED" in 0|1) ;; *) fail "OUTPOST_MDNS must be 0 or 1" ;; esac
case "$ALLOW_UNVERIFIED_CI" in
  0|1) ;;
  *) fail "OUTPOST_ALLOW_UNVERIFIED_CI must be 0 or 1" ;;
esac
case "$HAILO_RELEASE_GRACE_SECONDS" in
  ''|*[!0-9]*) fail "OUTPOST_HAILO_RELEASE_GRACE_SECONDS must be an integer from 0 to 30" ;;
esac
[ "$HAILO_RELEASE_GRACE_SECONDS" -le 30 ] || \
  fail "OUTPOST_HAILO_RELEASE_GRACE_SECONDS must be an integer from 0 to 30"

if command -v git >/dev/null 2>&1 && git -C "$PROJECT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  FULL_REVISION=$(git -C "$PROJECT_DIR" rev-parse HEAD)
  REVISION=$(git -C "$PROJECT_DIR" rev-parse --short=12 HEAD)
else
  FULL_REVISION=
  REVISION=$(date -u +%Y%m%dT%H%M%SZ)
fi
RELEASE_ID=${OUTPOST_RELEASE_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$REVISION}
RELEASE_DIR=$PREFIX/releases/$RELEASE_ID
CURRENT_LINK=$PREFIX/current
PREVIOUS_LINK=$PREFIX/previous
OLD_TARGET=$(readlink "$CURRENT_LINK" 2>/dev/null || true)

if [ -n "$OLD_TARGET" ] && [ -n "$FULL_REVISION" ]; then
  if [ "$ALLOW_UNVERIFIED_CI" = 1 ]; then
    echo "WARNING: proceeding with an unverified upgrade by explicit operator override." >&2
  elif [ "$CI_VERIFIED_REVISION" != "$FULL_REVISION" ] || [ -z "$CI_EVIDENCE" ]; then
    fail "upgrades from a Git checkout require exact-commit green CI via deploy/update.sh; "\
"for an offline emergency only, set OUTPOST_ALLOW_UNVERIFIED_CI=1"
  else
    echo "Accepted exact-commit CI evidence for $FULL_REVISION"
  fi
fi

getent group outpost >/dev/null 2>&1 || groupadd --system outpost
getent group outpost-sdr >/dev/null 2>&1 || groupadd --system outpost-sdr
getent passwd outpost >/dev/null 2>&1 || useradd --system --gid outpost --home "$STATE_DIR" --shell /usr/sbin/nologin outpost
getent group dialout >/dev/null 2>&1 && usermod -a -G dialout outpost
usermod -a -G outpost-sdr outpost
install -d -m 0755 "$PREFIX" "$PREFIX/releases"
install -d -m 0750 -o outpost -g outpost "$STATE_DIR" "$STATE_DIR/.data" \
  "$STATE_DIR/backups" "$STATE_DIR/models" "$LOG_DIR"
install -d -m 0750 -o root -g outpost "$CONFIG_DIR"
install -d -m 0750 -o root -g outpost "$CONFIG_DIR/tls"
install -d -m 0755 "$SYSTEMD_DIR" "$UDEV_RULES_DIR" "$AVAHI_SERVICES_DIR" "$SBIN_DIR" "$LIB_DIR"
install -m 0644 "$SCRIPT_DIR/70-outpost-rtl-sdr.rules" \
  "$UDEV_RULES_DIR/70-outpost-rtl-sdr.rules"
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --action=change

echo "Staging Outpost release $RELEASE_ID"
python3 -m venv "$RELEASE_DIR"
if [ -n "$CI_EVIDENCE" ] && [ "$CI_VERIFIED_REVISION" = "$FULL_REVISION" ]; then
  printf '%s\n' "$CI_EVIDENCE" > "$RELEASE_DIR/ci-evidence.json"
  chmod 0644 "$RELEASE_DIR/ci-evidence.json"
fi
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
  "$RELEASE_DIR/bin/python" "$SCRIPT_DIR/configure.py" --config "$CONFIG_DIR/config.yaml" \
    --state "$STATE_DIR/onboarding.json"
  chown root:outpost "$CONFIG_DIR/config.yaml"
  chmod 0640 "$CONFIG_DIR/config.yaml"
else
  echo "First-run wizard skipped; edit $CONFIG_DIR/config.yaml before production use."
fi
OUTPOST_CONFIG="$CONFIG_DIR/config.yaml" "$RELEASE_DIR/bin/python" -c 'from outpost.config import load_config; load_config(); print("Configuration validated")'
outpost_configure_health "$RELEASE_DIR/bin/python" "$CONFIG_DIR/config.yaml"
if [ "$MDNS_ENABLED" = 1 ]; then
  "$RELEASE_DIR/bin/python" "$SCRIPT_DIR/render_avahi.py" \
    --config "$CONFIG_DIR/config.yaml" --output "$AVAHI_SERVICES_DIR/outpost.service"
  if systemctl cat avahi-daemon.service >/dev/null 2>&1; then
    if systemctl enable avahi-daemon.service && systemctl restart avahi-daemon.service; then
      echo "mDNS service advertised by Avahi; use this host's .local name."
    else
      echo "Avahi is installed but mDNS activation failed; inspect avahi-daemon.service." >&2
    fi
  else
    echo "mDNS declaration installed, but avahi-daemon is absent; install it to enable discovery."
  fi
else
  echo "mDNS discovery disabled by OUTPOST_MDNS=0."
fi
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
  if [ ! -e /dev/hailo0 ] && [ ! -e /dev/h1x-0 ]; then
    fail "Hailo-10H is not ready; expected /dev/hailo0 or /dev/h1x-0 after reboot"
  fi
  install -m 0644 "$SCRIPT_DIR/hailo-ollama.service" \
    /etc/systemd/system/hailo-ollama.service
  install -d -m 0750 -o root -g outpost "$CONFIG_DIR/hailo-ollama"
  install -m 0640 -o root -g outpost "$SCRIPT_DIR/hailo-ollama.json" \
    "$CONFIG_DIR/hailo-ollama/hailo-ollama.json"
  chown -R outpost:outpost /usr/share/hailo-ollama/models
fi
if [ "$AI_PROVIDER" = hailo_vlm ]; then
  if [ ! -e /dev/hailo0 ] && [ ! -e /dev/h1x-0 ]; then
    fail "Hailo-10H is not ready; expected /dev/hailo0 or /dev/h1x-0 after reboot"
  fi
  if [ -n "$HAILORT_WHEEL" ]; then
    [ -f "$HAILORT_WHEEL" ] || fail "OUTPOST_HAILORT_WHEEL is not a file: $HAILORT_WHEEL"
    "$RELEASE_DIR/bin/pip" install "$HAILORT_WHEEL"
    "$RELEASE_DIR/bin/pip" check
  fi
  "$RELEASE_DIR/bin/python" -c \
    'from hailo_platform import VDevice; from hailo_platform.genai import VLM' || fail \
    "hailo_vlm requires the HailoRT 5.3 Python wheel; set OUTPOST_HAILORT_WHEEL"
  HAILO_VLM_MODEL_PATH=$(OUTPOST_CONFIG="$CONFIG_DIR/config.yaml" \
    "$RELEASE_DIR/bin/python" - <<'PY'
from outpost.config import load_config
print(load_config().ai.hailo_vlm.model_path)
PY
  )
  case "$HAILO_VLM_MODEL_PATH" in
    "$STATE_DIR"/*) ;;
    *) fail "hailo_vlm model_path must be inside $STATE_DIR for service access" ;;
  esac
  install -d -m 0750 -o outpost -g outpost "$(dirname -- "$HAILO_VLM_MODEL_PATH")"
  if [ -n "$HAILO_VLM_MODEL_SOURCE" ]; then
    [ -f "$HAILO_VLM_MODEL_SOURCE" ] || fail \
      "OUTPOST_HAILO_VLM_MODEL is not a file: $HAILO_VLM_MODEL_SOURCE"
    if [ "$HAILO_VLM_MODEL_SOURCE" != "$HAILO_VLM_MODEL_PATH" ]; then
      install -m 0640 -o outpost -g outpost \
        "$HAILO_VLM_MODEL_SOURCE" "$HAILO_VLM_MODEL_PATH"
    fi
  fi
  [ -f "$HAILO_VLM_MODEL_PATH" ] || fail \
    "Qwen3-VL HEF is missing; set OUTPOST_HAILO_VLM_MODEL to the downloaded model"
  chown outpost:outpost "$HAILO_VLM_MODEL_PATH"
  chmod 0640 "$HAILO_VLM_MODEL_PATH"
  command -v sha256sum >/dev/null 2>&1 || fail "hailo_vlm requires: sha256sum"
  printf '%s  %s\n' \
    '3e302b1d0bdc4beaf4ff982cb34f18bc957d3acd1e20e275eb0dd3536b3043a7' \
    "$HAILO_VLM_MODEL_PATH" | sha256sum -c -
fi
METRICS_URL="$OUTPOST_LOOPBACK_BASE/metrics"
TILE_PATH=$(OUTPOST_CONFIG="$CONFIG_DIR/config.yaml" "$RELEASE_DIR/bin/python" - <<'PY'
from outpost.config import load_config
print(load_config().store.tiles_path)
PY
)

if [ ! -f "$TILE_PATH/manifest.json" ] && \
  "$RELEASE_DIR/bin/python" -c 'import sys,yaml; d=yaml.safe_load(open(sys.argv[1])) or {}; raise SystemExit(0 if d.get("node",{}).get("location") else 1)' "$CONFIG_DIR/config.yaml"; then
  echo "Installing bounded offline map pack for node.location"
  if "$RELEASE_DIR/bin/python" "$PROJECT_DIR/tools/build_tile_pack.py" \
    --config "$CONFIG_DIR/config.yaml"; then
    chown -R outpost:outpost "$TILE_PATH"
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
install -m 0644 "$SCRIPT_DIR/outpost.service" "$SYSTEMD_DIR/outpost.service"
install -m 0755 "$SCRIPT_DIR/rollback.sh" "$SBIN_DIR/outpost-rollback"
install -m 0755 "$SCRIPT_DIR/setup-hotspot.sh" "$SBIN_DIR/outpost-setup-hotspot"
ln -sfn "$CURRENT_LINK/bin/outpost-setup-token" "$SBIN_DIR/outpost-setup-token"
ln -sfn "$CURRENT_LINK/bin/outpost-diagnostics" "$SBIN_DIR/outpost-diagnostics"
ln -sfn "$CURRENT_LINK/bin/outpost-onboarding" "$SBIN_DIR/outpost-onboarding"
ln -sfn "$CURRENT_LINK/bin/outpost-replay" "$SBIN_DIR/outpost-replay"
install -m 0755 "$SCRIPT_DIR/release_recovery.py" "$LIB_DIR/release_recovery.py"
install -m 0755 "$SCRIPT_DIR/health_probe.sh" "$LIB_DIR/health_probe.sh"
printf '%s\n' "$PROJECT_DIR" > "$CONFIG_DIR/install-source"
chmod 0640 "$CONFIG_DIR/install-source"
systemctl daemon-reload
if [ "$AI_PROVIDER" = hailo ]; then
  systemctl enable hailo-ollama.service
  systemctl restart hailo-ollama.service
fi
if [ "$AI_PROVIDER" = hailo_vlm ]; then
  systemctl disable --now hailo-ollama.service 2>/dev/null || true
fi
systemctl enable "$SERVICE_NAME"
if [ -n "$OLD_TARGET" ]; then
  systemctl stop "$SERVICE_NAME"
  wait_for_hailo_release
fi
systemctl start "$SERVICE_NAME"

healthy=0
if outpost_wait_for_health; then
  healthy=1
else
  health_status=$?
fi
if [ "$healthy" -eq 1 ] && ! metrics_probe | grep -q '^# HELP outpost_'; then
  echo "Exact /metrics deployment smoke check failed." >&2
  healthy=0
fi
if [ "$healthy" -ne 1 ]; then
  echo "New release failed health verification; rolling back." >&2
  systemctl stop "$SERVICE_NAME" || true
  wait_for_hailo_release
  rollback_action=code-only
  rollback_snapshot_at=
  if [ -n "$OLD_TARGET" ] && [ -f "$RELEASE_DIR/rollback.json" ]; then
    if ! rollback_plan=$(python3 "$SCRIPT_DIR/release_recovery.py" plan \
      --metadata "$RELEASE_DIR/rollback.json" \
      --current-release "$RELEASE_DIR" --target-release "$OLD_TARGET"); then
      fail "release failed health verification and rollback compatibility planning failed"
    fi
    rollback_action=$(json_field "$rollback_plan" '"action"')
    rollback_snapshot_at=$(json_field "$rollback_plan" '"snapshot_created_at"')
  fi
  if [ "$rollback_action" = restore ]; then
    failed_path="$DATABASE_PATH.failed-$RELEASE_ID"
    python3 "$SCRIPT_DIR/release_recovery.py" snapshot \
      --source "$DATABASE_PATH" --destination "$failed_path" >/dev/null || \
      fail "could not preserve the failed release database; live data was not replaced"
    python3 "$SCRIPT_DIR/release_recovery.py" restore \
      --source "$BACKUP_PATH" --destination "$DATABASE_PATH" \
      --maximum-schema "$PREVIOUS_SCHEMA_CAP" >/dev/null || \
      fail "could not restore the schema-compatible pre-upgrade snapshot"
    chown outpost:outpost "$failed_path" "$DATABASE_PATH"
    chmod 0640 "$failed_path" "$DATABASE_PATH"
    echo "Restored database snapshot from $rollback_snapshot_at; writes after that point "\
"were discarded. Failed-release forensic copy: $failed_path" >&2
  else
    echo "Rollback is code-only; the live database was left untouched." >&2
  fi
  if [ -n "$OLD_TARGET" ]; then
    ln -sfn "$OLD_TARGET" "$CURRENT_LINK.next"
    mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"
  fi
  [ -n "$OLD_TARGET" ] && systemctl restart "$SERVICE_NAME" || true
  if [ "${health_status:-1}" -eq 2 ]; then
    fail "release health probe could not be performed; previous release restored but unverified"
  fi
  fail "release $RELEASE_ID did not become healthy; previous release restored ($rollback_action)"
fi
if [ -n "$OLD_TARGET" ]; then
  rm -f "$PREVIOUS_LINK"
  ln -s "$OLD_TARGET" "$PREVIOUS_LINK"
fi
if cleanup_result=$(OUTPOST_CONFIG="$CONFIG_DIR/config.yaml" "$RELEASE_DIR/bin/python" - \
  "$PREFIX/releases" "$CURRENT_LINK" "$PREVIOUS_LINK" <<'PY'
import sys
from pathlib import Path

from outpost.config import load_config
from outpost.store import Database
from outpost.store.backups import BackupService, prune_release_directories

config = load_config()
backups = BackupService(Database(config.store.path), config.store.backup)
backup_files = backups.rotate()
release_dirs = prune_release_directories(
    Path(sys.argv[1]),
    Path(sys.argv[2]),
    Path(sys.argv[3]),
    config.store.backup.superseded_release_keep,
)
print(f"Recovery retention removed {backup_files} backup file(s) and {len(release_dirs)} release(s).")
PY
); then
  echo "$cleanup_result"
else
  echo "Recovery retention could not be completed; the healthy release remains active." >&2
fi
printf '%s\n' "$RELEASE_ID" > "$PREFIX/installed-release"
echo "Outpost $RELEASE_ID is healthy at $HEALTH_URL"
echo "Dashboard setup token (first install): sudo outpost-setup-token show"
echo "Dashboard setup recovery (local root only): sudo outpost-setup-token reset"
echo "Resumable first-run checklist: sudo outpost-onboarding status"
echo "Isolated traffic replay and drills: sudo outpost-replay --help"
echo "Rollback command: sudo outpost-rollback"
