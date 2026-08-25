# 01 — Product Requirements

**Status:** Baseline · **Audience:** implementing agent, project owner

---

## 1. Problem

When the network goes away — a hurricane, a wildfire evacuation, a rural valley with no
cell coverage, a grid failure, a campus with dead spots — communities lose the thing they
need most: a shared place to post information and find it again.

Meshtastic solves the *link* problem elegantly. It does not solve the *place* problem.
Out of the box it is a group chat with no memory: a message you missed is a message you
never see, there is no way to ask "what happened while I was out of range", there is no
structure to separate a lost-dog notice from a road washout, and there is nobody to answer
a question at 2am.

Existing community bots bolt features onto that chat. They help, but they inherit chat's
weaknesses — no persistence model worth the name, no structure, and a tendency to congest
the very mesh they run on.

## 2. Product vision

**Outpost is the community's own server, reachable over a radio link nobody else owns.**

A single Raspberry Pi in someone's garage holds the neighbourhood's bulletin boards, its
private mail, its incident log, its weather, and an AI assistant that can answer questions
about all of it — and every one of those works with the internet unplugged. Anyone within
mesh range, on a $30 handheld, can read the boards, post to them, send private mail, report
an incident, check in as safe, and ask a question in plain English.

It is a BBS for the post-internet moment: local, sovereign, low-bandwidth, and durable.

## 3. Users

### 3.1 Primary personas

**Dana — the resident (majority of traffic).**
Owns a Meshtastic handheld or runs the phone app. Not technical. Types short messages with
one thumb. Wants: "did anyone say when the power's back?", "post that the bridge is out",
"send Marcus a private note", "is a storm coming?". Will abandon anything that takes more
than two exchanges to accomplish, and will not read a help screen.
*Design implication:* every common action must be reachable in one message. Bare-word and
natural-language input must work, not just command syntax.

**Ray — the node operator.**
Installed the Pi, runs the mesh, is technical enough to edit YAML but has a day job. Wants:
a dashboard that shows the mesh is healthy, control over who can do what, the ability to
moderate a bad post, and confidence the thing will run for six months untouched.
*Design implication:* observability, a real admin UI, sane defaults, and no required
maintenance.

**Jo — the coordinator.**
CERT volunteer, HOA board member, or ranch manager. During an event, needs to raise an
alert to everyone, see who has checked in, know where reported incidents are, and track
which responders acknowledged. Not on the Pi — on a handheld in the field, and sometimes on
a laptop at a command post.
*Design implication:* community-watch features must be usable entirely over the radio,
with the dashboard as an enhancement rather than a requirement.

### 3.2 Secondary

**Kit — the neighbouring operator.** Runs another Outpost node 6 km away. Wants the two
nodes to share boards and relay mail without either operator managing the other.

**Sam — the visitor.** Passing through with a handheld, unknown to the node. Should be able
to read public boards and see active alerts without registration, and be blocked from
anything that costs the community airtime or trust.

## 4. Goals

| ID | Goal | Measured by |
|---|---|---|
| G1 | Fully functional with zero internet connectivity | Every Phase 1–3 acceptance test passes with the node's WAN interface down |
| G2 | A first-time user completes a useful action without reading docs | ≥80% of test users post to a board within 3 messages, unprompted |
| G3 | The system is a good citizen on a shared mesh | Node's share of channel airtime stays under its configured budget (default 8%) in a 7-day soak on a live mesh |
| G4 | The AI assistant is worth its airtime | ≥70% pass rate on the 60-item local eval set, with zero safety-category failures (doc 14 §8) |
| G5 | Survives unattended operation | 30-day soak with no manual intervention; automatic recovery from radio disconnect, power loss, and provider failure |
| G6 | An incident reported in the field is visible mesh-wide and on the map within 60s | Measured end-to-end in the simulator and on hardware |

## 5. Non-goals

Explicitly out of scope. An agent **MUST NOT** implement these without a spec revision.

| ID | Non-goal | Rationale |
|---|---|---|
| N1 | Voice, image, or file transfer over the mesh | 1 kbps. Physically inappropriate. |
| N2 | Being a Meshtastic firmware fork or replacement | Outpost is an application on top of stock firmware. |
| N3 | Internet-facing multi-tenant SaaS | Single community, single operator, LAN-scoped dashboard. |
| N4 | Replacing 911 / official emergency services | Outpost is a community *supplement*. This must be stated in the UI and in the alert text itself. |
| N5 | Cryptographic guarantees beyond what Meshtastic provides | Channel PSKs are shared secrets and the header is plaintext. Outpost adds application-layer trust, not new crypto primitives. See doc 12 §7. |
| N6 | Cloud sync, accounts, or telemetry to any vendor | Sovereignty is the point. Zero outbound calls except operator-configured data sources. |
| N7 | Real-time chat semantics (typing indicators, read receipts, presence) | Round-trip latency is seconds to minutes. |
| N8 | Games | ZephyrGate has them. They are airtime-expensive and off-mission. May be added later as a plugin by others. |

## 6. Scope by phase

Full detail and exit criteria in [13-ROADMAP.md](13-ROADMAP.md). Summary:

| Phase | Name | Delivers |
|---|---|---|
| **0** | Foundation | Radio link, Airtime Governor, router, session model, SQLite store, config, health, systemd packaging. No user features. |
| **1** | **MVP** | Boards, threads, private mail, node directory, help, digests. Read-only web dashboard. **This is the first shippable system.** |
| **2** | Assistant | LLM provider abstraction + benchmark harness, tool-calling agent, local retrieval, safety rails, per-channel policy. |
| **3** | Community Watch | Incidents, alerts + escalation, check-in roster, situational map. |
| **4** | Environment | Weather, NWS/CAP ingest, SAME over RTL-SDR, geo services, waypoints. |
| **5** | Federation | Node-to-node sync of boards, mail, and incidents over a private portnum. |
| **6** | Hardening | Full RBAC, admin UI, backup/restore, observability, install tooling, docs. |

**REQ-PROD-001** — The system **MUST** be useful and shippable at the end of Phase 1
without any AI, weather, or watch features. Phase 1 is not a prototype.

**REQ-PROD-002** — Every phase after 1 **MUST** be independently disableable at runtime via
config, and the system **MUST** start and function correctly with any subset of them
disabled.

## 7. Deployment target

**Reference hardware (the spec is written against this):**

- Raspberry Pi 5, 8 GB (16 GB recommended)
- Raspberry Pi AI HAT+ 2 — Hailo-10H, 40 TOPS INT4, 8 GB on-board RAM
- Raspberry Pi OS Trixie, 64-bit
- Official 27 W USB-C PSU (5 V / 5 A) — **required**, not optional, under inference load
- Active cooler
- Meshtastic radio over USB serial (primary) or BLE (fallback)
- Boot/storage on microSD or USB SSD — **NVMe is unavailable**, the AI HAT+ 2 occupies the
  single PCIe lane
- *Optional:* RTL-SDR v3/v4 + 162 MHz antenna for SAME/NOAA Weather Radio ingest

**REQ-PROD-003** — The system **MUST** also run, with the AI provider set to a CPU or
remote backend, on: a Pi 4 (4 GB+), a Pi 5 without the AI HAT, and an x86-64 Linux host.
The radio link and all non-AI features **MUST NOT** depend on Pi-5-specific hardware.

**REQ-PROD-004** — Idle steady-state resource use **MUST** stay under 400 MB RSS and 5% CPU
on the reference hardware, excluding the inference sidecar.

## 8. Constraints carried into design

These are restated from [00-README.md](00-README.md) §"three constraints" because they are
the source of most design decisions downstream.

| ID | Constraint | Verified against |
|---|---|---|
| C1 | 233-byte hard payload cap; 200-byte practical text budget | [mesh.proto](https://github.com/meshtastic/protobufs/blob/master/meshtastic/mesh.proto) |
| C2 | `LONG_FAST` default = 1.07 kbps shared among all nodes in range | [Radio Settings](https://meshtastic.org/docs/overview/radio-settings/) |
| C3 | Firmware silently drops TX above 40% channel utilisation (25% polite) | [airtime.cpp](https://github.com/meshtastic/firmware/blob/master/src/airtime.cpp) |
| C4 | EU regions enforce a 10% duty cycle (effective ~5% air-util TX gate) | [airtime.cpp](https://github.com/meshtastic/firmware/blob/master/src/airtime.cpp) |
| C5 | Default hop limit 3, max 7 (3-bit field) | [LoRa Config](https://meshtastic.org/docs/configuration/radio/lora/) |
| C6 | Broadcasts get implicit ACK only; no per-recipient delivery confirmation | [Mesh Broadcast Algorithm](https://meshtastic.org/docs/overview/mesh-algo/) |
| C7 | Local LLM ceiling on Hailo: **1.7B params max, 1.5B in practice**, **2048-token context**, ~5–10 tok/s | [Hailo MODELS.rst](https://github.com/hailo-ai/hailo_model_zoo_genai/blob/main/docs/MODELS.rst) (vendor); [CNX](https://www.cnx-software.com/2026/01/20/raspberry-pi-ai-hat-2-review-a-40-tops-ai-accelerator-tested-with-computer-vision-llm-and-vlm-workloads/) (independent). Provisional — doc 15 U2–U4 |
| C8 | Hailo model appears to unload on idle; ~25–40 s cold start reported | [Hackster hands-on](https://www.hackster.io/news/gen-ai-on-your-raspberry-pi-a-hands-on-review-of-the-raspberry-pi-ai-hat-2-3c829a8894dd). Provisional — doc 15 U5 |
| C9 | No NVMe alongside the AI HAT+ 2 | [Raspberry Pi news](https://www.raspberrypi.com/news/introducing-the-raspberry-pi-ai-hat-plus-2-generative-ai-on-raspberry-pi-5/) |

## 9. Product principles

Ranked. When two principles conflict, the higher one wins.

1. **Airtime is sacred.** The mesh belongs to everyone in range, including people who never
   use Outpost. Never transmit what wasn't asked for, never transmit twice what could be
   transmitted once, and never transmit at all when a query could be answered locally.

2. **Offline is the design case, not the degraded case.** Internet-dependent features are
   enhancements layered onto a system that is complete without them. No feature may become
   unavailable because the WAN went down.

3. **Structure beats chat.** An incident is a record with a type, a location, a severity,
   and a lifecycle — not a message with an emoji in it. Structure is what makes data
   queryable, mappable, syncable, and summarisable by a small model.

4. **Terse is kind.** 200 characters is the canvas. Write for someone reading a 1.3-inch
   monochrome screen in the rain. Abbreviate aggressively, drop units where unambiguous,
   and lead with the answer.

5. **Fail loud locally, quiet on the air.** Errors go to the log, the dashboard, and
   metrics in full detail. Over the radio, an error is one short line and never a stack
   trace, never a retry storm.

6. **The operator is sovereign.** Every behaviour — what the AI may say, which boards
   exist, who may broadcast, how much airtime the node may consume — is the operator's to
   configure, and defaults are conservative.

7. **Never impersonate authority.** Outpost is not the National Weather Service, not 911,
   not the sheriff. Relayed official content is labelled as relayed and possibly stale;
   AI-generated content is labelled as AI-generated. This is a safety requirement, not a
   legal fig leaf.

## 10. Success metrics

Instrumented and exposed on the dashboard (doc 11 §5.1) and via Prometheus (doc 12 §9).

**Adoption**
- Distinct member node IDs interacting per week
- Posts per week; mail per week; incidents per week
- Ratio of returning to first-time members

**Health**
- Node airtime share (target: ≤ configured budget, default 8%)
- Command success rate (target ≥ 98% excluding user typos)
- Median command round-trip latency (target ≤ 10 s for non-AI, ≤ 25 s for AI, warm)
- Multi-part response rate (target ≤ 15% of responses)
- Uptime and unplanned restart count

**Assistant quality**
- Tool-call success rate
- Answers grounded in retrieved local data vs. model-parametric (target ≥ 60% grounded)
- Refusal/fallback rate
- p95 time-to-first-token and full response

## 11. Risks

| Risk | Impact | Mitigation | Owner doc |
|---|---|---|---|
| Node congests the mesh, community turns hostile | Project failure | Airtime Governor is mandatory and enforced at the only egress point; default budgets conservative; soak test G3 | 03 §4 |
| 1.7B model produces confident wrong answers about safety-critical topics | Real-world harm | Retrieval-grounded answers only; hard refusal list; mandatory `[AI]` labelling; never AI-generate alert content | 06 §7 |
| Hailo throughput or availability disappoints | Phase 2 slips | Provider abstraction + benchmark harness; CPU llama.cpp is a first-class provider, not a fallback | 06 §2–3 |
| BLE link instability on Linux | Unattended operation fails | Serial is the default and recommended transport; BLE is supervised with backoff and explicitly best-effort | 03 §2 |
| SD card wear from write-heavy SQLite | Node death at 6 months | WAL mode, batched writes, log rotation to tmpfs, documented USB-SSD recommendation | 05 §8 |
| Operator has no time to moderate | Boards fill with noise | Rate limits by trust level; auto-expiry; quiet-hours; one-tap moderation from the dashboard | 12 §5 |
| A malicious peer spoofs a node ID | Trust model collapse | Meshtastic PKI where available; trust levels gate destructive actions; all admin actions require out-of-band enrolment | 12 §3 |

## 12. Open product questions

These require a human decision. Tracked in [15-DECISIONS.md](15-DECISIONS.md).

- **Q1.** Should unregistered peers be able to *read* boards, or must everyone register?
  (Default assumed: read yes, write no.)
- **Q2.** What is the default retention for posts, mail, and incidents?
  (Default assumed: posts 90d, mail 180d, incidents 365d, alerts forever.)
- **Q3.** Should the assistant be available on the primary public channel at all, or DM-only
  by default? (Default assumed: DM-only; public channel opt-in per-channel by the operator.)
- **Q4.** Is a legal disclaimer required in the alert text itself for your jurisdiction, and
  what must it say?
- **Q5.** Federation trust: does a neighbouring node's content appear as first-class local
  content, or is it visually and structurally segregated? (Default assumed: segregated,
  attributed to origin node.)
