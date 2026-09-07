# Automatic incident delivery — #135

The application drains its durable incident-change journal through a supervised
`incident-delivery` task, independently of hourly reconciliation. This implements
the prompt event-driven sender path in REQ-FED-034. Software tests do not establish
the complete physical G6 field-report → mesh-wide/map outcome.

## Scheduling and authorization

A five-second poll visits at most four active, incident-sharing peers, resuming by
peer ID and wrapping fairly. Unsupported or lineage-blocked peers get a visible
`_incident_worker` diagnostic without stopping eligible neighbors. Each peer gets
separate pages of at most four fresh and four backlog heads. First discovery seeds
the most recent page as fresh while the original cursor continues older history.
Subsequent fresh pages ascend a separate durable revision cursor.

Each lane considers at most two due intents per visit, with at most one active
automatic batch per lane and two per peer. The shared queue limit still applies.
Fresh critical/urgent/other work uses priorities -30/-20/-10; backlog uses 0.
Every frame remains `FEDERATION`: there is no critical-alert reserve access,
quiet-hours exemption, new sharing opt-in, or automatic human approval.

Current modules, peer capability/trust/liveness, geographic scope, producer
identity, epoch/revision/full digest, key and exact parent storage evidence are
checked. The existing attempt guard revalidates these after writer/owner waits,
including restart and transport retries. Terminal policy or elapsed delivery
deadlines deny new reservations. This boundary is reservation, not the later RF
instant; in-flight bytes cannot be recalled. No radio I/O occurs under the writer.

The journal retains one coalesced head per identity; one peer cannot consume
another's work. Superseded intermediate versions are not a promised event history.
Notes require storage evidence for their current original parent, not an old
parent version or bulk-sync cursor. Idle scans do not rewrite unchanged checkpoints;
unchanged peer diagnostics refresh at most once a minute.

## Finite delivery policy

First automatic consideration arms a persistent 30-minute window for the staged
version/scope, before fallible encoding/admission. Polling, queue rejection,
missing peers, restart and rescanning do not renew it. This is a window from worker
observation, not a claim about an incident's relevance lifetime or cold-backlog age.

Admission, frame association and application-attempt count commit together.
There are at most three admitted application attempts per window. Existing
transport retries remain separate, bounded and airtime-accounted. After all frames
become terminal, receipt backoff is 60, 120, then 240 seconds from the latest
transport completion/failure, before retry or exhaustion. Radio success alone is
`awaiting_receipt`. Application retry uses the exact event with a fresh shared peer
counter, recovering loss/overtaking without weakening replay rejection.

Expired, cancelled, invalid/oversized, authorization-denied or exhausted work is
not automatically revived. A new revision/scope starts a new policy context.
Explicit operator retry starts another finite window after active work finishes
or is cancelled, with current source/peer/key/parent checks. Cancel/retry are
operator-only, CSRF-protected and audited. Confirmation binds the displayed
version/policy and queue generation; an old tab cannot act on a replacement batch
just because both actions occurred within the same second.

Backward wall-clock steps do not extend an in-process window or shorten backoff.
Future retained deadlines remain conservative across restart. This is not RTC,
arbitrary forward-step, trusted-time or backup-lineage qualification (#141/#146).

## Receipts and operator evidence

Identical pending exact receipt replies coalesce per peer/source identity.
Reply counter, complete governed batch and association commit together. Their
attempt guard rechecks current key, policy/lineage, exact retained inbox content
and routing, including recovery. Newer versions supersede only unsent old replies.
After a reply finishes or expires, a fresh authenticated event retry may request
another reply to recover loss. Duplicate retries do not consume new-event quota
or bypass the airtime budget.

`GET /api/v1/federation/incident-delivery` is an operator-only, non-cacheable,
keyset-paged read model (default 50, maximum 100). The Federation panel separates
local recording, queue state, radio completion and exact observed remote storage,
with explanations for waits, expiry and blocked work. It exposes no payloads,
locations, secrets, scope fingerprints or credential fingerprints.

Remote human review, responder notification and responder acknowledgement are
explicitly `not_reported`: this protocol does not transmit those facts. The receiving
inbox still requires version-bound human review. Remote resolution cannot silently
resolve a locally monitored incident, even after approved import. Public alert
approval and local responder acknowledgement remain separate workflows.

## Test envelope and limits

The deterministic tests use two opted-in simulated Outposts with
`incident_events:1`, `reconciliation:2` and, for notes, `incident_updates:1`.
They use real app/store/codec/governor/receiver wiring, US/LONG_FAST costing,
default airtime shares and gaps, no initial radio backlog, inactive federation
quiet hours and synchronized virtual clocks. No internet or physical radio is used.

| Short synthetic case | Exact storage receipt |
| --- | --- |
| Create/update/resolve/reopen observed at poll | 25 simulated seconds |
| Current parent plus short note observed at poll | 55 seconds |
| Incident arriving just after poll, including full 5-second phase | 30 seconds |
| Parent plus note, including full poll phase | 60 seconds |

The handheld REPORT-command → reviewed map API feed test separately measures
25 seconds of transport plus an explicitly assumed 10 seconds of human review.
Browser refresh, RF contention/loss, multi-hop topology and actual human response
times are not covered by that number. These are samples within a declared envelope,
not arbitrary-payload/load SLAs or complete G6 qualification.

Fault tests cover loss, counter overtaking, finite exhaustion, pending/interrupted/
unreceipted restart, rollback/cancellation, expiry during waits, fresh/backlog
scheduling, blocked peers, reply coalescing, changed source/key/scope, stale actions
and monitored-incident reconciliation. Browser tests cover the new panel, escaping,
explicit actions and mobile layout.

Remaining boundaries:

- Physical G6, multi-node/WAN-down exercises and hardware activation remain held.
  G6 means mesh-wide and map visibility, not admission or storage alone.
- Quiet hours can prevent prompt delivery and cause expiry. Saturated links,
  unsupported peers and oversized payloads have no 60-second promise. Content is
  not truncated or granted a larger fragment ceiling.
- Out-of-scope/deleted heads stop current local publication; they do not send a
  remote withdrawal/invalidation or erase previously shared public copies.
- An observed receipt does not prove continued retention after remote deletion or
  restore. Missing parent storage cannot be repaired by inventing a receipt;
  catch-up and explicit lineage/node-loss recovery remain separate requirements.
- Existing mixed-version bulk-sync and oversized-page limitations remain. The
  automatic path neither requires nor claims completion of the hourly cycle.

Migration 182 adds policy metadata/indexes and one retained receipt association
per peer/source identity. Payloads and wire capabilities are unchanged. Backups
retain these tables; peer deletion cascades their metadata. Older binaries reject
newer schemas. Deploy only through the approved backup/migration/release procedure.
This implementation did not access/migrate the live store, deploy a release,
restart services/radios, change channels or activate the held node.
