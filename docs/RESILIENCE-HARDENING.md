# September 2026 resilience hardening

Implementation evidence for the [resilience tracker](https://github.com/baal-bot/Outpost-Meshtastic/issues/130).
Automated evidence here is not physical-radio, power-loss, or deployment qualification.

## Local mail atomicity — #132

Local sends and replies now insert the message and finalize its UID and conversation
key in one serialized transaction. The existing `node:row-id` format for new messages
is unchanged. Readers cannot observe an unfinished message, and cancellation or a
failed write cannot leave the shared `pending` UID blocking future sends.

Migration `0173_recover_pending_mail.sql` repairs previously committed `pending` mail.
Because those rows do not retain their original node ID, the migration assigns a
`recovered:mail:<random-128-bit-hex>` UID without inventing an origin. It preserves
the row ID, content, recipient, state, expiry, reply links, federation routing, and
existing conversation key. Only a missing conversation key is initialized from the
recovered UID. The repair is idempotent and does not delete mail.

The migration runs on opening the store with the updated application. Back up before
deploying: once migration 173 is recorded, older binaries will correctly refuse that
newer store. This code change does not update or restart the installed appliance.

`tests/integration/test_mail_transactions.py` covers concurrent sends/replies, reader
isolation, failure and actual task cancellation at both write boundaries, reopening,
and recovery from the real pre-fix schema. Existing local, operator, and federated
mail tests remain compatibility checks. These tests do not prove survival of sudden
power removal; commit durability and physical power-cut qualification remain #137
and #44 respectively.

## Airtime eligibility — #133

The scheduler now visits available candidates in priority order and checks both the
rolling global limit and the item's class allowance before selecting a transmission.
A temporarily budget-blocked alert no longer prevents an eligible reply from sending.
A smaller packet can also pass a larger packet that does not fit the remaining budget.
Skipped work stays queued in its original order, without consuming an attempt.

Critical alerts retain priority and exclusive access to the emergency reserve. Other
classes retain round-robin scheduling. Severity/priority ordering and FIFO apply among
eligible items; deferred items retain their relative order when capacity returns.
Channel-utilization limits, quiet hours, optional low-power shedding, held admissions,
retry timing, pacing, supersession, and expiry remain in force. Invalid payloads become
terminal failures without stopping the candidate scan, which tolerates cancellation
of a later candidate while that failure is being persisted.

`tests/integration/test_governor_eligibility.py` uses the same durable-outbox construction
as the application. It covers all thirty distinct class pairs, smaller-packet bypass
in every class, global and reserve boundaries, policy gates, round-robin/FIFO behavior,
held/retry work, invalid payloads, cancellation, and restart-time/live expiry outcomes.
These checks do not establish physical delivery or sustainable community-scale radio
capacity (#142 / #152 / #157).

## Stable incident references — #138

New incident identities receive monotonically increasing positive local numbers.
Migration 174 adds an append-only `incident_reference` ledger containing only the
number and opaque incident UID. It has no member ID, report body, location, or timestamp,
and is deliberately retained for the life of the database. Database triggers reserve
the binding atomically with creation and reject reassignment, including insert paths
outside the local service. The same allocator is used for federated imports. Importing
the same purged UID restores its original number, not a different incident's number.

The migration preserves unambiguous legacy references. Where multiple retained records
share a number, it retires that number and gives every affected record a fresh one;
incident IDs, UIDs, content, provenance, and relationships are unchanged. Operators
should review and reannounce active reports whose numbers changed. Previously composed
messages that name retired numbers now fail instead of selecting a guessed target.
Unique retained terminal references still show the original report; CONFIRM/DISPUTE
explicitly refuse an inactive report. Merged references continue to follow the
human-selected canonical incident and return to the source after unmerge.

`INC`, `CONFIRM`, and `DISPUTE` retain their numeric syntax, scoped to the addressed
Outpost. Reaction acknowledgements add a title (up to 48 UTF-8 bytes) and origin (up
to 24 bytes), and the incident API includes its existing stored origin. Number width
grows instead of recycling. Federation origin UIDs and wire messages are unchanged.
REQ-WATCH-011 now records this stronger identity rule instead of active-only reuse.

The ledger protects references from the migration onward and detects ambiguity still
present in retained history. It cannot reconstruct already purged pre-upgrade bindings,
undo a prior misdirected reaction, or recover ledger entries lost by restoring an old
backup. A new/reset database or rollback to an older backup must not silently continue
the same advertised identity without a reference-lineage recovery plan (#145/#146).
Copying the current complete database preserves the ledger. Back up before deployment;
the updated application records schema 174 and older binaries refuse that store.

Tests cover each terminal status with/without content deletion across restart, delayed
commands through the real router, maintenance retention, import/merge/unmerge/reimport,
legacy migration and database guards. Creation-failure tests verify that the ledger
reservation rolls back with the incident, origin, and provenance.

## Authorized location corrections — #140

`UPD <number> <where>` now completes the missing-location acknowledgement. The original
reporter uses a verified PKI DM (including replay protection); a radio ID alone is not
authentication. Guests may still file reports without a reviewed key, but must ask the
operator to review their radio key or correct the location on their behalf. Ownership
and current key state are rechecked under the writer lock. Unrelated residents cannot
edit another report, even if they have responder trust. The authenticated operator API
`POST /api/v1/incidents/{id}/location` accepts `{"location":"North gate"}` with the normal
session, role, and CSRF checks. Watch supplies a keyboard-accessible **Correct location**
control directly on each active incident card, including reports with no map marker.
The handheld incident menu also offers the original reporter a guided correction input.

Inputs are limited to 200 UTF-8 bytes, with place labels limited to 160. Place-only edits
clear old coordinates and remain geometrically unconfirmed. `-share <lat> <lon>` (also
comma-separated) or `-share -wp <name>` explicitly publishes coordinates. `-nopos [place]`
withholds the current coordinates; a place-only edit preserves prior suppression.
Corrections never read cached member positions or change position-sharing preferences.
The UI explains that operators need the reporter's consent before sharing coordinates.
This is not retroactive erasure: earlier public locations remain in retained provenance,
message history, exported records, and backups according to their existing policies.

Corrections reject terminal or merged records rather than following an old reference
into another report. The stable reference ledger from #138 protects purged numbers.
Five writes (incident, ordered update, local origin version, before/after provenance,
and non-location-bearing actor audit) share one transaction. Identical retries are no-ops.
The current timestamp-based federation version advances even for same-second edits or
a backward clock step. This narrow ordering safeguard does not solve the requester-clock
and pagination defects tracked in #134; timestamps can temporarily lead wall time.

The UID and federation wire format are unchanged. In-area corrections and coordinate
withholding pass through existing peer consent, quarantine, and import/reconciliation.
Existing policy allows reports without coordinates. Moving a report outside a peer's
permitted area prevents the new location from being exported, but does not withdraw
that peer's old copy; reliable withdrawal/convergence remains part of #134/#135. Remote
history and local human-reconciliation policy must not be bypassed to force erasure.

Tests in `test_incident_location.py` cover the exact ACK-to-reply journey, guided input,
ownership/PKI/replay rejection, consent, validation, stale references, ordered concurrent
edits, rollback/cancellation at all five writes with database reopen, authenticated web
roles/CSRF, and production federation export/quarantine/import. Browser tests exercise
unlocated intake → explicit location → map marker and subsequent operator actions on
phone and desktop in all three themes. No new schema migration or periodic work is added.

## Clock-independent reconciliation — #134

Migration 175 installs a producer-owned AUTOINCREMENT revision index. Transactional triggers
advance the index for export-relevant incident, origin-binding, post/thread/board, and alert
changes. Repeated edits replace the current metadata head rather than retaining payload copies.
Deleted or moved identities retain metadata-only heads, so retention cannot recycle the producer
clock. The index contains stream names, opaque UIDs and revision numbers, not bodies or locations.
A random lineage is initialized once. Full backups must preserve that lineage, the index and
SQLite's sequence state. Older binaries refuse schema 175; back up before a planned deployment.

The negotiated `reconciliation: 2` capability leaves radio framing at version 1 and the existing
188-byte fragment ceiling. The initial request has no timestamp or producer watermark. The
producer chooses a high-water revision, and pages scan at most 100 indexed metadata heads and
return at most eight permitted records. Cursors ascend and persist across budget-limited cycles.
The local item budget and 16-round ceiling cannot be increased by peer metadata or scope resets.
Scope changes restart discovery under the new policy; new producer lineages stop for review.
An observed watermark or requested revision ahead of the producer also stops for recovery review,
rather than silently replaying an older backup as a valid continuation. This detects observed
rollback; it cannot identify every fork whose reused counters have already overtaken the old head.

This is bounded change discovery, not a historical snapshot. A concurrent edit can move a head
beyond the current watermark; the next cycle discovers that newer revision. Fetching an advertised
item can return its newer current revision. Per-peer durable receipts prevent an older packet or
a timestamp-only downgrade from replacing the newer payload. The same revision with a different
payload is rejected. Checkpoints hold the pending page until all fetched payloads and receipts
are committed together, so restart/lost fetches cannot silently advance past missing data. A
source deletion or scope change during fetch returns an explicit unavailable/reset result.

Imported authoritative-origin incidents use source revision ordering even if `updated_at` moved
behind `created_at`. Local origin, merge and monitoring protections remain in place. Revisioned
post edits and alert cancellations update their existing original-producer records on approval;
they do not override local moderation or another origin. Revision import records its actor,
lineage, revision and digest in the audit log, without duplicating report content there.

Legacy peers still use timestamp ordering. They remain usable, but are explicitly not clock-skew
qualified. Negotiated revision use is pinned independently of later unauthenticated HELLO
capability changes. The status API exposes `cursor_mode` and `clock_independent`; the dashboard
renders revision numbers as revisions, not 1970-era dates. Modern retry/cycle delays use monotonic
time; after restart an active page retries, while a completed/budget-stopped cycle conservatively
waits one interval. Quiet hours, liveness and expiries still follow their existing policies (#141).

Out-of-scope unavailability does not erase an old replica or its history. Reliable withdrawal and
event-driven incident propagation remain #135. Signed multi-hop relay envelopes retain their
existing protocol and lifetime rules; they cannot silently downgrade an origin already pinned to
revisions. Database rollback/lineage adoption needs the recovery work in #145/#146. This migration
does not restart or upgrade the installed appliance, and automated framing tests are not physical
radio or prolonged-partition qualification.
