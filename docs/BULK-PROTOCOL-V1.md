# Bulk IP protocol v1: B1 contract

**Implemented: message codec, configuration validation and transaction-owned replay/work
primitives. No HTTPS listener, synchronization worker or domain-import integration exists yet.**
B1 follows the [#159 architecture decision](BULK-BACKHAUL-DECISION-2026-09-09.md).
Remaining work: B2 shared ingress, B3 HTTPS, B4 reconciliation, B5 operator controls and
B6 software qualification. The new protocol is not advertised to existing peers.

## Encoding and authentication

The IP format is independent of radio CBOR/fragments and counters. It uses a strict UTF-8
JSON envelope with exactly these fields. Extra/duplicate fields are rejected.

| Field | Value |
| --- | --- |
| `format`, `version` | `outpost-bulk`, integer `1` |
| `kind`, `operation` | `request` or `response`; `manifest`, `fetch` or `receipt` |
| `source`, `destination` | Distinct lowercase `!1234abcd` mesh identities |
| `generation` | 64 lowercase hex characters derived from current pairing and both identities |
| `sequence` | Integer 1 through 2^63−1; responses echo the request sequence |
| `request_id` | 32 lowercase hex characters derived from request direction and sequence |
| `request_digest` | `null` for requests; full SHA-256 of the exact complete request for responses |
| `payload_digest` | Full SHA-256 of decoded payload bytes, in lowercase hex |
| `payload` | Standard padded base64 of a bounded UTF-8 JSON object |
| `mac` | Full HMAC-SHA-256 over the context and canonical envelope without `mac` |

Canonical envelope JSON sorts field names lexicographically, has no whitespace, uses normal
JSON lowercase literals and plain decimal integers, and escapes non-ASCII characters. All
envelope string values are ASCII. Payload bytes are authenticated exactly as supplied; receivers
do not normalize them before authentication. The encoder emits compact, sorted JSON with
ASCII escaping. Retrying uses persisted original bytes. JSON nesting is limited to 12 levels
and 4,096 values; duplicate keys, non-finite numbers and invalid Unicode are rejected.
The complete-message and decoded-payload limits are checked before their respective JSON parse.
B3 must additionally enforce the complete-message limit while receiving the HTTP body.

The current mutually approved 32-byte pairing secret is `S`. `J(x)` means canonical JSON;
`HMAC` below is HMAC-SHA-256. Labels include the literal trailing NUL byte:

```text
generation = hex(HMAC(S,
  "outpost-bulk-v1-generation\0" || J(sorted([source, destination]))))
key(source, destination) = HKDF-SHA256(
  ikm=S, salt="outpost-bulk-v1\0",
  info="authentication\0" || J([source, destination]), length=32)
request_id = first_32_hex_characters(HMAC(key(request_source, request_destination),
  "outpost-bulk-v1-request-id\0" || uint64_big_endian(sequence)))
mac = hex(HMAC(key(source, destination),
  "outpost-bulk-v1-message\0" || J(envelope_without_mac)))
```

This uses the existing dependency's standard
[HKDF implementation](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions.html#hkdf)
and [RFC 5869](https://www.rfc-editor.org/rfc/rfc5869.html). Keys are separated by protocol and
direction. The generation token is public authentication context, not a replacement secret.
Responses reverse authentication direction and bind the exact operation, sequence, request ID
and full request digest. B3 must verify TLS independently and route only through this protocol.
Link confidentiality belongs to B3's verified TLS connection.

The [frozen vectors](../tests/fixtures/bulk-v1.json) contain synthetic keys and three complete
request/response pairs, constructed independently with standard-library HKDF-Extract/Expand
and HMAC operations and checked against the production codec. These are interoperability
fixtures, not an independent security audit or live-peer run.

## Operations and content

Requests contain a 32-hex `cycle`. Epochs retain the producer's 32-hex lineage token. Scope
and record digests retain the existing 16-hex reconciliation representation. Envelope/page
operation hashes use full SHA-256 and must not be truncated.

| Operation | Request fields besides `cycle` | Successful response `result` fields |
| --- | --- | --- |
| `manifest` | `epoch`, `scope`, `after`, `snapshot`, `limit` | `cycle`, `epoch`, `scope`, `after`, `snapshot`, `next`, `done`, `items` |
| `fetch` | `epoch`, `scope`, `items` | `cycle`, `items` |
| `receipt` | `page_digest`, `items` | `cycle`, `page_digest` |

An initial manifest may use `null` epoch, scope and snapshot; after is nonnegative and limit
is 1–8. Later requests bind the retained epoch/scope/snapshot. Manifest items contain exactly
`stream`, `uid`, `revision`, `digest`; revisions increase between the requested after cursor
and returned next cursor. A completed page's next cursor equals its snapshot. Responses
respect the requested limit and snapshot.

Fetch requests contain 1–8 references. Each response record adds `epoch` and `payload`, or
`epoch` and `unavailable: true`. The response accounts for the exact requested identities,
revisions, digests and epoch. Changed producer content requires another scoped page.

Records are restricted to original-producer boards, incidents and plain incident notes.
Payloads reuse existing public record schemas and digest rules. The codec checks the
producer-prefixed UID, content bound and unchanged digest. Alerts, mail, members, welfare,
credentials, maps, models, backups and arbitrary files are excluded. **Structural validity
does not grant sharing/import permission.** B2 must apply current board, module, geographic,
note-support, parent, quota and review rules in the shared domain transaction.
B2 must recheck those permissions before returning a cached response as well; the replay
ledger alone cannot authorize disclosure of a previously permitted payload.

Receipt items add `epoch` and `state` (`stored` or `duplicate`) to each reference. Their
`page_digest` is the full digest of the exact fetch response whose records the receiver
committed. B2/B4 must verify this relationship against durable work. A receipt response
confirms the receipt operation; it does not indicate human approval, notification, responder
acknowledgement or continued remote retention.

Responses contain exactly `status` and `result`. Success is `ok` with the result above.
`reset` and `rollback` carry only current `epoch` and `scope`; `denied` has an empty result.
These outcomes can be committed and recovered on an exact retry. `busy`, `storage_full` and
`unavailable` are transient empty results: they do not advance receive state or clear the
pending request. The same request continues with its remaining attempt allowance.

## Replay, persistence and finite limits

Migration **187** adds `fed_bulk_state` and protective triggers without changing existing
domain rows or radio counters. One row per peer holds its current generation, independent
transmit/receive sequences, exact pending request, persistent attempt count, and last committed
inbound request digest/response. Each side may independently initiate requests; a response
uses the initiating direction's sequence without allocating another one.

| Resource | Hard limit |
| --- | --- |
| Complete message | 196,608 bytes (192 KiB) |
| Decoded JSON payload | 131,072 bytes (128 KiB) |
| Page | Eight records, each at most 12,000 encoded record bytes |
| Current peer states/configured endpoints | 32 |
| Retained message bytes per peer | One request plus one response, at most 393,216 bytes |
| Aggregate retained message bytes | 8,388,608 bytes (8 MiB), enforced by SQLite triggers |
| Attempts per pending operation | Five, reserved durably before each attempt |

The byte cap covers message blobs; fixed metadata and SQLite page/index overhead are separate
and bounded by row count. It is not a total database/disk quota. B4's additional checkpoint
and queue metadata must have explicit limits before implementation.

New requests must have the next contiguous sequence. Exact retries of the last committed
request recover its exact response; changed content at that sequence, older sequences and
gaps are rejected. Senders cannot prepare different work until they commit the current
response. Receiving the next request therefore permits replacing the previous response
without unlimited operation history. Derived request IDs cannot be reused at a different
sequence. Domain revision/rejection receipts remain independently owned and must never be
pruned using this response-compaction rule.

At a cap, admission fails and rolls back; replay protection is never evicted for space.
Sequence exhaustion fails without wrapping. Unpairing/key replacement retires the old secret
and deletes unusable IP work; peer deletion cascades. Pauses and approval holds with the same
key preserve replay history. Disabling bulk or removing an endpoint does not clear it.
Restored identity fencing denies all ledger operations.

Each ledger row also binds its operator-selected endpoint/TLS settings by digest. Changing
the address, port, expected certificate identity/pin, TLS file paths, listener or test-only
mode blocks pending work and response acceptance without erasing counters. B5 must provide
an explicit audited rebind that preserves replay history. B3 must additionally detect file
content changes and revalidate actual TLS state at the connection boundary.

`BulkLedger` requires the database's currently owned writer transaction. B2 must include
shared domain effects and the IP operation receipt in that transaction; cancellation/rollback
undoes both. No network I/O belongs inside it. B1 tests use synthetic effects to exercise
this boundary; actual domain integration remains B2.

## Configuration and remaining activation work

`fed.bulk.enabled` defaults to `false`; old YAML remains valid. B1 validates reserved
configuration but creates no runtime service even when set to `true`. Configuration acceptance
is not successful TLS provisioning or an available connection.

Settings include `listen_address`, `listen_port` (8444), absolute `certificate_file`,
`private_key_file` and `trust_root_file`, and up to 32 `peers`. Each peer supplies `mesh_id`,
literal `address`, `port`, exact `certificate_identity` and `public_key_sha256` (SHA-256 of
DER SubjectPublicKeyInfo). Initial addresses are restricted to RFC1918 IPv4 and IPv6 ULA.
URLs, hostnames as endpoints, wildcard/listen-all, public, multicast, link-local and mapped
IPv6 addresses are rejected. `test_only_loopback` requires every address to be loopback.

The ledger also requires enabled federation/bulk, a configured peer, current mutually
approved pairing, matching commissioned local identity and no recovery fence. It never reads
TLS files or opens sockets. B3 must implement certificate/identity/pin verification, clock
checks, private-root lifecycle, no redirects/proxies, endpoint-change revalidation and bounded
HTTP receiving. B5 supplies named-operator configuration and audited retry controls. An address
or authenticated IP traffic does not prove RF reachability.

## Validation

Codec/configuration and two-store tests cover frozen vectors, altered/oversized messages,
private streams, wrong peers/generations/operations, exact retries, lost responses, restarts,
cancellation, full ledgers, same-key holds and key replacement. IP sequence 2 leaves radio
sequence 1 valid. The schema-186 upgrade test compares every existing table's records and
checks database/foreign-key integrity afterward. Executable evidence is in
`tests/unit/test_federation_bulk_format.py`, `tests/unit/test_federation_bulk_config.py` and
`tests/integration/test_federation_bulk_contract.py`. These tests open no production store,
radio, TLS connection or model provider.

The September 9 B1 verification passed **570 tests**, including the new contract cases and
existing configuration, federation, store ownership, radio-power and encrypted-recovery
regressions. Coverage is **93% for the codec and 96% for the ledger**; both now have a 90%
per-file CI floor. Formatting, lint, strict typing, repository documentation/requirement
gates and the locked-runtime package smoke passed. The package contains both modules and
migration 187, defaults bulk off and passes fresh-store/reopen/ownership checks. This is
local Python 3.13 software evidence; full GitHub CI remains a separate release gate.
