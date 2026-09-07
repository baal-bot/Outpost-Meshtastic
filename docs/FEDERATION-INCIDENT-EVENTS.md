# Incident event receive contract

Receiver-side software slice [#166](https://github.com/baal-bot/Outpost-Meshtastic/issues/166),
following the [source journal](INCIDENT-CHANGE-EVENTS.md). **No automatic sender is added.**
Prompt propagation, transactional peer handoff, sender retries/supersession/expiry, operator
delivery stages and G6 qualification remain [#135](https://github.com/baal-bot/Outpost-Meshtastic/issues/135).
The later [#167 staging service](INCIDENT-SENDER-HANDOFF.md) adds atomic per-peer source handoff;
governed sender admission and receipt matching remain unimplemented.

## Negotiation and wire shape

Watch-enabled HELLOs advertise `incident_events:1`. Both the current peer's integer capability
and `reconciliation:2` are required, along with active trust, incident sync policy, and locally
enabled Federation/Watch. Legacy peers continue using existing bulk synchronization. Plain
notes additionally require `incident_updates:1`; no new incident status or responder action
is introduced. This capability declares receive support, not an installed sender worker.

The reserved `INCIDENT` type (`0x30`) now accepts:

```json
{
  "mesh_id": "!producer",
  "target_mesh_id": "!receiver",
  "mode": 1,
  "event": {
    "stream": "incidents",
    "uid": "!producer:permanent-uid",
    "epoch": "0123456789abcdef0123456789abcdef",
    "revision": 42,
    "payload": {"uid": "!producer:permanent-uid"}
  }
}
```

The payload above is abbreviated; it must contain the existing valid incident export fields
or a supported plain-note payload. The event map has exactly the five displayed keys. UIDs
must name the authenticated original producer; payload UID must match. A positive integer
revision and 32-character lowercase hexadecimal epoch are mandatory. JSON encoding rejects
non-finite numbers and is limited to 12,000 bytes before storage; existing framing imposes the
tighter on-air bound. Inbound incident fields must have appropriate text, timestamp and numeric
position types. A changed or blocked lineage is never automatically repaired.

Current peer policy is reloaded inside the writer transaction, not trusted from an earlier
lookup. Located incidents must satisfy that peer's geographic policy. As in bulk policy,
an unlocated incident can be shared. A note's original parent must already be retained in this
peer's versioned incident inbox in the same epoch; its location supplies the geographic check.
That parent need not have been human-approved to store the note. Importing the note still
requires prior original-parent import and fresh human review. An unknown-parent note is refused,
so a future sender must send/retry the parent first or use bulk catch-up.

Frames require the existing paired-secret authentication, an explicit matching target and a
fresh monotonic peer counter. `INCIDENT` receives **no replay exception**. A sender recovering
a lost receipt must re-encode the same event with a fresh counter. Partial/invalid/replayed
frames cannot store content. Ordinary revisioned `ITEM` still needs an authorized reconciliation
page; event support does not create an unsolicited ITEM bypass.

## Storage receipt, not approval

`IncidentEvents.receive` owns a transaction containing current policy/lineage checks, a quota
charge, the quarantine inbox write and its existing producer-revision receipt. The shared
quarantine core accepts the caller's `Transaction`; it does not nest transactions or perform
radio I/O. Commit precedes return to the application and governed reply admission.

`INCIDENT_RECEIPT` (`0x32`) returns `mode:1`, `state:"stored"`, stream, UID, epoch, revision,
full SHA-256 content digest, sender `mesh_id` and explicit producer `target_mesh_id`. The digest
is lowercase hexadecimal SHA-256 of UTF-8 Python canonical JSON: sorted keys, separators
`,`/`:`, default ASCII escaping, no non-finite numbers. It is not the older truncated manifest
digest. The receiver verifies the exact stored payload before attesting to it.

Same-revision/content fresh-counter retries reconstruct the same receipt after restart without
rewriting the inbox or resetting pending/imported/rejected human decisions. Older revisions,
same-revision conflicts, changed lineage, or missing exact retained content get no positive
receipt. A newer revision is charged even if its payload is identical. All storage changes
roll back together on failure or pre-commit cancellation. Cancellation after commit or failed
reply admission can leave stored content without a received receipt; a fresh retry recovers it.

`stored` says nothing about import, current human-review state, responder notification, public
alert approval, responder ACK or downstream replication. The new receipt type cannot update
legacy board delivery state. This release deliberately rejects incoming incident receipts:
there is no sender intent against which to validate them yet.

## Bounds, compatibility and evidence limits

New event revisions consume an additional fixed one-hour per-peer counter at the configured
`quota_items_per_hour`. This closes the one-UID overwrite loophole in a row-count-only quota.
Identical retries are exempt from new-event charging, but their replies remain governed.
Bulk inbox quota checks still apply. The two-integer counter occupies one reserved
`fed_cursor` row per peer (`_incident_event_rate`, receive direction); full backups retain it.
No schema migration or unbounded per-event history is introduced. A backward wall-clock step
does not refill the counter; a forward step of at least one hour starts a new window. This is
not a rolling-hour limit or time-confidence solution (#141), and repeated backward steps can
delay recovery until the original window ends. Corrupt state fails closed.

Receipts use the existing authenticated governed broadcast carrier, Federation traffic class,
quiet hours, queue bounds, expiry and airtime budget. They contain identity/revision metadata,
not incident bodies or coordinates. Framing authentication does not itself provide payload
confidentiality; retain the deployment's channel/privacy precautions. Limits remain 188 bytes
per full app frame, 170-byte fragment body, eight fragments. No critical reserve or urgent
quiet-hours exemption is granted. Rejected/admission-failed replies are not durable sender work.

`tests/integration/test_federation_incident_events.py` exercises real codec/app dispatch and
governed simulated replies, policy races, exact receipts, failure/cancellation rollback,
reopen/SIGKILL recovery, quota/revision storms, note boundaries and adversarial input. The existing
review, revision reconciliation and note suites remain gates. These are temporary-store and
simulated-radio checks, not physical power-loss/RF or end-to-end latency evidence. Source
journal consumption, a complete sender/receiver loss simulation and the inactive-node field
exercises remain open. No live store, service, radio or appliance settings are changed.
