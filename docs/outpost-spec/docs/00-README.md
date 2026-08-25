# OUTPOST — Specification Set

> **Working codename.** "Outpost" is a placeholder. Replace it project-wide before public
> release; a global find/replace on `Outpost` / `outpost` is sufficient. Nothing in this spec
> depends on the name.

Outpost is an off-grid community platform that runs on a Raspberry Pi 5 attached to a
Meshtastic LoRa radio. It gives a neighbourhood, valley, island, or campus a shared
bulletin board, a private mail system, a situational-awareness layer for incidents and
alerts, and a local AI assistant — all of it functioning with **no internet connection
whatsoever**.

The lineage is deliberate: 1990s dial-up BBSes, Hotline, Carracho, and Wired-era
decentralised servers. Those systems worked because a community ran its *own* node, set
its *own* rules, and reached it over a link nobody else controlled. Outpost is that
pattern rebuilt on top of LoRa mesh, where the link genuinely is nobody else's.

---

## How to use this specification set

This spec set is written to be built by an AI coding agent. It is deliberately split into
focused documents so that a single unit of work maps to a single document, rather than
requiring the agent to hold a 200-page monolith in context.

**Read in this order before writing any code:**

| # | Document | Read it when |
|---|---|---|
| 01 | [PRD](01-PRD.md) | Always first. Vision, users, goals, non-goals, success criteria. |
| 02 | [Architecture](02-ARCHITECTURE.md) | Always second. Process model, stack, module boundaries. |
| 03 | [Mesh Transport](03-MESH-TRANSPORT.md) | Before touching the radio, the router, or anything that emits a message. |
| 04 | [Command Grammar](04-COMMAND-GRAMMAR.md) | Before implementing any user-facing over-the-air behaviour. |
| 05 | [Data Model](05-DATA-MODEL.md) | Before writing any persistence code or migration. |
| 06 | [AI Agent](06-AI-AGENT.md) | Phase 2. Inference providers, tool calling, retrieval, safety rails. |
| 07 | [BBS & Mail](07-BBS-AND-MAIL.md) | Phase 1. Boards, threads, mail, moderation. |
| 08 | [Community Watch](08-COMMUNITY-WATCH.md) | Phase 3. Incidents, alerts, escalation, check-in roster. |
| 09 | [Weather & Alerts](09-WEATHER-AND-ALERTS.md) | Phase 4. NWS, Open-Meteo, CAP, SAME/RTL-SDR. |
| 10 | [Node Federation](10-NODE-FEDERATION.md) | Phase 5. Multi-node sync over a private portnum. |
| 11 | [Web API & Dashboard](11-WEB-API-AND-DASHBOARD.md) | Phase 1 onward. REST/WS contract, SPA, admin. |
| 12 | [Security & Identity](12-SECURITY-AND-IDENTITY.md) | Before any auth, trust, or moderation code. |
| 13 | [Roadmap](13-ROADMAP.md) | For sequencing. Defines phase exit criteria. |
| 14 | [Testing](14-TESTING.md) | Before writing the first test. Defines the mesh simulator. |
| 15 | [Decisions & Open Questions](15-DECISIONS.md) | ADRs, and the list of things a human must decide. |

**Do not begin Phase N+1 until the exit criteria for Phase N in
[13-ROADMAP.md](13-ROADMAP.md) are met and demonstrable.**

---

## Requirement identifiers

Every normative requirement carries a stable ID of the form `REQ-<AREA>-<NNN>`, e.g.
`REQ-TRANSPORT-012`. Areas are:

`PROD` · `ARCH` · `TRANSPORT` · `CMD` · `DATA` · `AI` · `BBS` · `WATCH` ·
`WX` · `FED` · `API` · `UI` · `SEC` · `TEST`

`BBS` covers boards, mail, and the channel directory — all of doc 07. Operational
requirements (systemd, backups, metrics) live under `SEC` in doc 12 §9.

Requirement IDs are permanent. If a requirement is dropped, mark it `WITHDRAWN` in place —
never reuse the number.

Keywords **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, **MAY** are used per
[RFC 2119](https://www.rfc-editor.org/rfc/rfc2119).

Every commit that implements a requirement **MUST** reference its ID in the commit message,
e.g. `feat(bbs): thread reply ordering (REQ-BBS-012)`.

---

## The three constraints that shape everything

An agent building this system will make bad decisions unless it internalises these first.
They are not preferences. They are physics and vendor limits.

### 1. The link is ~1 kbps and shared

The default Meshtastic preset (`LONG_FAST`) carries **1.07 kbps**. A short message occupies
the channel for roughly **half a second**; a full 233-byte packet for around **two seconds**
(doc 03 §1). Every node in radio range shares that channel. Meshtastic firmware **skips
sending** — the packet never goes out, with only a log line — when channel utilisation
exceeds 40% (25% in polite mode).

**Consequence:** verbosity is not a style problem, it is a denial-of-service problem.
Outpost is a **pull/digest** system, not a push/firehose system. Every byte transmitted
must be justified. See [03-MESH-TRANSPORT.md](03-MESH-TRANSPORT.md) §4 (Airtime Governor).

### 2. A message holds ~200 usable **bytes**

The Meshtastic `Data` protobuf payload is capped at **233 bytes**
([`Constants.DATA_PAYLOAD_LEN`](https://github.com/meshtastic/protobufs/blob/master/meshtastic/mesh.proto)).
The Android client composer caps user input at **200 bytes**, and PKI-encrypted direct
messages consume a further 12 bytes of overhead.

**Consequence:** Outpost targets **≤ 200 bytes per transmitted message** as a hard design
budget, and multi-part responses are an explicit, rate-limited, opt-in feature — not the
default. Every limit in this spec set is a **byte** count of UTF-8, never a character count.
All human-facing text is written in a terse register defined in
[04-COMMAND-GRAMMAR.md](04-COMMAND-GRAMMAR.md) §7.

### 3. The local model is small and its context is capped at 2048 tokens

As of this spec's writing, the Hailo GenAI Model Zoo for the Hailo-10H lists nothing above
**Qwen3-1.7B-Instruct**, and the `hailo-ollama` server has been observed serving a smaller
subset still (1.5B Qwen variants plus Llama3.2-3B). **There is no 4B-class model**, and every
compiled model in the zoo declares a **2048-token context** baked into the `.hef` rather than
exposed as a runtime parameter.

Hailo has said publicly that further model support is in development, so treat the specific
model list as a snapshot to be re-verified on the actual unit — see
[15-DECISIONS.md](15-DECISIONS.md) Part C, unknowns U2–U7. What is *not* in doubt is the
shape of the constraint: a small model with a small context.

**Consequence:** the AI assistant is a *retrieval-and-summarise* agent over a small,
carefully-budgeted context — not a general chatbot. Prompt engineering must fit system
prompt + retrieved evidence + conversation + question inside 2048 tokens with headroom for
the answer. See [06-AI-AGENT.md](06-AI-AGENT.md) §5 (Token Budget).

---

## What makes this net-new

Outpost draws inspiration from three existing projects but is not an amalgamation of them.
The differentiators, each of which is a first-class subsystem here and absent or vestigial
in the references:

1. **The Airtime Governor.** A single mandatory outbound scheduler with per-class budgets,
   priority preemption, and backpressure. No subsystem may transmit directly. Existing mesh
   bots transmit ad hoc and congest their own meshes.
   → [03-MESH-TRANSPORT.md](03-MESH-TRANSPORT.md) §4

2. **A unified session model that fuses stateless commands with BBS modality.** Global
   verbs always work, from anywhere, at any depth. A session may *additionally* hold a
   context stack (you are "in" a board, mid-compose, mid-incident-report) which supplies
   defaults and enables bare-word input. Existing systems pick one paradigm and suffer for
   it — modal BBSes are painful over a lossy link with 60-second round trips; purely
   stateless bots cannot express a threaded conversation.
   → [04-COMMAND-GRAMMAR.md](04-COMMAND-GRAMMAR.md) §3

3. **An AI agent with tools over local community data.** The assistant is not a passthrough
   to a chat model. It is a constrained tool-calling agent whose tools read the local BBS,
   incident log, node directory, weather cache, and operator-curated knowledge base. Asking
   "what's happening on Route 9?" performs retrieval over local incident records and
   summarises them into 200 characters, offline. This is the feature that makes the AI
   worth its airtime.
   → [06-AI-AGENT.md](06-AI-AGENT.md) §4

4. **Community watch as structured data, not as chat.** Incidents are typed, geotagged,
   lifecycle-managed records with acknowledgement tracking and escalation policy — queryable,
   mappable, and syncable between nodes. Not free-text messages with a keyword in them.
   → [08-COMMUNITY-WATCH.md](08-COMMUNITY-WATCH.md)

5. **Node-to-node federation on a private portnum with a compact binary protocol.** Human
   traffic uses `TEXT_MESSAGE_APP`; inter-node replication uses a dedicated
   `PRIVATE_APP`-range portnum with CBOR framing and delta sync — so replication never
   pollutes human channels and costs a fraction of the airtime.
   → [10-NODE-FEDERATION.md](10-NODE-FEDERATION.md)

---

## Glossary

| Term | Meaning |
|---|---|
| **Node** | An Outpost installation: one Pi 5 + one Meshtastic radio + one database. |
| **Radio** | The Meshtastic device attached to the node. Distinct from "node". |
| **Peer** | A Meshtastic device on the mesh that is not an Outpost node — i.e. a user's handheld. |
| **Mesh node ID** | Meshtastic's 32-bit node number, rendered `!a1b2c3d4`. |
| **Member** | A person, identified by their peer's mesh node ID plus a claimed handle. |
| **Session** | Per-member conversational state held by the router. |
| **Board** | A BBS topic area containing threads. |
| **Thread** | An ordered set of posts under a board, rooted at an opening post. |
| **Mail** | Private store-and-forward message between members. |
| **Incident** | A structured community-watch report with type, severity, location, lifecycle. |
| **Alert** | A high-priority broadcast, either operator-raised or ingested from CAP/SAME. |
| **Digest** | A compact "what's new since you last checked" summary. |
| **Governor** | The Airtime Governor: mandatory outbound scheduler. |
| **Provider** | A pluggable LLM inference backend. |
| **Federation** | Replication of boards/mail/incidents between Outpost nodes. |
| **Airtime** | Channel occupancy time. The scarce resource. |

---

## Repository layout the agent MUST produce

```
outpost/
├── pyproject.toml               # hatchling; project name "outpost"
├── README.md
├── docs/                        # this spec set, copied in verbatim
├── src/outpost/
│   ├── __main__.py              # entrypoint: `python -m outpost`
│   ├── config.py                # pydantic-settings; YAML + env overlay
│   ├── app.py                   # composition root, lifespan, DI wiring
│   ├── transport/               # radio link, framing, governor, chunking  (doc 03)
│   ├── router/                  # dispatch, sessions, context stack        (doc 04)
│   ├── commands/                # one module per command family            (doc 04)
│   ├── store/                   # SQLite, migrations, repositories         (doc 05)
│   ├── ai/                      # providers, tools, retrieval, budget      (doc 06)
│   ├── bbs/                     # boards, threads, mail                    (doc 07)
│   ├── watch/                   # incidents, alerts, escalation, checkin   (doc 08)
│   ├── env/                     # weather, CAP, SAME ingest                (doc 09)
│   ├── fed/                     # node-to-node sync                        (doc 10)
│   ├── web/                     # FastAPI app, routers, WS hub             (doc 11)
│   ├── render/                  # string catalogue, terse register, abbrev (doc 04 §7)
│   └── security/                # identity, trust, authorisation, limits   (doc 12)
├── web-ui/                      # SPA source; built assets vendored to src/outpost/web/static
├── config/
│   ├── config.yaml              # shipped defaults                         (doc 02 §7)
│   ├── config.local.yaml        # operator overrides (gitignored)
│   └── intents.yaml             # natural-language intent table            (doc 04 §8)
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── sim/                     # mesh simulator harness                   (doc 14 §3)
│   ├── eval/                    # AI evaluation set                        (doc 14 §8)
│   ├── fixtures/                # seeded community, recorded HTTP          (doc 14 §9)
│   └── hardware/                # manual hardware checklist                (doc 14 §7)
├── deploy/
│   ├── outpost.service          # systemd unit
│   ├── install.sh               # Pi 5 bootstrap
│   └── config.example.yaml
└── tools/
    ├── bench_inference.py       # provider benchmark harness               (doc 06 §3)
    └── seed_dev_data.py
```

---

## Sources

Meshtastic and NWS constraints were verified against primary sources (protocol definitions,
firmware source, official API documentation). Hailo performance figures come from a mix of
vendor documentation and independent reviews and are treated as **provisional** — see
[15-DECISIONS.md](15-DECISIONS.md) Part C. Key references:

- [Meshtastic `mesh.proto` — `DATA_PAYLOAD_LEN`](https://github.com/meshtastic/protobufs/blob/master/meshtastic/mesh.proto)
- [Meshtastic `portnums.proto` — `PRIVATE_APP`](https://github.com/meshtastic/protobufs/blob/master/meshtastic/portnums.proto)
- [Meshtastic Mesh Broadcast Algorithm](https://meshtastic.org/docs/overview/mesh-algo/)
- [Meshtastic Radio Settings — modem presets](https://meshtastic.org/docs/overview/radio-settings/)
- [Meshtastic firmware `airtime.cpp` — TX gating](https://github.com/meshtastic/firmware/blob/master/src/airtime.cpp)
- [Meshtastic Python API](https://python.meshtastic.org/)
- [Hailo GenAI Model Zoo — MODELS.rst](https://github.com/hailo-ai/hailo_model_zoo_genai/blob/main/docs/MODELS.rst)
- [CNX Software — AI HAT+ 2 LLM benchmarks](https://www.cnx-software.com/2026/01/20/raspberry-pi-ai-hat-2-review-a-40-tops-ai-accelerator-tested-with-computer-vision-llm-and-vlm-workloads/)
- [NWS API documentation](https://www.weather.gov/documentation/services-web-api)
- [OASIS CAP v1.2](https://docs.oasis-open.org/emergency/cap/v1.2/CAP-v1.2-os.html)

Inspiration projects, reviewed but not vendored:
[Mesh-API](https://github.com/mr-tbot/mesh-api) ·
[ZephyrGate](https://github.com/netnutmike/Meshtastic-ZephyrGate) ·
[TC²-BBS-mesh](https://github.com/TheCommsChannel/TC2-BBS-mesh)
