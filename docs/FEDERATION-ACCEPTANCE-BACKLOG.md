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

- [ ] Sync allowed items while excluded or over-radius data remains private.
- [ ] Relay encrypted mail in both directions and verify receipts, quotas, and deduplication.
- [ ] Reject tampered, expired, unauthenticated, and replayed frames.
- [ ] Remove peer trust and confirm subsequent synchronization and relay attempts fail closed.
