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

<!-- capability-summary:start -->
Capability evidence snapshot (2026-08-29): 16 tracked — Automated-tested 9, Simulated 2, Single-node field-tested 1, Two-node field-tested 2, Hardware-gated 2. No capability is marked production-ready. See [Features and maturity](docs/FEATURES.md) for test, field,
hardware, revision, limitation, and roadmap evidence.
<!-- capability-summary:end -->

## What it provides

- First-class Meshtastic interface with guided, capability-aware direct-message screens and
  command shortcuts
- Member handles, trust levels, approval controls, and recently heard radios
- Bulletin boards, threaded posts, subscriptions, search, digests, private mail, and an audited
  operator operations inbox
- Incident reporting, confirmations, disputes, responder alerts, and map operations
- Welfare events, check-ins, reviewed solicitation, rosters, and CSV export
- Weather, forecasts, CAP alerts, astronomy, earthquakes, member positions, and waypoints
- Responsive dashboards for operations, radio, members, BBS, mail, environment, federation, and
  named operator access
- Airtime budgets, priority queues, deduplication, quiet hours, and emergency reserve
- SQLite migrations, integrity checks, rotating backups, and audited restore
- Optional authenticated federation, bounded synchronization, peer services, and mail relay
- Local AI assistant with guarded retrieval, review tooling, and target-hardware provider evaluation
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
- Python 3.12 or 3.13 with `venv` and `pip`
- Meshtastic-compatible radio over USB serial, TCP, or BLE
- Storage for the database, backups, and optional offline map tiles

Serial is recommended for a fixed installation. BLE is best-effort. Optional RTL-SDR/SAME support
is receive-only, review-gated, and validated on Raspberry Pi hardware; local antenna/transmitter
coverage still determines field reliability.

## Install

```sh
git clone https://github.com/baal-bot/Outpost-Meshtastic.git
cd Outpost-Meshtastic
sudo ./deploy/install.sh
systemctl status outpost
```

The installer creates a restricted service account, stages versioned releases under
`/opt/outpost/releases`, creates `/etc/outpost/config.yaml` from the public template on first
install, and starts a hardened systemd service. Edit the node identity and radio configuration,
then restart:

```sh
sudoedit /etc/outpost/config.yaml
sudo systemctl restart outpost
sudo journalctl -u outpost -f
```

Interactive first install includes a guided setup. Upgrades are staged as versioned releases with
a verified database snapshot, atomic activation, health-gated automatic rollback, and an explicit
`sudo outpost-rollback` command. Use `./deploy/update.sh <tag-or-ref>` from a clean checkout for a
safe GitHub update.

Open `http://<outpost-address>:8080/`. First startup creates a short-lived, one-time setup token;
retrieve it locally with `sudo outpost-setup-token show`, then choose a permanent password. The
token is never written to the service journal. Follow the complete
[installation guide](docs/INSTALLATION.md) for prerequisites, upgrades, and verification.

The migrated first account is named `operator` and has the Administrator role. From **Access**, an
administrator can create named Administrator, Operator, or Read-only / wallboard accounts, enable
offline TOTP authentication, save one-use recovery codes, and revoke active browser sessions.
Web access is normally limited to the local operator while community users interact over the mesh.
The optional wallboard role receives only a redacted aggregate status contract; HTTPS remains an
operator-supplied option rather than an installation requirement for offline field deployments.

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

Direct-message the Outpost and send `?` to open the guided interface. Reply with a displayed number;
forms ask for one value at a time, and `0` returns Home. Available choices reflect enabled modules
and the sender's trust level.

Commands remain fast shortcuts from any screen. They normally start with `!` on channels; direct
messages accept them without a prefix.

```text
?
SITREP
WX TODAY
WARN
BOARDS
REPORT Tree blocking the eastbound lane
```

Responses may return by direct message. Availability depends on trust, modules, and channel policy.
See the [Meshtastic interface guide](docs/MESH-INTERFACE.md) and
[mesh command reference](docs/COMMANDS.md).

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
./tools/pre-push.sh
.venv/bin/ruff check src tests tools/build_release_metadata.py tools/check_capabilities.py \
  tools/check_commands.py tools/check_ci_evidence.py tools/check_dependency_lock.py \
  tools/check_mypy_ratchet.py tools/pytest_evidence_plugin.py tools/verify_release.py \
  deploy/configure.py deploy/render_avahi.py
.venv/bin/mypy
.venv/bin/pytest --cov=outpost --cov-report=term --cov-report=json:coverage.json
.venv/bin/pytest -m production_wiring --cov=outpost \
  --cov-report=json:production-coverage.json
.venv/bin/python tools/check_critical_coverage.py coverage.json \
  --production-report production-coverage.json
sh deploy/smoke-package.sh
```

Read [Development and contributing](docs/DEVELOPMENT.md). The detailed documents in
[`docs/outpost-spec`](docs/outpost-spec/README.md) are design references, not claims of completed
field acceptance.

## Documentation

- [Documentation index](docs/README.md)
- [Features and maturity](docs/FEATURES.md)
- [Installation](docs/INSTALLATION.md)
- [Field-appliance onboarding](docs/ONBOARDING.md)
- [Configuration](docs/CONFIGURATION.md)
- [Mesh commands](docs/COMMANDS.md)
- [Meshtastic interface](docs/MESH-INTERFACE.md)
- [Dashboard and API](docs/DASHBOARD.md)
- [Dashboard design system](docs/UI-DESIGN-SYSTEM.md)
- [Operations](docs/OPERATIONS.md)
- [Data retention and storage](docs/RETENTION.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Federation](docs/FEDERATION.md)
- [Security](docs/SECURITY.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Development](docs/DEVELOPMENT.md)
- [Releases and artifact verification](docs/RELEASES.md)
- [Release evidence checklist](docs/RELEASE-CHECKLIST.md)
- [Detailed specification](docs/outpost-spec/README.md)

## Contributing

Issues and focused pull requests are welcome. Include hardware and firmware details for radio
reports, never attach real databases or precise member locations, and add tests for behavior
changes. Report security issues privately to the repository owner before public disclosure.

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
