# Atomic incident sender handoff

Sender-side staging slice [#167](https://github.com/baal-bot/Outpost-Meshtastic/issues/167),
following the [source journal](INCIDENT-CHANGE-EVENTS.md) and [event receiver](FEDERATION-INCIDENT-EVENTS.md).
**There is still no automatic sender loop or radio admission.** This service establishes the
local commit boundary needed by [#135](https://github.com/baal-bot/Outpost-Meshtastic/issues/135).

## One peer, one bounded transaction

`FederationSyncService.incident_handoff.stage(peer_id, limit=100)` observes at most 100 source
heads plus one lookahead through a producer-revision index. It reloads the peer, requires active
trust, integer `incident_events:1` and `reconciliation:2`, incident permission, enabled local
Federation/Watch, and a known producer radio identity. It checks the retained producer epoch,
sequence and per-peer checkpoint before processing any work.

Every payload, note and incident-origin lookup uses the caller's writer connection through the
optional `export_items(..., transaction=tx)` adapter. This observes source heads and exported
content consistently, without nested transactions or separate-reader visibility gaps. Existing
bulk callers retain their behavior. Geographic/original-producer and plain-note capability
checks use existing export policy. A foreign or self-prefixed retained head cannot accidentally
resolve to a different local record after prefix stripping.

Exportable changes upsert `fed_incident_intent`, keyed by peer/stream/local UID. The row contains
producer epoch/revision, full payload SHA-256 using the receiver's encoding contract, scope
fingerprint, first-staged revision and, for notes, the original parent wire UID. It contains no
payload, coordinates, author, timestamp, radio counter or receipt. Supersession replaces the
current revision/digest while retaining its first-staged position. It does not preserve an edit
history or transmit an intermediate version that was superseded before observation.

The per-peer `fed_incident_handoff` checkpoint advances in the **same transaction** as all staging
decisions for that page. It records the scan revision, observed producer high-water mark, epoch,
peer/producer identities and scope fingerprint. Fault/cancellation before commit rolls back the
entire page; a committed page survives restart. Concurrent later source edits get a new revision
above the cursor and remain discoverable. A cursor is staging evidence, never a remote receipt.
The returned `staged` count includes blocked-intent updates, not just pending payloads; inspect
each row's state. A missing checkpoint alongside retained intents requires review, not a reset.

## Filtering, scope and lineage

Source journal rows are deliberately **retained**, not deleted by a single peer's scan. A peer's
cursor is not all-peer completion. Unexportable heads without a prior intent are skipped without
creating per-peer payload/work; the checkpoint records progress under that scope. If an existing
intent becomes unexportable, its current state becomes `not_exportable`. Content exceeding the
receiver's JSON bound or failing JSON encoding is `invalid_payload`, without dropping later
records from the page. Deletion is not a remote withdrawal instruction.

Changing geographic policy or negotiated note support changes the scope fingerprint and restarts
that peer's scan at zero. Previously staged rows with another fingerprint are immediately reported
as `policy_changed` during inspection, even before the rescan reaches them. Disabled/revoked
peers cannot advance the cursor. Newly enabled note support can discover notes below a previous
scan watermark; parent-location revision triggers cover later geographic changes.

A changed epoch, peer/producer identity, observed sequence rollback, inconsistent source head,
newer/different-lineage existing intent, or conflicting full content at the same staged revision
fails closed without advancing the page. There is no automatic reset/recovery API. A coherent
older full backup can still carry an older consistent checkpoint and sequence; these checks do
not solve restore forks or establish authority to rejoin (#146).

## Inspection is not permission to send

`pending(peer_id, after=0, limit=100)` returns a bounded indexed metadata page, including blocked
entries. It reports `lineage_blocked`, `policy_changed` or `source_changed` when the corresponding
current metadata no longer matches; missing/invalid global lineage produces an explicit error.
The underlying staging states are `pending`, `not_exportable` and `invalid_payload`.

Its `first_revision` cursor is only for paging within an inspection pass. Repeat passes from zero
to observe supersession; **do not use it as the durable producer-revision scan watermark**.
Inspection does not re-export/hash every payload and is never authorization to transmit. The
future sender must recheck current trust/scope, producer revision, exact payload/digest and
parent dependencies inside its governed admission transaction. `pending` does not assert that
the payload fits the radio envelope or that the remote parent has been stored.

## Capacity and next integration

Migration 179 creates the revision-seek index, one checkpoint per peer, and at most one intent
per peer/incident-or-note identity. It does not prepopulate peer progress or alter source heads,
lineage, sequence, source journal entries or remote receipts. Both new tables follow the peer's
explicit deletion lifecycle and are otherwise preserved by maintenance and full backups.
Digests and UIDs remain linkable metadata, not public/anonymized exports.

Bounds apply to source heads and returned intent rows per call, not lifetime storage or the
retained content/origin-list size of an individual source record. There is no per-peer history
row per edit, but retained identities times peers can still grow (#148). Each new peer or scope
rescan starts at zero. Large initial backlogs require repeated bounded calls and can delay newer
heads; separating catch-up from fresh/urgent scheduling and qualifying that latency remain #135.
This slice makes no 60-second or all-peer fairness claim and installs no timer.

An isolated aarch64/Python 3.13.5 probe on the development Pi used 120,000 synthetic incident
heads and 30 successive 100-head staging pages while local regressions were also running.
Staging measured median 69.141 ms / maximum 93.339 ms; 100-intent inspection measured median
2.654 ms / maximum 5.040 ms. It staged 3,000 intents, retained all 120,000 source rows and created
zero radio work. These are observed temporary-store timings with small synthetic payloads,
not latency bounds, cold-start qualification, arbitrary-origin-list cost or G6 evidence.

The [#168 shared outbox boundary](OUTBOX-COMMIT-PUBLICATION.md) now publishes in-memory admission
and supersession only after commit, preserving old work on rollback. It does not activate this
incident service or make existing held-work recovery a peer/source authorization check.
The next integration must atomically connect version-checked intents to held governed outbox
work, reconcile post-commit release/recovery, recheck source/policy before attempts,
match exact #166 storage receipts, coalesce duplicate receipt replies, and implement parent-first
notes, retries, supersession and expiry. Radio admission, attempts, remote storage, review, public
alert approval, responder notification and responder ACK remain distinct facts. Existing frame
limits, quiet hours, critical reserve and human-review requirements are unchanged.

## Verification and deployment

`tests/integration/test_incident_sender_handoff.py` exercises fresh/upgrade behavior, independent
peer progress, bounded query plans, source/peer races, rollback/cancellation, test-owned SIGKILL,
restart and backup recovery, scope expansion, foreign identity filtering and note dependencies.
The existing journal, federation/receiver/review, maintenance, backup and boot tests remain gates.
These temporary-store/simulated-process checks do not establish physical power-loss durability,
RF delivery or G6. The inactive-node hardware hold remains in place.

Back up before an approved deployment. Opening a database with this source applies migration
179; an older binary correctly refuses the newer schema. This task does not open/migrate the
live store, restart services/radios, deploy the release, or change browser assets/dependencies.
