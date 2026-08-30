# Signed federation store-and-forward

Outpost can transfer a bounded envelope through explicitly authorized federation peers when its
destination is not directly reachable. The implementation is deliberately custody-oriented: a
peer accepts one durable item, acknowledges custody, and later transfers it onward. Direct delivery
to an authorized destination is always selected before another relay.

This feature is experimental. The partition, duplicate-path, clock-skew, malicious-input, quota,
reconnect, and receipt paths are covered by automated multi-node simulation. Multi-hop physical
radio and power-loss qualification still remain.

## Security boundary

- The origin generates a persistent Ed25519 key and signs the canonical CBOR core. The signature
  covers origin, destination, scope, idempotency key, creation time, expiry, hop limit, and payload.
  Relays cannot change those fields without invalidating the envelope ID and signature.
- Every transfer also uses the existing paired-peer HMAC frame, durable replay counter, target node
  ID, bounded fragmentation, and application acknowledgement. This authenticates the immediate
  custodian even when the Meshtastic carrier is a broadcast.
- The custody route is appended at each honest relay and checked for repeats, sender mismatch, and
  hop exhaustion. It is protected on each link by the paired-peer HMAC, but it is not part of the
  origin signature because future hops do not exist when the origin signs. A malicious authorized
  custodian can drop or delay an envelope and may misreport earlier route metadata. Relay policy is
  therefore a trust decision, not a Byzantine-consensus protocol.
- Ordinary `incident`, `request`, and `receipt` payloads are not end-to-end encrypted by this
  protocol. Relay operators can observe their payload bytes plus origin, destination, scope, times,
  size, hop limit, and route. Meshtastic channel encryption protects the carrier only according to
  the radios' channel configuration. Use the `opaque` scope only for ciphertext produced by a
  higher-level protocol; the dashboard does not create arbitrary opaque envelopes.
- A signing key first seen directly from its paired origin is trusted. A key learned through a
  different peer is recorded only as a candidate: it never becomes the authoritative pin, and its
  envelope is quarantined until the destination operator verifies and uses or rejects it.
- A key that differs from an existing pin is quarantined and creates an operator-inbox decision
  naming both fingerprints and their presenting peers. A changed or rejected key therefore fails
  closed without becoming an unrecoverable denial.
- A planned rotation carries a successor key proof signed by the previously trusted key. The proof
  binds the origin node ID and successor public key, allowing a peer with the predecessor pin to
  advance it without weakening first-contact rules.

## Signed core and wire envelope

| Field | Bound | Protection |
| --- | --- | --- |
| `origin`, `destination` | Meshtastic `!xxxxxxxx` node IDs | Origin signature |
| `scope` | `incident`, `request`, `receipt`, or `opaque` | Origin signature and per-peer allowlist |
| `idempotency_key` | 1–64 characters, unique per origin/destination | Origin signature and database uniqueness |
| `created_at`, `expires_at` | At most seven days; five-minute future-skew tolerance | Origin signature |
| `hop_limit` | 1–4 custodians | Origin signature and per-hop enforcement |
| `payload` | 1–800 encoded bytes | Origin signature; confidentiality only for `opaque` ciphertext |
| `envelope_id` | First 128 bits of SHA-256 over canonical signed core | Recomputed at every receiver |
| `origin_public_key`, `origin_signature` | 32-byte Ed25519 key, 64-byte signature | Verified before durable acceptance |
| `rotation_from_public_key`, `rotation_signature` | Optional 32-byte predecessor and 64-byte successor proof | Previous trusted key signs the origin ID and successor key |
| `route` | Unique node IDs, beginning at origin and ending at sender | Immediate-peer HMAC and local append-only audit |

Unknown fields, malformed bounds, exhausted routes, loops, expired/future messages, bad signatures,
identity conflicts, and disallowed scopes are rejected before storage. Duplicate envelopes return
their retained state and do not consume another storage slot. The origin/destination/idempotency
constraint also prevents a different signed envelope from replacing an existing logical item.

## Custody and recovery

An envelope progresses through `queued`, `forwarding`, and `forwarded` custody states before
`delivered`. At the destination, delivery is not acknowledged until the decoded payload has passed
through its registered local-domain handler. `quarantined` waits for origin-key review; `paused` is
an operator hold. `expired` and `purged` are terminal. `rejected` is normally terminal, except when
the queue identifies a retryable local-dispatch failure. Purge removes payload bytes but retains
routing metadata and append-only events for accountability.

Expiry removes payload bytes in every state, including delivered or rejected items, while retaining
the envelope metadata and an expiry event. Until then, every received payload still counts against
the sending peer's byte/item storage ceiling even after custody or delivery acknowledgement.

`RELAY_PUT` transfers the signed envelope. `RELAY_ACK` reports queued custody, quarantine, or final
delivery. Final receipts travel back along the retained previous-hop chain. If a custody ACK is
lost, a forwarding item becomes eligible again after five minutes; the next transfer uses a fresh
paired-peer replay counter. Receiver idempotency makes duplicate paths and retries safe. Delivery
receipts that could not be queued are retried by the relay task after restart.

### Local-domain dispatch

Destination dispatch runs inside the same database transaction that records accepted custody. Each
handler receives the verified origin identity and decoded payload under a five-second bound. A
handler has its own savepoint: if it raises or times out, its partial writes roll back, the envelope
is retained as `rejected` with `dispatch_status='failed'`, and the dashboard exposes the error and a
**Retry local dispatch** action. A successful retry changes the envelope to `delivered` and makes
the final receipt eligible again. Payloads left at a destination by an older release are recovered
once after upgrade through this same path.

- `incident` accepts either a normal federation incident item (`stream`, `uid`, `digest`, `payload`)
  or the incident object directly. Its UID must belong to the signed origin. It enters the existing
  incident-origin, provenance, merge, stale-update, and resolution-withholding logic.
- `request` carries `request_id`, `service`, `args`, and optional `expires_at`. Admission uses the
  immediate paired peer's existing service permissions, hourly request quota, concurrency limit,
  provider circuit, response-byte ceiling, and response-airtime ceiling. Execution is queued and
  bounded rather than holding the custody transaction open on a network provider.
- `receipt` carries the request and request-envelope IDs, service, outcome, result, provenance, and
  error. The receiver accepts it only when it matches a locally originated request envelope and the
  signed response origin is that request's destination.
- `opaque` is intentionally retained without local dispatch. It remains the extension point for a
  higher-level protocol.

Allowing `request` in a peer policy automatically allows `receipt`; a request without its return
path would permit work to be performed while making the outcome structurally undeliverable.

The queue records whether the selected next hop was the direct destination or another relay and
whether an inbound transfer arrived through observed LoRa or MQTT. All frames still pass through
the global airtime governor.

### Origin-key recovery and rotation

Origin-key changes require a current step-up session. In **Federation → Signed store-and-forward**,
an operator can compare a quarantined candidate with the origin out of band, then **Use candidate**
to replace the pin or **Reject candidate** to retain the denial. **Forget pin** removes only the
authoritative pin; retained envelopes and append-only audit events remain, and the next direct
origin proof can establish the key again. These actions record both a relay event and a general
audit-log entry.

For a planned change, the origin operator uses **Rotate local key** while the previous private key
is still available. New envelopes carry its signed successor proof, so peers holding the previous
trusted fingerprint update automatically. After identity loss or reinstallation, no predecessor
signature exists; the first new envelope is quarantined and each destination operator must verify
and explicitly use the candidate. The operator inbox contains the old and new fingerprints and the
peer that presented each.

## Resource controls

Relay is disabled for every peer by default. Each paired peer has an independent policy containing:

- enabled and operator-paused state;
- allowed content scopes;
- simultaneous stored-item and stored-byte ceilings;
- accepted/forwarded items per hour; and
- relay airtime seconds per hour.

Inbound admission applies the sending peer's scope, rate, and storage policy. Outbound custody uses
the selected next hop's scope, rate, and airtime policy. These limits are additional to global
fragment, queue, retention, and airtime limits. Denials increment bounded hourly usage and append a
safe event without retaining the rejected payload.

## Operator workflow

On **Federation → Signed store-and-forward**:

1. Enable only the necessary scopes for a paired peer and set conservative quotas. Enabling
   `request` also enables its `receipt` return path.
2. Create a small test request and inspect its direct/relay selection and custody route.
3. Verify any observed multi-hop origin fingerprint out of band before choosing **Use candidate**.
4. Pause a peer policy to stop new transfers through it. Pause an item to hold that one envelope.
5. Purge a payload when its operational need ends; the action is irreversible from the dashboard.
6. Review the custody queue, relay events, general audit log, and message-log LoRa/MQTT observations
   when investigating delivery.

Revoking or pausing a federation peer prevents it from being selected as a next hop. Existing
durable metadata remains available for audit and duplicate suppression.

## Verification

`tests/integration/test_federation_relay.py` creates independent A, B, and C databases and exercises
A→B→C partition custody, destination incident reconciliation, authorized request/receipt dispatch,
handler rollback/time bounds/retry, opaque non-dispatch, direct-path preference, forged-pin
resistance, identity-regeneration recovery, signed successor rotation, propagated receipts,
duplicate deliveries, dropped-ACK recovery, clock skew, signature tampering, loops, rate limits,
pause/resume/purge controls, the step-up-protected operator API, and append-only event enforcement.
