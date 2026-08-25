# Installation

## Prepare the host

Use a dedicated Linux host where practical. Confirm reliable power, adequate storage, LAN access,
and a supported Meshtastic radio. Record radio firmware and channel configuration first.

Required software is Python 3.12+, Python `venv`/`pip`, Git, systemd, and standard Linux account
tools. The installer stops when Python is too old or required commands are absent.

## Connect the radio

USB serial is recommended. Prefer a stable device path:

```sh
ls -l /dev/serial/by-id/ 2>/dev/null
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
```

`/dev/serial/by-id/...` is safer than `/dev/ttyUSB0`, which may change after reboot. The installer
adds `outpost` to `dialout`; distribution-specific udev rules can still be necessary.

## Install

```sh
git clone https://github.com/baal-bot/Outpost-Meshtastic.git
cd Outpost-Meshtastic
sudo ./deploy/install.sh
```

| Purpose | Path |
| --- | --- |
| Active configuration | `/etc/outpost/config.yaml` |
| Distributed defaults | `/etc/outpost/config.yaml.dist` |
| Command intents | `/etc/outpost/intents.yaml` |
| Python environment | `/opt/outpost/venv` |
| Database/runtime files | `/var/lib/outpost` |
| Service unit | `/etc/systemd/system/outpost.service` |

Later installer runs preserve active configuration, refresh the `.dist` comparison copy, reinstall
the checked-out revision into `/opt/outpost/venv`, and restart the production service. `git pull`
alone does not update the running installation.

## Federation acceptance host

Keep development tools separate from the minimal production environment. From the repository
checkout on a test host:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,radio]'
.venv/bin/pip check
```

The checkout-local `.venv` runs tests and linting. The systemd service continues to use
`/opt/outpost/venv`. After pulling application changes, rerun `sudo ./deploy/install.sh`; running
tests from `.venv` does not upgrade or restart the production service.

## First configuration

```sh
sudoedit /etc/outpost/config.yaml
```

Set identity, contact, timezone, units, and radio transport. Keep
`router.intents_file: /etc/outpost/intents.yaml`. Replace the environment user-agent example with
a real operator contact. Configure channel indices to match the radio; Outpost does not create or
distribute Meshtastic channel keys.

```sh
sudo systemctl restart outpost
sudo systemctl status outpost --no-pager
sudo journalctl -u outpost -n 100 --no-pager
```

First startup logs a generated dashboard password once. Store it securely, open
`http://<host>:8080/`, and replace it immediately.

## Verify

```sh
curl -fsS http://127.0.0.1:8080/api/v1/health
sudo journalctl -u outpost --since '10 minutes ago' --no-pager
```

Send `PING` and `HELP` by direct message from a member device. Verify the dashboard Radio page
shows the intended local node, transport, firmware information, utilization, and reconnect state.

## Offline maps

When `node.location` exists at installation, the installer seeds a bounded USGS pack. Later:

```sh
sudo -u outpost /opt/outpost/venv/bin/python tools/build_tile_pack.py \
  --config /etc/outpost/config.yaml --output /var/lib/outpost/.data/tiles
```

Run that from the repository or use its absolute tool path. The tool caps radius/zoom/tile count
and refuses bulk downloads from the standard OpenStreetMap tile server. Online OpenStreetMap is
used interactively; the local USGS pack is the fallback.

## Upgrade

1. Create and download a validated backup.
2. Review release and configuration changes.
3. Pull the desired revision and rerun `sudo ./deploy/install.sh`. A pull by itself does not update
   `/opt/outpost/venv` or the running process.
4. Compare active config with `/etc/outpost/config.yaml.dist`.
5. Verify health, login, radio connectivity, and a mesh `PING`.

Migrations run forward at startup. Do not downgrade a production database without a specific
recovery plan.

## A second Outpost

Use a distinct identity, radio, database, and password. Do not copy the first database or peer
secret. Once both nodes pass independent verification, follow [Federation](FEDERATION.md).
