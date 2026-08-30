# 02 — Architecture

**Status:** Baseline · **Prerequisite:** [01-PRD.md](01-PRD.md)

---

## 1. System context

```
                    ┌──────────────────────────────────────────┐
   Handhelds        │            Raspberry Pi 5                │
   & phone app      │                                          │
      📱 📻         │  ┌────────────────────────────────────┐  │
       │            │  │        outpost (asyncio)           │  │
       │  LoRa      │  │                                    │  │
       ▼            │  │  transport ── router ── modules    │  │
   ┌────────┐  USB  │  │      │          │          │       │  │
   │ Mesh-  │───────┼──┼──────┘          │          │       │  │
   │ tastic │  BLE  │  │            store (SQLite)  │       │  │
   │ radio  │       │  │                 │          │       │  │
   └────────┘       │  │            web (FastAPI) ──┘       │  │
                    │  └──────────┬──────────┬──────────────┘  │
                    │             │          │                 │
                    │   ┌─────────▼───┐  ┌───▼──────────┐      │
                    │   │ hailo-ollama│  │ rtl_fm|samedec│     │
                    │   │  :8000      │  │  (optional)   │     │
                    │   └─────────────┘  └───────────────┘     │
                    └──────────────┬───────────────────────────┘
                                   │ LAN (HTTP/WS)          │ WAN (optional)
                                   ▼                        ▼
                            Operator dashboard        NWS · Open-Meteo · USGS
```

**Trust boundaries**

| Boundary | Nature | Controls |
|---|---|---|
| Mesh ↔ node | Untrusted input from unauthenticated peers | Input validation, trust levels, rate limits (doc 12) |
| LAN ↔ node | Semi-trusted operator network | Session auth, CSRF, rate limits, LAN bind by default (doc 12 §6) |
| Node ↔ WAN | Outbound only, operator-configured | Allowlist of data-source hosts; no inbound; no telemetry |
| Node ↔ inference sidecar | Local loopback | Bound to 127.0.0.1; treated as untrusted output (doc 06 §7) |

---

## 2. Process model

**REQ-ARCH-001** — The system **MUST** run as a **single long-lived asyncio process**
(`outpost`) containing transport, router, modules, store, and web API. Sidecars
(`hailo-ollama`, SAME decoder) are separate OS processes reached over loopback HTTP or a
pipe.

**Rationale.** A Pi 5 with one radio and one database has no need for a message broker or
service mesh. A single process eliminates an entire class of IPC bugs and keeps idle RSS
under the 400 MB budget (REQ-PROD-004). Sidecars are separate only because they already
are: `hailo-ollama` is a vendor C++ daemon, and the SDR chain is a Unix pipeline.

**REQ-ARCH-002** — Blocking work **MUST NOT** run on the event loop. Specifically:
SQLite calls, `meshtastic` library calls (the library is synchronous and pubsub-based),
and any filesystem or subprocess work **MUST** be dispatched to a bounded thread pool via
`asyncio.to_thread` or an explicit executor. Inference and HTTP calls **MUST** use async
clients (`httpx.AsyncClient`).

**REQ-ARCH-003** — The process **MUST** be supervised by systemd with
`Restart=always`, `RestartSec=10`, and a watchdog (`WatchdogSec=120`; the app pings
`sd_notify` from a health task). See doc 12 §9.

**REQ-ARCH-004** — Startup **MUST** be resilient: the process **MUST** start successfully
and serve the web API even when the radio is absent, the database is empty, or the
inference sidecar is down. Degraded subsystems report `unhealthy` on `/api/v1/health` and
retry with exponential backoff; they **MUST NOT** abort startup.

---

## 3. Layered decomposition

Layers are strictly ordered. **A layer may only import from layers below it.** This is
enforced by a lint rule (doc 14 §6).

```
┌─────────────────────────────────────────────────────────────┐
│ L5  Interfaces      web/ (FastAPI, WS)   commands/ (over-air)│
├─────────────────────────────────────────────────────────────┤
│ L4  Modules         bbs/  watch/  env/  ai/  fed/            │
├─────────────────────────────────────────────────────────────┤
│ L3  Router          dispatch · sessions · context stack      │
├─────────────────────────────────────────────────────────────┤
│ L2  Transport       radio link · framing · Airtime Governor  │
├─────────────────────────────────────────────────────────────┤
│ L1  Core            store/ · security/ · config · events     │
└─────────────────────────────────────────────────────────────┘
```

### L1 — Core

- **`config`** — `pydantic-settings` model. Precedence: environment (`OUTPOST_*`) >
  `config.local.yaml` > `config.yaml` > built-in defaults. Fully typed, validated at
  startup, fail-fast on invalid values. Hot-reloadable subset marked in the model.
- **`store`** — SQLite (WAL). Repository classes per aggregate; no ORM. Raw
  parameterised SQL, migrations as numbered `.sql` files applied at startup. See doc 05.
- **`security`** — mesh identity resolution, trust levels, authorisation, and rate-limiting
  primitives. Mesh authorization uses six ordered trust levels (doc 12 §3). Dashboard access uses
  a separate named-account role boundary; web roles deliberately do not map onto or mutate member
  trust. See docs 11 and 12.
- **`events`** — in-process pub/sub (`EventBus`). Typed dataclass events, async handlers,
  fan-out with per-subscriber error isolation. This is how modules talk to each other and
  to the WebSocket hub without importing each other.

**REQ-ARCH-005** — Modules **MUST NOT** import each other directly. Cross-module
communication is via the `EventBus` (for notifications) or via an explicitly-registered
service interface obtained from the app container (for queries). The AI tool registry is
the one sanctioned place where a module exposes a callable surface to another.

### L2 — Transport

Owns the radio and is the **only** code permitted to transmit. See doc 03.

- `RadioLink` — connection lifecycle, reconnect, pubsub bridge to asyncio queues
- `InboundPipeline` — decode, deduplicate, normalise, attribute to a member
- `AirtimeGovernor` — the mandatory outbound scheduler
- `Chunker` — response splitting policy
- `Framing` — CBOR framing for the federation portnum

### L3 — Router

Receives normalised inbound messages, resolves identity and session, dispatches to a
handler, and hands the result to the Governor. See doc 04.

### L4 — Modules

Feature areas. Each module:
- registers commands with the router
- registers AI tools with the tool registry
- registers REST routers with the web app
- subscribes to and publishes `EventBus` events
- owns its repositories and its migration files

**REQ-ARCH-006** — Every module **MUST** implement the `Module` protocol:

```python
class Module(Protocol):
    name: str
    def enabled(self, cfg: Config) -> bool: ...
    async def startup(self, ctx: AppContext) -> None: ...
    async def shutdown(self) -> None: ...
    def commands(self) -> Sequence[CommandSpec]: ...
    def tools(self) -> Sequence[ToolSpec]: ...
    def api_router(self) -> APIRouter | None: ...
    async def health(self) -> HealthReport: ...
```

Module discovery is explicit registration in `app.py` — **not** filesystem plugin scanning.
Third-party plugin loading is deferred past Phase 6 and is deliberately not specified here.

### L5 — Interfaces

Two front doors onto the same domain services: `commands/` (over-the-air) and `web/`
(HTTP/WS). Neither contains business logic; both are thin adapters.

**REQ-ARCH-007** — Any state-changing operation **MUST** be reachable from both interfaces
and **MUST** execute the same domain service with the same authorisation checks. Divergence
between what is possible over the radio and what is possible on the dashboard is a defect.

---

## 4. Technology stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.12+ | Matches the Meshtastic reference library; available on Trixie |
| Async runtime | `asyncio` + `uvloop` | uvloop optional, guarded by import |
| Radio | `meshtastic` (PyPI) | Synchronous + `pypubsub`; bridged to asyncio in `RadioLink` |
| Web framework | FastAPI + Uvicorn | Also serves the SPA's built assets |
| Validation | Pydantic v2 | Config, API schemas, AI tool argument schemas |
| Database | SQLite 3.40+, WAL | stdlib `sqlite3` in a thread executor. **No ORM.** |
| Migrations | Numbered `.sql` files + a tiny runner | No Alembic; the schema is small and the runtime is constrained |
| HTTP client | `httpx` (async) | Data sources, inference providers |
| Serialisation (federation) | CBOR (`cbor2`) | Compact binary framing on the private portnum |
| Geospatial | `shapely` (point-in-polygon), `pyproj` not required | Haversine implemented locally; avoid heavy GIS deps |
| Templating/SPA | Preact + Vite, built to static assets | Small bundle; no Node runtime on the Pi |
| Charts/map | Leaflet + locally-cached tiles | **Must** work with no internet; see doc 11 §5 |
| Metrics | `prometheus-client` | `/metrics` on the LAN interface |
| Logging | `structlog` → JSON | Journald + rotating file |
| Testing | `pytest`, `pytest-asyncio`, `hypothesis` | Plus the mesh simulator, doc 14 |
| Packaging | `hatchling`, wheel + systemd unit | `deploy/install.sh` for the Pi |

**REQ-ARCH-008** — Dependencies **MUST** be pinned with a lockfile and **MUST** install
cleanly on `linux/arm64` from wheels where available. Any dependency requiring compilation
on the Pi **MUST** be justified in [15-DECISIONS.md](15-DECISIONS.md).

**REQ-ARCH-009** — The system **MUST NOT** require Docker. A Docker Compose file **MAY** be
provided for development, but the reference deployment is a systemd unit and a venv,
because the Pi's storage and memory budget do not favour containers and because the radio
device and PCIe accelerator passthrough add avoidable failure modes.

**Why no ORM.** The schema is ~20 tables, the query patterns are known and few, and a Pi's
SD card makes query shape matter. Raw SQL in repository classes is faster, smaller, more
inspectable, and avoids an entire dependency's worth of ARM wheel risk. This is a
deliberate decision recorded as ADR-004.

---

## 5. Request lifecycle

The canonical path for an inbound radio message. An agent implementing the router should
treat this as the reference sequence.

```
1.  RadioLink            pubsub callback → thread-safe enqueue → asyncio.Queue
2.  InboundPipeline      decode; drop duplicates (packet id LRU); normalise to InboundMessage
3.  Identity             resolve mesh node id → Member (create provisional if unknown)
4.  RateLimit            token bucket per member per class; over-limit → drop or terse deny
5.  Session              load/create Session; apply idle expiry; read context stack
6.  Router               match command grammar → CommandSpec, or fall through to AI handler
7.  Authorise            check trust level + RBAC for the resolved command
8.  Handler              module service executes; may mutate store; emits EventBus events
9.  Compose              handler returns a Response (structured, not a string)
10. Render               Response → text using the terse register + locale rules
11. Chunk                split into ≤200-byte parts per policy; refuse if over part budget
12. Governor             enqueue with class + priority; scheduled against airtime budget
13. RadioLink            transmit with wantAck for DMs; record outcome
14. Audit                persist inbound + outbound + latency to message_log
```

**REQ-ARCH-010** — Steps 9–10 **MUST** be separated: handlers return structured `Response`
objects, never pre-formatted strings. Rendering is a distinct concern so that the same
response can be rendered for the radio (terse, chunked), the dashboard (rich), and the AI
tool interface (structured JSON).

**REQ-ARCH-011** — Every step **MUST** be individually observable: step timings recorded as
histogram metrics, and the full trace attached to the `message_log` row at debug level.

**REQ-ARCH-012** — An unhandled exception at any step **MUST** be caught at the router
boundary, logged with full context, counted as a metric, and answered over the air with a
single generic line (`Err. Try again or send HELP.`). Internal detail **MUST NOT** be
transmitted.

---

## 6. Concurrency model

**REQ-ARCH-013** — Inbound processing **MUST** be concurrent but bounded: a worker pool of
N (default 4) coroutines consuming the inbound queue, so one slow AI call cannot block the
BBS.

**REQ-ARCH-014** — Per-member serialisation: messages from the *same* member **MUST** be
processed in order, one at a time, to keep session state coherent. Implement with a
per-member `asyncio.Lock` held for the handler duration, with a timeout (default 60 s)
after which the lock is force-released and a warning logged.

**REQ-ARCH-015** — AI inference **MUST** be serialised globally behind a semaphore of size
1 by default (the Hailo device is assumed non-concurrent — see doc 06 §2.5) and **MUST**
have a queue depth limit; requests beyond the limit are rejected immediately with a terse
"busy, try shortly" rather than queued indefinitely.

**REQ-ARCH-016** — The store **MUST** use a single writer. Reads may be concurrent (WAL
allows this); writes go through a serialised executor to avoid `SQLITE_BUSY` storms on slow
storage. Multi-record domain mutations **MUST** use `Database.transaction()` so their reads
and writes run on that writer between `BEGIN IMMEDIATE` and one commit. Exceptions and task
cancellation roll the complete unit back; uncommitted records are never visible to WAL readers.

---

## 7. Configuration

Single YAML file, environment overlay, fully typed.

**REQ-ARCH-022** — `src/outpost/config.py` is the **authoritative schema**. The block below
is the complete set of top-level keys with their shipped defaults, and every key with a
normative default anywhere in this spec set **MUST** appear in it. If a later document
introduces a key, it **MUST** also be added here. A key that exists in prose but not in
`config.py` is a defect.

```yaml
node:
  name: "Cedar Ridge Outpost"
  short_name: "CRO"            # ≤4 bytes, alternate command prefix, appears in alerts
  operator_contact: "ray@example.org"
  emergency_number: "911"
  timezone: "America/New_York"
  locale: "en_US"
  units: metric                # metric | imperial ; default for members
  location: { lat: 44.1234, lon: -72.5678 }   # node's own position; optional
  disclaimer: "Community system. Not 911."

radio:
  transport: serial            # serial | tcp | ble
  serial: { port: "/dev/ttyUSB0" }
  tcp:    { host: "192.168.1.50", port: 4403 }
  ble:    { address: "AA:BB:CC:DD:EE:FF" }
  reconnect: { initial_s: 2, max_s: 120, jitter: 0.2 }
  liveness_timeout_s: 300
  federation_portnum: 260      # PRIVATE_APP range 256-511
  bridge_node_ids: []          # relays whose traffic is read but never replied to

airtime:
  budget_percent: 8.0          # node's max share of channel airtime, rolling 1h
  utilisation_ceiling: 25.0    # refuse to TX above this measured channel utilisation
  emergency_reserve_percent: 4.0   # extra channel share usable ONLY by `critical` alerts
  min_gap_s: 2.0
  interpart_delay_s: 12
  queue_max_items: 500
  dedupe_window_s: 300
  quiet_hours: { start: "22:00", end: "06:00", classes: [digest, bulletin, federation] }
  class_shares:                # fraction of budget_percent, must sum to <= 1.0
    alert: 0.30
    reply: 0.30
    ai: 0.15
    bulletin: 0.05
    digest: 0.10
    federation: 0.10
  max_parts: { reply: 3, ai: 2, digest: 4, alert: 2, bulletin: 2, federation: 1 }

channels:
  # index → policy. Index 0 is the primary/public channel.
  # bbs: none | read_only | full   — what BBS commands are permitted here
  # alerts: may members view warnings/incidents or invoke responder alert/event operations here
  # accept_reports: may members create or update incident reports from this channel
  0: { name: "public",  ai: false, bbs: read_only, alerts: true, accept_reports: true  }
  2: { name: "outpost", ai: true,  bbs: full,      alerts: true, accept_reports: true  }
  3: { name: "watch",   ai: false, bbs: none,      alerts: true, accept_reports: true  }

router:
  prefix: "!"
  session_idle_minutes: 30
  page_ttl_minutes: 15
  inbound_workers: 4
  inbound_queue_max: 256
  member_lock_timeout_s: 60
  intents_file: "config/intents.yaml"

modules:
  bbs:   { enabled: true }
  ai:    { enabled: false }    # Phase 2
  watch: { enabled: false }    # Phase 3
  env:   { enabled: false }    # Phase 4
  fed:   { enabled: false }    # Phase 5

bbs:
  immediate_max_per_hour: 3
  immediate_enabled: true
  self_delete_minutes: 30

mail:
  hold_unknown_days: 14

security:
  require_approval: false      # handle claims need operator approval before `member`
  coarse_precision_m: 500
  global_rate_ceiling: 60      # inbound commands/min before the circuit breaker
  safety_repeat_window_seconds: 120
  safety_attempt_retention_hours: 72

ai:
  provider: hailo_vlm          # hailo_vlm | hailo | llamacpp | ollama | openai_compat | null
  model: "Qwen3-VL-2B-Instruct"
  hailo_vlm:  { model_path: "/var/lib/outpost/models/Qwen3-VL-2B-Instruct.hef" }
  hailo:      { base_url: "http://127.0.0.1:8000" }
  llamacpp:   { base_url: "http://127.0.0.1:8080" }
  ollama:     { base_url: "http://127.0.0.1:11434" }
  openai_compat: { base_url: "", api_key_env: "OUTPOST_AI_KEY" }
  budget: { context_tokens: 2048, reserve_output_tokens: 220,
            max_evidence_tokens: 820, max_history_tokens: 200 }
  max_concurrency: 1
  queue_depth: 3
  timeout_s: 45
  keep_warm: { enabled: true, interval_s: 240 }
  persona_addendum: ""         # ≤40 tokens; cannot remove structural clauses
  circuit_breaker: { failures: 5, window_minutes: 10, open_minutes: 15 }

watch:
  position_max_age_minutes: 30
  dedupe_radius_m: 500
  dedupe_window_minutes: 120
  alert_repeat_max: 3
  alert_repeat_interval_minutes: 20
  emergency_keywords_enabled: false
  emergency_keywords: ["sos", "mayday", "emergency", "help me", "911"]
  emergency_cooldown_minutes: 10
  escalation:                  # see doc 08 §6 for the full shape
    urgent:
      stages:
        - { after_minutes: 0,  notify: responders, channels: [3] }
        - { after_minutes: 10, notify: trusted,    channels: [3] }
        - { after_minutes: 20, notify: all,        channels: [0, 3] }
      ack_threshold: 2
    critical:
      stages:
        - { after_minutes: 0,  notify: all, channels: [0, 3] }
        - { after_minutes: 10, notify: all, channels: [0, 3], repeat: true }
      ack_threshold: 3

env:
  user_agent: "(CHANGE-ME.example.org, ray@example.org)"   # NWS requires this
  fresh_threshold_minutes: 60
  providers: { primary: nws, fallback: open_meteo }
  region: { state: "VT", counties: ["050027"] }             # SAME/FIPS codes
  poll: { forecast_minutes: 15, alerts_seconds: 120, quake_seconds: 60 }
  cap_gate: { status: [Actual], severity: [Extreme, Severe],
              urgency: [Immediate, Expected], exclude_certainty: [Unlikely] }
  same:
    enabled: false
    device: "rtl_sdr:0"
    frequency_hz: 162550000
    silence_alarm_minutes: 720

fed:
  hello_interval_hours: 12
  sync_interval_minutes: 60
  sync_retry_minutes: 10
  max_items_per_cycle: 20
  max_fragments: 8
  reassembly_timeout_s: 300
  incident_radius_km: 25
  peer_stale_hours: 72
  peer_flood_threshold: 200    # inbound items/hour before auto-pause

web:
  bind: "0.0.0.0"              # all local interfaces; the installer opens no WAN port.
                               # Set a specific LAN address to restrict further.
  port: 8080
  auth: { session_hours: 12 }
  tls: { enabled: false, cert: "", key: "" }

store:
  path: "/var/lib/outpost/outpost.db"
  retention:
    posts_days: 90
    mail_days: 180
    incidents_days: 365
    position_days: 7
    message_log_days: 30
    ai_interaction_days: 90
    message_log_max_rows: 500000
  backup: { enabled: true, cron: "0 3 * * *", keep: 14,
            path: "/var/lib/outpost/backups", gpg_recipient: "" }
  maintenance_cron: "0 3 * * *"
```

**REQ-ARCH-017** — Config validation **MUST** reject at startup:

- `airtime.class_shares` summing above 1.0, or naming a class outside the closed set
- an incomplete `airtime.class_shares` or `airtime.max_parts` map
- `airtime.budget_percent + airtime.emergency_reserve_percent > 20.0`
- `airtime.budget_percent + airtime.emergency_reserve_percent >= airtime.utilisation_ceiling`
- invalid `airtime.quiet_hours` times or class names
- an unknown `node.timezone`, unavailable host IANA timezone database, or malformed `node.locale`
- channel indices outside 0–7, or a channel index absent from the radio's configuration
- an AI provider with no reachable base URL when `modules.ai.enabled`
- `env.user_agent` still containing `CHANGE-ME` when `modules.env.enabled` (REQ-WX-005)
- any path the process cannot write

The tolerant intent map is validated during startup. A missing/unreadable file, malformed YAML,
invalid entry, or invalid regular expression **MUST** produce indexed warnings and a persistent
degraded-readiness state while built-in exact intents remain available. Transient read/parse
failure **MUST NOT** be cached as a successful timestamp.

**REQ-ARCH-018** — A subset of config (airtime budgets and class shares, channel policy,
quiet hours, retention, escalation policy, AI persona addendum, intents table) **MUST** be
hot-reloadable via `SIGHUP` and from the dashboard, without dropping the radio connection.

Board definitions are **not** config — they are `board` table rows managed from the
dashboard (REQ-BBS-002). Only the *seed* set shipped for first run lives in the repository.

---

## 8. Deployment topology

**Single node (Phases 0–4).** One Pi, one radio, one DB, dashboard on the LAN.

**Federated (Phase 5).** Multiple independent Outpost nodes, each fully autonomous, that
replicate selected boards, mail-in-transit, and incidents over the mesh using a private
portnum. There is **no** central node, **no** master, and **no** cloud component. Each
operator opts in per-peer and per-board.

**REQ-ARCH-019** — Federation **MUST** be safe to disable unilaterally at any time; a node
that stops federating **MUST** remain fully functional on its local content.

**REQ-ARCH-020** — A node **MUST** function correctly when it is the only Outpost node on
a mesh shared with non-Outpost Meshtastic users, and **MUST NOT** emit federation traffic
when no peer nodes are known.

---

## 9. Failure modes and required behaviour

| Failure | Required behaviour |
|---|---|
| Radio disconnects (USB unplug, firmware reboot) | Detect immediately on a transport-level disconnect event, or within `radio.liveness_timeout_s` (default 300) if the link fails silently (REQ-TRANSPORT-008); exponential backoff reconnect; queue outbound (bounded, with TTL per class); dashboard shows `radio: down`; **never** exit |
| BLE link flaps | Same, plus a hard reconnect cycle that fully tears down the Bleak client (Linux BlueZ requires it); after 5 consecutive failures, log a recommendation to switch to serial |
| Channel utilisation exceeds ceiling | Governor stops scheduling all classes except `alert`; emits a metric; dashboard warns |
| Inference sidecar/device down or 5xx | AI commands answer with one terse line offering non-AI alternatives; module health `degraded`; retry with backoff; **never** block the BBS. If AI is configured as readiness-required, deployment health is 503 until it recovers; disabled or best-effort AI is non-blocking. |
| Inference cold start (25–40 s) | Keep-warm task pings the model on an interval; if a request lands cold, respond immediately with `Thinking…` **only if** the requester is on a DM and the airtime class allows, else answer late without the placeholder |
| Model returns oversized output | Truncate at the render step to the configured part budget with an ellipsis marker; never emit an unbounded chunk train |
| SQLite `SQLITE_BUSY` | Serialised writer plus `busy_timeout=5000`; on persistent failure, degrade to read-only mode and alert the operator |
| Disk full | Detected by a health check at 90%; retention pruning runs early; write-path returns a terse error; alerts still transmit |
| WAN down | All internet-backed data sources serve cached values, age-stamped; SAME path (if present) continues to work; no user-visible failure other than staleness labels |
| Power loss | WAL + `synchronous=NORMAL`; on restart, integrity check; recover unexpired durable outbound work in safety order; sessions are volatile and simply reset |
| Clock skew (no RTC on a Pi) | On boot, if system time is implausible (< build date), mark timestamps `unsynced` and prefer monotonic ordering; NTP when WAN available; an RTC module is recommended in the install docs |

**REQ-ARCH-021** — The outbound queue **MUST** be bounded (default 500 items) with per-class
TTLs (e.g. `reply` 5 min, `digest` 1 h, `alert` 24 h). Expired items are dropped with a
counted metric, never transmitted stale. Admission **MUST** commit to durable storage before an
item becomes eligible to transmit. Startup **MUST** reconcile interrupted attempts, pending
acknowledgements, supersession, deduplication, and priority without producing duplicate log rows.

---

## 10. Architectural decisions

Recorded in full in [15-DECISIONS.md](15-DECISIONS.md). Summary:

| ADR | Decision |
|---|---|
| ADR-001 | Single asyncio process, sidecars only where the vendor forces it |
| ADR-002 | Serial is the default radio transport; BLE best-effort |
| ADR-003 | SQLite, WAL, single writer, no ORM |
| ADR-004 | Raw SQL repositories instead of an ORM |
| ADR-005 | Mandatory Airtime Governor as sole egress |
| ADR-006 | Hybrid session model (stateless verbs + optional context stack) |
| ADR-007 | Provider-abstracted inference; no hard dependency on Hailo |
| ADR-008 | Retrieval-grounded, tool-calling AI rather than chat passthrough |
| ADR-009 | Federation on a private portnum with CBOR, not on text channels |
| ADR-010 | Explicit module registration, no filesystem plugin scanning in v1 |
