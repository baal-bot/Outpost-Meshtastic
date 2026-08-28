# 12 — Security, Identity & Privacy

**Status:** Baseline · **All phases** · **Prerequisite:** [02-ARCHITECTURE.md](02-ARCHITECTURE.md)
**Implements:** `src/outpost/security/`

---

## 1. Threat model

Be honest about what this system can and cannot defend against. Overclaiming here would be
worse than underclaiming, because people may make safety decisions based on it.

| Adversary | Capability | Outpost's defence |
|---|---|---|
| **Curious neighbour** | Has the public channel PSK (it is `AQ==`, publicly known) | Sensitive content lives on a secondary channel with a real PSK; trust levels gate actions |
| **Nuisance user** | Floods the board, spams mail, wastes airtime | Trust levels, per-class rate limits, mute/block, operator moderation |
| **Spoofer** | Forges a mesh node ID in a packet header | Meshtastic PKI verification where available; destructive actions require out-of-band-established trust; audit log |
| **Prompt injector** | Posts text designed to manipulate the AI for other users | Evidence is delimited and declared non-instructional; output post-filter; read-only tools (doc 06 §7) |
| **Passive RF listener** | Receives every packet in range | Channel AES-CTR encryption; PKI for DMs; **the packet header is always plaintext** — traffic analysis is always possible |
| **Node-adjacent attacker** | On the LAN with the dashboard | Auth, CSRF, rate limits, LAN-only bind |
| **Malicious federated peer** | A paired Outpost node acting badly | HMAC-authenticated pairing, per-peer quotas, auto-pause on flood, no propagating deletes |
| **Physical access to the Pi** | Full compromise | **Out of scope.** Full-disk encryption is documented as an operator option; it is not implemented by Outpost |

**REQ-SEC-001** — Documentation, the dashboard, and over-the-air help **MUST NOT** claim
security properties the architecture does not provide. Specifically they **MUST** state:
mail is readable by the node operator; the primary channel is not private; message metadata
is visible to anyone in radio range.

---

## 2. Identity

**REQ-SEC-002** — The identity anchor is the Meshtastic node ID (`mesh_id`). Handles are
labels bound to a node ID, never identity themselves.

**REQ-SEC-003** — Where the connected radio reports the peer's Meshtastic public key after a
successful PKI direct-message decrypt, the node **MUST** record its fingerprint. A first-seen key
is pending until an operator explicitly approves it. Promotion above `member` requires an approved
key, and every elevated mesh action requires a direct PKI packet matching that key. Packet IDs are
retained with the approved fingerprint to reject replay across process restarts.

A different public key for a known node ID **MUST** be treated as a security event: deny the
action, log and audit both fingerprints, place the key in the dashboard review queue, demote the
identity to `guest`, and prevent handle re-enrollment until an operator approves or rejects the
change. Approving a replacement preserves the member record but does not silently restore elevated
social trust; the operator must separately review that promotion. Radios or firmware that do not
provide usable authenticated key metadata may use ordinary member features, but elevated mesh
actions are dashboard-only.

**REQ-SEC-004** — Handle claim rules: 2–12 chars `[a-z0-9_-]`, unique, not a command name or
alias, not reserved (`admin`, `operator`, `system`, `outpost`, `all`, `here`, the node's own
short name).

**REQ-SEC-005** — A handle already bound to a different node ID **MUST NOT** be reclaimable
over the air. Transfer requires operator action from the dashboard with an audit entry.

**REQ-SEC-006** — Handle changes **MUST** be rate-limited (default 1 per 24 h) and the
previous handle **MUST** be held in reserve for 30 days to prevent rapid impersonation cycling.

---

## 3. Trust levels

**REQ-SEC-007** — Six ordered levels:

| Level | Granted by | Can |
|---|---|---|
| `blocked` | Operator | Nothing. All input discarded silently after a single notification |
| `guest` | Automatic on first contact | Read public boards, view alerts, `HELP`, `WHOAMI`, `OK` check-in, `REPORT` |
| `member` | Claiming a handle (or operator, if `security.require_approval`) | Post, reply, mail, subscribe, confirm incidents, `ASK` |
| `trusted` | Operator | Resolve incidents, post to restricted boards, higher rate limits |
| `responder` | Operator | Raise alerts, set `critical` severity, open watch events, see the roster |
| `operator` | Config / dashboard | Everything, including over-air operator commands |

**REQ-SEC-008** — `guest` **MUST** be able to `REPORT` an incident and `OK` check in.
Requiring registration before someone can report a fire is a safety failure. Guest reports
carry a `unverified` flag and do not auto-escalate.

**REQ-SEC-009** — Trust promotion above `member` **MUST** require an explicit operator action
recorded in `audit_log`. There **MUST NOT** be automatic promotion by activity, tenure, or
any heuristic.

**REQ-SEC-010** — `security.require_approval` (default `false`) makes handle claims require
operator approval before granting `member`. Communities in contested or high-abuse
environments turn this on.

**REQ-SEC-011** — Blocking **MUST** send one notification then silently discard. Repeatedly
telling a blocked user they are blocked spends airtime on someone abusing the system.

---

## 4. Authorisation

**REQ-SEC-012** — Authorisation **MUST** be enforced in the domain service layer, not in the
command parser and not in repositories. Both interfaces (radio, web) call the same services
and receive the same decisions (REQ-ARCH-007).

**REQ-SEC-013** — Every `CommandSpec` declares `min_trust`. The router **MUST** check it
before dispatch, and the service **MUST** check again — defence in depth, because the web
interface does not go through the router.

**REQ-SEC-014** — Authorisation denials **MUST** be terse and **MUST NOT** leak the existence
of resources the requester cannot see. A board they cannot read is reported as not existing,
not as forbidden.

**REQ-SEC-015** — Over-the-air operator commands are restricted to the list in doc 04 §4.8.
Configuration changes, board creation, trust escalation to `operator`, and federation pairing
**MUST** require the dashboard. A forged node ID must not be able to take over the node.

---

## 5. Rate limiting and abuse

**REQ-SEC-016** — Token-bucket rate limits **MUST** be applied per member per class, with
buckets persisted across restarts (in `kv`) so a restart is not a reset for an abuser.

| Bucket | guest | member | trusted/responder | operator |
|---|---|---|---|---|
| commands/min | 4 | 10 | 20 | 60 |
| commands/hour | 30 | 120 | 300 | ∞ |
| posts/hour | 0 | 12 | 30 | ∞ |
| threads/hour | 0 | 4 | 10 | ∞ |
| mail/hour | 0 | 8 | 20 | ∞ |
| ai/hour | 0 | 6 | 15 | 60 |
| incidents/hour | 2 | 6 | 15 | ∞ |
| ↳ *emergency path* | *exempt — see REQ-WATCH-022a* | | | |
| alerts/hour | 0 | 0 | 0 (responder: 6) | ∞ |

**REQ-SEC-017** — There **MUST** be a node-wide circuit breaker: if total inbound command
rate exceeds `security.global_rate_ceiling` (default 60/min), the node enters defensive mode
— serves only `alert`, `HELP`, and the emergency path (`REPORT`, `OK`, `HELPME`, and the
keyword handler; REQ-WATCH-022a) — logs, and raises a dashboard alarm. A mesh under a
flooding attack must not be amplified by the node replying to everything, but neither may
the defence silence the one message that matters.

Safety-floor attempts still count toward member and node defensive telemetry. The first
`REPORT`, `REPORT!`, `OK`, or `HELPME` fingerprint is admitted even when ordinary limits are
exhausted; an equivalent normalized command, details, and current position is then silently
coalesced for `security.safety_repeat_window_seconds` (default 120 seconds). Changed details,
status, or position remain admissible. Fingerprints and aggregate counts are persisted in
`safety_floor_attempt`, shown in the operator workspace, and retained for
`security.safety_attempt_retention_hours` (default 72 hours).

**REQ-SEC-018** — Repeated rate-limit violations (default 20 in an hour) **MUST**
auto-mute the member for an escalating duration (15 m → 1 h → 6 h → 24 h) with an audit entry
and a dashboard notification.

**REQ-SEC-019** — Rate-limit responses **MUST** be sent at most once per bucket per window.
After the first notification, subsequent over-limit messages are discarded silently.

**REQ-SEC-020** — Content limits **MUST** be enforced on write: post body ≤1000 chars
(≤200 over the air), subject ≤64, handle ≤12, alert headline ≤140 bytes, incident title ≤64.
All **MUST** be validated with byte counts for UTF-8, not character counts.

---

## 6. Web security

**REQ-SEC-021** — Default bind is the LAN; `0.0.0.0` is permitted but the install script
**MUST NOT** open a WAN firewall port and the dashboard **MUST** warn when it detects it is
reachable from a non-private address.

**REQ-SEC-022** — Passwords **MUST** be hashed with Argon2id at reasonable Pi-appropriate
parameters (documented in code, benchmarked so login takes ~250 ms on a Pi 5).

**REQ-SEC-023** — Security headers **MUST** be set: `Content-Security-Policy` with no
`unsafe-inline` for scripts and no external origins, `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`.

**REQ-SEC-024** — HTTPS **SHOULD** be supported via an operator-supplied certificate or a
self-signed certificate generated at install. HTTP on the LAN is the default and this
tradeoff **MUST** be documented.

**REQ-SEC-025** — All API input **MUST** be validated by Pydantic models. Path traversal,
SQL injection (REQ-DATA-034), and XSS (SPA escapes by default; no `dangerouslySetInnerHTML`)
**MUST** be covered by tests.

**REQ-SEC-026** — Dependency scanning (`pip-audit`) **MUST** run in CI and **MUST** fail the
build on a known high-severity vulnerability.

**REQ-SEC-026a** — A web operator account and a mesh member identity are distinct security
principals. Audit rows for dashboard actions **MUST** name the authenticated web account; granting
mesh trust `operator` **MUST NOT** grant dashboard access, and creating a web Operator **MUST NOT**
change mesh trust. An Administrator **MAY** link one mesh Operator radio to one Administrator or
Operator web account for identity attribution. That association **MUST NOT** merge the
principals or copy authority in either direction, and a mesh Operator without a web account **MUST**
still appear in the Operator Access inventory.

**REQ-SEC-026b** — TOTP recovery values **MUST** be suitable for offline field transcription,
displayed only at issuance, stored only as hashes, and consumed atomically on use. Diagnostic
bundles **MUST** redact account password hashes, TOTP secrets, and recovery-code hashes.

**REQ-SEC-026c** — Web access is operator-only by default. The optional `viewer` account is an
unattended-display principal, not a junior operator: it **MUST** be default-denied from authenticated
APIs except its own credential/session controls and a purpose-built aggregate wallboard contract.
That contract **MUST NOT** contain identities, stable member identifiers, message content, private
mail metadata, coordinates, welfare or operator notes, backups, audit records, AI review data, or
configuration secrets. Registering a new API route **MUST NOT** grant viewer access implicitly.

---

## 7. What encryption does and does not give you

**REQ-SEC-027** — The following **MUST** be stated verbatim (or equivalently) in the
operator documentation and summarised in `HELP PRIVACY`:

> **What is protected.** Messages on a channel with a real pre-shared key are encrypted with
> AES-CTR between radios. Direct messages on recent Meshtastic firmware are additionally
> encrypted to the recipient's public key.
>
> **What is not protected.**
> - The **default public channel PSK is publicly known** (`AQ==`). Anything on it is public.
> - **Packet headers are always sent unencrypted** so that nodes can relay traffic they cannot
>   read. Who is talking to whom, how often, and from roughly where is visible to anyone in
>   range.
> - **There is no forward secrecy.** A key compromised later exposes traffic captured earlier.
> - **Channel messages have no integrity check.** Anyone with the channel key can forge a
>   message on it.
> - **The node stores everything in plaintext.** Mail, posts, positions, and AI conversations
>   are readable by whoever controls the Pi.
>
> Treat this system as a village noticeboard with a lockable back room — not as a secure
> messenger.

**REQ-SEC-028** — Outpost **MUST NOT** implement its own message encryption on top of
Meshtastic. Rolling application-layer crypto here would add risk without adding meaningful
protection, given plaintext headers and a plaintext store. The federation HMAC (doc 10 §4) is
authentication only and **MUST** be described as such.

---

## 8. Position privacy

Location is the most sensitive data the system handles, and a mesh broadcasts it by default.

**REQ-SEC-029** — Members **MUST** be able to control position sharing with three settings
(`prefs.position`):

| Setting | Behaviour |
|---|---|
| `full` | Position visible to `member`+ via `POS`, plotted on the operator map |
| `coarse` (default) | Rounded to `security.coarse_precision_m` (default 500 m) for all views except the operator's |
| `off` | Position ingested for incident attachment only if the member explicitly attaches it; never queryable, never plotted |

**REQ-SEC-030** — `POS <handle>` **MUST** respect the target's setting, **MUST** require trust
≥ `member`, and **MUST** be rate-limited. A `guest` **MUST NOT** be able to locate anyone.

**REQ-SEC-031** — Position **MUST NOT** be available to the AI (REQ-AI-056) and
**MUST NOT** appear in any AI-retrievable evidence.

**REQ-SEC-032** — Position history **MUST** be retained no longer than
`store.retention.position_days` (default **7**) and the operator **MUST** be able to set it to
0, keeping only the current position.

**REQ-SEC-033** — A position attached to an incident is **published** and this **MUST** be
stated at the moment of attachment (REQ-WATCH-010). Publishing is a deliberate act, not a
side effect.

**REQ-SEC-034** — The operator map shows full precision to the operator. This **MUST** be
disclosed in `HELP PRIVACY` and on the dashboard — the operator can see where people are.

**REQ-SEC-035** — Bulk position export **MUST** be operator-only and audit-logged.

---

## 9. Operational security and observability

**REQ-SEC-036** — Logs **MUST** be structured JSON via `structlog`, to journald and a rotating
file, with configurable level.

**REQ-SEC-037** — Logs **MUST NOT** contain: passwords, API tokens, session cookies,
federation shared secrets, channel PSKs (`channel_dir.psk_b64`), or full mail bodies. Mail is
logged by id and length only. Post bodies **MAY** be logged at debug level, and this **MUST**
be documented. A `detect-secrets`-style test **MUST** assert that a synthetic PSK and a
synthetic federation secret never appear in captured log output.

**REQ-SEC-038** — Every privileged action **MUST** write an `audit_log` row (doc 05 §3.4).
Audit rows are never auto-pruned.

**REQ-SEC-039** — `/metrics` **MUST** be available on the LAN interface, **MUST NOT** require
auth by default (it contains no secrets), and **MUST** be config-gated for operators who want
it restricted.

**REQ-SEC-040** — systemd hardening **MUST** be applied in the shipped unit:

```ini
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/outpost /var/log/outpost
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
MemoryDenyWriteExecute=no      # required by some ML runtimes
SystemCallFilter=@system-service
DeviceAllow=/dev/ttyUSB0 rw
DeviceAllow=/dev/hailo0 rw
DeviceAllow=/dev/h1x-0 rw
Restart=always
RestartSec=10
WatchdogSec=120
```

**REQ-SEC-041** — The service **MUST** run as a dedicated unprivileged user (`outpost`) in the
`dialout` group for serial access, never as root.

**REQ-SEC-042** — Backups (doc 05 §10) **MUST** be operator-encryptable with a supplied GPG
key or passphrase, since they contain all mail and positions.

**REQ-SEC-043** — The health endpoint **MUST NOT** leak internal detail to unauthenticated
callers: `{"status":"ok|degraded|down","version":"…"}` only. Full per-module detail requires
auth.

---

## 10. Incident response

**REQ-SEC-044** — The operator **MUST** have a single **panic control** — dashboard button and
`OP QUIET on` over the air — that immediately: stops all outbound traffic except `critical`
alerts, suspends federation, disables the AI, and raises a persistent banner. This is the
control an operator reaches for when something is badly wrong and they need the node to stop
talking *now*.

**REQ-SEC-045** — Recovery from `OP QUIET` **MUST** require the dashboard, not the radio, so a
spoofer cannot re-enable a node an operator silenced.

**REQ-SEC-046** — The install documentation **MUST** include: how to rotate the operator
password, how to rotate a channel PSK and redistribute it, how to revoke a federation peer,
how to restore from backup, and how to wipe member data on request.

**REQ-SEC-047** — A `tools/purge_member.py` utility **MUST** exist to remove a member's
personal data (positions, mail, handle, notes) while preserving board content integrity
(posts become `author_label` "removed" with a null `author_id`). Communities will be asked
for this and it should not require hand-written SQL.

---

## 11. Acceptance criteria

| # | Criterion |
|---|---|
| 1 | A `guest` can `REPORT` and `OK` but cannot post, mail, or use the AI |
| 2 | Trust promotion above `member` is impossible without an operator action; audit row exists |
| 3 | A handle bound to another node ID cannot be claimed over the air |
| 4 | A changed Meshtastic public key for a known node ID triggers demotion and a dashboard warning |
| 5 | Rate limits fire at the documented thresholds and persist across a restart |
| 6 | The global circuit breaker engages under a simulated flood and the node stops replying |
| 7 | A blocked member receives exactly one notification, then silence |
| 8 | `POS` respects `coarse`/`off` settings; a `guest` cannot locate anyone |
| 9 | The AI cannot retrieve mail or positions — verified by explicit negative tests |
| 10 | A prompt-injection payload posted to a board does not alter the AI's system behaviour or the `[AI]` marker |
| 11 | Over-air operator commands cannot change configuration or create boards |
| 12 | `OP QUIET on` silences everything but `critical`; recovery requires the dashboard |
| 13 | Logs contain no secrets (passwords, tokens, PSKs, federation secrets) and no mail bodies |
| 14 | `pip-audit` passes in CI |
| 15 | `purge_member.py` removes personal data while board threads remain coherent |
| 16 | The privacy statement in REQ-SEC-027 appears in the docs, the dashboard, and `HELP PRIVACY` |
