# 08 — Community Watch

**Status:** Baseline · **Phase:** 3 · **Prerequisite:** [05-DATA-MODEL.md](05-DATA-MODEL.md)
**Implements:** `src/outpost/watch/`

---

## 1. Premise

The failure mode of every informal community-alert system is the same: information arrives
as chat, so it cannot be counted, mapped, deduplicated, followed up, or handed to the next
shift. Three people report the same downed tree; nobody knows whether the first report was
resolved; the message scrolls away.

**REQ-WATCH-001** — Community watch data **MUST** be structured records with a type, a
severity, a location, a lifecycle, and an update history — not text messages with keywords.
The over-the-air *interface* is conversational; the *storage* is structured.

Four capabilities, per the project brief:

1. **Incident reports with geotags** — §2–3
2. **Alert broadcast with escalation** — §5–6
3. **Check-in / welfare roster** — §7
4. **Situational map view** — §8 and doc 11 §5

**REQ-WATCH-002** — Every user-facing surface of this module **MUST** carry the disclaimer
that Outpost is not an emergency service. Over the air this is abbreviated to a single
configurable clause on first use and on any `critical` alert.

---

## 2. Incident taxonomy

**REQ-WATCH-003** — Incident types **MUST** be a closed, config-extensible set with short
aliases users can type:

| Type | Aliases | Default severity | Default expiry |
|---|---|---|---|
| `hazard` | `haz`, `tree`, `flood`, `ice` | caution | 48 h |
| `road` | `rd`, `closure`, `washout` | caution | 72 h |
| `fire` | — | urgent | 12 h |
| `medical` | `med` | urgent | 6 h |
| `police` | `pol`, `suspicious`, `crime` | caution | 24 h |
| `utility` | `util`, `power`, `outage`, `watermain` | info | 48 h |
| `missing` | `lost`, `mp` | urgent | 168 h |
| `animal` | `wildlife`, `dog` | info | 24 h |
| `weather` | `wx` | caution | 12 h |
| `resource` | `shelter`, `water`, `fuel`, `supplies` | info | 168 h |
| `other` | — | info | 24 h |

**REQ-WATCH-003a** — Aliases **MUST** be globally unique across types. A startup check
**MUST** fail if the configured taxonomy contains a duplicate alias, because
REQ-WATCH-006 requires deterministic inference and an ambiguous alias makes that impossible.
(`water` belongs to `resource`; a burst main is `watermain` under `utility`.)

**REQ-WATCH-004** — Severity is a four-level ordinal: `info` < `caution` < `urgent` <
`critical`.

**REQ-WATCH-005** — A member **MUST NOT** be able to self-assign `critical`. `critical`
requires trust ≥ `responder`, or automatic assignment from an ingested CAP alert of severity
Extreme. A member's `urgent` report **MAY** be promoted to `critical` by a responder.

**REQ-WATCH-006** — Type **MUST** be inferable. `REPORT tree down blocking cedar ln` **MUST**
resolve to `hazard` via the alias/keyword table without the user naming a type. Explicit type
as the first token overrides inference.

---

## 3. Reporting

**REQ-WATCH-007** — Filing an incident **MUST** take exactly **one** message:

```
REPORT tree down blocking cedar ln near the church
> ✓ INC 31 hazard · Cedar Ln · 📍44.123,-72.567 · sent to #watch
```

**REQ-WATCH-008** — Location **MUST** be resolved in this priority order:

1. Explicit coordinates in the message (`44.123,-72.567` or `44.123 -72.567`)
2. A named waypoint the community has saved (`REPORT hazard at millbridge`)
3. The reporter's last known position from `POSITION_APP`, if fresher than
   `watch.position_max_age_minutes` (default 30)
4. Free-text location retained with `lat/lon` null, flagged `location_unconfirmed`

**REQ-WATCH-009** — When location resolves from the reporter's GPS, the acknowledgement
**MUST** say so, so the reporter can correct it. When no location resolves, the
acknowledgement **MUST** ask for one in the same line:
`✓ INC 31 hazard. No location — send UPD 31 <where>.`

**REQ-WATCH-010** — Position attached to an incident is **published** to everyone who can see
the incident. This differs from ordinary position privacy (doc 12 §8) and **MUST** be stated
in `HELP REPORT`. A reporter **MAY** suppress it with the `-nopos` flag.

**REQ-WATCH-011** — `local_ref` (the short number) **MUST** be assigned from the smallest
free integer among currently-active incidents, and **MUST NOT** be recycled while the
incident is visible in any listing.

### 3.1 Deduplication and confirmation

**REQ-WATCH-012** — Before creating an incident, the module **MUST** search for a likely
duplicate: same type, within `watch.dedupe_radius_m` (default 500), within
`watch.dedupe_window_minutes` (default 120), with token overlap in the title above a
threshold.

**REQ-WATCH-013** — On a probable duplicate, the node **MUST NOT** silently merge. It
**MUST** respond in one line offering both paths:
`Similar: INC 31 tree Cedar Ln 40m. CONFIRM 31, or REPORT! to file new.`

**REQ-WATCH-014** — `CONFIRM <n>` increments `confirm_count` and appends an
`incident_update` of kind `confirm`. `DISPUTE <n> [note]` increments `dispute_count`.

**REQ-WATCH-015** — Confirmation count **MUST** be displayed in listings (`✓3`) and **MUST**
raise the incident's ranking. Three independent confirmations of an `urgent` incident
**MUST** notify the operator on the dashboard.

**REQ-WATCH-016** — `dispute_count > confirm_count` on an unresolved incident **MUST** flag
it for operator review and **MUST** suppress it from broadcast escalation, but **MUST NOT**
auto-delete it.

---

## 4. Emergency keywords

**REQ-WATCH-017** — The node **MAY** be configured with an emergency keyword set that
triggers a response *without* the command prefix, including on the primary public channel.
This is the **only** exception to REQ-TRANSPORT-015.

**REQ-WATCH-018** — Default keyword set (config `watch.emergency_keywords`), matched as
whole words, case-insensitive: `sos`, `mayday`, `emergency`, `help me`, `911`.

**REQ-WATCH-019** — This feature **MUST** default to **disabled** and **MUST** require the
operator to enable it explicitly, having read the accompanying config comment. False
positives on a public channel are expensive and embarrassing.

**REQ-WATCH-020** — When triggered, the node **MUST**:

1. Create an incident with type `other`, severity `urgent`, source `member`.
2. Reply to the sender by DM in one line with the emergency number and confirmation that the
   report was filed.
3. Notify responders per §6, **without** auto-broadcasting to the whole mesh (a keyword match
   is not verified; a broadcast is).
4. Raise a prominent, audible dashboard alert.

**REQ-WATCH-021** — Escalation to a mesh-wide broadcast from a keyword trigger **MUST**
require a human: a responder's `ALERT` command or an operator dashboard action.

**REQ-WATCH-022** — Rate limit: at most one keyword-triggered incident per member per
`watch.emergency_cooldown_minutes` (default 10). Subsequent matches append to the existing
incident.

**REQ-WATCH-022a (the safety floor)** — The emergency path — the keyword handler, `REPORT`,
`OK`, and `HELPME` — is governed **only** by REQ-WATCH-022's cooldown. It is exempt from:

- the per-member incident token bucket (doc 12 §5), including the `guest` limit of 2/hour
- the node-wide circuit breaker of REQ-SEC-017, which otherwise serves only `alert` and
  `HELP`

Both exemptions are deliberate. A `guest` who has already filed two incidents this hour may
still be the person who sees the fire, and a mesh under flood is exactly when someone needs
to say they are in trouble. The cooldown alone prevents a single member from flooding the
path, and every exempted action is still bounded by the Governor's airtime budget on the way
out — the node accepts the input; it does not promise to answer it immediately.

**REQ-WATCH-022b** — The exemption applies to *accepting and recording* the report. It does
**not** exempt the reply from the Governor, and it does **not** authorise a broadcast:
REQ-WATCH-021 still requires a human to escalate.

---

## 5. Alerts

An **alert** is the act of spending community airtime to push information to everyone. It is
deliberately a separate, privileged concept from an incident.

**REQ-WATCH-023** — Alerts **MUST** originate from exactly one of:

| Source | Authority |
|---|---|
| `operator` | Dashboard or `OP BCAST` |
| `incident` | A `responder`+ promoting an incident with `ALERT <sev> <text>` |
| `cap` | Ingested NWS/CAP alert passing the severity gate (doc 09 §4) |
| `same` | Decoded SAME/EAS header from NOAA Weather Radio (doc 09 §5) |

**REQ-WATCH-024** — The AI **MUST NOT** be an alert source, may not author alert text, and
may not trigger escalation (REQ-AI-041).

**REQ-WATCH-025** — Alert headline **MUST** be ≤140 bytes, validated on write, so the
rendered broadcast fits one packet:

```
⚠URGENT Tree down blocking Cedar Ln at the church. Impassable. Exp 18:00. CRO
```

**REQ-WATCH-026** — Alert severity is a **three**-level ordinal — `caution` < `urgent` <
`critical` — a strict subset of the four incident severities. An `info` incident is never
worth a broadcast, so `info` is not a valid alert severity and the schema forbids it
(doc 05 §6).

Rendered alert format **MUST** be:
`<marker><SEVERITY> <headline> [Exp <HH:MM>] <node_short>`
Markers: `caution` → `!`, `urgent` → `⚠`, `critical` → `⚠⚠`.

**REQ-WATCH-027** — Alert broadcasts use airtime class `alert`, which preempts all other
classes (doc 03 §4.2), and **MUST NOT** be suppressed by quiet hours or by a member's `STOP`
unless the operator has explicitly enabled alert opt-out.

**REQ-WATCH-028** — Repeat broadcast of the same alert **MUST** be limited to
`watch.alert_repeat_max` (default 3) at `watch.alert_repeat_interval_minutes` (default 20),
and **MUST** stop early once the acknowledgement threshold (§6) is met.

**REQ-WATCH-029** — Cancelling or updating an alert **MUST** use the Governor's supersession
mechanism (REQ-TRANSPORT-027) so a queued stale repeat is removed rather than transmitted
after the all-clear.

**REQ-WATCH-030** — An `all clear` **MUST** be broadcast when a `critical` or `urgent` alert
is resolved, once, at `alert` class. Communities need the resolution as much as the warning.

---

## 6. Escalation

**REQ-WATCH-031** — Escalation policy **MUST** be config-driven per severity:

```yaml
watch:
  escalation:
    urgent:
      stages:
        - { after_minutes: 0,  notify: responders,      channels: [3] }
        - { after_minutes: 10, notify: trusted,         channels: [3] }
        - { after_minutes: 20, notify: all,             channels: [0, 3] }
      ack_threshold: 2       # stop escalating once 2 responders ack
    critical:
      stages:
        - { after_minutes: 0,  notify: all,             channels: [0, 3] }
        - { after_minutes: 10, notify: all,             channels: [0, 3], repeat: true }
      ack_threshold: 3
```

**REQ-WATCH-032** — Escalation **MUST** be driven by a durable scheduler backed by
`alert.next_escalation_at`, so a node restart does not lose a pending escalation.

**REQ-WATCH-033** — Escalation **MUST** halt immediately when `ack_threshold` acknowledgements
are recorded, when the alert is cancelled, or when it expires.

**REQ-WATCH-034** — `ACK <n>` **MUST** record the acknowledging member and time, respond in
≤40 bytes (`✓ ack INC 31`), and **MUST** be visible on the dashboard in real time.

**REQ-WATCH-035** — The escalation state machine **MUST** be unit-tested against a virtual
clock with these cases: acked before stage 2, never acked through all stages, cancelled
mid-escalation, node restart between stages, and two alerts escalating concurrently.

**REQ-WATCH-036** — Total alert-class airtime **MUST** still be bounded. Escalation
transmits through the Governor like everything else and is subject unchanged to the
airtime invariant defined once in **REQ-TRANSPORT-049**: `alert` traffic draws on
`airtime.class_shares.alert` of the budget, and `critical` alerts may additionally draw on
`airtime.emergency_reserve_percent` (default 4% of channel time). Nothing exceeds
`budget_percent + emergency_reserve_percent` — default 12% — under any circumstance.

> An escalating alert storm that saturates the channel prevents the very responders it is
> trying to reach from communicating. The ceiling is a safety feature, not a limitation.

---

## 7. Check-in / welfare roster

**REQ-WATCH-037** — `OK [note]` **MUST** record a check-in with the member's position if
available, and respond in ≤50 bytes: `✓ ok 14:22. 9/14 in.`

**REQ-WATCH-038** — The node **MUST** support named **watch events** (`watch_event`, doc 05
§6) opened by an operator or responder. During an open event, check-ins bind to it and the
roster is scoped to it.

**REQ-WATCH-039** — Roster states: `ok`, `need_help`, `unaccounted`, `evacuated`.
`unaccounted` is derived, not self-reported: any roster member with no check-in since the
event opened.

**REQ-WATCH-040** — Roster policy per event: `all` (every known member), `responders`, or
`subscribed` (members who opted into the roster).

**REQ-WATCH-041** — `ROSTER` **MUST** render a summary in one part:
`> Event "Ice storm": 9 ok · 1 help · 4 unaccounted. ROSTER? for names.`
Names are a second, explicitly-requested page — a 14-name list is three packets.

**REQ-WATCH-042** — `need_help` check-ins **MUST** immediately notify responders and raise a
dashboard alert. This is the highest-value signal in the entire system.

**REQ-WATCH-043** — During an open event the node **MAY** send a single check-in solicitation
per member per event, DM only, `digest` class, subject to airtime budget and never during
the first 15 minutes of a `critical` alert (responders need the channel).

**REQ-WATCH-044** — Check-in data **MUST** be exportable from the dashboard as CSV, because
after a real event someone will have to hand it to somebody official.

---

## 8. Situational picture

**REQ-WATCH-045** — `INCIDENTS [radius_km]` **MUST** return active incidents ordered by
severity then proximity to the asker's last known position, rendered ≤55 bytes each:

```
> 3 active near you
31 ⚠ hazard Cedar Ln 0.8km 40m ✓3
28 ! road Mill Rd 2.1km 6h ✓5
19   util Power out E side 3.4km 1d
```

**REQ-WATCH-046** — Distance and bearing **MUST** be computed with the haversine formula from
the asker's last known position. With no known position, results are ordered by severity then
recency and the response says so in ≤20 bytes.

**REQ-WATCH-047** — `INC <n>` **MUST** return the incident with its most recent 2 updates and
a count of the rest.

**REQ-WATCH-048** — The dashboard map (doc 11 §5) **MUST** plot: active incidents by type and
severity, node positions with last-heard age, alert polygons from CAP, waypoints, and
check-in status when an event is open. It **MUST** function with locally-cached map tiles and
no internet.

**REQ-WATCH-049** — The map **MUST** offer a time scrub over the last 24 hours so an operator
can see how a situation developed.

---

## 9. Lifecycle

```
        REPORT / CAP / SAME
                │
                ▼
            ┌───────┐  CONFIRM/UPDATE   ┌────────────┐
            │ open  │◄─────────────────►│ monitoring │
            └───┬───┘                   └─────┬──────┘
                │ CLEAR (trust≥trusted)       │
                ▼                             │
          ┌──────────┐                        │
          │ resolved │◄───────────────────────┘
          └──────────┘
                ▲            expires_at reached
                │        ┌──────────┐
                └────────│ expired  │
                         └──────────┘
   any state ──DISPUTE+operator──► false_alarm
```

**REQ-WATCH-050** — Auto-expiry **MUST** run on a schedule; an expired incident leaves active
listings but remains queryable and mappable in history.

**REQ-WATCH-051** — Resolving an incident **MUST** require trust ≥ `trusted`, **except** that
the original reporter **MAY** resolve their own incident within
`watch.self_resolve_hours` (default 24).

**REQ-WATCH-052** — Resolution **MUST** capture a note, and the resolution **MUST** be
included in the all-clear broadcast when one is sent.

---

## 10. Metrics

```
outpost_incidents_created_total     counter {type,severity,source}
outpost_incidents_active            gauge   {type,severity}
outpost_incident_confirms_total     counter
outpost_incident_disputes_total     counter
outpost_alerts_raised_total         counter {severity,source}
outpost_alert_broadcasts_total      counter {severity,stage}
outpost_alert_acks_total            counter {severity}
outpost_alert_time_to_first_ack_seconds histogram {severity}
outpost_escalation_stage_reached_total  counter {severity,stage}
outpost_checkins_total              counter {status}
outpost_roster_unaccounted          gauge
outpost_emergency_keyword_triggers_total counter
```

---

## 11. Acceptance criteria (Phase 3 exit)

| # | Criterion |
|---|---|
| 1 | `REPORT <freetext>` files a typed, geotagged incident in one message with GPS auto-attach |
| 2 | Type inference resolves correctly on ≥90% of a 40-item phrase corpus |
| 3 | A near-duplicate report is detected and offered `CONFIRM` rather than merged or duplicated |
| 4 | `ALERT` from a responder broadcasts within 5 s at `alert` class, preempting a queued digest |
| 5 | Escalation advances on schedule, halts on `ack_threshold`, and survives a node restart mid-escalation |
| 6 | An all-clear supersedes a queued repeat broadcast; the stale repeat is never transmitted |
| 7 | `OK` check-in and `ROSTER` work during an open event; `unaccounted` is derived correctly |
| 8 | `need_help` reaches responders and the dashboard within 5 s |
| 9 | `INCIDENTS` orders by severity then true haversine distance |
| 10 | The map renders incidents, nodes, alert polygons and roster state with **no internet** |
| 11 | Alert-class airtime stays within its share + emergency reserve during a simulated 3-alert storm |
| 12 | Emergency keywords are off by default; when enabled they notify responders but never auto-broadcast |
| 13 | Roster export produces a valid CSV |
| 14 | The AI cannot raise, author, escalate, or cancel an alert — verified by an explicit negative test |
