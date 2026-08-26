# Architecture

Outpost is one Python service with explicit boundaries. FastAPI serves dashboard/API, SQLite stores
durable state, and an asynchronous supervisor owns the radio.

## Components

- **Transport supervisor:** serial/TCP/BLE connection, liveness, reconnect.
- **Inbound workers:** packet normalization, dedupe, member-scoped serialization.
- **Command router:** prefixes, DM shorthand, aliases, context, trust, modules, channels.
- **Domain services:** BBS, mail, environment, incidents, alerts, welfare, federation.
- **Airtime governor:** traffic classes, queues, channel limits, pacing.
- **Database:** ordered migrations and transactional reads/writes.
- **Web app:** authenticated APIs, static dashboards, tiles, metrics.
- **Providers:** weather, CAP, seismic, and map data with caching.
- **Federation:** peer trust, framing, replay protection, filtering, import.

## Message path

```text
radio packet
 → normalization and dedupe
 → member discovery/lookup
 → command parse and authorization
 → domain transaction
 → bounded rendering
 → airtime governor
 → radio send
```

Position packets may update an allowed member position or start report/waypoint interaction; they
are not blindly converted to incidents.

## Persistence

SQLite is authoritative. Ordered SQL under `src/outpost/store/migrations` covers members, messages,
BBS/mail, incidents/alerts, environment caches, welfare, web auth, settings, audit, and federation.
Online backup supports validation and a controlled recovery coordinator: maintenance gating,
in-flight request drain, transport/background-task quiescence, a verified pre-restore safety copy,
atomic database replacement, durable sidecar progress, and a supervisor-driven process restart.

## Offline behavior

Core commands and stored data work without WAN. Provider views may serve bounded cached data with
age/provenance or show unavailable. Maps use online tiles and an optional regional fallback. Radio
federation is internet-independent; MQTT is not.

## Trust boundaries

- A heard radio is not automatically a member.
- Dashboard auth is separate from mesh trust.
- A discovered Outpost is not paired.
- Paired peers receive only policy-allowed data.
- Provider and peer payloads remain untrusted inputs.

## Packaging

`pyproject.toml` is authoritative. Wheels include packages, migrations, dashboard assets, and
Figtree. Deployment adds systemd, host config, writable state, and optional tiles.
