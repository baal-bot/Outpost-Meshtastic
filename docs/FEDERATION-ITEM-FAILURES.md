# Oversized federation items — #162 / #135 prerequisite

An authorized report can be too large for the existing transport. That is now an
explicit failure, not successful reconciliation and not permission to truncate it.
This is software/simulated-radio qualification on temporary stores, not deployment,
physical RF, power-loss or G6 latency acceptance.

## Protocol and compatibility

Both peers must advertise integer `reconciliation: 2` and `item_failures: 1` to
exchange negative ITEM responses. HELLO advertises the failure capability; it
does not add a content stream or change discovery scope tokens. Older peers see
no unknown negative extension. A modern sender still records local diagnostics
when the receiver lacks the extension; that receiver retains its previous retry
behavior without the remote explanation. The legacy timestamp adapter remains
outside this diagnostic/recovery contract.

`FrameTooLarge`, a `FrameError` subtype, identifies only the fragment-ceiling
failure. Other encoding, authentication, queue admission or radio errors are not
reported as oversized content. The complete envelope remains subject to **188
bytes per authenticated application frame, 170 body bytes, at most eight fragments
/ 1,360 encoded bytes**. Compression and all governor policies are unchanged.

A capable sender returns an authenticated ITEM containing `stream`, `uid`,
`epoch`, `revision`, `cycle`, the canonical payload's 16-hex-digit `digest`, and
`failure: "payload_too_large"`. It includes neither `payload` nor `unavailable`.
It rechecks current scope and capability before queuing this explanation. The
receiver requires current active trust and local scope, the outstanding page's
cycle/epoch and item identity, and a revision no older than advertised. Equal
revisions must match the advertised digest; a newer failed revision becomes a
floor for subsequent responses to that pending item. Conflicting equal-revision
digests, ambiguous envelopes, unknown codes and unnegotiated failures are rejected.
Delayed failures for content already stored are ignored.

## Durable state and recovery

The source owns a small diagnostic journal in the existing `fed_cursor` table:
stream `_encoding_failures`, direction `send`. One row per peer contains at most
eight recent failed identities with stream, UID, producer epoch/revision, digest,
code, observation time and a saturating repeat count. Repeated failures coalesce;
an old export cannot overwrite the current producer head's diagnostic. There are
no report bodies, authors, coordinates or secrets in this journal. IDs remain
operational metadata; this is not anonymization.

Successful admission of the same or a newer revision to the durable governed
queue clears that identity's diagnostic. This means **encoding/admission succeeded**,
not that the radio sent it or another node received it. A crash between admission
and diagnostic clearing may leave a conservative stale warning until a later
attempt. This bounded recent journal is not an exhaustive delivery ledger: older
entries can be evicted and an unrequested/deleted record's diagnostic may remain.

The receiver retains failure metadata on its existing outstanding page, at most
eight entries. Other valid items on that page remain receivable. When only failed
items remain missing, status is `blocked_payload`; the page, previous cursor,
watermark, used-item count and round budget stay intact. A negative response never
creates an inbox item, producer-revision receipt, successful-sync timestamp or
positive ITEM_RECEIPT, even if an older inbox version of that UID exists.

The existing monotonic `sync_retry_minutes` interval paces another request for the
still-missing items. Duplicate failures do not resend them or postpone the next
deadline. Reopening a blocked checkpoint waits one retry interval; wall-clock
steps cannot turn that retry deadline into a tight loop or a future-date stall.
Metadata stays visible while retrying, and retries do not replenish the cycle's
item/round budget. These are rate bounds, **not a finite attempt limit**. Peer
liveness and governor quiet hours, quota, queue bounds and expiry still apply;
this is not a delivery-time guarantee or a critical-reserve bypass.

A corrected newer payload that fits follows normal quarantine and human review.
Already-received neighboring records are not requested again. Producer scope reset
and lineage/rollback checks still work while payload-blocked. Source unavailability
retains its existing meaning: no content receipt and no replica-withdrawal proof.

## Operator visibility, performance and limits

Federation transfer health distinguishes recent source encoding failures from
receiving-page failures, showing identity/revision/code only. A checkpoint write
is no longer counted as a successful sync, and protocol counters are not labeled
as transmitted frames. The dashboard escapes identities and
uses existing refresh scheduling and theme primitives. An older backend without
the new fields still renders normally. No additional timer, provider request or
recurring database query was added: status reuses its existing cursor read.

On sending, each item adds a bounded journal lookup; an oversized item also checks
its current producer head before the small transactional diagnostic update.
Diagnostic writes and radio admission are separate transactions, never a writer
lock held across network I/O. No schema migration or live configuration change is
required for this extension. Deploy a matching upgraded pair during an approved
maintenance window; the installed appliance was not restarted for this work.

**Later pages can still wait behind an oversized record.** Neither arbitrary-size
transport nor non-starving bulk progress is solved here. A legal 500-character
Unicode note, long incident snapshot, or large merged-origin list may not fit.
Content is preserved locally; this change does not shorten it automatically or
claim that it reached the community. Prompt event publication, priority delivery,
revision-specific positive receipts, expiry/supersession and G6 remain #135.

Evidence: `tests/integration/test_federation_item_failures.py` exercises real
codec/application dispatch and durable outbox with simulated radios, metadata
bounds, malformed/stale/conflicting responses, revocation, existing older inbox
versions, reopen, clock steps, unchanged budgets, repaired content and scope reset.
The production coverage gate separately enforces this diagnostic service. The
federation resource-limit browser regression checks escaping, capped rendering,
older-backend compatibility, phone/desktop layout and all three themes.
