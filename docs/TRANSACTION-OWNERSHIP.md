# Transaction and delivery ownership

Original boundary inspection: `d9ff6e97e2ea8bec0011c21f1e85d9a3d328fd98` (2026-09-05).
Updated for the automatic incident worker, authenticated ingress coordinator and
service-owned HTTP route groups in
[#153](https://github.com/baal-bot/Outpost-Meshtastic/issues/153).
The software boundaries below are implemented; exact-commit verification and issue
completion are recorded on GitHub. This is not a blanket race-free claim or physical
G6 qualification for [#135](https://github.com/baal-bot/Outpost-Meshtastic/issues/135).
The earlier review fix deliberately tightened human version checks. The final
dispatcher/route extraction preserves its preceding API, schema and wire behavior.

## Shared transaction contract

[`Database.transaction()`](../src/outpost/store/database.py) owns the process's writer lock and
`BEGIN IMMEDIATE` / commit / rollback boundary. `Transaction.read/write` use that writer's
connection. `Database.read` uses a separate bounded reader pool and must not be used to read an
uncommitted mutation or make a dependent allocation inside the transaction.

`Database.write` commits one operation. Several awaited calls are not a compound transaction.
The return value is a row ID, **not an affected-row count**; conditional updates need an explicit
transactional result check. Cancellation rolls back writer work where possible, but cancellation
after a commit cannot prove that nothing was saved. Callers must not promise exactly-once effects
from timeout handling alone. Commit settings and process recovery tests do not establish
physical power-loss qualification (#44/#148). The software policy is the
[checked WAL/FULL contract](COMMIT-DURABILITY.md) (#137).

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
| [`IncidentChangeEvents`](../src/outpost/watch/change_events.py) / migration 178 | The revision-insert trigger captures incident/note metadata in the source transaction across direct SQL, import, merge and retention writers; supersession retains first-pending order. | Bounded read-only inspection reports head/lineage mismatches. The journal retains source intent; the separate handoff/sender/worker own peer progress and delivery. See [journal scope and limits](INCIDENT-CHANGE-EVENTS.md). |
| [`incident_reference`](../src/outpost/store/incident_refs.py) | Uses the caller's transaction for reference identity and retired-number ledger decisions. | Content retention must not turn retired references into new identities. Off-device restore lineage remains #146. |
| [`MailService.send`](../src/outpost/bbs/mail.py) | The placeholder INSERT, permanent UID and conversation context are committed together. Migration 173 recovers old committed placeholders. | Recipient lookup precedes the transaction; this fix is not a blanket proof of all identity/authorization races. Reading/delivery notification and any transport are separate operations. |
| [`BBSService.create_thread/reply`](../src/outpost/bbs/service.py) | Thread/post allocation, UID/sequence and aggregate metadata use owned transactions. | Subscriptions, external notifications and federation scheduling are separate. Scope/trust rules must survive extraction; an atomic write is not authorization evidence by itself. |
| [`OutboxStore.admit_many`](../src/outpost/store/outbox.py) | Queue bounds, dedupe, supersession and batch insertion are one transaction, or participate in an explicitly supplied domain transaction. Durable governor queue publication now follows commit through the same owner. | IDs are provisional inside a caller transaction. Rollback publishes nothing; publication failure blocks egress until quiesced recovery. `pending`/`held` is not transmitted or delivered. |
| [`AirtimeGovernor`](../src/outpost/transport/governor.py) with `OutboxStore` | A single egress lock serializes ticks. Reservation rechecks current owner and dispatch policy after awaited validation; outcomes/expiry/recovery own separate transactions. [Timing contract](DISPATCH-TIMING.md). | Radio I/O stays outside SQLite. Reservation is not the physical RF instant. Interrupted calls are conservatively charged and can remain uncertain; RF ACK, application receipt and human acknowledgement are different facts. Critical reserve is unchanged. |
| [`CheckinService.solicit`](../src/outpost/watch/checkin.py) | Solicitation recipients and held outbox work share one writer transaction, with queue publication after commit. | Release follows commit. Cancellation during commit can retain held work and recipient rows; rollback cleanup does not retract a committed durable batch. Check-in recovery and incident sender authorization remain distinct responsibilities. |
| [`FederationSyncService.quarantine`](../src/outpost/fed/sync.py) | Modern inbox content and producer-revision receipt are persisted together after validation. | A durable receipt permits reconciliation progress; it is not import approval, responder notification or community visibility. Legacy compatibility has different ordering limits. |
| [`FederationBundleService`](../src/outpost/fed/bundles.py) | Version-bound physical-file approval rechecks current operator/key/peer/scope and calls existing quarantine/import cores in one writer with item/bundle receipts and audit. Export signing binds exact revision payloads. | Preview is non-mutating. Files are signed, not encrypted; no new trust, worker or RF action. Whole-page rollback and replay receipt handle interruption, not lost history after old-backup restore. [Transfer contract](FEDERATION-BUNDLES.md). |
| [`IncidentEvents`](../src/outpost/fed/incident_events.py) / [`quarantine_transaction`](../src/outpost/fed/sync.py) | Event receive owns current-peer/lineage/scope checks, durable per-peer quota, exact quarantine content and revision receipt in one writer transaction. The shared quarantine core uses that writer. | Typed storage receipts are admitted only after commit; failed admission is recovered by a fresh-counter identical retry. The receiver performs no human action; automatic source sending belongs to the worker. See [receive contract](FEDERATION-INCIDENT-EVENTS.md). |
| [`IncidentHandoff`](../src/outpost/fed/incident_handoff.py) | Current peer/source/scope checks, writer-connected exports, coalesced exact-version/content sender intents and a per-peer scan checkpoint share a bounded transaction. | Source rows are retained; staging is not all-peer completion or radio admission. Worker-driven admission passes through the sender's current-policy transaction. See [handoff contract](INCIDENT-SENDER-HANDOFF.md). |
| [`IncidentSender`](../src/outpost/fed/incident_sender.py) | Explicit current-content admission, peer counter, complete frames and exact association share one writer; receipt storage and unsent cancellation likewise commit together. A durable owner guard revalidates at attempt reservation. | Automatic polling/retry policy belongs to the worker. Radio I/O follows reservation outside SQL; remote storage is not review or continued retention. See [sender contract](INCIDENT-SENDER-ADMISSION.md). |
| [`IncidentWorker`](../src/outpost/fed/incident_worker.py) / [`IncidentReceipts`](../src/outpost/fed/incident_receipts.py) | The existing supervised task schedules bounded fresh/backlog staging and guarded finite retries; receipt replies coalesce exact pending storage acknowledgements through the governed outbox. | Worker scheduling is not an additional writer, public-alert approval or physical G6 proof. Budgets, quiet hours and the critical reserve remain authoritative. See [automatic delivery](INCIDENT-AUTOMATIC-DELIVERY.md). |
| [`FederationDispatcher.receive`](../src/outpost/fed/dispatcher.py) | Owns the ingress sequence: decode/reassembly, sender/target and counter checks, current peer/domain dispatch, existing receipt mutations and redacted rejection outcomes. Borrowed domain services retain their existing transaction boundaries. | Stateless per-call adapter, not another database, reassembler, counter store or sender. Typed callbacks bind existing governed egress and service handlers; no application-object back-reference, new task or radio I/O inside a new SQL boundary. |
| [`FederationReviewService`](../src/outpost/fed/review.py) | Reads pending content/provenance and checks the expected review fingerprint, rechecks active peer trust, calls the transactional import core and saves human audit together. Rejection and its audit use the same version guard and writer transaction. | Dashboard full-content and handheld metadata-only previews happen earlier, outside the writer lock. A mismatch requires fresh review. The tag is not authorization or a restore-lineage guarantee. |
| [`SelfCheckService.record_observation`](../src/outpost/self_check.py) | Reads the current observation slot and checks its process/policy-bound review token; records the structured observation, authenticated audit and old-report invalidation in one writer transaction. Local cache invalidation follows commit before releasing the writer. | Read-only probes and the subsequent assessment run outside that transaction. A lost response can follow a committed observation; the dashboard requires review, not automatic resubmission. An operator statement is not measured qualification and cannot override a measured failure. See [readiness evidence](SAFETY-READINESS.md). |
| [`web.routes.federation_review`](../src/outpost/web/routes/federation_review.py) / [`web.routes.readiness`](../src/outpost/web/routes/readiness.py) | Translate six existing routes through their existing domain owners. Registration remains conditional under `create_web_app`; no extra transaction is introduced around a service-owned mutation. | Shared authentication, current-actor context, CSRF, module gates, transport middleware and response schemas remain in force. Only diagnostics retains its existing loopback exception; it cannot record an observation. |
| [`FederationSyncService.import_inbox/import_inbox_transaction`](../src/outpost/fed/sync.py) | The domain core reads pending content and current import scope, mutates the destination, records provenance/revision audit and marks imported using its caller's transaction. The automatic-policy wrapper owns its transaction. | Automatic board imports remain explicit policy, not human decisions. Human routes must go through the review service, never the unguarded policy wrapper/core. |
| [`Reconciliation`](../src/outpost/fed/reconciliation.py) | Durable per-peer checkpoint/receipt state controls advancing a producer snapshot. A per-peer lock prevents overlapping in-process cycle handling. | The lock is not a SQL transaction and checkpoint sends are separate. A source's `unavailable` response is not proof of replica withdrawal or permission to delete local content. |
| [`IncidentUpdates`](../src/outpost/fed/incident_updates.py) | Under the review caller's transaction, checks current note capability/scope and original-parent identity, imports/revises a neutral note and records provenance. Migration 177's local note triggers share the source mutation transaction. | No nested transaction, responder action, radio I/O or automatic import. Producer heads/receipts are not prompt delivery intents. See [note scope and limits](FEDERATION-INCIDENT-NOTES.md). |
| [`ItemFailures`](../src/outpost/fed/item_failures.py) | Owns the bounded metadata-only source journal transaction, checks the current producer head, and guards clearing against stale admissions. | Encoding, journal commit and governed radio admission are separate. Receiver failures belong to the reconciliation checkpoint, never a content receipt. See [oversized-item limits](FEDERATION-ITEM-FAILURES.md). |
| [`OutpostApp`](../src/outpost/app.py) / [`web.api`](../src/outpost/web/api.py) / [`MeshOperationsCenter`](../src/outpost/operations_center.py) | Construct services, translate authenticated commands/API intent, coordinate callbacks, and record additional response state. Human federation review now passes the expected version to its service-owned transaction/audit. | Other SQL and audit still cross these layers. Handheld preview and confirmation checks are additional guards, not a substitute for the transactional service check. |

`FederationAdoptionService` borrows the application's existing peer/database owner.
Preview/commit recheck current session/role/modules, retired predecessor/pin,
successor pairing/key and one-to-one namespace. Association and redacted audit
share one writer, with no upsert, nested writer, worker, private-data/key/counter
transfer or RF. Only legacy BBS uses these aliases; incidents and alerts do not.
See the [node-loss contract](NODE-LOSS-AND-MOBILITY.md).

This map identifies ownership, not proof that every method in a listed class is race-free.
Direct SQL writers, migrations, import paths, automatic board handling and maintenance must be
included whenever a domain invariant changes.

## Delivery vocabulary

Encrypted recovery uses the shared `store.recovery_snapshot` validator and SQLite
memory backup. `Database.recovery_snapshot` holds its serialized writer lock
through completion/cancellation. The separate operator CLI opens only a read-only
source connection and never migrates it; it is not another application writer.
Decryption/configuration/schema checks precede target creation. Only a fresh
destination image is modified, in memory, to discard cloned sessions, write the
durable identity fence and record the restore audit before publication. Startup
then owns only the local review heartbeat; no queue publication or external I/O
is authorized by restoring identity material. See [recovery](ENCRYPTED-RECOVERY.md).

`IncidentResponsibilityService` now owns a separate local coordination transaction for
current responder/web-session authorization, version comparison, pending/accepted owner,
next action, decision history and redacted audit. `TASK` and `web.routes.responsibility`
translate intent; only the normal reply path touches the governor. Target identities are
non-reusable; merge and member-removal paths preserve the responsibility boundary. See
[accepted handoffs](INCIDENT-RESPONSIBILITY.md). Assignment acceptance/completion is not
any delivery stage below, and federation never owns this local record.

The following stages need separate evidence and must not collapse into one “sent” flag:

1. Local authoritative record committed.
2. Durable per-peer change intent pending (bounded staging and automatic scheduling implemented; physical G6 remains held in #135).
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

These selected slices are implemented. Keep #153 open until the final extraction
passes its exact-commit evidence gates; do not close #135 on documentation alone.

1. **Federation review service (`fed/review.py`, implemented software slice).** Own preview identity/version, transactional approval/rejection,
   scope recheck and audit. Web routes translate request/409 responses, while dashboard and
   handheld/operations-center flows carry the reviewed version. Both approve and reject must
   refuse changed content; an item ID alone is insufficient. Include two-reviewer races,
   replacement during review, restart, revoked policy, rollback and no-auto-broadcast tests.
   Automatic board imports need a distinct policy entry point, not a token bypass on a human API.
2. **Incident change publication (`watch/change_events.py` and `fed/incident_worker.py`, implemented).**
   Migration 178 captures coalesced source intent in the producer revision transaction.
   Handoff, sender and worker own bounded peer staging/admission, supersession and finite retries.
   The declared load/priority/quiet-hour envelope is documented without borrowing the critical
   reserve or implying public-alert approval. Physical G6 remains separate and held.
3. **Federation receive/dispatch coordinator (`fed/dispatcher.py`, implemented).**
   Explicit typed domain dependencies replace the inbound handler's application-object coupling.
   A thin application adapter binds current owners/callbacks per invocation while the original
   shared reassembler and durable counters survive across fragments. Modern/legacy negotiation,
   replay handling, single-flight controls, local cycle/page ceilings and receipt ordering remain.
4. **Domain web route groups (`web/routes/`, implemented for review and readiness).**
   Request models and six handlers moved after their mutation contracts became service-owned.
   Middleware remains centralized; paths/methods, actor context, CSRF, module behavior and error
   schemas are unchanged. Other route groups stay in place: this is incremental extraction,
   not a requirement to move every endpoint or create a new web service.

## Final extraction equivalence checks

Baseline: `38b3580fd9825ddd7df2b26e5b73f6068eccf68d`. Parsed AST comparison found the
entire inbound method and rejection classifier identical after normalizing only the
method name; the six route handlers (including decorators) and both request-model
definitions also match exactly. This checks code preservation, not all possible runtime
behavior. Production-wired regression tests additionally exercise shared reassembly,
receipt admission versus RF, callback wiring, current module/role gates, request schemas,
missing optional services, rollback/review conflicts and actual browser flows.

Guardrails are the maintained incident/mail transaction tests, incident reference/location tests,
federation sync/revision/page-cost tests, emergency bursts, web authorization/browser regressions,
type/lint ratchets and packaged application checks. Add focused fault/concurrency tests for each
new boundary. A smaller `app.py` or `api.py` is not itself an acceptance criterion.
