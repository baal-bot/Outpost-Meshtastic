# Federated incident notes — #161 / #135 content prerequisite

Plain operator update notes now have separate producer revisions and a reviewed
import path. Previously an operator note advanced the incident snapshot timestamp,
but the exported snapshot omitted its text. This change preserves the text; it does
**not** implement prompt event delivery or satisfy G6's physical latency gate.

## Supported contract

Both endpoints must advertise `reconciliation: 2` and `incident_updates: 1`, have
Watch enabled, and explicitly allow incident synchronization for the active peer.
HELLO advertises the note capability only while Watch is enabled. The separate
`incident_updates` stream uses the existing authenticated ITEM framing and bounded
revision reconciliation. Legacy manifests and modern peers without the note
capability do not include notes. Enabling the capability changes scope and restarts
discovery so older retained notes are not skipped behind a previous watermark.

Migration 177 records immutable note/parent ownership and producer epoch/revision
for imports. It backfills metadata heads for retained local `update` rows attached
to locally owned incidents. Insert/edit/delete triggers commit note heads with the
note mutation, including the existing transactional operator-update path. A parent's
coordinate correction also refreshes its local note heads: previously out-of-area
notes can become eligible without a text edit. That write is proportional to the
parent's retained notes; it is not an independently constant-cost operation.

Exports are plain `update` notes only: nonblank body up to 500 characters, nonblank
author label up to 160 characters, original note and parent UIDs, and source creation
time. Content is preserved, not silently truncated. Structured coordinate-update
rows, ACKs, confirmations, disputes and status-change notes are not this stream.
Unsupported historical rows are withheld. Imported notes and local commentary on
foreign-owned reports are not republished as local-producer authority. Multi-hop
note relay and successor-identity adoption are not implemented here.

The current original parent's coordinates control export radius filtering; exact
coordinates are not separate fields in a note payload. Arbitrary note text can
still contain names or locations. Operators must apply their community's sharing
policy to the content; this is not automatic anonymization or replica erasure.

## Receiving and reviewing

The receiver stores content and its producer-revision receipt atomically in the
existing inbox. An authenticated receipt or completed reconciliation checkpoint
means durable quarantine, not import, human acknowledgement or notification.
Automatic board-import policy never imports notes.

Use the federation inbox's full-content dashboard preview to inspect the original
parent UID, note, author and revision. Import the **original parent incident first**,
then review/import the note. A failed parent prerequisite leaves the note pending;
there is no automatic parent creation or approval. A merged-origin alias alone is
not sufficient: the receiver must have imported the original report. A source that
merged or removed the parent before it reached the receiver can therefore leave a
note pending; operators must reconcile that missing parent explicitly.

Approval uses the [version-bound review transaction](FEDERATION-REVIEW-SAFETY.md).
Current peer trust, capabilities, module, incident permission and parent geographic
policy are checked again inside it. The note UID and original parent must belong
to the sending producer. Imported identity, parent and source are immutable. Newer
revisions can update note text/author/time only after fresh review; stale revisions,
equal-revision conflicting payloads, reparenting and incompatible producer lineages
are rejected. Source clocks may move backwards; producer revisions determine order.

An imported row remains attached to the original incident through local merge and
unmerge. The full incident report includes related originals; canonical provenance
also records where the note was reviewed. The author is visibly prefixed with
`federation:<source>`, and source UID/epoch/revision remain stored. The domain import,
provenance, revision audit, inbox decision and human audit share one transaction.
It does not change local incident status, severity, confirmation counts, monitoring,
responder assignments or acknowledgement. It does not enqueue a radio message or
broadcast an alert on behalf of the receiver.

## Bounds, evidence and open work

The [indexed page bound](FEDERATION-PAGING-QUALIFICATION.md) grows from at most 22
selected streams to 23 with notes: at most 2,323 pre-merge metadata rows, 101 returned
to Python, 100 per-record export checks and eight manifest items. No-note peers do
not scan the new stream. Tests exercise 100,000 retained notes and the 23-branch
maximum. Source metadata is still a latest-head index, not delivery-event history.

`test_federation_incident_notes.py` covers source transactions, historical migration,
same-second notes, clock skew/backsteps, close/reopen, parent dependency, original
identity, revisions, policy revocation, no automatic import and review rollback/
cancellation. The existing real-framing/durable-outbox reconciliation test also runs
with a note-capable pair and simulated radios. It checks received content and the
188-byte fragment ceiling, not field RF performance.

The wire ceiling remains **188 bytes per authenticated application frame, 170 bytes
of body, eight fragments / 1,360 encoded bytes per message**. A 500-character Unicode
note is not guaranteed to fit that compressed encoded ceiling. [Oversized-item
failures are visible and retryable on capable peers](FEDERATION-ITEM-FAILURES.md),
but larger-content transport remains #135; this neither widens framing nor claims every legal
report/note can be delivered. Long incident snapshots and large merged-origin lists
also retain that limitation.

Dedicated prompt scheduling, coalescible per-peer delivery intents, stage/expiry
visibility, delivery receipts and G6 remain #135. Hourly reconciliation and existing
quiet-hour, quota and airtime rules still apply. Federation does not use the governor's
discretionary low-power class throttle. A deletion or policy withholding is source
unavailability, not a remote withdrawal. Backup-lineage recovery, cloned producer
identity, RTC confidence and physical power-loss qualification remain separate.

No installed appliance or live database is migrated by this work. Before an approved
deployment, take and verify a full backup, preserve revision lineage/sequence state,
and deploy the matching binary. Older binaries refuse schema 177. Migration work
and storage scale with retained history; SQLite WAL/NORMAL settings are unchanged
and software rollback/reopen tests do not prove sudden-power-loss survival.
