# Guarded incident admission and storage receipts

Software slice [#169](https://github.com/baal-bot/Outpost-Meshtastic/issues/169), following
[#167 staging](INCIDENT-SENDER-HANDOFF.md) and [#168 commit publication](OUTBOX-COMMIT-PUBLICATION.md).
There is **no automatic sender timer**. [#135](https://github.com/baal-bot/Outpost-Meshtastic/issues/135)
still owns bounded scheduling, automatic application retries/expiry, fresh-versus-backfill fairness,
receipt-reply coalescing, operator delivery-stage presentation and end-to-end/physical G6 acceptance.

## Explicit admission

`app.incident_sender.admit(peer_id, stream, local_uid, retry=False)` processes one already staged
incident or plain note. It reloads the active/online peer, capabilities, incident permission, current
producer identity/lineage, checkpoint, source revision, scope and exported content through a single
writer transaction. Content must match the staged full SHA-256. A note additionally requires an
exact storage receipt for its **current original parent** under the current identity/scope/key.
An older parent version or a bulk reconciliation cursor is insufficient for this sender path.

Peer secret lookup and monotonic counter allocation explicitly participate in that writer; no
nested transaction or separate-reader allocation is used. Encoding uses the existing authenticated
INCIDENT envelope, 188-byte full frames, 170-byte fragment bodies and eight-fragment maximum.
Counter exhaustion, oversize content and queue rejection roll back without consuming a counter or
replacing old queued work. There is no automatic key/counter reset or oversized-content bypass.

Counter, complete outbox batch, frame IDs and `fed_incident_dispatch` association commit together.
The association binds producer/peer identity, epoch/revision/full digest, scope and paired-secret
fingerprint. Queue publication follows commit. It can be published pending because the durable
attempt guard is mandatory; a held flag is not used as a substitute for authorization.
An exact pending batch is reused. Newer staged content supersedes unsent old frames only on commit.
Cancellation after commit remains ambiguous to the caller; inspect/retry the retained association,
not the original incident mutation. A radio send is never performed under the writer lock.

| Explicit admission result | Evidence |
| --- | --- |
| `queued` | Exact current batch retained with active work; not radio delivery. |
| `awaiting_receipt` | Retained frames have transport completion, but no application storage proof. |
| `retry_required` | Work is terminal/missing without storage proof; not delivery. |
| `stored` | Matching authenticated application receipt retained for this exact binding. |

`retry=True` permits re-admission only after the same-version batch has no active frames. It
re-encodes with a **fresh counter**, preserving the receiver's strict replay rule. A transport retry
of an existing fragment is different from recovering a lost application receipt. Existing outbox
TTL (30 minutes for Federation), transport retry limits, operator cancellation and airtime accounting
remain in force. Explicit re-admission is not an automatic retry policy and is not operator consent
for a future worker to revive cancelled work.

## Authorization at attempt reservation

`outbound_work.guard_kind` retains required owner validation across restart, held-work recovery,
uncertain sends, transport retries and operator requeueing. The production app wires the incident
guard during construction. A missing/unknown handler fails closed, as does a missing/deleted or
superseded association. Unguarded existing traffic retains its previous behavior.

Inside the attempt reservation transaction the guard checks current source/export/digest,
peer/producer identity, active/online trust, capabilities, geographic scope, key, parent storage
and current radio channel/port. It reconstructs the exact authenticated frame and checks its
position in the retained frame-ID list. A previously received receipt prevents more attempts.
The candidate used for I/O is snapshotted and matched to the durable payload/routing/class fields;
mutating an in-memory payload or clearing its guard cannot bypass the persisted requirement.

A denial records `failed` / `dispatch authorization denied`, without reserving an attempt or
sending radio bytes. Unexpected storage/guard faults propagate to existing core-task supervision;
they are not interpreted as permission to send. Restart does not erase owner requirements.

The authorization linearization point is **attempt reservation**, not the later physical RF
instant. Source/peer writes serialize with that reservation. Policy changes after reservation
cannot recall already authorized/in-flight radio I/O; asynchronous local configuration changes
likewise cannot be made atomic with the radio. A fragment may already have left before a later
fragment is denied. This does not establish immediate remote withdrawal or exactly-once delivery.
Normal recovery must still quiesce egress/admissions before rebuilding the governor.

## Exact receipts, not human action

The authenticated dispatcher accepts the existing targeted `INCIDENT_RECEIPT` shape. The consumer
checks integer mode/revision, epoch, full lowercase digest, producer UID, explicit target, current
peer permission/identity and paired secret against a retained association. An unknown or mismatched
version receives no completion credit. Fresh-counter duplicate receipts preserve the first storage
timestamp and cancel only remaining unsent frames of that exact association, after commit.

A late receipt can record an older still-retained association while a newer intent waits, but never
clears or modifies that newer intent. If a newer association replaced it, the old receipt is ignored.
No source journal/intent is consumed and no per-edit dispatch history is retained. Parent gating
requires current exact evidence, so an older receipt cannot authorize a newer note's parent state.

`stored_at` records local receipt processing time, not a trusted remote clock, human review, import,
public alert approval, responder notification/acknowledgement, downstream replication or continued
remote retention. Remote deletion/restore can invalidate previously observed storage; refusal of a
note then requires catch-up/recovery policy, not an invented positive receipt. Receiver reply
coalescing is not added here; duplicate event retries can still queue multiple governed replies.

## Compatibility, retention and limits

Migration 180 adds the nullable guard column, indexed queue-key lookup and one association per
peer/incident-or-note identity.
Existing work is unguarded and unchanged. Associations follow explicit peer deletion and are otherwise
protected from maintenance; full backups preserve them. Outbox terminal history retains its existing
maintenance policy. There is no unbounded association history per retry, but retained source
identities times peers remain a storage-capacity concern (#148). Metadata and key fingerprints are
linkable and must not enter public diagnostics/exports. HMAC authentication is **not payload
encryption**; underlying channel confidentiality and existing geographic/privacy policy are separate.

Each call handles one identity and at most eight frames. Individual exports/origin lists still need
their existing content validation and cost; this is not a constant-time or latency-bound claim.
No new priority, critical-alert reserve, quiet-hours exemption, sharing policy or automatic import
is introduced. Coherent backup forks, time confidence, physical power-loss durability and RF G6
remain separate open work.

`tests/integration/test_incident_sender.py` uses temporary stores, real app/codec/governor wiring,
simulated peers, writer/failure injection, restart/backup/upgrade and test-owned process kills.
These checks do not qualify real RF delivery or G6. Back up before an approved migration/deployment;
older binaries correctly refuse schema 180. This work does not open/migrate the live store, deploy,
restart a service/radio, activate the extra node or alter browser assets/dependencies.
