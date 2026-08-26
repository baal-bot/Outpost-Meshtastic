# Federation acceptance backlog

Two-node acceptance began on 2026-08-25 with `!699c2f30` and `!3136b053`. Checked items were
observed over live radios, not only in automated tests.

## Pairing and discovery

- [x] Discover an Outpost over radio without admitting ordinary radios.
- [x] Discover an Outpost through Meshtastic MQTT when enabled.
  The replacement Outpost01 radio `!b2a711ec` was observed with `discovery_transports=["mqtt"]`
  before RF rediscovery; discovery remained untrusted and created only a pending peer.
- [x] Pair both nodes and confirm fingerprints out of band before trust is granted.
  Re-pairing with `!b2a711ec` also validated targeted broadcast bootstrap and durable approval
  receipts across mixed RF/MQTT paths; both databases converged to both approvals and active trust.
- [x] Restart either node and confirm pairing and replay counters survive.

## Resilience and routing

- [ ] Exchange federation traffic over radio only, MQTT only, and automatic fallback.
  Radio-only authenticated alert and weather services passed. MQTT paths remain open.
- [x] Disconnect one node from the internet and route its agent request through the online peer.
  With Outpost01's WAN unavailable but its LAN and Meshtastic radio intact, a peer weather
  request completed through the online `!699c2f30` Outpost over the radio federation path.
- [x] Restore connectivity and confirm queued frames are deduplicated rather than replayed.
  After Outpost01's WAN was restored, local weather and a new peer weather request each produced
  one current result; the earlier offline request was neither replayed nor duplicated.
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
