# 10 — Node Federation

**Status:** Baseline · **Phase:** 5 · **Prerequisite:** [03-MESH-TRANSPORT.md](03-MESH-TRANSPORT.md)
**Implements:** `src/outpost/fed/`

---

## 1. Premise and constraints

Two Outpost nodes 6 km apart, each serving its own cluster of residents, both reachable
across the same mesh. Federation lets them share selected boards, relay mail between their
communities, and propagate incidents — without either operator administering the other, and
without a server anywhere.

This is the Hotline/Carracho lineage made literal: independent servers, peered by consent,
no directory authority.

**The constraint that shapes everything here:** replication over a 1 kbps shared channel is
absurdly expensive. A single 200-byte post costs ~2 s of channel time to move one hop, and
the mesh floods, so it costs that from *everyone's* perspective.

**REQ-FED-001** — Federation **MUST** default to **disabled** and **MUST** require explicit
per-peer, per-board opt-in by both operators.

**REQ-FED-002** — Federation traffic **MUST** use the lowest airtime class, **MUST** be
suspended entirely whenever channel utilisation exceeds `airtime.utilisation_ceiling`, and
**MUST** never preempt human traffic.

**REQ-FED-003** — Federation **MUST** be safe to disable unilaterally at any moment.
Previously-synced content remains, marked with its origin.

---

## 2. Transport

**REQ-FED-004** — Federation **MUST** use the private portnum defined in doc 03 §6 (default
**260**), with the magic-byte framing specified there. It **MUST NOT** use
`TEXT_MESSAGE_APP` and **MUST NOT** appear on any human-visible channel.

**REQ-FED-005** — Bodies **MUST** be CBOR-encoded, and **MUST** be deflate-compressed when
compression reduces size (signalled in the message type's high bit).

**REQ-FED-006** — Payload budget is **215 bytes per fragment** (233 − 18 bytes of header,
counter, and HMAC — see doc 03 §6 for the exact byte map). Messages larger than that **MUST**
fragment, with at most `fed.max_fragments` (default 8) fragments per message — a **1720-byte**
ceiling. Anything larger is redesigned, not fragmented further.

**REQ-FED-007** — Fragments **MUST** be reassembled with a timeout (`fed.reassembly_timeout_s`,
default 300). Incomplete sets are discarded and counted; the sender is not notified (a NAK
would cost as much as a retry).

**REQ-FED-008** — All federation messages except the discovery beacon **MUST** be sent as
direct messages to a specific peer with `want_ack=True`.

---

## 3. Message types

| Value | Name | Direction | Purpose |
|---|---|---|---|
| `0x01` | `HELLO` | broadcast | Peer discovery beacon |
| `0x02` | `PAIR_REQ` | A→B | Request pairing |
| `0x03` | `PAIR_ACK` | B→A | Accept pairing; exchange capabilities |
| `0x04` | `PAIR_NAK` | B→A | Decline |
| `0x10` | `SYNC_REQ` | A→B | "What do you have on stream S after cursor C?" |
| `0x11` | `SYNC_MANIFEST` | B→A | Compact list of item ids + digests available |
| `0x12` | `ITEM_REQ` | A→B | Request specific items by id |
| `0x13` | `ITEM` | B→A | One item payload |
| `0x14` | `SYNC_DONE` | either | Stream complete up to cursor C |
| `0x20` | `MAIL_RELAY` | A→B | A mail message destined for a member on B |
| `0x21` | `MAIL_RECEIPT` | B→A | Delivered / undeliverable |
| `0x30` | `INCIDENT` | A→B | Incident create or update |
| `0x31` | `ALERT_RELAY` | A→B | High-severity alert propagation |
| `0x40` | `PING` | either | Liveness |
| `0x41` | `PONG` | either | Liveness reply |

**REQ-FED-009** — Unknown message types **MUST** be ignored and counted, never error-replied.
Forward compatibility matters: two nodes will run different versions.

---

## 4. Discovery and pairing

**REQ-FED-010** — A node **MAY** broadcast `HELLO` on its Outpost channel at most once per
`fed.hello_interval_hours` (default 12), containing: node mesh id, node name, protocol
version, and a list of board slugs offered for federation. Total ≤120 bytes.

**REQ-FED-011** — `HELLO` **MUST** be suppressed when no federation is configured, when
utilisation is elevated, and on the primary public channel.

**REQ-FED-012** — Receiving a `HELLO` from an unknown node **MUST NOT** auto-pair. It
**MUST** create a `fed_peer` row in state `pending` and raise a dashboard notification for
the operator to approve or reject.

**REQ-FED-013** — Pairing **MUST** require both operators to act. The initiating operator
sends `PAIR_REQ`; the receiving operator approves it on their dashboard, which sends
`PAIR_ACK`. There **MUST** be no way to pair without a human on both ends.

**REQ-FED-014** — Pairing establishes a **32-byte shared secret generated from a CSPRNG**,
exchanged inside `PAIR_REQ`/`PAIR_ACK` (which travel encrypted on the channel or as PKI
DMs). The 6-digit code displayed on both dashboards is a **confirmation code** — a
truncated hash of the exchanged secret plus both node IDs — that the two operators compare
out of band (phone, radio, in person) to detect a machine-in-the-middle.

> The confirmation code **MUST NOT** be used as key material. Deriving a key from six
> decimal digits would give roughly 20 bits of entropy, brute-forceable offline from a single
> captured frame. The code's job is to let two humans confirm they paired with each other;
> the secret's job is to authenticate frames. Conflating them is a classic and serious error.

**REQ-FED-015** — Every subsequent federation frame **MUST** carry the first 8 bytes of
`HMAC-SHA256(shared_secret, header ‖ counter ‖ body)` in the frame's `hmac` field
(doc 03 §6). Frames failing the HMAC, or carrying a replayed counter, **MUST** be discarded
and counted.

**REQ-FED-015a** — Shared secrets **MUST** be stored as secrets: excluded from logs
(REQ-SEC-037), never returned by any API, and included in the encrypted-backup scope
(REQ-SEC-042). Re-pairing **MUST** generate a fresh secret and reset both counters.

> This is authentication, not confidentiality. The channel PSK already provides
> confidentiality against outsiders; the HMAC protects against a node on the *same channel*
> impersonating a peer, and the counter protects against replay of a captured frame. Do not
> overstate it (doc 12 §7).

**REQ-FED-015a** — After pairing, one operator flow **MUST** configure boards, incident radius,
alerts, mail, service permissions, and quotas. It **MUST** present a before/after policy diff and
require explicit confirmation before making a local-only board federation-eligible. The applied
operator, timestamp, and optional review date are retained with the peer policy.

---

## 5. Board replication

**REQ-FED-016** — Only boards explicitly listed in `fed_peer.boards` **MUST** be replicated,
in the direction(s) configured. One-way replication (publish-only or subscribe-only)
**MUST** be supported — a small node may want to receive the regional roads board without
publishing its own chatter.

**REQ-FED-017** — Replication **MUST** be delta-based using a per-stream cursor
(`fed_cursor`, doc 05 §9), not full-state comparison.

**REQ-FED-018** — Sync cycle:

```
A → B  SYNC_REQ   {stream: "board:roads", after: cursor, max: 20}
B → A  SYNC_MANIFEST {items: [{uid, ts, len, digest8}, ...], next_cursor}
       A diffs the manifest against local state
A → B  ITEM_REQ   {uids: [...]}          # only what A lacks
B → A  ITEM × n                          # one item per message, fragmented if needed
A → B  SYNC_DONE  {stream, cursor}
```

**REQ-FED-019** — The manifest **MUST** be compact: 8-byte truncated digest, 4-byte timestamp
delta, short uid. A 20-item manifest **MUST** fit within `fed.max_fragments` at the 215-byte
per-fragment budget; if the encoded manifest would exceed it, `max` **MUST** be reduced and
the cycle continued from the cursor rather than the manifest truncated silently.

**REQ-FED-020** — Sync **MUST** be scheduled, not continuous. Default `fed.sync_interval_minutes`
= 60, jittered, and skipped entirely when utilisation is elevated or the airtime budget is
spent. Reconciliation control frames **MUST** be single-flight per peer. An unanswered request
MUST remain the only queued request and may be retried no sooner than
`fed.sync_retry_minutes` (default 10).

**REQ-FED-021** — A per-cycle item cap (`fed.max_items_per_cycle`, default 20) **MUST** be
enforced, and any truncation **MUST** be logged and surfaced on the dashboard. Silent
truncation would let an operator believe a board is fully mirrored when it is not.

### 5.1 Identity and conflict

**REQ-FED-022** — Every federated item carries a globally unique `uid` of the form
`<origin_node_mesh_id>:<local_id>` (doc 05 §1). Uids are immutable and never reassigned.

**REQ-FED-023** — Federation is **append-only** for posts. There is no distributed edit or
delete. A moderation action on node A hides the item on A only; it **MUST NOT** propagate.

> Rationale: propagating deletions across an unauthenticated mesh creates a trivial censorship
> vector and an unbounded tombstone-replication problem. Each operator moderates their own
> node. This is the Hotline model and it is the right one here.

**REQ-FED-024** — Conflicts cannot occur for posts (append-only, unique uids). For incidents,
last-writer-wins by `updated_at` with the origin node as tiebreak, and every update is
retained in `incident_update` so nothing is lost.

**REQ-FED-025** — Replicated content **MUST** be visually and structurally attributed to its
origin node in every rendering:

```
B roads
> roads · 12 threads (3 from RVO)
1 Mill Rd bridge out     4rep 2h  @dana
2 Hollow Rd washout ›RVO 1rep 4h  @kit
```

**REQ-FED-026** — Whether federated content appears inline in a board or in a segregated view
**MUST** be an operator setting, defaulting to inline-with-attribution.

**REQ-FED-027** — Loop prevention: a node **MUST NOT** relay an item whose `origin_node` is
the receiving peer, and **MUST** maintain a seen-uid set per peer. Three-node topologies
(A–B–C) **MUST** converge without infinite relay, verified by simulator test.

---

## 6. Mail relay

**REQ-FED-028** — Mail addressed to `handle@nodename` **MUST** be routed to that peer via
`MAIL_RELAY` when `fed_peer.relay_mail` is enabled.

**REQ-FED-029** — Mail relay **MUST** be single-hop only in Phase 5. A node relays to a
directly-paired peer, not through intermediaries. Multi-hop mail routing is deferred.

**REQ-FED-030** — `MAIL_RECEIPT` **MUST** be returned as `delivered` (stored on the peer) or
`undeliverable` (unknown handle, peer refused). The sender is notified once, piggy-backed.

**REQ-FED-031** — Relayed mail **MUST** be counted against the sender's mail rate limit on the
originating node, and against `fed_peer` quotas on the receiving node.

**REQ-FED-032** — Mail relay **MUST** be disableable per peer, and a peer **MUST** be able to
refuse relayed mail entirely without breaking board federation.

**REQ-FED-032a** — A `MAIL_RELAY` conversation **MUST** carry a bounded opaque conversation ID,
the member/system distinction, the named participant, and an explicit reply handle inside the
authenticated encrypted payload. The receiver **MUST NOT** infer routing from display labels.
Messages to the `operator` catch-all are web-operator-only. A named member's contextual reply
**MUST** retain that member's handle and return to the initiating operator conversation.

**REQ-FED-032b** — The operator inbox **MUST** preview the stored peer, recipient, and available
transport before a reply. It **MUST** expose delivery/receipt state, unread/read, archive, search,
member/system filters, and audit conversation views, workflow changes, and replies.

---

## 7. Incident and alert propagation

**REQ-FED-033** — Incidents **MUST** propagate when `fed_peer.sync_incidents` is enabled and
the incident is within `fed.incident_radius_km` (default 25) of the peer node's location, or
has no location.

**REQ-FED-034** — Incident propagation is **event-driven**, not batch: an incident create or
status change sends an `INCIDENT` message promptly, subject to the airtime budget. Incidents
are the one federation stream where latency matters.

**REQ-FED-035** — Alerts of severity `urgent` or `critical` **MAY** propagate via
`ALERT_RELAY` when `fed_peer.relay_alerts` is enabled. This **MUST** default to **off**.

**REQ-FED-036** — A relayed alert **MUST NOT** auto-broadcast on the receiving node. It
**MUST** create a pending alert requiring the receiving operator's or a responder's approval,
**except** where the receiving operator has explicitly configured `auto_accept_alerts` for
that peer and that severity.

> Two nodes auto-relaying each other's alerts with escalation enabled is a mesh-saturating
> feedback loop. A human gates the propagation.

**REQ-FED-037** — Relayed alerts **MUST** be attributed to the origin node in their text and
**MUST NOT** be presented as locally-verified.

---

## 8. Airtime accounting

**REQ-FED-038** — Federation **MUST** operate strictly within `airtime.class_shares.federation`
(default 0.10 of an 8% budget ≈ 0.8% of channel time).

**REQ-FED-039** — The dashboard **MUST** show federation's airtime consumption and a
projection: "at current rates, syncing board `roads` with RVO will take 40 min and 0.6% of
channel time." Operators need to see the cost before enabling a board.

**REQ-FED-040** — A federation cycle that cannot complete within its budget **MUST** checkpoint
its cursor and resume next cycle, never restart from the beginning.

**REQ-FED-041** — The operator **MUST** be able to enable a **"sneakernet" mode**: export a
signed sync bundle to a file, carry it on a USB stick or over a LAN/Wi-Fi link when the two
nodes are co-located or bridged, and import it. Federation over LoRa is expensive; sometimes
the right answer is not to use LoRa at all.

---

## 9. Failure behaviour

| Failure | Behaviour |
|---|---|
| Peer unreachable | Backoff; mark `last_seen_at`; after `fed.peer_stale_hours` (default 72) mark stale and stop attempting until a `HELLO` or inbound message |
| HMAC failure | Discard, count, log with the claimed sender; after 10 in an hour, raise a dashboard security warning |
| Fragment loss | Reassembly timeout; item re-requested next cycle |
| Protocol version mismatch | Negotiate down to the lower version's message set; if incompatible, mark peer `paused` and warn both dashboards |
| Peer floods items | Per-peer inbound rate limit; excess discarded and counted; auto-pause at `fed.peer_flood_threshold` |
| Clock skew between nodes | Cursors are opaque and monotonic per stream, **not** wall-clock timestamps, precisely so skew cannot break sync |

**REQ-FED-042** — Sync cursors **MUST** be opaque monotonic sequence values, never timestamps.
Neither node may assume that the other has a reliable clock or an RTC.

The revision-based implementation advertises `capabilities.reconciliation: 2` within the
existing authenticated version-1 framing. A cursor consists of a persistent producer lineage
and producer-owned SQLite revision, shared across the selected streams. The producer chooses
the discovery high-water mark; the requester never supplies its wall clock as ordering authority.
This is a change-discovery watermark, not a historical copy of every payload. An edit committed
after the watermark may be fetched early at its newer revision or discovered next cycle.

Pages advance only when every advertised item has a durable quarantine receipt, or the producer
explicitly reports that it is no longer exportable. Quarantine is not operator approval, and
unavailability is not proof that an older replica was withdrawn. Item and round limits remain
locally owned; a budget-limited cycle resumes its existing watermark and position. Duplicate or
late pages cannot advance another cycle. Source revisions, not display timestamps, order
quarantine and authoritative-origin incident updates. Human merge and local-monitoring decisions
remain authoritative.

Mixed-version links retain the legacy timestamp protocol and **do not satisfy this clock-skew
requirement** until both endpoints are upgraded. A successfully negotiated revision link is pinned
against silent downgrade. A changed producer lineage blocks automatic import pending operator
identity/recovery review; restoring an older backup must not pretend to be a fresh continuation.
Quiet hours, expiry, peer liveness, and signed multi-hop envelope lifetimes still have separate
time-confidence requirements. Tests isolate quiet scheduling when testing cursor clock skew.

Evidence: `tests/integration/test_federation_revisions.py` covers six-hour offsets and in-cycle
clock steps, durable radio framing in both directions, restart, edits during pagination, lost
fetches, duplicate/delayed pages, local limits, receipt rollback/cancellation, and schema upgrade.

Producer page discovery uses migration 176's covering stream/revision index. Scope and revision
ranges are selected before a bounded merge: at most 101 heads per selected stream (20 boards
plus incidents and alerts), 101 merged heads returned, 100 export-policy evaluations and eight
manifest items. Geographic filtering can produce an empty advancing page. The complete query
plans, 120,000-head synthetic measurement and legacy-adapter limitations are recorded in
[indexed paging qualification](../../FEDERATION-PAGING-QUALIFICATION.md).

**REQ-FED-043** — A peer public-alert service request **MUST** be evaluated for the request's
normalized latitude/longitude, never against the serving Outpost's local alert inbox. The
serving node **MUST** validate that point against the provider's supported service area and
return the query point, service area, provider timestamp, fetch time, cache age, and serving
Outpost identity. `empty`, `stale`, `unsupported_region`, and `provider_failure` are distinct
results. Exact-point caches **MUST NOT** cross location keys or serve an expired alert.

**REQ-FED-044** — Peer use of this Outpost's weather, public-alert, or knowledge providers
**MUST** be authorized independently for each paired peer and service; pairing alone grants
none. The serving Outpost **MUST** enforce an hourly request count, concurrent-request limit,
maximum encoded response size, and hourly admitted-airtime budget before provider work or
egress. Duplicate request IDs replay the stored result at most three times without repeating
provider work; new IDs still consume the peer's request budget. Three consecutive provider
failures open a five-minute circuit. Current usage, denials, and open circuits **MUST** be
visible to the operator, and service history/provider caches **MUST** have bounded retention
and cardinality.

---

## 10. Metrics

```
outpost_fed_peers                    gauge   {state}
outpost_fed_sync_cycles_total        counter {peer,stream,outcome}
outpost_fed_items_sent_total         counter {peer,stream}
outpost_fed_items_received_total     counter {peer,stream}
outpost_fed_bytes_total              counter {peer,direction}
outpost_fed_hmac_failures_total      counter {claimed_peer}
outpost_fed_fragments_lost_total     counter {peer}
outpost_fed_mail_relayed_total       counter {peer,outcome}
outpost_fed_cycle_truncated_total    counter {peer,stream}
outpost_fed_airtime_seconds_total    counter {peer}
```

---

## 11. Acceptance criteria (Phase 5 exit)

| # | Criterion |
|---|---|
| 1 | Two simulated nodes discover each other via `HELLO` without auto-pairing |
| 2 | Pairing requires operator approval on both dashboards and a matching verification code |
| 3 | An unpaired node's federation messages are rejected on HMAC and counted |
| 3b | A captured frame replayed verbatim is rejected on the counter and counted (REQ-TRANSPORT-036a) |
| 3c | The 6-digit confirmation code is never used as key material — verified by inspecting the derivation (REQ-FED-014) |
| 4 | A board syncs bidirectionally; item uids are stable; no duplicates on either side |
| 5 | One-way (subscribe-only) federation works |
| 6 | A three-node A–B–C topology converges with no infinite relay |
| 7 | Federated content is attributed to its origin in every rendering |
| 8 | Moderation on A does **not** propagate to B (REQ-FED-023 verified as a negative test) |
| 9 | Mail to `handle@peer` relays and returns a receipt; unknown handle returns `undeliverable` |
| 10 | An incident propagates within 60 s subject to airtime |
| 11 | A relayed alert does **not** auto-broadcast without operator approval |
| 12 | Federation is fully suspended above the utilisation ceiling and resumes cleanly |
| 13 | A cycle interrupted by budget exhaustion resumes from its checkpoint, not from the start |
| 14 | Disabling federation leaves the node fully functional with prior content intact |
| 15 | Sneakernet export/import round-trips a board correctly |
| 16 | Clock skew of ±6 hours between nodes does not affect sync correctness |
