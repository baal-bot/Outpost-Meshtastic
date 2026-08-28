#!/bin/sh
set -eu

CONNECTION=outpost-setup
SSID=${OUTPOST_SETUP_SSID:-Outpost Setup}
ADDRESS=10.42.0.1
CONFIG=${OUTPOST_CONFIG:-/etc/outpost/config.yaml}
OUTPOST_PYTHON=${OUTPOST_PYTHON:-/opt/outpost/current/bin/python}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SELF=$SCRIPT_DIR/${0##*/}

fail() { echo "Outpost setup hotspot: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "run as root: sudo $0 $*"

stop_hotspot() {
  systemctl stop outpost-setup-hotspot-expiry.timer >/dev/null 2>&1 || true
  nmcli connection down "$CONNECTION" >/dev/null 2>&1 || true
  nmcli connection delete "$CONNECTION" >/dev/null 2>&1 || true
  nft delete table inet outpost_setup >/dev/null 2>&1 || true
  nft delete table ip outpost_setup_nat >/dev/null 2>&1 || true
  echo "Outpost setup hotspot stopped."
}

case "${1:-status}" in
  status)
    command -v nmcli >/dev/null 2>&1 || fail "NetworkManager (nmcli) is required"
    if nmcli -g NAME connection show --active | grep -Fx "$CONNECTION" >/dev/null 2>&1; then
      echo "Outpost setup hotspot is active at http://$ADDRESS"
    else
      echo "Outpost setup hotspot is inactive."
    fi
    ;;
  stop)
    for command in nmcli nft systemctl; do
      command -v "$command" >/dev/null 2>&1 || fail "missing required command: $command"
    done
    stop_hotspot
    ;;
  start)
    interface=${2:-}
    minutes=${3:-30}
    [ -n "$interface" ] || fail "usage: $0 start WIFI_INTERFACE [5-60 minutes]"
    case "$interface" in *[!A-Za-z0-9_.-]*|'') fail "invalid Wi-Fi interface" ;; esac
    case "$minutes" in *[!0-9]*|'') fail "duration must be 5 to 60 minutes" ;; esac
    [ "$minutes" -ge 5 ] && [ "$minutes" -le 60 ] || fail "duration must be 5 to 60 minutes"
    for command in nmcli nft systemctl systemd-run; do
      command -v "$command" >/dev/null 2>&1 || fail "missing required command: $command"
    done
    [ -x "$OUTPOST_PYTHON" ] || fail "Outpost is not installed at $OUTPOST_PYTHON"
    [ -f "$CONFIG" ] || fail "configuration not found: $CONFIG"
    port=$(OUTPOST_CONFIG="$CONFIG" "$OUTPOST_PYTHON" - <<'PY'
from outpost.config import load_config
print(load_config().web.port)
PY
    )
    transport=$(OUTPOST_CONFIG="$CONFIG" "$OUTPOST_PYTHON" - <<'PY'
from outpost.config import load_config
print(load_config().web.transport.mode)
PY
    )
    case "$port" in *[!0-9]*|'') fail "configured dashboard port is invalid" ;; esac
    [ "$transport" = trusted_http ] || fail \
      "setup hotspot requires trusted_http mode; use the documented local recovery procedure"
    active=$(nmcli -g GENERAL.CONNECTION device show "$interface" 2>/dev/null || true)
    if [ -n "$active" ] && [ "$active" != "--" ]; then
      fail "$interface is carrying '$active'; disconnect it explicitly before starting setup access"
    fi
    password=$(
      "$OUTPOST_PYTHON" -c 'import secrets; print(secrets.token_urlsafe(15))'
    )
    systemctl stop outpost-setup-hotspot-expiry.timer >/dev/null 2>&1 || true
    systemctl reset-failed outpost-setup-hotspot-expiry.service >/dev/null 2>&1 || true
    nmcli connection delete "$CONNECTION" >/dev/null 2>&1 || true
    nft delete table inet outpost_setup >/dev/null 2>&1 || true
    nft delete table ip outpost_setup_nat >/dev/null 2>&1 || true
    nmcli connection add type wifi ifname "$interface" con-name "$CONNECTION" ssid "$SSID"
    starting=1
    trap '[ "$starting" -eq 0 ] || stop_hotspot' 0
    trap 'exit 1' HUP INT TERM
    nmcli connection modify "$CONNECTION" \
      802-11-wireless.mode ap 802-11-wireless.ap-isolation yes \
      802-11-wireless-security.key-mgmt wpa-psk \
      802-11-wireless-security.psk "$password" \
      ipv4.method shared ipv4.addresses "$ADDRESS/24" ipv4.never-default yes \
      ipv6.method disabled connection.autoconnect no
    nft -f - <<EOF
table inet outpost_setup {
  chain input {
    type filter hook input priority -10; policy accept;
    iifname "$interface" udp dport { 53, 67, 5353 } accept
    iifname "$interface" tcp dport { 53, $port } accept
    iifname "$interface" drop
  }
  chain forward {
    type filter hook forward priority -10; policy accept;
    iifname "$interface" drop
  }
}
table ip outpost_setup_nat {
  chain prerouting {
    type nat hook prerouting priority dstnat; policy accept;
    iifname "$interface" tcp dport 80 redirect to :$port
  }
}
EOF
    if ! nmcli connection up "$CONNECTION"; then
      fail "NetworkManager could not start the access point"
    fi
    systemd-run --quiet --collect --unit outpost-setup-hotspot-expiry \
      --on-active="${minutes}m" \
      "$SELF" stop
    starting=0
    trap - 0 HUP INT TERM
    echo "Temporary setup access is active for at most $minutes minutes."
    echo "SSID: $SSID"
    echo "One-time Wi-Fi password: $password"
    echo "Open: http://$ADDRESS:$port/ (plain HTTP on this isolated setup network)"
    echo "Stop early: sudo outpost-setup-hotspot stop"
    ;;
  *)
    fail "usage: $0 {status|start WIFI_INTERFACE [5-60 minutes]|stop}"
    ;;
esac
