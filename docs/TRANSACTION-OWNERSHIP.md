# Transaction and delivery ownership

Inspected source: `d9ff6e97e2ea8bec0011c21f1e85d9a3d328fd98` (2026-09-05).
This is the boundary map and incremental extraction plan for
[#153](https://github.com/baal-bot/Outpost-Meshtastic/issues/153), not a claim that its refactoring
or prerequisite [event-driven incident delivery](https://github.com/baal-bot/Outpost-Meshtastic/issues/135)
is complete. It introduces no service, schema, radio behavior or deployment change.

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

## Current boundaries

| Owner / entry points | Atomic local work | Separate work or limitation |
| --- | --- | --- |
| [`IncidentService`](../src/outpost/watch/incidents.py): `create`, `react`, `operator_patch`, `operator_update`, location corrections, expiry | Creation binds the permanent reference, incident, origin and provenance together. Reactions allocate a sequence and update the count/provenance together. Corrections and expiry use their domain transactions; expiry rechecks each candidate after acquiring the writer. | Rendering, responder notifications and radio admission follow the mutation. A saved report does not establish a queued reply or prompt peer delivery. The producer revision trigger is discovery state, not a per-peer delivery event (#135). |
| [`incident_reference`](../src/outpost/store/incident_refs.py) | Uses the caller's transaction for reference identity and retired-number ledger decisions. | Content retention must not turn retired references into new identities. Off-device restore lineage remains #146. |
| [`MailService.send`](../src/outpost/bbs/mail.py) | The placeholder INSERT, permanent UID and conversation context are committed together. Migration 173 recovers old committed placeholders. | Recipient lookup precedes the transaction; this fix is not a blanket proof of all identity/authorization races. Reading/delivery notification and any transport are separate operations. |
| [`BBSService.create_thread/reply`](../src/outpost/bbs/service.py) | Thread/post allocation, UID/sequence and aggregate metadata use owned transactions. | Subscriptions, external notifications and federation scheduling are separate. Scope/trust rules must survive extraction; an atomic write is not authorization evidence by itself. |
| [`OutboxStore.admit_many`](../src/outpost/store/outbox.py) | Queue bounds, dedupe, supersession and batch insertion are one transaction, or participate in an explicitly supplied domain transaction. | In-memory governor state must follow committed admission. `pending`/`held` is not transmitted or delivered. |
| [`AirtimeGovernor`](../src/outpost/transport/governor.py) with `OutboxStore` | Attempt reservation, persisted outcomes, expiry and restart recovery have their own transaction boundaries. | The radio side effect is necessarily outside SQLite. Interrupted attempts can be uncertain; RF ACK, application receipt and a person's acknowledgement are different facts. Critical reserve remains governed, not available to arbitrary high-priority work. |
| [`FederationSyncService.quarantine`](../src/outpost/fed/sync.py) | Modern inbox content and producer-revision receipt are persisted together after validation. | A durable receipt permits reconciliation progress; it is not import approval, responder notification or community visibility. Legacy compatibility has different ordering limits. |
| [`FederationSyncService.import_inbox`](../src/outpost/fed/sync.py) | Reads a pending record and current import scope, mutates the destination domain, records relevant provenance/revision audit and marks the inbox item imported in one transaction. | Operator preview happened earlier. A transaction alone cannot bind an approval to the payload actually reviewed. That known gap is recorded under #153. Existing automatic board import paths must be treated as explicit policy, not described as a human decision. |
| [`Reconciliation`](../src/outpost/fed/reconciliation.py) | Durable per-peer checkpoint/receipt state controls advancing a producer snapshot. A per-peer lock prevents overlapping in-process cycle handling. | The lock is not a SQL transaction and checkpoint sends are separate. A source's `unavailable` response is not proof of replica withdrawal or permission to delete local content. |
| [`OutpostApp`](../src/outpost/app.py) / [`web.api`](../src/outpost/web/api.py) / [`MeshOperationsCenter`](../src/outpost/operations_center.py) | Construct services, translate authenticated commands/API intent, coordinate existing callbacks, and record additional audit/response state. | Some SQL and audit still cross these layers. The federation web import audit follows the domain import; web rejection currently owns its own write. Handheld and operations-center review are additional mutation entry points, not exceptions to a future review-version check. |

This map identifies ownership, not proof that every method in a listed class is race-free.
Direct SQL writers, migrations, import paths, automatic board handling and maintenance must be
included whenever a domain invariant changes.

## Delivery vocabulary

The following stages need separate evidence and must not collapse into one “sent” flag:

1. Local authoritative record committed.
2. Durable per-peer change intent pending (incident-specific implementation remains #135).
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

These are planned boundaries, not changes already implemented. Keep #153 open until the slices
actually selected for it meet their evidence gates; do not close #135 on documentation alone.

1. **Federation review service (`fed/review.py`, proposed).** Own preview identity/version, transactional approval/rejection,
   scope recheck and audit. Web routes translate request/409 responses, while dashboard and
   handheld/operations-center flows carry the reviewed version. Both approve and reject must
   refuse changed content; an item ID alone is insufficient. Include two-reviewer races,
   replacement during review, restart, revoked policy, rollback and no-auto-broadcast tests.
   Automatic board imports need a distinct policy entry point, not a token bypass on a human API.
2. **Incident change publication (`watch/change_events.py`, proposed; #135 prerequisite).** Own a durable, coalescible change intent
   in the same transaction as each authoritative change. A worker owns peer scope, supersession,
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
