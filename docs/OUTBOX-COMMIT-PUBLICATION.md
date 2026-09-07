# Commit-safe governed queue publication

Software prerequisite [#168](https://github.com/baal-bot/Outpost-Meshtastic/issues/168) for
incident sender integration [#135](https://github.com/baal-bot/Outpost-Meshtastic/issues/135)
and explicit transaction ownership [#153](https://github.com/baal-bot/Outpost-Meshtastic/issues/153).
No automatic incident sender, receipt matching, schema migration or live deployment is added.

## The failure and boundary

Previously, a durable `AirtimeGovernor.admit_many_result(..., transaction=tx)` could replace the
in-memory queue before the caller's SQLite transaction committed. An isolated reproduction on
`c34658e` replaced pending item 1 with provisional item 2, then rolled back and retracted item 2.
SQLite still retained pending item 1, but the running queue was empty. Restart could recover it;
the running process had lost its scheduling visibility. No radio was used for this reproduction.

Durable admissions now register synchronous local publication with `Transaction.after_commit`.
The transaction owns both the outbox rows and the eventual in-memory update. Queue supersession,
new items, held IDs and successful-admission metrics remain unchanged until SQLite commit
succeeds. Multiple publications execute in registration order without yielding to egress or
another writer. Failed writes, pre-commit cancellation and rollback discard all publications.

Caller-transaction admission IDs are **provisional**, not proof of commit or permission to send.
Standalone durable admission owns a transaction and uses the same publication boundary. Queue
bounds and dedupe include unpublished rows inside that transaction, including prior admissions.
Snapshots bind publication to the fields admitted to SQLite even if caller-owned inputs change
while a transaction yields. Publication restores those fields into the original item object to
preserve its existing identity/status API. After publication the governor owns the mutable item;
this is not an immutable-payload or per-attempt authorization mechanism. Arbitrary direct SQL or
post-publication item mutation is not a supported substitute for the outbox/domain APIs.

## Transaction and cancellation contract

Use only the active `Transaction` yielded by `Database.transaction()`, from its owning task and
with the same database. Closed, manually constructed, foreign-store and cross-task handles are
rejected. Sharing a transaction through `asyncio.create_task` or `wait_for(coroutine)` is not
supported; those create another task. Await domain operations directly inside the owner.

`after_commit` is for trusted, synchronous, non-I/O local publication, not SQL, radio/network
calls, async functions or a general background-work framework. Callbacks must return `None`.
They run on the event-loop thread after commit, before releasing writer ownership. Keep both
the transaction and publication work bounded; do not register unbounded callback histories.
The context invalidates its handle before settling commit or rollback.

Cancellation cannot stop SQLite work already running on its writer thread. Both compound
transactions and single-operation `Database.write` retain the writer lock until settlement,
even across repeated cancellation. When commit has begun, it may succeed: publication completes
before cancellation is propagated. A cancelled call is therefore not proof that nothing was
saved. Cancellation latency includes waiting for the writer; no hard shutdown deadline or
physical power-loss guarantee is established. WAL/NORMAL policy remains unchanged (#137).

## Publication failure is not rollback

If a callback fails after commit, other registered publications are still attempted and the
caller receives `PostCommitError`, explicitly identifying committed storage. There is no
pretend rollback or deletion of authoritative domain/outbox rows. Inspect retained state before
retrying a domain action; a failed response must not automatically create a duplicate action.

A failed or partially applied governor publication blocks egress with `StoreError`, including
ticks that were already awaiting telemetry or an attempt reservation. The existing core-task
supervision reports that failure rather than continuously sending from an incomplete queue.
Radio I/O already in flight cannot be recalled; an interrupted reserved attempt remains uncertain
and is handled by the existing durable recovery accounting.

Rebuild with `governor.recover()` only after egress and admissions are quiesced, as at application
startup. A failed recovery keeps egress blocked. This is not a hot-recovery API safe to race
against live sends. Successful reconstruction clears the publication fault. Existing expiry,
uncertain-attempt accounting, and held-work startup release behavior remain unchanged.

## Existing caller and next incident integration

Check-in solicitation keeps recipient rows and held outbox rows in its domain transaction, then
releases the committed batch. Its rollback path no longer retracts durable queue IDs: rollback
published nothing, while commit-time cancellation may already have saved/published the batch.
Committed held solicitations survive that cancellation or publication failure for normal restart
recovery, without re-soliciting the same recipients. The non-durable test/legacy governor retains
its existing explicit retraction behavior; the production appliance uses the durable outbox.

This fixes the shared commit boundary but does **not** connect the [incident staging service](INCIDENT-SENDER-HANDOFF.md)
to radio admission. Incident integration must still recheck current source/revision/content,
peer trust/scope and original-parent dependencies before every recovered/retried attempt.
Ordinary held-work startup release alone is not that authorization. The later
[#169 admission service](INCIDENT-SENDER-ADMISSION.md) adds explicit association, exact receipts
and mandatory per-attempt authorization. Receipt-reply coalescing, fresh-versus-backfill scheduling,
automatic application retry/expiry policy, operator delivery stages and G6 remain #135.
Quiet hours, airtime shares, critical reserve and human
review policy are unchanged.

## Evidence and deployment

`tests/integration/test_outbox_commit_publication.py` uses real database/outbox/governor wiring,
with temporary stores and simulated radios. It covers commit visibility, supersession, rejection,
input snapshots, ownership/lifetime, repeated cancellation, publication/recovery faults,
already-selected attempts, check-in domain coupling, production core-task supervision and
test-owned process kills immediately before/after commit. Existing transaction, durable-outbox,
check-in, eligibility, burst, federation, maintenance and backup suites remain regression gates.
The production coverage configuration now gates the transaction store itself at 90%.

These are process/software checks, not power-cut, RF delivery or G6 evidence. No new migration,
protocol type, external dependency or recurring worker is introduced. No live store was opened,
service/radio restarted, release deployed or inactive hardware activated by this work.
