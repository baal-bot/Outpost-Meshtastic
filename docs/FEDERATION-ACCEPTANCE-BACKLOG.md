# Federation acceptance backlog

These tests require two independently installed Outposts and remain deferred until a second
node is available.

## Pairing and discovery

- Discover an Outpost over radio without admitting ordinary radios.
- Discover an Outpost through Meshtastic MQTT when enabled.
- Pair both nodes and confirm fingerprints out of band before trust is granted.
- Restart either node and confirm pairing, replay counters, and transport policy survive.

## Resilience and routing

- Exchange federation traffic over radio only, MQTT only, and automatic fallback.
- Disconnect one node from the internet and route its agent request through the online peer.
- Restore connectivity and confirm queued frames are deduplicated rather than replayed.
- Partition both transports, reconnect, and verify bounded catch-up respects airtime policy.

## Data and mail

- Sync allowed items while excluded or over-radius data remains private.
- Relay encrypted mail in both directions and verify receipts, quotas, and deduplication.
- Reject tampered, expired, unauthenticated, and replayed frames.
- Remove peer trust and confirm subsequent synchronization and relay attempts fail closed.
