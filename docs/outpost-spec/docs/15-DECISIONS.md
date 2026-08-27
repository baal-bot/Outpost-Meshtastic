# 15 — Architecture Decisions & Open Questions

**Status:** Living document — update as decisions are made and questions resolved.

---

## Part A — Architecture Decision Records

Each ADR states the decision, the forces behind it, what was rejected, and what would make us
revisit it. An implementing agent that disagrees with an ADR **MUST** raise it rather than
quietly implementing the alternative.

---

### ADR-001 — Single asyncio process with sidecars only where forced

**Decision.** All Outpost code runs in one long-lived asyncio process. Separate processes
exist only where a vendor forces it (`hailo-ollama`, the `rtl_fm | samedec` pipeline).

**Forces.** One Pi, one radio, one database, one operator. A message broker or service mesh
would add IPC failure modes, memory, and complexity with no corresponding benefit. The
400 MB idle RSS budget (REQ-PROD-004) does not accommodate multiple Python runtimes.

**Rejected.** Microservices; a Celery/Redis task queue; Docker Compose as the reference
deployment.

**Revisit if.** The node needs to serve multiple radios on separate physical interfaces, or
inference moves to a separate machine and the coupling becomes awkward.

---

### ADR-002 — Serial is the default radio transport; BLE is best-effort

**Decision.** `serial` is the documented default and the recommended production transport.
BLE is supported and explicitly labelled best-effort.

**Forces.** The Meshtastic Python `BLEInterface` carries a source comment stating that on
Linux the BLE device is not disconnected without an `atexit` hook and that "future connection
attempts will fail" — the library's own assessment of BlueZ. Unattended six-month operation
cannot rest on that.

**Rejected.** BLE-first for cable-free installation.

**Revisit if.** The library's Linux BLE handling materially improves, or a supervised
BLE deployment demonstrates 30 days without intervention.

---

### ADR-003 / ADR-004 — SQLite with raw SQL repositories, no ORM

**Decision.** SQLite in WAL mode, single writer, stdlib driver in a thread executor,
repository classes over parameterised SQL. No SQLAlchemy, no Alembic.

**Forces.** ~20 tables with known query shapes. SD-card I/O makes query shape matter and makes
inspectability valuable. An ORM adds a large dependency, ARM wheel risk, and an abstraction
between the author and the query plan — all cost, little benefit at this scale.

**Rejected.** SQLAlchemy Core or ORM; Alembic; Postgres (needs a server, more RAM, and offers
nothing a single-node deployment needs); a document store.

**Revisit if.** The schema exceeds ~40 tables, or federation forces genuinely distributed
data semantics.

---

### ADR-005 — A mandatory Airtime Governor as sole egress

**Decision.** Every transmission passes through one scheduler with per-class budgets and
priority. Enforced by an import-linter contract, not convention.

**Forces.** This is the difference between a good mesh citizen and a project the community
asks to be turned off. Every reviewed reference project transmits ad hoc, and congestion
complaints are the recurring theme in Meshtastic bot discussions. Enforcement must be
mechanical because "remember to use the queue" always fails eventually.

**Rejected.** Per-module rate limiting; advisory guidelines; relying on the firmware's own
40%/25% gates (by the time the firmware is dropping your packets, you are the problem).

**Revisit if.** Never, while the medium is shared LoRa.

---

### ADR-006 — Hybrid session model: stateless verbs plus an optional context stack

**Decision.** Every command works standalone. A session may additionally hold up to 3 context
frames that supply defaults and enable bare-word input.

**Forces.** Modal BBS menus cost a round trip per level, and round trips here are 5–30 s and
sometimes lost. Purely stateless commands make threaded conversation unwieldy. The hybrid
gives one-shot capability with conversational convenience, and degrades gracefully when a
packet is lost — the user simply repeats a self-contained command.

**Rejected.** Pure menu-driven (TC²-BBS model); pure stateless (Mesh-API model).

**Revisit if.** Usability testing shows the context stack confuses users more than it helps —
in which case, drop the stack and keep the verbs, which is the safe direction.

---

### ADR-007 — Provider-abstracted inference; no hard dependency on Hailo

**Decision.** Four real inference backends (plus a `null` provider) behind one interface,
with a mandatory benchmark harness that selects the default on the operator's actual
hardware.

**Forces.** The brief assumed a 4B Qwen on the Hailo-10H. No such text model is shipped: the
GenAI Model Zoo text ceiling is Qwen3-1.7B, the context is hard-capped at 2048 tokens, and in
independent benchmarks the Pi 5 CPU **outperformed** the NPU on every shipped model. The
accelerator's real advantages are power draw and CPU offload. Committing the architecture to
a specific accelerator on vendor claims would be a mistake. The 2B Qwen3-VL model is multimodal,
requires suite 5.3.0 or newer, and is exposed through C++/Python rather than Hailo-Ollama. The
provider abstraction now includes a direct adapter for that API.

**Rejected.** Hailo-only; cloud-only; a single hardcoded model.

**Revisit if.** Hailo ships larger models with longer context — their R&D has said publicly
that new model support is in progress. The abstraction is what lets us adopt it without a
rewrite.

**Target-node result (2026-08-27).** Hailo-10H with `qwen2.5:1.5b` (published as
`qwen2.5-instruct:1.5b` by suite 5.1.1) was the initial Phase 2 default. It measured 9.0 s warm p50
and 16.7 s p95, versus 22.4 s p50 and 72.3 s p95 for
Llama 3.2 3B. The guarded corpus passed 60/60 with zero safety failures, but no raw Qwen
factual output passed the marker/citation post-filter; production therefore relies on the cited
extractive fallback whenever synthesis is not provably well-formed. See
`docs/benchmarks/HAILO-H10-QWEN-2026-08-27.md`.

After the reversible HailoRT 5.3.0 upgrade, `Qwen3-VL-2B-Instruct` loaded successfully through the
new direct `hailo_vlm` adapter. It is the current configured model for text inference; image input
remains future work. Its guarded corpus passed 60/60 with zero safety failures and a 21.31-second
six-prompt p95 at the 96-token production cap. Qwen 2.5 through Hailo-Ollama remains the rollback
path.

---

### ADR-008 — Retrieval-grounded tool-calling AI, not a chat passthrough

**Decision.** The assistant answers from retrieved local evidence with citations, or declines.
Its tools are read-only and scoped to local community data.

**Forces.** A 1.5B model knows nothing about your valley. Its parametric knowledge is worse
than useless here because it is confidently wrong about exactly the local facts people ask
about. Retrieval is what makes the feature worth ~2 s of shared channel time. It is also the
clean differentiator from the reference projects, where the AI is a chat passthrough.

**Rejected.** Chat passthrough with a persona; fine-tuning a model on community data (the
Hailo SDK has no working custom GenAI compile path, and community data changes daily).

**Revisit if.** Local models get good enough that ungrounded answers become reliable — which
does not appear imminent at this parameter scale.

---

### ADR-009 — Federation on a private portnum with CBOR, not on text channels

**Decision.** Inter-node replication uses `PRIVATE_APP` portnum 260 with magic-byte-framed,
CBOR-encoded, HMAC-authenticated messages.

**Forces.** Replication on text channels would pollute human channels, be visible as noise in
every Meshtastic client, and cost several times the airtime in text encoding. A binary
protocol on a separate portnum is invisible to humans and far cheaper. The private range
(256–511) is sanctioned for exactly this.

**Rejected.** Text-channel replication; MQTT (requires internet, and public brokers let anyone
inject into your mesh); a custom Meshtastic firmware module.

**Revisit if.** Portnum collision with another project becomes common — the magic byte handles
it safely, but a coordinated registry would be better.

---

### ADR-010 — Explicit module registration, no filesystem plugin scanning in v1

**Decision.** Modules are registered explicitly in `app.py`. No plugin discovery, no manifest
scanning, no third-party plugin loading.

**Forces.** ZephyrGate's manifest-based plugin system is impressive and is the right long-term
shape, but it is a large surface: dependency resolution, isolation, health monitoring, a
stable API contract, and a security model for third-party code on a node that holds the
community's mail. Shipping it in v1 would consume effort better spent on the Governor and the
BBS.

**Rejected.** Filesystem plugin discovery in v1.

**Revisit if.** After Phase 6, with a deliberate plugin API design and a security model — the
`Module` protocol (REQ-ARCH-006) is deliberately shaped to make this a small step.

---

### ADR-011 — Pull/digest, not push

**Decision.** The node transmits board content only when asked. Digests are opt-in,
coalesced, and DM-only. Push is reserved for alerts.

**Forces.** Airtime. A 12-member community with `immediate` push on 7 boards would saturate
the channel on its own.

**Rejected.** Push-by-default subscriptions; broadcasting new posts to a channel.

---

### ADR-012 — Structured records, not chat, for community watch

**Decision.** Incidents, alerts, and check-ins are typed records with lifecycle, not messages
with keywords.

**Forces.** Structure is what enables deduplication, mapping, escalation, federation,
handover between shifts, and summarisation by a small model. It is also what lets an operator
hand a roster to somebody official after a real event.

**Rejected.** Keyword-scraped chat, which is what most community mesh setups do today.

---

### ADR-013 — No propagating deletes in federation

**Decision.** Moderation is node-local. Hiding a post on node A does not hide it on node B.

**Forces.** Propagating deletes across an unauthenticated mesh is a censorship vector and an
unbounded tombstone-replication problem. Each operator moderates their own node — the Hotline
model, and the right one for a federation of sovereign community servers.

**Rejected.** Distributed moderation; a shared blocklist.

**Revisit if.** Operators report real harm from content they cannot remove — the mitigation is
per-peer content filtering on ingest, not distributed deletion.

---

### ADR-014 — No application-layer message encryption

**Decision.** Outpost relies on Meshtastic's channel AES-CTR and PKI DMs, and adds no
encryption of its own. Federation HMAC is authentication only.

**Forces.** Packet headers are plaintext by design (relays must route what they cannot read),
and the node stores everything in plaintext. Adding an application crypto layer would create
the *impression* of protection that the surrounding architecture does not deliver — the
worst possible outcome, because people make safety decisions on that impression. Honest
documentation (REQ-SEC-027) serves users better than a false floor.

**Rejected.** Application-layer E2E mail encryption; encrypted-at-rest by default (documented
as an operator-level FDE choice instead).

**Revisit if.** A design emerges where key management is workable for non-technical community
members — the hard part is not the crypto.

---

## Part B — Open questions requiring a human decision

An agent **MUST NOT** resolve these unilaterally. Each has an assumed default so
implementation can proceed; changing the answer later should be cheap.

| # | Question | Assumed default | Cost to change later |
|---|---|---|---|
| **Q1** | Should unregistered peers read boards, or must everyone register? | Read yes, write no | Low |
| **Q2** | Default retention: posts / mail / incidents / positions | 90d / 180d / 365d / 7d | Low |
| **Q3** | Is the assistant available on any non-DM channel by default? | No — DM only; per-channel opt-in | Low |
| **Q4** | What disclaimer does the operator's jurisdiction require in alert text? | `"Community system. Not 911."` | Low, but **legally significant — get an answer** |
| **Q5** | Does federated content appear inline or segregated? | Inline with `›ORIGIN` attribution | Medium |
| **Q6** | Node name and short name (≤4 chars, appears in every alert) | Placeholder `OUTPOST` / `OPT` | Low |
| **Q7** | Region and modem preset — determines airtime maths and duty-cycle policy | US / `LONG_FAST` | Medium — affects the ToA model |
| **Q8** | Is an RTL-SDR in the build? It is the strongest offline-alert story | Optional, off | Low |
| **Q9** | Metric or imperial default | From node locale | Low |
| **Q10** | Emergency keywords on the public channel — on or off? | **Off** | Low |
| **Q11** | Which local services should seed the KB? (transfer station, shelters, burn rules, plow schedule, who to call) | Placeholder template | Low, but **this is what makes the AI useful — gather it early** |
| **Q12** | Who are the initial `responder`-trust people? | None | Low |
| **Q13** | Is this deployment commercial? Affects Open-Meteo licensing | Non-commercial | Low |
| **Q14** | Multi-node from day one, or single node first? | Single node; federation at Phase 5 | Low |
| **Q15** | Project name (replaces "Outpost" throughout) | `Outpost` placeholder | Low if done before Phase 1 |

---

## Part C — Technical unknowns to resolve empirically

These are things no amount of documentation will settle. Each has an owning phase and a
deliverable.

| # | Unknown | Resolve in | How |
|---|---|---|---|
| **U1** | Actual time-on-air per packet at the deployed preset | Phase 0 | Compare the `toa()` model against the radio's `airUtilTx` telemetry over 24 h; recalibrate (REQ-TEST-014) |
| **U2** | Whether Hailo or CPU llama.cpp is the better provider on this hardware | Phase 2 | **Partially resolved 2026-08-27:** Hailo Qwen passed the gate; CPU comparison remains optional |
| **U3** | Whether `hailo-ollama` serves Qwen3-1.7B (it is in the zoo but absent from the only published `/hailo/v1/list` dump) | Phase 2 | **Resolved 2026-08-27:** not in the unit's served list; use Qwen 2.5 Instruct 1.5B |
| **U4** | Whether `hailo-ollama` handles concurrent requests or serialises them | Phase 2 | Measure; assume serial until proven otherwise (REQ-AI-011) |
| **U5** | Real cold-start latency and whether keep-warm reliably prevents unload | Phase 2 | Measure; a hands-on review reports 25–40 s cold starts with no documented `keep_alive` control |
| **U6** | Embedding throughput on the Pi 5 CPU — no credible public benchmark exists | Phase 2 | Measure MiniLM-L6 via ONNX on the target |
| **U7** | Whether a 2048-token budget leaves enough room for useful retrieval | Phase 2 | **Resolved 2026-08-27:** guarded 60-item corpus passed 60/60 within the fixed budget |
| **U8** | Practical mesh reliability for multi-part responses at the deployed hop count | Phase 1 | Measure ordering and loss; tune the inter-part delay from 12 s |
| **U9** | Pi CPU load of the `rtl_fm \| samedec` chain | Phase 4 | Measure; no published figure exists |
| **U10** | Whether federation is affordable at all on a busy mesh | Phase 5 | Measure a real sync cycle's airtime; ADR-009's sneakernet mode exists because the answer may be "no" |
| **U11** | Thermal behaviour under sustained inference with the active cooler | Phase 2 | 1-hour sustained-load test; no independent throttling measurement is published |
| **U12** | SD-card wear rate at the chosen write volume | Phase 6 | Monitor over the 30-day soak; the USB-SSD recommendation depends on the answer |
| **U13** | Whether the Pi's SQLite build is ≥3.43 on the target OS image (required for FTS5 `contentless_delete`; external-content FTS avoids the issue but the version check stands) | Phase 0 | `sqlite_version()` at startup (REQ-DATA-002b) |
| **U14** | Whether a 20-item federation manifest actually fits `fed.max_fragments` once CBOR-encoded | Phase 5 | Encode a worst-case manifest and measure (REQ-FED-019) |

---

## Part E — Numbers that are stated once and referenced everywhere

When two documents disagree about a number, the source of truth is the row below. Change it
in one place; every other mention is a cross-reference.

| Quantity | Value | Defined in |
|---|---|---|
| Max Meshtastic data payload | 233 bytes | doc 03 §1 |
| Practical text budget | 200 bytes | doc 03 §1, REQ-TRANSPORT-030 |
| Node airtime budget | 8% of channel time, rolling 1 h | `airtime.budget_percent`, doc 02 §7 |
| Emergency reserve | 4%, `critical` alerts only | `airtime.emergency_reserve_percent`, REQ-TRANSPORT-020a |
| Absolute airtime ceiling | budget + reserve = 12% | REQ-TRANSPORT-049 |
| Channel utilisation ceiling | 25% measured | `airtime.utilisation_ceiling` |
| EU duty-cycle clamp | 2.5% own air-util | REQ-TRANSPORT-022 |
| Traffic classes | `alert`, `reply`, `ai`, `bulletin`, `digest`, `federation` | doc 03 §4.2 |
| Trust levels | `blocked`, `guest`, `member`, `trusted`, `responder`, `operator` | doc 12 §3 |
| Incident severities | `info`, `caution`, `urgent`, `critical` | doc 08 §2 |
| Alert severities | `caution`, `urgent`, `critical` (no `info`) | REQ-WATCH-026 |
| AI context budget | 2048 tokens, allocated in doc 06 §5 | REQ-AI-029 |
| AI output length | model ≤180 B · renderer 1 part ≤200 B · transport 2 parts | doc 06 §1 |
| Federation fragment body | 215 bytes; 8 fragments = 1720 bytes | REQ-FED-006, doc 03 §6 |
| AI eval set | 60 items, ≥70% pass, 0 safety failures | REQ-TEST-015…017 |
| Coverage gate | ≥80% transport/router/security/store, ≥70% overall | REQ-TEST-012 |
| Retention defaults | posts 90d · mail 180d · incidents 365d · positions 7d · msg log 30d | `store.retention`, doc 02 §7 |

---

## Part D — Deliberate divergences from the reference projects

Recorded so reviewers understand these are choices, not oversights.

| Reference behaviour | Outpost | Why |
|---|---|---|
| Mesh-API: AI as a provider passthrough | Retrieval-grounded, tool-calling, cited | ADR-008 |
| Mesh-API: randomised command alias suffixes (`/ai-XY`) | Node short-name prefix (`CRO help`) | More memorable, solves the same collision problem, and reads as a name rather than noise |
| Mesh-API: MCP server exposing mesh functions | Not in v1 | Interesting, but a remote-agent control surface on a community node needs a security model first |
| ZephyrGate: manifest-based plugin system | Explicit module registration | ADR-010 |
| ZephyrGate: games (BlackJack, DopeWars, …) | Not implemented | Airtime cost, off-mission (doc 01 §5 N8) |
| ZephyrGate: email gateway | Not in v1 | Requires internet, spam surface, and deliverability problems; federation mail relay covers the community case |
| ZephyrGate: JS8Call integration | Not in v1 | Worth considering post-v1 as a second transport |
| TC²-BBS: modal menu navigation | Hybrid session model | ADR-006 |
| TC²-BBS: sync by listing peer node IDs in config | Operator-approved pairing with HMAC | Unauthenticated sync is a content-injection vector |
| All three: ad-hoc transmission | Mandatory Airtime Governor | ADR-005 |
| All three: push-oriented notifications | Pull/digest | ADR-011 |
