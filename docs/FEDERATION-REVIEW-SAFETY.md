# Version-bound federation review

Software bug [#160](https://github.com/baal-bot/Outpost-Meshtastic/issues/160), extracted from
[#153](https://github.com/baal-bot/Outpost-Meshtastic/issues/153).
This is not physical federation qualification, event-driven incident propagation (#135),
or permission to deploy/restart the appliance.

## Decision contract

`FederationReviewService` owns each human decision's writer transaction. Approval checks
the pending version and current peer trust, invokes the existing domain import core on the
same connection (including its current module, peer-stream and destination-board checks),
and records the human audit before commit. Rejection checks the same version and commits
the rejection and audit together. Rejection is permitted after peer scope is revoked;
it grants no import or broadcast permission.

The full SHA-256 review fingerprint covers the inbox ID, peer ID, stream, UID, state,
exact stored payload JSON, receive time, producer epoch/revision and displayed peer identity.
It does not trust the short federation wire digest. The tag is deterministic across an
ordinary service restart and changes on a newer producer revision even if the text and
receive second are unchanged. The audit retains the accepted fingerprint, not the payload.

This is an optimistic-concurrency tag, **not authentication, a one-time secret, or proof
that a person read the record**. Existing authenticated web roles, CSRF and reviewed direct
mesh PKI still apply. An identical legacy record with identical receiver metadata has the
same fingerprint; the tag is not a history counter or backup-restore lineage guarantee.
Current pending state is always rechecked under the writer lock. A competing decision or
different content requires a new review; it cannot be silently approved/rejected by ID.

## Operator interfaces and compatibility

- The inbox GET includes `review_token`. PATCH requires it alongside `state` and optional
  rejection `reason`. Missing/malformed tags return 422; a changed/already-reviewed/missing
  pending item returns 409 `review_conflict`. Scope failures return 409 `review_failed`.
  Other pre-existing authentication/module/transport gates retain their own errors.
- The dashboard binds buttons to the loaded record, offers escaped full-payload details,
  reports failures, and refreshes after a conflict without automatically resubmitting.
  Transport failure is an **unconfirmed outcome**, not a promise that nothing committed.
  Refresh before another decision. A successful import still does not broadcast an alert.
- The handheld remains **metadata-only**. Its server-side preview tag must match when
  preparing confirmation and again when executing it. No payload or full hash is sent
  over radio. Confirmation is one-shot and bounded to ten minutes; existing session,
  role/PKI and interruption checks remain. Direct `OPS IMPORT <id>` now requires opening
  that item in `OPS REVIEWS` first. This is metadata-based authorization, not full-content
  review equivalent to the dashboard.
- The new frontend disables review buttons if an older backend returns no version tag;
  it explicitly requests a matching backend update. An old cached frontend against the
  new backend fails closed with 422. This deliberate API/command safety tightening is
  not a schema/wire-format migration. Deploy matching assets/backend during an authorized
  maintenance window; no live restart was performed for this work.

Automatic approved-board handling continues through the separate trusted
`FederationSyncService.import_inbox` entry point. It does not claim a human review or
manufacture a version tag. Its policy and the existing domain import behavior are unchanged.
`import_inbox_transaction` is the shared caller-owned-transaction core; it must never be
exposed as an unguarded human route. Generic human import audit moved from app/operations
callbacks into the review transaction. No nested transaction, network I/O or new service
infrastructure is introduced.

## Evidence and limits

- `tests/integration/test_federation_review.py`: real service graph and temporary SQLite;
  same-second replacement, identical-text producer revisions, peer rename, two-reviewer
  races, writer-lock contention, failed/cancelled audit rollback, scope/trust/module
  revocation, duplicate receipt, fresh-object restart, no automatic RF/public broadcast,
  authenticated web/CSRF/tag validation and conflict recovery, cross-item tag rejection,
  destination-board revocation and accepted audit fingerprint.
- `tests/integration/test_mesh_operations_center.py`: guarded app import and human audit;
  handheld preview/confirmation replacement, expiry, revoked scope, and one-shot commands.
- `tests/browser/test_mobile_navigation.py`: mobile/desktop stale approve/reject recovery,
  no automatic resubmission or false success event, escaped payload details, and mixed
  backend/frontend fail-closed behavior, plus lost-response outcomes both with and without
  a committed server-side decision.
- Existing federation import/revision/reconciliation, transaction fault, web authorization
  and production-wiring tests remain the extraction guardrails.

Temporary-store cancellation/rollback is not physical power-loss qualification. Field
G1–G6, restore lineage, replica withdrawal, prompt incident delivery, human usability and
the remaining #153 extraction work are still open. No live data or RF transmissions are
used by these new tests.
