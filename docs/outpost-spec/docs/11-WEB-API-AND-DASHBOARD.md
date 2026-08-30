# 11 — Web API & Dashboard

**Status:** Baseline · **Phase:** 1 onward · **Prerequisite:** [02-ARCHITECTURE.md](02-ARCHITECTURE.md)
**Implements:** `src/outpost/web/`, `web-ui/`

---

## 1. Purpose and posture

The dashboard is the operator's window and the coordinator's command post. It is **not** the
primary user interface — the radio is. Every feature here exists to serve someone who has a
screen, while the system remains complete for someone who has only a handheld.

**REQ-API-001** — The web interface **MUST** bind to the LAN by default and **MUST NOT** be
exposed to the internet by the shipped configuration. The install script **MUST NOT** open a
firewall port to WAN.

**REQ-API-002** — The web interface **MUST** remain operational with no internet: no CDN
application assets, external fonts, or analytics. Online map tiles **MAY** be used when available,
but coordinates, markers, controls, detail cards, and an installed regional fallback **MUST**
continue working without them.

**REQ-API-003** — The SPA **MUST** be pre-built and vendored into
`src/outpost/web/static/`. The Pi **MUST NOT** need Node.js at runtime or install time.

**REQ-API-004** — The dashboard **MUST** be usable on a phone browser. A coordinator in the
field with a phone on the node's Wi-Fi is a primary scenario during an event.

---

## 2. Authentication

**REQ-API-005** — Auth modes, config-driven:

| Mode | Behaviour |
|---|---|
| `password` (default) | Named local accounts with Argon2id passwords and roles; the first account is `operator` |
| `users` | Compatibility name for the named-account mode; no cloud identity dependency |
| `none` | Only permitted when bound to `127.0.0.1`; **MUST** refuse to start otherwise |

**REQ-API-006** — First run **MUST** generate a short-lived one-time setup token, retain only its
hash in the database, store the plaintext in a restrictive local file, and **MUST NOT** emit it to
the normal console or journal. The first successful login consumes it, **MUST** force a permanent
password, and completion **MUST** invalidate every bootstrap and dashboard session secret. Recovery
requires local privileged access.

**REQ-API-007** — Sessions **MUST** be HttpOnly, SameSite=Lax cookies with a configurable
lifetime (default 12 h), server-side revocable individually or per account, attributed to a named
account, and invalidated on password change.

**REQ-API-007a** — Web accounts **MUST** support Administrator, Operator, and Read-only / wallboard
roles. Web authority is separate from mesh member trust. The last enabled Administrator **MUST NOT**
be demoted or disabled. Operator Access **MUST** separately inventory every mesh identity with
`operator` trust and **MAY** maintain a one-to-one attribution link from an Administrator or
Operator web account to its actual handheld radio.

**REQ-API-007b** — Accounts **MUST** support offline TOTP and one-use recovery codes. Sensitive
trust, federation-policy, restore, and emergency actions **MUST** require a recent password and,
when configured, second-factor confirmation.

**REQ-API-008** — All state-changing endpoints **MUST** require a CSRF token or an
`Authorization: Bearer` API token. Login **MUST** be rate-limited (5 attempts / 15 min per
IP) with a constant-time comparison.

**REQ-API-009** — API tokens **MUST** be supported for automation, scoped read-only or
read-write, revocable, and audit-logged on creation and use.

---

## 3. REST API

**REQ-API-010** — All endpoints under `/api/v1`, JSON, `snake_case` fields, ISO-8601 UTC
timestamps in responses (storage remains epoch integers per REQ-DATA-004).

**REQ-API-011** — FastAPI route/schema validation **MUST** run in CI, but the field appliance
**MUST NOT** expose OpenAPI, Swagger, or ReDoc endpoints. Operator documentation is bundled and
available offline without disclosing the complete control-plane route catalogue.

### 3.1 Endpoint catalogue

**Authentication**
```
GET    /api/v1/auth/setup                 # setup state only; unauthenticated, never the token
POST   /api/v1/auth/login                 # permanent password or active one-time setup token
GET    /api/v1/auth/session
POST   /api/v1/auth/password              # completes setup/change; invalidates every session
POST   /api/v1/auth/logout
POST   /api/v1/auth/step-up               # short protected-action confirmation window
GET/POST/PATCH /api/v1/auth/accounts...   # administrator-only account lifecycle
GET/DELETE /api/v1/auth/sessions...       # inventory and revocation
POST/DELETE /api/v1/auth/mfa...           # TOTP enrollment/confirmation/disable
```

**System**
```
GET    /api/v1/health                     # liveness + per-module health; unauthenticated
GET    /api/v1/status                     # node, radio, airtime, queues, versions
GET    /api/v1/config                     # redacted config
PATCH  /api/v1/config                     # hot-reloadable subset only
POST   /api/v1/config/reload
GET    /api/v1/metrics                    # Prometheus text (also at /metrics)
GET    /api/v1/audit
```

The audit read surface supports bounded pagination plus time, actor, action, target, and outcome
filters. Credential-shaped values in event detail are redacted before the API returns them.

**Mesh**
```
GET    /api/v1/mesh/nodes                 # radio node DB + member join
GET    /api/v1/mesh/messages              # message_log, filterable, paginated
POST   /api/v1/mesh/send                  # operator-initiated message (goes via Governor)
GET    /api/v1/mesh/airtime               # rolling window, by class
GET    /api/v1/mesh/queue                 # current outbound queue
DELETE /api/v1/mesh/queue/{id}            # cancel a queued item
```

**Members**
```
GET    /api/v1/members
GET    /api/v1/members/{id}
PATCH  /api/v1/members/{id}               # trust, handle, notes, mute
POST   /api/v1/members/{id}/block
```

**BBS**
```
GET    /api/v1/boards
POST   /api/v1/boards
PATCH  /api/v1/boards/{id}
GET    /api/v1/boards/{id}/threads
POST   /api/v1/boards/{id}/threads
GET    /api/v1/threads/{id}
POST   /api/v1/threads/{id}/posts
PATCH  /api/v1/posts/{id}                 # hide/unhide, edit (operator)
GET    /api/v1/search?q=
```

**Mail** *(operator visibility is explicit and audited — see REQ-API-016)*
```
GET    /api/v1/mail?state=&member=
GET    /api/v1/mail/{id}
```

**Watch**
```
GET    /api/v1/incidents?status=&type=&bbox=&since=
POST   /api/v1/incidents
GET    /api/v1/incidents/{id}
PATCH  /api/v1/incidents/{id}             # status, severity, resolution
POST   /api/v1/incidents/{id}/updates
GET    /api/v1/alerts
POST   /api/v1/alerts                     # raise
POST   /api/v1/alerts/{id}/cancel
GET    /api/v1/alerts/{id}/acks
GET    /api/v1/events                     # watch events
POST   /api/v1/events
GET    /api/v1/events/{id}/roster
GET    /api/v1/events/{id}/roster.csv
```

**Environment**
```
GET    /api/v1/wx/current
GET    /api/v1/wx/forecast
GET    /api/v1/wx/alerts
GET    /api/v1/wx/sources                 # provider health, cache ages
POST   /api/v1/wx/refresh
GET    /api/v1/waypoints
POST   /api/v1/waypoints
```

**AI**
```
GET    /api/v1/ai/status                  # provider, model, health, queue
GET    /api/v1/ai/interactions            # review queue
POST   /api/v1/ai/interactions/{id}/rate
POST   /api/v1/ai/ask                     # operator test console; does NOT transmit
GET    /api/v1/ai/kb
POST   /api/v1/ai/kb
PATCH  /api/v1/ai/kb/{id}
POST   /api/v1/ai/kb/reindex
```

**Federation**
```
GET    /api/v1/fed/peers
POST   /api/v1/fed/peers/{id}/approve
POST   /api/v1/fed/peers/{id}/pause
DELETE /api/v1/fed/peers/{id}
POST   /api/v1/fed/sync                   # trigger a cycle
GET    /api/v1/fed/export                 # sneakernet bundle
POST   /api/v1/fed/import
```

**REQ-API-012** — Every state-changing endpoint that results in a transmission **MUST** route
through the Airtime Governor and **MUST** return the queued item's id so the UI can show its
scheduled status. The web API **MUST NOT** be a bypass around airtime policy.

**REQ-API-013** — List endpoints **MUST** be cursor-paginated (`?cursor=&limit=`, default 50,
max 200) and **MUST** return `next_cursor`.

**REQ-API-014** — Errors **MUST** use a uniform envelope:
```json
{"error": {"code": "rate_limited", "message": "…", "detail": {...}}}
```

**REQ-API-015** — `POST /api/v1/ai/ask` **MUST NOT** transmit anything to the mesh. It is a
test console for the operator to evaluate the assistant safely.

**REQ-API-016** — Operator access to mail **MUST** be audit-logged per message viewed, and the
dashboard **MUST** display a standing notice that mail is operator-readable, matching what
members are told (REQ-BBS-038). The system must not quietly do what it tells users it does
openly.

---

## 4. WebSocket

**REQ-API-017** — `GET /api/v1/ws` **MUST** provide a live event stream, authenticated by the
session cookie, with a subscription model so a phone on a slow link does not receive
everything.

```jsonc
// client → server
{"op":"subscribe","topics":["mesh.message","incident","alert","airtime","health"]}

// server → client
{"topic":"mesh.message","ts":"2026-08-24T14:22:03Z","data":{...}}
```

**REQ-API-018** — Topics **MUST** include at minimum: `mesh.message`, `mesh.node`,
`airtime`, `health`, `incident`, `alert`, `checkin`, `post`, `mail`, `ai.interaction`,
`fed.peer`.

**REQ-API-019** — The WS hub **MUST** subscribe to the internal `EventBus` and **MUST NOT**
poll the database.

**REQ-API-020** — Backpressure: a slow client **MUST** be dropped after a bounded send-queue
overflows, with a close code, never allowed to block the event loop.

**REQ-API-021** — Heartbeat every 30 s; the client reconnects with exponential backoff and
re-subscribes.

---

## 5. Dashboard views

### 5.1 Overview (landing)

Single screen answering "is this thing working?" — designed to be readable across a room.

- Radio link state, uptime, firmware/region/preset
- **Airtime gauge**: node's own rolling-1h share vs budget, plus measured channel utilisation
  vs the 25% ceiling. Two numbers, prominent. This is the number an operator must watch.
- Outbound queue depth by class
- Active alerts (prominent, colour-coded by severity)
- Active incidents count
- Members heard in 24 h / 7 d
- Module health chips: radio, store, ai, wx, same, fed
- Recent activity feed (live via WS)

**REQ-UI-001** — The airtime gauge **MUST** be on the landing view above the fold. It is the
single most important operational signal in the product.

### 5.2 Map

**REQ-UI-002** — One shared map controller **MUST** serve incident, member, and environment
views. OpenStreetMap raster tiles are the online default; the install script **MUST** support a
bounded regional tile pack at zoom 8–14 as the automatic fallback. When neither source is
available, coordinates, markers, controls, and detail cards **MUST** remain functional and the
missing basemap **MUST** be stated rather than rendered as broken or blank tiles.

**REQ-UI-002a** — Tile and marker DOM **MUST** persist through ordinary pans. Pointer work
**MUST** be animation-frame bounded, and mouse, touch, keyboard, selection, popup, attribution,
and offline behavior **MUST** be consistent across map pages unless a domain action is documented.

**REQ-UI-003** — Layers, individually toggleable: incidents (icon by type, colour by
severity), node positions (opacity by last-heard age), CAP alert polygons, waypoints,
check-in status, and an optional coverage heatmap derived from received SNR by position.

**REQ-UI-004** — A 24-hour time scrubber **MUST** replay how the situation developed
(REQ-WATCH-049).

**REQ-UI-005** — Clicking a marker opens a detail panel with one-click actions: acknowledge,
update, resolve, raise alert.

### 5.3 Messages

Three-column live view — direct messages, channel messages, node list — with per-node send.
This layout is proven in Mesh-API's dashboard and is worth adopting.

**REQ-UI-006** — Every outbound message composed here **MUST** show its Governor status
(`queued` → `sent` → `acked`/`timeout`) and its estimated airtime cost **before** sending.

**REQ-UI-007** — A message-log view **MUST** support filtering by member, channel, command,
outcome, and time, with CSV export.

### 5.4 Boards, Mail, Members

Full CRUD for boards; thread/post reading and moderation with one-click hide and a required
reason; member list with trust editing, mute, block, and per-member activity.

**REQ-UI-008** — Moderation actions **MUST** require a reason and **MUST** show the resulting
audit entry inline.

### 5.5 Watch console

**REQ-UI-009** — During an open watch event the console **MUST** show, on one screen: roster
status counts, unaccounted names, active alerts with ack counts and escalation stage,
incident list, and a single prominent **Raise Alert** action with a severity selector and a
character counter enforcing the 140-byte headline limit.

**REQ-UI-010** — `need_help` check-ins **MUST** trigger a visually distinct, optionally
audible notification.

### 5.6 Assistant

**REQ-UI-011** — Provider/model status, queue depth, latency percentiles; the review queue of
recent interactions with question, evidence, answer, and groundedness; one-click rate,
one-click "promote answer to KB", one-click "add refusal rule".

**REQ-UI-012** — A KB editor with markdown, tags, pinning, and a reindex action.

**REQ-UI-013** — The test console (`/api/v1/ai/ask`) **MUST** be clearly marked as
non-transmitting.

### 5.7 Settings

**REQ-UI-014** — Editable: node identity and location, airtime budgets and class shares,
quiet hours, channel policy, board definitions, retention, AI provider and persona addendum,
weather providers and location, escalation policy, federation peers, intents table.

**REQ-UI-015** — Config edits **MUST** be validated server-side against the same Pydantic
model as file config, **MUST** show a diff before applying, and **MUST** be audit-logged.

**REQ-UI-016** — Settings the operator **MUST NOT** be able to change from the dashboard:
the AI grounding/refusal/emergency prompt clauses (REQ-AI-035), the `[AI]` marker
(REQ-AI-040), and the firmware duty-cycle override (REQ-TRANSPORT-022).

---

## 6. Design constraints for the SPA

**REQ-UI-017** — Total initial bundle **MUST** be under 300 KB gzipped excluding map tiles.
The node serves this over Wi-Fi to a phone, sometimes on a congested AP.

**REQ-UI-018** — The UI **MUST** be usable on a 360 px-wide viewport and **MUST** meet WCAG
2.1 AA contrast, since it will be read outdoors on a phone.

**REQ-UI-019** — Severity **MUST NOT** be conveyed by colour alone — icon and text label
always accompany it.

**REQ-UI-020** — Time display **MUST** be local with a UTC tooltip, and relative ages
(`4m`, `2h`) **MUST** be used in lists.

**REQ-UI-021** — The UI **MUST** show a clear, persistent banner when the radio link is down,
when airtime is throttled, when a module is degraded, or when the AI is using an external
provider.

---

## 7. Performance

**REQ-API-022** — `/api/v1/status` **MUST** respond in <100 ms p95 on the reference hardware.

**REQ-API-023** — List endpoints **MUST** respond in <300 ms p95 at 100 000 message_log rows.

**REQ-API-024** — The web layer **MUST NOT** contribute more than 15% CPU under normal
dashboard use, and **MUST NOT** ever delay radio processing — verified by a test that holds
an open dashboard while measuring inbound message latency.

---

## 8. Acceptance criteria

| # | Criterion |
|---|---|
| 1 | Dashboard loads and functions with the WAN interface down, including the map |
| 2 | First run generates a password, forces a change, and refuses `auth: none` on a non-loopback bind |
| 3 | Airtime gauge on the landing view matches the Governor's internal accounting |
| 4 | A message sent from the dashboard is queued via the Governor, shows its cost estimate, and reports ACK status |
| 5 | WS delivers a new inbound message to an open dashboard in <1 s |
| 6 | A slow WS client is dropped without affecting radio processing |
| 7 | Map renders incidents, nodes, alert polygons and waypoints from cached tiles |
| 8 | Raising an alert enforces the 140-byte headline limit in the UI before submission |
| 9 | Moderation requires a reason and produces an audit entry |
| 10 | Viewing mail writes an audit row and the standing notice is displayed |
| 11 | `POST /api/v1/ai/ask` transmits nothing — verified by asserting zero Governor enqueues |
| 12 | Config edit shows a diff, validates, applies without dropping the radio link, and audits |
| 13 | Bundle size is under 300 KB gzipped |
| 14 | Dashboard is usable at 360 px width and passes AA contrast checks |
