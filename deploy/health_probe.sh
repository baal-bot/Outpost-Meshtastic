#!/bin/sh

# Shared loopback health probing for install and emergency rollback. The caller
# supplies HEALTH_URL (optionally), WEB_TRANSPORT_MODE, and retry settings.

outpost_configure_health() {
  health_python=$1
  health_config=$2
  WEB_TRANSPORT_MODE=$(OUTPOST_CONFIG="$health_config" "$health_python" - <<'PY'
from outpost.config import load_config
print(load_config().web.transport.mode)
PY
  )
  health_port=$(OUTPOST_CONFIG="$health_config" "$health_python" - <<'PY'
from outpost.config import load_config
print(load_config().web.port)
PY
  )
  if [ "$WEB_TRANSPORT_MODE" = direct_https ]; then
    OUTPOST_LOOPBACK_BASE="https://127.0.0.1:$health_port"
  else
    OUTPOST_LOOPBACK_BASE="http://127.0.0.1:$health_port"
  fi
  if [ -z "$HEALTH_URL" ]; then
    HEALTH_URL="$OUTPOST_LOOPBACK_BASE/api/v1/health"
  fi
}

outpost_probe_url() {
  probe_url=$1
  if [ "$WEB_TRANSPORT_MODE" = direct_https ]; then
    # Loopback liveness is certificate-name agnostic. Startup separately
    # validates certificate dates and key matching.
    curl -fkSs "$probe_url"
  else
    curl -fsS "$probe_url"
  fi
}

outpost_probe_status_is_retryable() {
  case "$1" in
    7|22|28|52|56) return 0 ;;
    *) return 1 ;;
  esac
}

outpost_wait_for_health() {
  health_attempt=0
  health_attempts=${OUTPOST_HEALTH_ATTEMPTS:-30}
  health_delay=${OUTPOST_HEALTH_DELAY_SECONDS:-2}
  while [ "$health_attempt" -lt "$health_attempts" ]; do
    if outpost_probe_url "$HEALTH_URL" >/dev/null 2>&1; then
      return 0
    else
      health_status=$?
    fi
    if ! outpost_probe_status_is_retryable "$health_status"; then
      echo "Health probe could not be performed (curl exit $health_status): $HEALTH_URL" >&2
      return 2
    fi
    health_attempt=$((health_attempt + 1))
    sleep "$health_delay"
  done
  return 1
}
