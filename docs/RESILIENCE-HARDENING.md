# September 2026 resilience hardening

Implementation evidence for the [resilience tracker](https://github.com/baal-bot/Outpost-Meshtastic/issues/130).
Automated evidence here is not physical-radio, power-loss, or deployment qualification.

## Automatic incident delivery — #135

The [automatic worker](INCIDENT-AUTOMATIC-DELIVERY.md) connects source changes to guarded
admission through independent fresh/backlog lanes and bounded peer pages. Finite retry/deadline
state survives recovery; explicit cancellation is not automatically revived. Fresh-counter
retries recover loss and counter overtaking. Exact receipt replies coalesce atomically and
retain current-evidence attempt guards. The operator-only, non-cacheable view separates
queue/radio/storage from human action, with version/queue-bound audited cancel/retry controls.
Migration 182 retains this metadata without changing payloads or sharing opt-ins.
The browser G6 follow-up adds opt-in `incident_compact:1` field-name encoding with
unchanged content, legacy fallback and guarded capability transitions. It corrects
reversed urgency priorities and prevents a continuous fresh lane from starving an
admitted backlog batch. Three simulated Outposts now pass the 60-second reviewed-map
gate for the documented short-report/daylight/empty-queue envelope; withholding human
review keeps remote maps empty despite stored receipts. Actual ASGI/Chromium and local
synthetic tiles are used with external browser requests blocked. This is not real
geographic coverage or firmware/RF qualification. Physical G6 remains held; no live
deployment or radio restart occurred.
Historical prerequisite sections below describe each original slice, not the current absence
of a worker.

## Current dispatch timing — #170

The [shared timing boundary](DISPATCH-TIMING.md) rechecks eligibility after awaited
telemetry/observation and inside attempt reservation after writer/owner validation.
Policy deferral preserves pending work without a phantom attempt. Serialized ticks,
current outcome/retry times, conservative success/failure/cancellation accounting and
recovered pacing prevent waits from backdating airtime or bypassing gaps. Migration 181
indexes the later of attempt start/completion for recovery. Class shares, critical reserve,
quiet-hour settings, trust and approval remain unchanged. Clock confidence and physical
qualification remain open; automatic incident scheduling still belongs to #135.

## Guarded incident admission and exact receipts — #169 / #135 software slice

The [explicit sender service](INCIDENT-SENDER-ADMISSION.md) commits peer counter, current source
binding, complete governed frames and exact association together. A durable owner guard rechecks
identity, peer/source/key/scope and parent-storage evidence at every attempt reservation, including
recovery and transport retries. Exact authenticated receipts record storage without clearing newer
intents or changing human review. Explicit lost-receipt retries use fresh counters. Migration 180
adds retained owner/association metadata and indexed queue lookup; existing traffic and radio
budgets/quiet hours are unchanged. Fault, process-kill, backup/upgrade and simulated end-to-end
checks are software evidence. Automatic scheduling/application retry policy, receipt-reply
coalescing, operator stages and held physical G6 remain #135; no live deployment is performed.

## Commit-safe governed queue publication — #168 / #135 and #153 prerequisite

The [shared outbox boundary](OUTBOX-COMMIT-PUBLICATION.md) publishes admitted items, supersession,
held IDs and successful-admission metrics only after SQLite commit. Rollback preserves previously
queued work; repeated cancellation waits for writer settlement. A post-commit publication failure
is reported as committed storage, blocks egress and requires quiesced recovery. Existing check-in
solicitation no longer retracts durable work that committed during cancellation. Original item
identity/status behavior is preserved with a pre-publication field snapshot. Ownership, queue,
process-kill, check-in and production supervision regressions cover the boundary. No migration,
live deployment, automatic incident sending or new radio policy is included in #168. The later
#169 slice adds incident authorization and exact matching; automatic scheduling/G6 remain #135.

## Atomic per-peer incident staging — #167 / #135 and #153 sender prerequisite

The [handoff service](INCIDENT-SENDER-HANDOFF.md) observes current source/peer policy and exports
through one writer transaction, stages coalesced exact-version/content metadata, and advances
that peer's revision cursor atomically. Source rows remain retained for independent peers;
scope expansion rescans without interpreting the first-pending inspection cursor as a watermark.
Migration 179 adds metadata tables and indexed bounded seeks. Crash/backup recovery, invalidation
and supersession are local guarantees, not sending or G6. Explicit outbox/receipt integration
follows in #169; automatic backlog scheduling, reply coalescing and physical qualification remain #135.

## Revision-bound incident event ingress — #166 / #135 receiver slice

The [negotiated incident event receiver](FEDERATION-INCIDENT-EVENTS.md) rechecks peer scope,
lineage and quota inside the quarantine transaction. A separate post-commit receipt identifies
the exact producer revision and full content digest, never human review or responder action.
Fresh-counter retries preserve human decisions; a durable per-peer counter bounds repeated
new revisions of one UID. Ordinary ITEM authorization, airtime and quiet-hours policy stay
unchanged. No sender worker, journal consumption, live migration or deployment is added;
prompt end-to-end delivery and held physical G6 qualification remain #135.

## Atomic source change intents — #165 / #135 and #153 prerequisite

The [incident change journal](INCIDENT-CHANGE-EVENTS.md) captures incident/note producer heads
in the source transaction, coalesces repeated changes and preserves first-pending order.
Migration 178 seeds current heads; bounded internal inspection exposes mismatched lineage or
revisions without inventing delivery. Metadata survives content retention and full backups.
This is undispatched source work, not a per-peer outbox. Peer scope, transactional handoff,
event receive/receipts, priority/retry/expiry and physical G6 remain #135. No worker, timer,
wire policy, dashboard assets or live deployment changes are involved.

## Local-first map selection — #164 / #139 browser slice

[Shared maps](OFFLINE-MAPS.md) try local tiles before optional Internet fallback, offer a
browser-origin offline-only preference, and retain per-visible-tile failure state. Explicit
retry rechecks the manifest; unchanged failed coverage does not cause a render-driven retry
storm. Retired image/manifest callbacks cannot start late fallbacks. Coordinates, markers,
selection, lists, and source-appropriate attribution remain available. No backend schema,
radio policy, recurring provider work, or appliance configuration changes are involved.
This does not certify whole-pack integrity/coverage: service-path provisioning, complete pack
validation, and a physical fresh-browser WAN-down gate remain #139.

## Independent boot-schema evidence — #163 / #136 and #149 software slice

The [existing readiness service](SAFETY-READINESS.md#boot-compatibility-is-separate-from-live-health)
now compares the inspected database with the package selected at boot. Incompatible, failed and
unknown selections cannot produce an all-ready result, even when developer HTTP is healthy.
The diagnostics CLI collects independent static evidence when the live backend is missing or old.
Collection has bounded subprocess output/deadlines and directory inventories, runs off the radio
event loop, and never executes alternate Python or changes a database/service. Exported evidence
omits raw unit commands, environment values and filesystem paths. No migration or radio policy
changes are involved. Installed release alignment, development database ownership, actual reboot
recovery (#136), and the remaining outage-readiness dimensions (#149) stay open.

## Visible oversized-item failures — #162 / #135 prerequisite

[Negotiated negative ITEM responses](FEDERATION-ITEM-FAILURES.md) retain a missing
page and bounded metadata diagnostics without inventing a receipt or successful
sync. Other items on that page remain receivable, and monotonic retry/reopen paths
preserve its cursor and budgets. Source diagnostics clear on queue admission, not
remote delivery. Existing radio limits and governor policy are unchanged. Larger
payload transport and progress past an oversized page still remain #135.

## Original-producer incident note content — #161 / #135 prerequisite

[Plain notes have a separate, capability-negotiated revision stream](FEDERATION-INCIDENT-NOTES.md).
Local note content and its producer head commit together; reviewed remote notes preserve source
identity and require an already imported original parent. They never acknowledge, resolve or
notify on behalf of a local responder. Scope and version checks, provenance and human audit
share the import transaction. Migration 177 backfills retained local notes; live deployment is
not part of this work. Prompt scheduling, oversized payload handling and physical G6 remain #135.

## Version-bound operator review — #160 / #153 software slice

[The review contract](FEDERATION-REVIEW-SAFETY.md) binds dashboard and handheld decisions to
the pending record version and commits mutation plus human audit together. Changed content,
competing reviewers and revoked import permissions cannot silently reuse a stale approval.
The dashboard handles conflicts without resubmitting and disables review when paired with
an older unversioned backend. Automatic board policy stays separate. This is a deliberate
human API/command safety tightening, not an on-air or SQLite schema change; #153 and its
event-publication prerequisite #135 remain open.

## Preliminary commit-policy latency — #137 remains open

[The temporary-store comparison](benchmarks/SQLITE-COMMIT-POLICY-2026-09-05.md) records NORMAL
versus FULL latency for concurrent incident/mail/outbox/quarantine-receipt operations on this
Pi's workspace SD-card filesystem. It changes no live setting and proves neither power-loss
survival nor energy/service-level acceptance. Default-policy selection and #44 remain open.

## Indexed producer page work — #144

Migration 176 adds a covering stream/revision index. Producer paging now merges at
most 101 heads from each permitted stream (originally 22; 23 with #161 notes), returns at most 101 heads to
Python, and applies export policy to at most 100 before emitting up to eight items.
Unselected/private history cannot consume catch-up pages. Geographic and hidden-record
filtering still uses the same policy as export. Legacy board manifests now also honor
hidden threads and archived boards.

[The paging qualification](FEDERATION-PAGING-QUALIFICATION.md) records query plans and
separate database/memory/wire measurements at 1,200 and 120,000 synthetic heads, plus
scope/order/edit/continuation regressions. These bounds apply to modern negotiated
links, not the clock-sensitive legacy adapter. Back up before migration 176; the
installed appliance has not been changed.

## Emergency-burst qualification — #152

[The synthetic burst report](EMERGENCY-BURST-QUALIFICATION.md) defines the tested load,
measured admission/storage/memory/latency, overload attribution, class exhaustion,
restart and SQLite-full recovery, responder fan-out and identity limitations.
Changed safety requests now retain distinct acknowledgement identities even when
their rendered text is identical. Cancelled/timed-out handlers release their retry
marker and propagate cancellation; service transactions retain commit/rollback
ownership. These are automated application guarantees, not RF or physical-power
qualification, indefinite storage bounds, or exactly-once emergency delivery.

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
producer chooses a high-water revision, and pages evaluate at most 100 metadata heads and
return at most eight permitted records. Migration 176's scoped index/merge bounds are detailed
above. Cursors ascend and persist across budget-limited cycles.
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
