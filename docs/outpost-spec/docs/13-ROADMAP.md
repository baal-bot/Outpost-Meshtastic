# 13 — Phased Roadmap

**Status:** Baseline · **Audience:** the implementing agent's work planner

This document sequences the build. **Do not begin a phase until the previous phase's exit
criteria are demonstrably met**, in the simulator and — from Phase 1 onward — on hardware.

Each phase lists: what it delivers, the requirement IDs it satisfies, the concrete work
items, and the exit gate.

---

## Phase 0 — Foundation

**Delivers:** a process that connects to a radio, receives messages, replies to `PING` and
`ABOUT`, respects an airtime budget, persists to SQLite, serves `/api/v1/health`, and runs
unattended under systemd. **No user-facing features.**

**Why this is its own phase:** every later phase depends on the Governor and the router being
correct. Building features on a broken transport layer means finding transport bugs through
feature symptoms, on a live radio channel, which is the worst possible debugging environment.

### Work items

1. Repository scaffold per doc 00 §"Repository layout", `pyproject.toml`, lockfile, CI.
2. `config.py` — full Pydantic settings model with the validation of REQ-ARCH-017.
3. `store/` — connection management, pragmas, migration runner, `member`, `message_log`,
   `kv`, `audit_log` tables and repositories.
4. `transport/radio_link.py` — serial, TCP, BLE; pubsub→asyncio bridge; supervision and
   reconnect (REQ-TRANSPORT-002…010).
5. `transport/toa.py` — time-on-air model, unit-tested against preset data rates
   (REQ-TRANSPORT-001).
6. `transport/governor.py` — classes, budgets, **the emergency reserve**, DRR scheduling,
   TTLs, dedupe, supersession, pacing, regional duty-cycle clamp, metrics
   (REQ-TRANSPORT-018…029).
7. `transport/chunker.py` — byte-accurate splitting, part suffixes, part budgets
   (REQ-TRANSPORT-030…035).
8. `transport/inbound.py` — normalisation, dedupe LRU, self-loop drop, bridge detection.
9. `router/` — session model, context stack, dispatch skeleton, per-member serialisation,
   error boundary.
10. `commands/core.py` — `PING`, `ABOUT`, `HELP` (from the registry), `WHOAMI`.
11. `security/` — identity resolution, trust levels, token-bucket rate limiter.
12. `events.py` — EventBus.
13. `web/` — FastAPI app, `/api/v1/health`, `/api/v1/status`, `/metrics`, static mount.
14. `tests/sim/` — `SimulatedRadioLink`, virtual clock, mesh harness (doc 14 §3).
15. `deploy/` — systemd unit with the hardening of REQ-SEC-040, `install.sh`.
16. Import-boundary lint rule (REQ-ARCH-005, REQ-TRANSPORT-018).

### Exit gate

| # | Criterion |
|---|---|
| 0.1 | Connects to a real Meshtastic radio over serial; survives unplug/replug with automatic reconnect |
| 0.2 | Replies to `!ping` from a handheld in under 10 s |
| 0.3 | Property test passes: airtime never exceeds budget in any 60-min window under any enqueue sequence (REQ-TRANSPORT-049) |
| 0.4 | Self-sent messages are never processed (loop test) |
| 0.5 | Duplicate flooded packets are deduplicated |
| 0.6 | Chunker never splits a multibyte sequence or a word; byte counts verified with non-ASCII input |
| 0.7 | Import lint fails the build if a module outside `transport/` calls `RadioLink.send_*` |
| 0.8 | Starts successfully with no radio attached and serves `/api/v1/health` reporting `radio: down` |
| 0.9 | Runs 72 h unattended under systemd with zero restarts and stable RSS |
| 0.10 | Idle RSS < 400 MB, idle CPU < 5% on a Pi 5 |
| 0.11 | The emergency reserve is implemented and tested (REQ-TRANSPORT-020a), even though nothing uses it until Phase 3 |
| 0.12 | Config validation rejects every case in REQ-ARCH-017 with a named, actionable error |

---

## Phase 1 — MVP (BBS, Mail, Directory)

**Delivers:** the first shippable system. A community can use this and nothing else.

**Satisfies:** doc 07 in full; doc 04 §§4.1–4.4; doc 11 §§1–5.4 read-mostly.

### Work items

1. `bbs/` — boards, threads, posts, read markers, subscriptions, digests, FTS5 search.
2. `bbs/mail.py` — store-and-notify mail, piggy-back notification, unknown-handle hold.
3. `commands/bbs.py`, `commands/mail.py`, `commands/directory.py`.
4. Renderer + terse-register string catalogue + byte-budget lint test (REQ-CMD-025).
5. Pagination and `MORE` cursors (doc 04 §5); drafts (§6).
6. Fuzzy matching + intents table (doc 04 §8).
7. Registration flow and handle rules (doc 04 §9, doc 12 §2).
8. Moderation: hide, self-delete window, audit.
9. Rate limits per trust level (doc 12 §5).
10. Dashboard: overview, messages, boards, members, message log. Read-mostly plus moderation.
11. Seed data: default boards, seed KB placeholder, `tools/seed_dev_data.py`.
12. Backups and retention pruning (doc 05 §10).

### Exit gate

All 15 criteria in [07-BBS-AND-MAIL.md](07-BBS-AND-MAIL.md) §10, criteria 1–7 and 9–14 of
[11-WEB-API-AND-DASHBOARD.md](11-WEB-API-AND-DASHBOARD.md) §8, criteria 1–8 and 11–16 of
[12-SECURITY-AND-IDENTITY.md](12-SECURITY-AND-IDENTITY.md) §11, plus:

| # | Criterion |
|---|---|
| 1.16 | Five real people on handhelds complete post / read / reply / mail without instructions beyond `?` |
| 1.17 | 7-day soak on a live mesh; node airtime share stays under 8% (goal G3) |
| 1.18 | Dashboard functional with WAN down |
| 1.19 | Backup runs, verifies with `integrity_check`, and restores into a working node |

---

## Phase 2 — Assistant

**Delivers:** the local AI, grounded in local data.

**Satisfies:** doc 06 in full; doc 04 §4.5; doc 11 §5.6.

### Work items

1. `ai/providers/` — `hailo`, `llamacpp`, `ollama`, `openai_compat`, `null`.
2. `tools/bench_inference.py` and the 60-item eval set (**do this before choosing a default**).
3. `ai/budget.py` — token budgeter with provider-reported context size.
4. `ai/retrieval.py` — classification, FTS+recency ranking, optional embedding re-rank,
   evidence packing.
5. `ai/tools/` — the 12 read-only tools of doc 06 §12 with Pydantic arg models.
6. `ai/agent.py` — native and constrained-emit tool protocols, 2-round cap, fallbacks.
7. `ai/safety.py` — pre-filter refusal list, output post-filter, citation verification,
   injection defences.
8. KB: schema, dashboard editor, seed template, promote-post-to-KB.
9. Keep-warm, concurrency semaphore, queue bound, circuit breaker.
10. Dashboard AI console: status, review queue, rating, test console, KB editor.
11. Per-channel AI policy.

### Exit gate

| # | Criterion |
|---|---|
| 2.1 | Benchmark report exists; the chosen default provider/model is justified in doc 15 |
| 2.2 | ≥70% of the 60-item eval set passes; **zero** safety-category failures (REQ-AI-050) |
| 2.3 | ≥60% of answers to `local_knowledge` and `board_content` questions are grounded with a valid citation |
| 2.4 | `howto` questions are answered from the command registry with **zero** model calls |
| 2.5 | An answer never exceeds 2 parts; p95 end-to-end under 25 s warm |
| 2.6 | Provider killed mid-request → terse failure line; BBS latency unaffected |
| 2.7 | Circuit breaker engages after 5 failures in 10 min and recovers |
| 2.8 | A board post containing a prompt-injection payload does not alter behaviour or the marker |
| 2.9 | The AI cannot read mail or positions (negative tests) |
| 2.10 | AI is disabled on channel 0 by default; `ASK` there returns the DM redirect |
| 2.11 | Every interaction is logged with evidence refs |
| 2.12 | With `modules.ai.enabled: false`, Phase 1 behaviour is byte-identical |
| 2.13 | Criteria 9–11 of [12-SECURITY-AND-IDENTITY.md](12-SECURITY-AND-IDENTITY.md) §11 (AI cannot read mail or positions; injection defence) |
| 2.14 | Criteria 11–13 of [11-WEB-API-AND-DASHBOARD.md](11-WEB-API-AND-DASHBOARD.md) §8 (AI console transmits nothing) |

---

## Phase 3 — Community Watch

**Satisfies:** doc 08 in full; doc 04 §4.6; doc 11 §§5.2, 5.5.

### Work items

1. `watch/incidents.py` — taxonomy, type inference, geo resolution, dedupe/confirm, lifecycle.
2. `watch/alerts.py` — raise, render, broadcast, supersede, all-clear.
3. `watch/escalation.py` — durable scheduler, stages, ack thresholds, restart survival.
4. `watch/checkin.py` — check-ins, watch events, roster derivation, CSV export.
5. `commands/watch.py`; emergency keyword handler (off by default).
6. Dashboard: map with layers and time scrub, watch console, alert composer with byte counter.
7. Local tile pack tooling for the map.

*(The Governor's emergency reserve is **not** here — it is built in Phase 0 per
REQ-TRANSPORT-020a, and Phase 3 is the first thing to use it.)*

### Exit gate

All 14 criteria in [08-COMMUNITY-WATCH.md](08-COMMUNITY-WATCH.md) §11, the map and watch
console items of [11-WEB-API-AND-DASHBOARD.md](11-WEB-API-AND-DASHBOARD.md) §8 (criteria
7–8), plus:

| # | Criterion |
|---|---|
| 3.15 | A tabletop exercise with 6 participants: alert raised → acked → escalated → resolved → all-clear, entirely over the radio |
| 3.16 | Simulated 3-concurrent-alert storm stays within the invariant of REQ-TRANSPORT-049 |

---

## Phase 4 — Environment

**Satisfies:** doc 09 in full; doc 04 §4.7.

### Work items

1. `env/providers/` — NWS, Open-Meteo, USGS; conditional requests; host allowlist.
2. `env/cache.py` — staleness policy, age labelling, refusal beyond max age.
3. `env/cap.py` — CAP ingest, severity gate, geo relevance, `msgType` handling, dedupe.
4. `env/same/` — SDR supervisor, `samedec` pipeline, SAME→CAP mapping, silence alarm.
5. `env/astro.py` — local sunrise/sunset/twilight/moon.
6. `env/geo.py` — haversine, bearing, coordinate parsing, waypoints.
7. Static reference data fetch and region clipping.
8. `commands/env.py`; weather abbreviation table.
9. Dashboard: weather panel, provider health, alert gate review, waypoint editor.

### Exit gate

All 14 criteria in [09-WEATHER-AND-ALERTS.md](09-WEATHER-AND-ALERTS.md) §11, plus:

| # | Criterion |
|---|---|
| 4.15 | 30-day run capturing at least one real NWS alert end-to-end, correctly gated and rendered |
| 4.16 | Full WAN-down day: `WX` degrades with age labels, `SUN` unaffected, SAME path (if fitted) still alerts |

---

## Phase 5 — Federation

**Satisfies:** doc 10 in full.

### Work items

1. `fed/framing.py` — magic, version, fragmentation, CBOR, deflate, HMAC.
2. `fed/discovery.py` — `HELLO`, pairing with verification code, operator approval.
3. `fed/sync.py` — manifest/diff/item protocol, cursors, checkpointing, budget awareness.
4. `fed/mail.py` — relay and receipts.
5. `fed/incidents.py` — event-driven incident and gated alert propagation.
6. Loop prevention, per-peer quotas, auto-pause.
7. Sneakernet export/import.
8. Dashboard: peer management, per-board sync toggles, cost projection.

### Exit gate

All 16 criteria in [10-NODE-FEDERATION.md](10-NODE-FEDERATION.md) §11, plus:

| # | Criterion |
|---|---|
| 5.17 | Two physical nodes, real radios, 1 km apart: a board syncs and mail relays over 48 h within budget |

---

## Phase 6 — Hardening & Release

**Delivers:** something another operator can install without you.

### Work items

1. Multi-user auth with roles, TOTP, recovery, sessions, and step-up (implemented); scoped API
   tokens remain pending.
2. Full settings UI with diff/validate/audit; hot reload.
3. Backup/restore UI; encrypted backups; `purge_member.py`.
4. Observability: full metric coverage, a shipped Grafana dashboard JSON, log rotation.
5. `install.sh` covering: OS prereqs, user creation, dialout group, venv, systemd, radio
   detection, optional Hailo setup, optional SDR setup, tile pack, first-run wizard.
6. Documentation: operator guide, user quick-reference card (one printable page of commands),
   troubleshooting, privacy statement (REQ-SEC-027), upgrade guide.
7. Upgrade path: migration testing from every prior release.
8. Localisation scaffolding for the string catalogue (not translation itself).
9. Release engineering: versioning, changelog, signed artefacts, `pip-audit` in CI.

### Exit gate

| # | Criterion |
|---|---|
| 6.1 | A second operator installs from scratch on fresh hardware using only the docs, in under 60 min |
| 6.2 | 30-day unattended soak: zero manual interventions, automatic recovery from at least one induced radio failure and one induced provider failure |
| 6.3 | Upgrade from the Phase 1 release preserves all data and passes all tests |
| 6.4 | Every requirement ID in this spec set is either implemented, explicitly deferred with a reason in doc 15, or marked WITHDRAWN |
| 6.5 | Test coverage ≥80% on `transport/`, `router/`, `security/`, `store/`; ≥70% overall (REQ-TEST-012) |
| 6.6 | The printable command card fits one page and covers every command a `member` can use |

---

## Dependency graph

```
Phase 0 ──┬─→ Phase 1 ──┬─→ Phase 2 (AI)
          │             ├─→ Phase 3 (Watch) ──→ Phase 4 (Env)
          │             │                          │
          │             └──────────────────────────┴─→ Phase 5 (Fed) ──→ Phase 6
          └─→ Governor emergency reserve (built Phase 0, first used Phase 3)
```

**Parallelisable:** Phases 2 and 3 are independent of each other and **MAY** be built
concurrently once Phase 1 is complete. Phase 4 depends on Phase 3 (alerts flow through the
watch module). Phase 5 depends on Phases 1 and 3.

**REQ-PROD-002 restated:** at every point after Phase 1, disabling any later module in config
**MUST** return the system to the previous phase's behaviour exactly.

---

## Estimation guidance

Rough scale for an AI agent working with human review, not a commitment:

| Phase | Relative size | Riskiest part |
|---|---|---|
| 0 | Large | Governor correctness; radio supervision on real hardware |
| 1 | Large | Terse-register rendering discipline; pagination cursors |
| 2 | Medium-large | Provider variance; the 2048-token budget; safety filters |
| 3 | Medium-large | Escalation state machine durability; map offline |
| 4 | Medium | CAP geo-filtering correctness; SDR supervision |
| 5 | Medium-large | Protocol correctness on a lossy link; loop prevention |
| 6 | Medium | Install script across hardware variations |

**The two highest-risk items in the whole project** are the Airtime Governor (Phase 0) and
the terse-register rendering discipline (Phase 1). Both are cheap to get wrong invisibly and
expensive to retrofit. Front-load the tests for both.
