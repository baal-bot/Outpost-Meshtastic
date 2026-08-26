# Architecture

Outpost is one Python service with explicit boundaries. FastAPI serves dashboard/API, SQLite stores
durable state, and an asynchronous supervisor owns the radio.

## Components

- **Transport supervisor:** serial/TCP/BLE connection, liveness, reconnect.
- **Inbound workers:** packet normalization, dedupe, member-scoped serialization.
- **Command router:** prefixes, DM shorthand, aliases, context, trust, modules, channels.
- **Domain services:** BBS, mail, environment, incidents, alerts, welfare, federation.
- **Airtime governor:** durable traffic classes, safety priority, channel limits, pacing.
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
 → durable outbox admission
 → airtime governor
 → radio send
 → atomic attempt/message-log completion
```

Position packets may update an allowed member position or start report/waypoint interaction; they
are not blindly converted to incidents.

## Persistence

SQLite is authoritative. Ordered SQL under `src/outpost/store/migrations` covers members, messages,
BBS/mail, incidents/alerts, environment caches, welfare, web auth, settings, audit, and federation.
The `outbound_work` state machine persists work before it becomes scheduler-eligible. Startup
recovers interrupted `sending` work, expires stale items, retains acknowledgement correlation, and
loads safety traffic ahead of ordinary traffic. Each completed radio attempt and its `message_log`
row commit together through a unique outbox ID; acknowledgement routing updates both records in one
transaction. A transport exception retries with bounded backoff and then remains operator-visible.

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
