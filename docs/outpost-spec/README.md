# Outpost — Specification Set

A requirements and design specification for a Meshtastic-based community platform:
bulletin boards, private mail, community watch, weather, and a local offline AI assistant,
running on a Raspberry Pi 5 attached to a LoRa mesh radio.

Written to be built from by an AI coding agent.

**Start here: [`docs/00-README.md`](docs/00-README.md)** — index, reading order, the three
constraints that shape every decision, and the repository layout to produce.

## Contents

| # | Document | Covers |
|---|---|---|
| 00 | [Index & Conventions](docs/00-README.md) | Reading order, glossary, constraints, repo layout |
| 01 | [PRD](docs/01-PRD.md) | Vision, users, goals, non-goals, risks, metrics |
| 02 | [Architecture](docs/02-ARCHITECTURE.md) | Process model, layering, stack, full config schema |
| 03 | [Mesh Transport](docs/03-MESH-TRANSPORT.md) | Radio link, **Airtime Governor**, chunking, framing |
| 04 | [Command Grammar](docs/04-COMMAND-GRAMMAR.md) | The over-the-air UX, session model, terse register |
| 05 | [Data Model](docs/05-DATA-MODEL.md) | SQLite schema, migrations, retention |
| 06 | [AI Agent](docs/06-AI-AGENT.md) | Providers, retrieval, token budget, safety rails |
| 07 | [BBS & Mail](docs/07-BBS-AND-MAIL.md) | Boards, threads, mail, moderation — the MVP |
| 08 | [Community Watch](docs/08-COMMUNITY-WATCH.md) | Incidents, alerts, escalation, check-in roster |
| 09 | [Weather & Alerts](docs/09-WEATHER-AND-ALERTS.md) | NWS, CAP, SAME over RTL-SDR, geo |
| 10 | [Node Federation](docs/10-NODE-FEDERATION.md) | Node-to-node sync on a private portnum |
| 11 | [Web API & Dashboard](docs/11-WEB-API-AND-DASHBOARD.md) | REST/WS contract, SPA, map, admin |
| 12 | [Security & Identity](docs/12-SECURITY-AND-IDENTITY.md) | Threat model, trust, privacy, hardening |
| 13 | [Roadmap](docs/13-ROADMAP.md) | Seven phases with exit gates |
| 14 | [Testing](docs/14-TESTING.md) | Mesh simulator, 100+ named scenarios, eval set |
| 15 | [Decisions](docs/15-DECISIONS.md) | 14 ADRs, open questions, unknowns, canonical numbers |

## Before you build

Three things in this spec were checked against primary sources and contradict common
assumptions. They shape the whole design:

1. **A Meshtastic payload is 233 bytes, and ~200 in practice.** The widely-quoted 237 is
   stale. Verbosity is a denial-of-service problem on a 1 kbps shared channel, not a style
   problem — hence the mandatory Airtime Governor.
2. **There is no 4B model for the Hailo-10H.** The GenAI zoo tops out at 1.7B, `hailo-ollama`
   serves a smaller subset still, and the context is a hard 2048 tokens. In independent
   benchmarks the Pi 5 CPU beat the NPU on every model. Hence the provider abstraction and
   the mandatory benchmark harness.
3. **NWS alerts must be queried by point or county, never by forecast zone.** Zone queries
   silently omit polygon warnings — tornado and severe thunderstorm. Getting this wrong drops
   exactly the alerts that matter.

## Before you start Phase 1

[`docs/15-DECISIONS.md`](docs/15-DECISIONS.md) Part B lists 15 questions that need a human
answer. Each has a working default so the build can proceed, but three are worth settling
early: **Q4** (the legal disclaimer for your jurisdiction), **Q11** (the local knowledge that
makes the AI worth its airtime), and **Q15** (the real project name).
