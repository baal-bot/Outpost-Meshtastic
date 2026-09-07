# Transaction and delivery ownership

Original boundary inspection: `d9ff6e97e2ea8bec0011c21f1e85d9a3d328fd98` (2026-09-05).
Updated for the [version-bound review extraction](FEDERATION-REVIEW-SAFETY.md).
This is the boundary map and incremental extraction plan for
[#153](https://github.com/baal-bot/Outpost-Meshtastic/issues/153), not a claim that its refactoring
or prerequisite [event-driven incident delivery](https://github.com/baal-bot/Outpost-Meshtastic/issues/135)
is complete. The review slice adds an in-process domain service and deliberately tightens
human review API/command inputs; it changes no schema, radio protocol or live deployment.

## Shared transaction contract

[`Database.transaction()`](../src/outpost/store/database.py) owns the process's writer lock and
`BEGIN IMMEDIATE` / commit / rollback boundary. `Transaction.read/write` use that writer's
connection. `Database.read` uses a separate bounded reader pool and must not be used to read an
uncommitted mutation or make a dependent allocation inside the transaction.

`Database.write` commits one operation. Several awaited calls are not a compound transaction.
The return value is a row ID, **not an affected-row count**; conditional updates need an explicit
transactional result check. Cancellation rolls back writer work where possible, but cancellation
after a commit cannot prove that nothing was saved. Callers must not promise exactly-once effects
from timeout handling alone. WAL/NORMAL and process recovery tests are not physical power-loss
durability qualification (#137/#44).

A domain operation should either own its transaction or receive a `Transaction` from its caller.
Do not nest an owned transaction under the same non-reentrant writer lock. Network I/O, radio
sends, model requests and human review must remain outside that lock. Immutable event/outbox
intent can be inserted inside it when the domain operation requires durable post-commit work;
actual sending belongs to the governed worker after commit.

`Transaction.after_commit` now publishes trusted synchronous local queue changes only after
commit, before releasing the writer. The handle is bound to the active writer and owning task;
closed/foreign/cross-task use is refused. Commit/rollback settlement withstands repeated
cancellation. `PostCommitError` means storage committed but local publication failed, not rollback.
See the [commit-safe outbox contract](OUTBOX-COMMIT-PUBLICATION.md) for snapshot/identity semantics,
bounded non-I/O callbacks, cancellation ambiguity and quiesced recovery requirements.

## Current boundaries

| Owner / entry points | Atomic local work | Separate work or limitation |
| --- | --- | --- |
| [`IncidentService`](../src/outpost/watch/incidents.py): `create`, `react`, `operator_patch`, `operator_update`, location corrections, expiry | Creation binds the permanent reference, incident, origin and provenance together. Reactions allocate a sequence and update the count/provenance together. Corrections and expiry use their domain transactions; expiry rechecks each candidate after acquiring the writer. Producer revision writes also capture a coalesced source change intent. | Rendering, responder notifications and radio admission follow the mutation. A saved report does not establish a queued reply or prompt peer delivery. The source intent is not a per-peer delivery event (#135). |
| [`IncidentChangeEvents`](../src/outpost/watch/change_events.py) / migration 178 | The revision-insert trigger captures incident/note metadata in the source transaction across direct SQL, import, merge and retention writers; supersession retains first-pending order. | Bounded read-only inspection reports head/lineage mismatches. There is no consume/send/receipt API until version-guarded transactional peer handoff exists. See [journal scope and limits](INCIDENT-CHANGE-EVENTS.md). |
| [`incident_reference`](../src/outpost/store/incident_refs.py) | Uses the caller's transaction for reference identity and retired-number ledger decisions. | Content retention must not turn retired references into new identities. Off-device restore lineage remains #146. |
| [`MailService.send`](../src/outpost/bbs/mail.py) | The placeholder INSERT, permanent UID and conversation context are committed together. Migration 173 recovers old committed placeholders. | Recipient lookup precedes the transaction; this fix is not a blanket proof of all identity/authorization races. Reading/delivery notification and any transport are separate operations. |
| [`BBSService.create_thread/reply`](../src/outpost/bbs/service.py) | Thread/post allocation, UID/sequence and aggregate metadata use owned transactions. | Subscriptions, external notifications and federation scheduling are separate. Scope/trust rules must survive extraction; an atomic write is not authorization evidence by itself. |
| [`OutboxStore.admit_many`](../src/outpost/store/outbox.py) | Queue bounds, dedupe, supersession and batch insertion are one transaction, or participate in an explicitly supplied domain transaction. Durable governor queue publication now follows commit through the same owner. | IDs are provisional inside a caller transaction. Rollback publishes nothing; publication failure blocks egress until quiesced recovery. `pending`/`held` is not transmitted or delivered. |
| [`AirtimeGovernor`](../src/outpost/transport/governor.py) with `OutboxStore` | A single egress lock serializes ticks. Reservation rechecks current owner and dispatch policy after awaited validation; outcomes/expiry/recovery own separate transactions. [Timing contract](DISPATCH-TIMING.md). | Radio I/O stays outside SQLite. Reservation is not the physical RF instant. Interrupted calls are conservatively charged and can remain uncertain; RF ACK, application receipt and human acknowledgement are different facts. Critical reserve is unchanged. |
| [`CheckinService.solicit`](../src/outpost/watch/checkin.py) | Solicitation recipients and held outbox work share one writer transaction, with queue publication after commit. | Release follows commit. Cancellation during commit can retain held work and recipient rows; rollback cleanup does not retract a committed durable batch. Existing restart recovery remains distinct from future incident policy checks. |
| [`FederationSyncService.quarantine`](../src/outpost/fed/sync.py) | Modern inbox content and producer-revision receipt are persisted together after validation. | A durable receipt permits reconciliation progress; it is not import approval, responder notification or community visibility. Legacy compatibility has different ordering limits. |
| [`IncidentEvents`](../src/outpost/fed/incident_events.py) / [`quarantine_transaction`](../src/outpost/fed/sync.py) | Event receive owns current-peer/lineage/scope checks, durable per-peer quota, exact quarantine content and revision receipt in one writer transaction. The shared quarantine core uses that writer. | Typed storage receipts are admitted only after commit; failed admission is recovered by a fresh-counter identical retry. No automatic sender or human action. See [receive contract](FEDERATION-INCIDENT-EVENTS.md). |
| [`IncidentHandoff`](../src/outpost/fed/incident_handoff.py) | Current peer/source/scope checks, writer-connected exports, coalesced exact-version/content sender intents and a per-peer scan checkpoint share a bounded transaction. | Source rows are retained; staging is not all-peer completion or radio admission. The future worker must revalidate and hand to the governed outbox atomically. See [handoff contract](INCIDENT-SENDER-HANDOFF.md). |
| [`IncidentSender`](../src/outpost/fed/incident_sender.py) | Explicit current-content admission, peer counter, complete frames and exact association share one writer; receipt storage and unsent cancellation likewise commit together. A durable owner guard revalidates at attempt reservation. | No automatic timer/application retry policy. Radio I/O follows reservation outside SQL; remote storage is not review or continued retention. See [sender contract](INCIDENT-SENDER-ADMISSION.md). |
| [`FederationReviewService`](../src/outpost/fed/review.py) | Reads pending content/provenance and checks the expected review fingerprint, rechecks active peer trust, calls the transactional import core and saves human audit together. Rejection and its audit use the same version guard and writer transaction. | Dashboard full-content and handheld metadata-only previews happen earlier, outside the writer lock. A mismatch requires fresh review. The tag is not authorization or a restore-lineage guarantee. |
| [`FederationSyncService.import_inbox/import_inbox_transaction`](../src/outpost/fed/sync.py) | The domain core reads pending content and current import scope, mutates the destination, records provenance/revision audit and marks imported using its caller's transaction. The automatic-policy wrapper owns its transaction. | Automatic board imports remain explicit policy, not human decisions. Human routes must go through the review service, never the unguarded policy wrapper/core. |
| [`Reconciliation`](../src/outpost/fed/reconciliation.py) | Durable per-peer checkpoint/receipt state controls advancing a producer snapshot. A per-peer lock prevents overlapping in-process cycle handling. | The lock is not a SQL transaction and checkpoint sends are separate. A source's `unavailable` response is not proof of replica withdrawal or permission to delete local content. |
| [`IncidentUpdates`](../src/outpost/fed/incident_updates.py) | Under the review caller's transaction, checks current note capability/scope and original-parent identity, imports/revises a neutral note and records provenance. Migration 177's local note triggers share the source mutation transaction. | No nested transaction, responder action, radio I/O or automatic import. Producer heads/receipts are not prompt delivery intents. See [note scope and limits](FEDERATION-INCIDENT-NOTES.md). |
| [`ItemFailures`](../src/outpost/fed/item_failures.py) | Owns the bounded metadata-only source journal transaction, checks the current producer head, and guards clearing against stale admissions. | Encoding, journal commit and governed radio admission are separate. Receiver failures belong to the reconciliation checkpoint, never a content receipt. See [oversized-item limits](FEDERATION-ITEM-FAILURES.md). |
| [`OutpostApp`](../src/outpost/app.py) / [`web.api`](../src/outpost/web/api.py) / [`MeshOperationsCenter`](../src/outpost/operations_center.py) | Construct services, translate authenticated commands/API intent, coordinate callbacks, and record additional response state. Human federation review now passes the expected version to its service-owned transaction/audit. | Other SQL and audit still cross these layers. Handheld preview and confirmation checks are additional guards, not a substitute for the transactional service check. |

This map identifies ownership, not proof that every method in a listed class is race-free.
Direct SQL writers, migrations, import paths, automatic board handling and maintenance must be
included whenever a domain invariant changes.

## Delivery vocabulary

The following stages need separate evidence and must not collapse into one “sent” flag:

1. Local authoritative record committed.
2. Durable per-peer change intent pending (bounded staging implemented in #167; automatic scheduling remains #135).
3. Governed RF work admitted, delayed, superseded, expired or rejected.
4. Radio attempt completed, failed or uncertain; RF acknowledgement where applicable.
5. Remote authenticated content stored in quarantine / application receipt returned.
6. Content imported by a named reviewer or an explicit automatic-import policy.
7. Responder notification admitted/delivered, then separately acknowledged by a responder.

Local monitoring and explicit public-alert approval remain authoritative. Catch-up watermarks
cannot stand in for these stages; a queued notification does not prove that anyone read it.
The [burst envelope](EMERGENCY-BURST-QUALIFICATION.md) and
[paging measurements](FEDERATION-PAGING-QUALIFICATION.md) qualify specific software work only.

## Concrete extraction slices

The review boundary and source-journal capture are implemented; dispatch and other extractions remain planned. Keep #153 open until the slices
actually selected for it meet their evidence gates; do not close #135 on documentation alone.

1. **Federation review service (`fed/review.py`, implemented software slice).** Own preview identity/version, transactional approval/rejection,
   scope recheck and audit. Web routes translate request/409 responses, while dashboard and
   handheld/operations-center flows carry the reviewed version. Both approve and reject must
   refuse changed content; an item ID alone is insufficient. Include two-reviewer races,
   replacement during review, restart, revoked policy, rollback and no-auto-broadcast tests.
   Automatic board imports need a distinct policy entry point, not a token bypass on a human API.
2. **Incident change publication (`watch/change_events.py`, capture implemented; worker remains #135).** Migration 178 owns a durable, coalescible source change intent
   in the same transaction as each producer revision. A future worker must own transactional per-peer handoff, scope, supersession,
   retry/expiry and bounded scheduling. Decide and document quiet-hour/priority behavior without
   borrowing critical-alert reserve or implying public-alert approval. Cover local, imported,
   merge/unmerge, expiry and restored-state writers; avoid an in-memory callback as the sole
   source of work. Physical G6 acceptance remains separate and held.
3. **Federation receive/dispatch coordinator (`fed/dispatcher.py`, proposed).** Extract framing validation, peer policy and
   typed protocol dispatch from `OutpostApp`, keeping one owner for each counter, receipt and
   transaction. Services receive explicit dependencies instead of reaching back through a large
   application object. Preserve modern/legacy negotiation, single-flight controls and local
   cycle/page ceilings with existing production-wiring tests.
4. **Domain web route groups (`web/routes/`, proposed).** Extract routes only after their mutation contract is owned by
   a service. Preserve authentication/context propagation, response/error schemas, disabled
   module behavior, callback signatures and frontend compatibility. A route move should not
   introduce migrations or silently change trust policy.

Guardrails are the maintained incident/mail transaction tests, incident reference/location tests,
federation sync/revision/page-cost tests, emergency bursts, web authorization/browser regressions,
type/lint ratchets and packaged application checks. Add focused fault/concurrency tests for each
new boundary. A smaller `app.py` or `api.py` is not itself an acceptance criterion.
