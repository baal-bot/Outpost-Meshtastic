#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname -- "$SCRIPT_DIR")

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

for command in python3 getent groupadd useradd install systemctl; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Missing required command: $command" >&2
    exit 1
  fi
done
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
  echo "Outpost requires Python 3.12 or newer." >&2
  exit 1
fi
if ! python3 -m venv --help >/dev/null 2>&1; then
  echo "Python venv support is missing. Install python3-venv and retry." >&2
  exit 1
fi

if ! getent group outpost >/dev/null 2>&1; then
  groupadd --system outpost
fi
if ! getent passwd outpost >/dev/null 2>&1; then
  useradd --system --home /var/lib/outpost --shell /usr/sbin/nologin outpost
fi
if getent group dialout >/dev/null 2>&1; then
  usermod -a -G dialout outpost
fi

install -d -m 0750 -o outpost -g outpost /var/lib/outpost /var/lib/outpost/.data /var/log/outpost
install -d -m 0755 /opt/outpost
install -d -m 0750 -o root -g outpost /etc/outpost
python3 -m venv /opt/outpost/venv
/opt/outpost/venv/bin/pip install "$PROJECT_DIR[radio]"

# Keep local settings intact on upgrades. The .dist file shows the current defaults.
install -m 0640 -o root -g outpost "$PROJECT_DIR/config/config.example.yaml" \
  /etc/outpost/config.yaml.dist
install -m 0640 -o root -g outpost "$PROJECT_DIR/config/intents.yaml" \
  /etc/outpost/intents.yaml.dist
if [ ! -e /etc/outpost/config.yaml ]; then
  install -m 0640 -o root -g outpost "$PROJECT_DIR/config/config.example.yaml" \
    /etc/outpost/config.yaml
  echo "Created /etc/outpost/config.yaml"
else
  echo "Preserved existing /etc/outpost/config.yaml"
fi
if [ ! -e /etc/outpost/intents.yaml ]; then
  install -m 0640 -o root -g outpost "$PROJECT_DIR/config/intents.yaml" \
    /etc/outpost/intents.yaml
fi

# Initial setup may set node.location before installation. When present, seed a bounded
# regional USGS tile pack so the dashboard map works without WAN access.
if /opt/outpost/venv/bin/python -c \
  'import sys,yaml; d=yaml.safe_load(open(sys.argv[1])) or {}; raise SystemExit(0 if d.get("node",{}).get("location") else 1)' \
  /etc/outpost/config.yaml; then
  echo "Installing the offline map pack for node.location"
  /opt/outpost/venv/bin/python "$PROJECT_DIR/tools/build_tile_pack.py" \
    --config /etc/outpost/config.yaml --output /var/lib/outpost/.data/tiles
  chown -R outpost:outpost /var/lib/outpost/.data/tiles
else
  echo "Offline map deferred: set node.location during initial setup, then run:"
  echo "  tools/build_tile_pack.py --config /etc/outpost/config.yaml --output /var/lib/outpost/.data/tiles"
fi

install -m 0644 "$SCRIPT_DIR/outpost.service" /etc/systemd/system/outpost.service
systemctl daemon-reload
systemctl enable --now outpost.service

echo "Outpost installed. Check status with: systemctl status outpost"
