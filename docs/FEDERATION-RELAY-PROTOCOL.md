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
- A signing key first seen directly from its paired origin is trusted. A new origin key learned
  through a multi-hop relay is quarantined until the destination operator verifies and trusts or
  rejects its fingerprint. A changed or rejected origin key fails closed.

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
| `route` | Unique node IDs, beginning at origin and ending at sender | Immediate-peer HMAC and local append-only audit |

Unknown fields, malformed bounds, exhausted routes, loops, expired/future messages, bad signatures,
identity conflicts, and disallowed scopes are rejected before storage. Duplicate envelopes return
their retained state and do not consume another storage slot. The origin/destination/idempotency
constraint also prevents a different signed envelope from replacing an existing logical item.

## Custody and recovery

An envelope progresses through `queued`, `forwarding`, and `forwarded` custody states before
`delivered`. `quarantined` waits for origin-key review; `paused` is an operator hold. `expired`,
`rejected`, and `purged` are terminal. Purge removes payload bytes but retains routing metadata and
append-only events for accountability.

Expiry removes payload bytes in every state, including delivered or rejected items, while retaining
the envelope metadata and an expiry event. Until then, every received payload still counts against
the sending peer's byte/item storage ceiling even after custody or delivery acknowledgement.

`RELAY_PUT` transfers the signed envelope. `RELAY_ACK` reports queued custody, quarantine, or final
delivery. Final receipts travel back along the retained previous-hop chain. If a custody ACK is
lost, a forwarding item becomes eligible again after five minutes; the next transfer uses a fresh
paired-peer replay counter. Receiver idempotency makes duplicate paths and retries safe. Delivery
receipts that could not be queued are retried by the relay task after restart.

The queue records whether the selected next hop was the direct destination or another relay and
whether an inbound transfer arrived through observed LoRa or MQTT. All frames still pass through
the global airtime governor.

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

1. Enable only the necessary scopes for a paired peer and set conservative quotas.
2. Create a small test request and inspect its direct/relay selection and custody route.
3. Verify any observed multi-hop origin fingerprint out of band before choosing **Trust**.
4. Pause a peer policy to stop new transfers through it. Pause an item to hold that one envelope.
5. Purge a payload when its operational need ends; the action is irreversible from the dashboard.
6. Review the custody queue, relay events, general audit log, and message-log LoRa/MQTT observations
   when investigating delivery.

Revoking or pausing a federation peer prevents it from being selected as a next hop. Existing
durable metadata remains available for audit and duplicate suppression.

## Verification

`tests/integration/test_federation_relay.py` creates independent A, B, and C databases and exercises
A→B→C partition custody, direct-path preference, quarantined origin keys, propagated receipts,
duplicate deliveries, dropped-ACK recovery, clock skew, signature tampering, loops, rate limits,
pause/resume/purge controls, the operator API, and append-only event enforcement.
