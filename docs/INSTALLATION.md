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
| Native Hailo models | `/var/lib/outpost/models` |
| Service unit | `/etc/systemd/system/outpost.service` |

The first interactive install opens a guided identity, units, radio, and optional location wizard.
If a supported RTL-SDR is attached, the wizard also offers receive-only SAME setup.
Set `OUTPOST_NONINTERACTIVE=1` for automated provisioning. Later runs preserve active configuration
and stage a new, isolated release under `/opt/outpost/releases`. The installer validates the
package and configuration, creates an integrity-checked pre-upgrade database backup, switches the
`current` symlink atomically, and allows up to two minutes for health. Failed health verification
always restores the previous code, but restores the pre-upgrade database only when the previous
release cannot read the live schema. Code-only failures leave live data untouched. A required data
restore first captures a verified failed-release forensic snapshot and reports the discarded time
window. `git pull` alone never updates the running installation.

After service startup, resume the complete field checklist with `sudo outpost-onboarding status`.
It covers credentials, identity/location, live radio and region/channel verification, maps and
providers, off-device backup, optional federation, and a separate wallboard account. The installer
also advertises the configured HTTP service through Avahi when available. See
[Field-appliance onboarding](ONBOARDING.md) for mDNS and the optional expiring setup hotspot.

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
`http://<host>:8080/`, leave the initial account name as `operator`, and choose a permanent
password:

```sh
sudo outpost-setup-token show
```

The default URL is intentionally plain HTTP for an offline, operator-only trusted LAN or setup
hotspot. It requires no internet, DNS, or certificate. Do not expose it to a shared/WAN network.
Operator-supplied direct HTTPS and explicitly trusted reverse-proxy/VPN modes are supported after
initial setup; see [Web transport and network boundary](WEB-TRANSPORT.md).

The first login consumes the token. Completing setup invalidates every dashboard session and asks
you to sign in with the permanent password. If the token expires, is lost, or setup is interrupted,
issue a replacement locally; this also revokes existing dashboard sessions:

```sh
sudo outpost-setup-token reset
```

The local reset restores the original `operator` as an Administrator and clears that account's MFA
enrollment. It preserves other named accounts and audit history.

After the clean sign-in, open **Access** to create personal named accounts and enroll an
authenticator. Store the recovery codes outside the Pi. Keep at least one enabled Administrator;
Outpost prevents disabling or demoting the last one.

## Verify

```sh
curl -fsS http://127.0.0.1:8080/api/v1/health
sudo journalctl -u outpost --since '10 minutes ago' --no-pager
```

Send `PING` and `HELP` by direct message from a member device. Verify the dashboard Radio page
shows the intended local node, transport, firmware information, utilization, and reconnect state.

## Optional RTL-SDR weather warning receiver

Attach a Realtek RTL2832/RTL2838-compatible dongle and its weather-band antenna before the first
interactive install. If SAME is enabled, the installer adds only the `outpost` service account to
the dedicated `outpost-sdr` group, installs a vendor/product-scoped udev rule, installs Debian's
`rtl-sdr` package, and downloads `samedec` 0.4.2 for the host architecture with a pinned SHA-256
check. The systemd cgroup permits the dynamic USB character-device class, while normal device
permissions restrict the service account to the supported SDR IDs.

Use the SDR serial shown below as `env.same.device`:

```sh
rtl_eeprom 2>&1 | sed -n '/Serial number/p'
```

Set `env.same.frequency_mhz` to the strongest local NWR channel and add the six-digit SAME codes
for the installation's counties. The first digit is the SAME county subdivision (normally `0`),
followed by the two-digit state FIPS and three-digit county FIPS. Confirm codes and transmitter
coverage with official NOAA/NWS material; do not guess a code.

After restart, Environment → NOAA Weather Radio · SAME shows frequency, signal, last decode, and
restart count. These receive-only acceptance commands do not send mesh traffic:

```sh
python tools/verify_same_audio.py
sudo -u outpost /opt/outpost/current/bin/python "$PWD/tools/check_same_hardware.py" \
  --device 51231467 --frequency 162.550 --county 042003
```

Run the tools from the repository checkout. The first command fetches a checksum-pinned National
Periodic Test audio fixture; the second uses a temporary database and requires sustained PCM audio.

## Optional Hailo AI HAT+ 2

The first-run wizard detects a Hailo-10H and can enable its local provider, but the operating-system
runtime must already be installed and the Pi rebooted. The default provider is native HailoRT 5.3
with `Qwen3-VL-2B-Instruct`. Pass `OUTPOST_HAILORT_WHEEL` and `OUTPOST_HAILO_VLM_MODEL` to the
installer; it copies the HEF into `/var/lib/outpost/models` and validates the known-good checksum.
Follow [Local AI](AI.md) for exact commands, rollback provider details, and the hardware benchmark.
Do not install the Hailo-8 `hailo-all` package on an AI HAT+ 2.

When AI is enabled, `ai.required_for_readiness: true` makes the configured provider part of the
deployment health gate. A missing HEF, unavailable accelerator, or failed model load makes
`/api/v1/health` return HTTP 503 until bounded background probes recover it. Set the option to
`false` only when AI is intentionally best-effort. Disabling the AI module is always non-blocking.
During an upgrade the installer stops the old process, waits for the configured release grace
(`OUTPOST_HAILO_RELEASE_GRACE_SECONDS`, default 5), and then starts the replacement. This avoids
two processes competing for the one Hailo VDevice.

Leave AI disabled until the model passes the safety evaluation. The radio, BBS, mail, Watch,
Environment, and federation features do not require the accelerator or any inference provider.

## Offline maps

When `node.location` exists at installation, the installer seeds a bounded USGS pack. Later:

```sh
sudo -u outpost /opt/outpost/current/bin/python tools/build_tile_pack.py \
  --config /etc/outpost/config.yaml
```

Run that from the repository or use its absolute tool path. The tool caps radius/zoom/tile count
and refuses bulk downloads from the standard OpenStreetMap tile server. Online OpenStreetMap is
used interactively; the local USGS pack is the fallback. The output defaults to
`store.tiles_path` (`/var/lib/outpost/.data/tiles` on packaged installs); set that absolute path in
the configuration before building when tiles belong on separate storage. Startup logs the
resolved path and whether its pack is ready, missing, or unreadable.

## Upgrade

1. Create and download a validated off-device backup in addition to the installer's local snapshot.
2. Review release and configuration changes.
3. From a clean checkout, pass a signed release tag such as `./deploy/update.sh v0.2.0`. The updater
   requires the GitHub CLI and verifies checksums, metadata, provenance attestations, and the exact
   tagged commit before activation. Tagged and development updates also require a completed,
   successful `ci.yml` run whose commit exactly matches the target. The normalized run evidence is
   stored in the activated release. A pull by itself does not update the running process. See
   [Releases and artifact verification](RELEASES.md).
4. Compare active config with `/etc/outpost/config.yaml.dist`.
5. Verify health, login, radio connectivity, local-AI readiness when required, and a mesh `PING`.

Migrations run forward at startup. Do not downgrade a production database without a specific
recovery plan.

A fresh install remains available without internet or GitHub so a field node can be commissioned
offline. An upgrade from a Git checkout is fail-closed when CI cannot be verified. Only when an
existing installation must be repaired without network access may the operator explicitly run
`OUTPOST_ALLOW_UNVERIFIED_CI=1 ./deploy/update.sh <local-ref>`. If origin is unreachable, only an
already-local revision can be selected. The updater and installer print a warning, and the release
receives no green-CI evidence file. Run the complete local gates first whenever the field situation
permits; the override is not a routine development shortcut.
If `gh` is installed outside PATH, set `OUTPOST_GH` to its executable; the standard Linuxbrew path
is detected automatically. Hardware-specific installer inputs such as `OUTPOST_HAILORT_WHEEL` and
`OUTPOST_HAILO_VLM_MODEL` are forwarded through the guarded updater.

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
state. All transport modes use the same loopback probe logic; direct HTTPS uses a certificate-name-
agnostic loopback check. A malformed probe aborts before downtime. Keep the generated safety
snapshots until the result has been independently verified.

## Releases and dependency lock

Production installation constrains runtime packages with `requirements.lock`. Tagged `v*`
revisions must pass the exact-commit CI matrix before the release workflow publishes a wheel,
checksums, release metadata, SPDX SBOM, and GitHub provenance attestations. Update the project
version and lock intentionally in the same reviewed release PR; do not regenerate the lock as an
incidental upgrade step.

## A second Outpost

Use a distinct identity, radio, database, and password. Do not copy the first database or peer
secret. Once both nodes pass independent verification, follow [Federation](FEDERATION.md).
