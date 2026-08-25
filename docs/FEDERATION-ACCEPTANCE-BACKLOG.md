# Federation acceptance backlog

Two-node acceptance began on 2026-08-25 with `!699c2f30` and `!3136b053`. Checked items were
observed over live radios, not only in automated tests.

## Pairing and discovery

- [x] Discover an Outpost over radio without admitting ordinary radios.
- [ ] Discover an Outpost through Meshtastic MQTT when enabled.
- [x] Pair both nodes and confirm fingerprints out of band before trust is granted.
- [x] Restart either node and confirm pairing and replay counters survive.

## Resilience and routing

- [ ] Exchange federation traffic over radio only, MQTT only, and automatic fallback.
  Radio-only authenticated alert and weather services passed. MQTT paths remain open.
- [ ] Disconnect one node from the internet and route its agent request through the online peer.
- [ ] Restore connectivity and confirm queued frames are deduplicated rather than replayed.
- [ ] Partition both transports, reconnect, and verify bounded catch-up respects airtime policy.

Live testing found that 225–233-byte application payloads were not reliably acknowledged by the
test radios, while payloads at 188 bytes were. Routine weather responses use compact wire keys and
restore descriptive keys after receipt, and all federation fragments are capped at 188 bytes.
Multipart retry remains an open resilience test rather than being assumed reliable from nominal
packet limits.

## Data and mail

- [x] Sync an approved board bidirectionally, including replies to local and remote threads,
  without duplicate posts. Verified over live radios between `!699c2f30` and `!3136b053`;
  durable item receipts/retries and automatic Web UI refresh were also observed.
- [ ] Sync allowed items while excluded or over-radius data remains private.
- [x] Relay encrypted mail in both directions and verify receipts, routing, and deduplication.
  Web operator mail to a named remote member and the member's `RR` back to the originating
  Outpost's web-only `@operator` mailbox passed over live radios. Per-peer quota enforcement
  remains covered by automated tests; a destructive live quota-exhaustion run is deferred.
- [ ] Reject tampered, expired, unauthenticated, and replayed frames.
- [x] Remove peer trust and confirm subsequent synchronization and relay attempts fail closed.
  Live revocation of `!3136b053` erased the shared secret, cleared both approvals, reset replay
  counters, and returned the peer to pending; subsequent trusted traffic was not admitted.
