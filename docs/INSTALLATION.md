# Installation

## Prepare the host

Use a dedicated Linux host where practical. Confirm reliable power, adequate storage, LAN access,
and a supported Meshtastic radio. Record radio firmware and channel configuration first.

Required software is Python 3.12 or 3.13, Python `venv`/`pip`, Git, systemd, and standard Linux
account tools. The installer stops when Python is unsupported or required commands are absent.

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
| Active Python release | `/opt/outpost/current` |
| Versioned releases | `/opt/outpost/releases/` |
| Previous release | `/opt/outpost/previous` |
| Database/runtime files | `/var/lib/outpost` |
| Service unit | `/etc/systemd/system/outpost.service` |

The first interactive install opens a guided identity, units, radio, and optional location wizard.
Set `OUTPOST_NONINTERACTIVE=1` for automated provisioning. Later runs preserve active configuration
and stage a new, isolated release under `/opt/outpost/releases`. The installer validates the
package and configuration, creates an integrity-checked pre-upgrade database backup, switches the
`current` symlink atomically, and waits for health. Failed health verification restores both the
previous code and pre-upgrade database. `git pull` alone never updates the running installation.

## Federation acceptance host

Keep development tools separate from the minimal production environment. From the repository
checkout on a test host, first install or update the production service and then prepare the
checkout-local acceptance environment as the normal (non-root) checkout owner:

```sh
sudo ./deploy/install.sh
./deploy/install-test-host.sh
```

Pass `--with-browser` when the host will run Playwright dashboard tests. Set
`OUTPOST_TEST_VENV=/another/path` to choose a different test environment. The helper refuses root,
validates its dependencies, and never writes to `/opt/outpost`, changes configuration, or restarts
the service. The checkout-local `.venv` runs tests and linting while systemd continues to use the
minimal `/opt/outpost/current` release. After pulling changes, rerun the helper to refresh test
tools; rerun `sudo ./deploy/install.sh` separately only when you intend to update the service.

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

First startup creates a mode-0600 setup token beside the database. It expires after 60 minutes and
is never written to the service journal. Retrieve it through local root access, open
`http://<host>:8080/`, and choose a permanent password:

```sh
sudo outpost-setup-token show
```

The first login consumes the token. Completing setup invalidates every dashboard session and asks
you to sign in with the permanent password. If the token expires, is lost, or setup is interrupted,
issue a replacement locally; this also revokes existing dashboard sessions:

```sh
sudo outpost-setup-token reset
```

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
sudo -u outpost /opt/outpost/current/bin/python tools/build_tile_pack.py \
  --config /etc/outpost/config.yaml --output /var/lib/outpost/.data/tiles
```

Run that from the repository or use its absolute tool path. The tool caps radius/zoom/tile count
and refuses bulk downloads from the standard OpenStreetMap tile server. Online OpenStreetMap is
used interactively; the local USGS pack is the fallback.

## Upgrade

1. Create and download a validated off-device backup in addition to the installer's local snapshot.
2. Review release and configuration changes.
3. From a clean checkout, run `./deploy/update.sh origin/main`, or pass a release tag such as
   `./deploy/update.sh v0.2.0`. It fetches the target, installs it, and returns the source checkout
   to its prior revision if installation fails. A pull by itself does not update the running process.
4. Compare active config with `/etc/outpost/config.yaml.dist`.
5. Verify health, login, radio connectivity, and a mesh `PING`.

Migrations run forward at startup. Do not downgrade a production database without a specific
recovery plan.

## Roll back

The installer automatically rolls back a release that fails its startup health check. After a
successful upgrade, explicitly return to the previous code with:

```sh
sudo outpost-rollback
```

The command first verifies that the current release, previous release, live database, schema
capacity, and recorded pre-upgrade snapshot form one compatible rollback plan. It does not stop the
healthy service if that dry run fails. After stopping, it creates another safety snapshot, restores
the matching pre-upgrade database only when a migration requires it, swaps code, and verifies
health. If verification fails, it automatically returns both code and data to their pre-attempt
state. Keep the generated safety snapshots until the result has been independently verified.

## Releases and dependency lock

Production installation constrains runtime packages with `requirements.lock`. Tagged `v*`
revisions run the package smoke test, build a wheel and checksum, and publish both to the matching
GitHub release. Update the project version and lock intentionally in the same reviewed release PR;
do not regenerate the lock as an incidental upgrade step.

## A second Outpost

Use a distinct identity, radio, database, and password. Do not copy the first database or peer
secret. Once both nodes pass independent verification, follow [Federation](FEDERATION.md).
