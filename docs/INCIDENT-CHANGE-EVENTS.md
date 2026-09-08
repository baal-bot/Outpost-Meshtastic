# Atomic incident change journal

Software slice [#165](https://github.com/baal-bot/Outpost-Meshtastic/issues/165), a prerequisite
to [prompt federation #135](https://github.com/baal-bot/Outpost-Meshtastic/issues/135) and the
[transaction ownership work](TRANSACTION-OWNERSHIP.md). This is **undispatched source work**,
not prompt transport by itself. #165 added no worker; the later
[automatic sender](INCIDENT-AUTOMATIC-DELIVERY.md) now drains these retained heads.

## Commit and supersession contract

Migration 178 adds `incident_change_event`. An insert trigger on the existing producer revision
index captures the `incidents` and `incident_updates` streams in the same SQLite transaction.
The journal therefore covers existing revision writers: local intake, corrections, reactions,
notes, resolve/reopen, location/origin changes, reviewed parent imports, merge/unmerge, expiry
and content deletion. A journal write failure aborts the source write. A surrounding rollback
or cancellation rolls back both. There is no radio/network work under the writer lock.

Each stream/UID has one row containing its producer epoch, latest revision and first-pending
revision. Multiple revision increments in a single domain transaction coalesce too. Repeated
changes retain the first-pending position rather than making a new history row; wall time is
not used. A different producer epoch starts a new position when a new head is inserted. This
does not authorize lineage rotation or prove an old-backup branch is safe to rejoin (#146).

Backfill records **current heads only**, including deletion heads. The first-pending revision
for these rows is the head observed at upgrade, not the time/revision of an unknowable first
historical change. Fresh stores start empty. The original revision index and sequence are not
changed, and no source payload, note, reference binding or remote receipt is rewritten.

## Scope, privacy and retention

The journal captures candidates, not permission to export. Imported incident heads can appear;
the dispatcher must still check original-producer identity, active peer trust, geographic and
module scope, and note capability through the existing export policy. Imported notes do not
produce local note heads and therefore do not become republished local notes. Board and alert
heads do not enter this journal. Human review, local monitoring and public-alert approval are
unchanged. Capture does not mean notification, responder ACK or critical-reserve eligibility.

Rows contain no body, coordinates, author, peer list or timestamps. UIDs are still linkable
metadata: do not treat the table as a public or anonymized export. Content deletion leaves the
pending metadata and its new revision; it is **not** a remote withdrawal instruction.
Maintenance preserves the journal, and full SQLite backups preserve it with the producer
lineage, revision index and sequence. There is at most one pending row per incident/note head,
not one per edit or per peer. This is not an absolute lifetime storage cap: retained identities
and tombstones can grow. No pending work is silently dropped to meet a row limit. Storage
policy remains #148 and physical commit durability remains #44. The checked WAL/FULL
software policy is complete (#137).

## Bounded inspection and the next dispatcher

`IncidentService.change_events.pending(after=0, limit=100)` reads at most 100 rows through the
first-pending index and point-lookups of the current heads/lineage. It returns `pending` only
when the recorded epoch/revision matches that head. Missing heads, changed revisions, and
missing/changed lineage are separately reported as `missing_head`, `revision_mismatch`, or
`lineage_mismatch`; inspection neither consumes nor repairs them. Even `pending` does not mean
the payload still exists or is currently exportable.

Use the last `first_revision` to page within an inspection pass. Restart subsequent passes at
zero: supersession preserves an old position, so this page cursor is **not a durable change
watermark**. There is intentionally no completion/receipt API yet. Before adding one, the
dispatcher must atomically replace a version-checked source intent with durable, scoped
per-peer work; stale handoffs must never erase a newer revision. It must separately handle
no peers, policy changes, unexportable/oversized content, blocked lineage, retries, expiry
and non-starving priority. Deleting a row after an in-memory send would violate this contract.

The existing bulk reconciliation and governed quiet-hours/airtime policies are unchanged.
Remote storage, human import/rejection, responder notification and human acknowledgement
remain separate facts. The [automatic worker](INCIDENT-AUTOMATIC-DELIVERY.md) now supplies
operator-facing per-peer states and declares the software timing envelope; physical G6 remains held. The
separate [#166 event receiver](FEDERATION-INCIDENT-EVENTS.md) now supplies authenticated ingress
and exact storage receipts; it does not consume this journal or install a sender.

The [#167 per-peer handoff](INCIDENT-SENDER-HANDOFF.md) now stages scoped, exact-version metadata
and scan progress atomically using a separate revision-seek index. It deliberately retains this
journal: one peer's checkpoint is not all-peer completion. The source inspection API and its
first-pending cursor semantics are unchanged. The automatic sender retains these heads;
there is still no single-peer consume API.

## Deployment and verification

Back up before an approved deployment. Opening a database with this release applies migration
178; older binaries correctly refuse the newer schema. Do not open the live store just to
inspect this feature before deployment approval. No live migration/restart is part of #165.
No browser assets, external providers, configuration or dependencies change.

`tests/integration/test_incident_change_events.py` covers transaction isolation, write failure,
actual task cancellation, restart, fresh/upgrade/backfill, backup recovery, first-pending
supersession, bounded query plans and mismatch reporting, plus the production incident,
merge, import and note boundaries. The existing incident/federation/maintenance/backup
regressions remain gates. These are process/SQLite guarantees, not sudden-power-loss or RF
delivery evidence; the inactive node and physical exercises remain held.
