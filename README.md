# Outpost

Outpost is an offline-first community server for Meshtastic networks. It connects a dedicated
Meshtastic radio to a Raspberry Pi-class computer and provides a web dashboard, bulletin boards,
private mail, member identity, incident reporting, welfare check-ins, environmental information,
and optional federation with other Outposts.

Outpost remains useful when internet service is unreliable. Local mesh messaging, stored community
data, cached maps, and operator controls do not depend on a cloud account. Internet-backed weather,
official alerts, earthquakes, map tiles, and peer services degrade independently.

> [!WARNING]
> Outpost is community communications software, not an emergency dispatch system. It is not a
> replacement for 911, public warning receivers, or professional emergency services.

## Project status

Outpost is active, pre-release software. Core application, dashboard, database migrations, radio
transport, and automated tests are implemented. A single real Meshtastic radio has been used during
development. Multi-node federation, extended unattended operation, destructive power testing, and
some optional radio integrations remain in the [acceptance backlog](docs/FEDERATION-ACCEPTANCE-BACKLOG.md).

The database schema, API, command grammar, and federation protocol are not stable until the project
publishes a compatibility policy and a 1.0 release.

## What it provides

- Meshtastic command router with direct-message and configured-channel support
- Member handles, trust levels, approval controls, and recently heard radios
- Bulletin boards, threaded posts, subscriptions, search, digests, and private mail
- Incident reporting, confirmations, disputes, responder alerts, and map operations
- Welfare events, check-ins, reviewed solicitation, rosters, and CSV export
- Weather, forecasts, CAP alerts, astronomy, earthquakes, member positions, and waypoints
- Responsive dashboards for operations, radio, members, BBS, mail, environment, and federation
- Airtime budgets, priority queues, deduplication, quiet hours, and emergency reserve
- SQLite migrations, integrity checks, rotating backups, and audited restore
- Optional authenticated federation, bounded synchronization, peer services, and mail relay
- Prometheus metrics and systemd watchdog integration

See [Features and maturity](docs/FEATURES.md) for implementation and validation details.

## Architecture

```text
Meshtastic clients
       │ LoRa
       ▼
Meshtastic radio ── serial, TCP, or BLE ── Outpost service
                                                ├── command router
                                                ├── airtime governor
                                                ├── SQLite store
                                                ├── environment providers
                                                ├── dashboard + API
                                                └── optional federation
```

The radio path and local database are central. Internet providers and peers are optional edges.
Read the [architecture guide](docs/ARCHITECTURE.md) for component and data-flow details.

## Requirements

- Raspberry Pi 4/5 or comparable Linux host
- 64-bit Raspberry Pi OS or Debian-family distribution
- Python 3.12+ with `venv` and `pip`
- Meshtastic-compatible radio over USB serial, TCP, or BLE
- Storage for the database, backups, and optional offline map tiles

Serial is recommended for a fixed installation. BLE is best-effort. RTL-SDR/SAME support is
optional and remains hardware-gated.

## Install

```sh
git clone https://github.com/baal-bot/Outpost-Meshtastic.git
cd Outpost-Meshtastic
sudo ./deploy/install.sh
systemctl status outpost
```

The installer creates a restricted service account, installs into `/opt/outpost/venv`, creates
`/etc/outpost/config.yaml` from the public template on first install, and starts a hardened systemd
service. Edit the node identity and radio configuration, then restart:

```sh
sudoedit /etc/outpost/config.yaml
sudo systemctl restart outpost
sudo journalctl -u outpost -f
```

Open `http://<outpost-address>:8080/`. First startup writes a one-time operator password to the
service journal; sign in and replace it immediately. Follow the complete
[installation guide](docs/INSTALLATION.md) for prerequisites, upgrades, and verification.

## Configure

The template is [`config/config.example.yaml`](config/config.example.yaml). Production uses
`/etc/outpost/config.yaml`; source runs normally use `config/config.local.yaml`. Local configuration
and runtime data are ignored by Git.

Review node identity/location, radio transport, Meshtastic channel policies, enabled modules,
environment user agent, web authentication, airtime policy, emergency behavior, and retention.
Configuration is strict: unknown keys and unsafe combinations stop startup.

Environment variables can override nested settings, for example `OUTPOST__WEB__PORT=8081`.
Read [Configuration](docs/CONFIGURATION.md) before enabling alerts, emergency keyword matching,
MQTT, AI, or federation.

## Use from Meshtastic

Commands normally start with `!`; direct messages also accept commands without it.

```text
!PING
!HELP
!NAME riverwatch
!POST general Road closure reported near the bridge
!WX TODAY
!REPORT Tree blocking the eastbound lane
!WPS 10
```

Responses may return by direct message. Availability depends on trust, modules, and channel policy.
See the [mesh command reference](docs/COMMANDS.md).

## Operate

```sh
sudo systemctl status outpost
sudo systemctl restart outpost
sudo journalctl -u outpost -f
curl http://127.0.0.1:8080/api/v1/health
```

Runtime state lives under `/var/lib/outpost`; configuration lives under `/etc/outpost`. The
dashboard creates, validates, downloads, and restores backups. Keep an off-device backup before
upgrades. See [Operations](docs/OPERATIONS.md), [Security](docs/SECURITY.md), and
[Troubleshooting](docs/TROUBLESHOOTING.md).

## Develop

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,radio]'
cp config/config.example.yaml config/config.local.yaml
# Set router.intents_file to config/intents.yaml and store.path to .data/outpost.db.
OUTPOST_CONFIG=config/config.local.yaml .venv/bin/python -m outpost
```

The service starts without a radio. Run CI-equivalent checks:

```sh
.venv/bin/ruff format --check src tests
.venv/bin/ruff check src tests
.venv/bin/mypy
.venv/bin/pytest --cov=outpost --cov-report=term
sh deploy/smoke-package.sh
```

Read [Development and contributing](docs/DEVELOPMENT.md). The detailed documents in
[`docs/outpost-spec`](docs/outpost-spec/README.md) are design references, not claims of completed
field acceptance.

## Documentation

- [Documentation index](docs/README.md)
- [Features and maturity](docs/FEATURES.md)
- [Installation](docs/INSTALLATION.md)
- [Configuration](docs/CONFIGURATION.md)
- [Mesh commands](docs/COMMANDS.md)
- [Dashboard and API](docs/DASHBOARD.md)
- [Operations](docs/OPERATIONS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Federation](docs/FEDERATION.md)
- [Security](docs/SECURITY.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Development](docs/DEVELOPMENT.md)
- [Detailed specification](docs/outpost-spec/README.md)

## Contributing

Issues and focused pull requests are welcome. Include hardware and firmware details for radio
reports, never attach real databases or precise member locations, and add tests for behavior
changes. Report security issues privately to the repository owner before public disclosure.

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
