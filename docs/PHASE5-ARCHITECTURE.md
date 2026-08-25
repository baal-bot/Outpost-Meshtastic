# Phase 5: Outpost Federation

Federation makes independently operated Outposts discoverable and pairable while preserving local operator control. Radio and MQTT are alternate transports for one protocol and one trust record; MQTT is not a separate trust domain.

## Trust and discovery

- Radio discovery is available through the private Meshtastic federation port.
- Meshtastic MQTT discovery is optional and disabled by default. Enabling discovery only creates a pending peer record.
- Discovery never pairs, activates, or grants data access to an Outpost.
- Pairing requires approval by operators at both Outposts and confirmation of the same six-digit code.
- Each pairing uses a new 256-bit secret. Re-pairing an active peer requires an explicit key-replacement action.
- Shared secrets are stored locally and are never exposed by peer list or status APIs.

## Wire protocol

- Payloads use canonical CBOR and optional DEFLATE compression.
- Messages are split into at most eight 215-byte fragments.
- Paired traffic uses a truncated HMAC-SHA256 on every fragment.
- Per-peer monotonic counters are persisted. Replayed or older counters are rejected after restart.
- Unsigned frames are accepted only for `HELLO` discovery messages.
- Radio and MQTT feed the same decoder, replay checks, reassembler, quota checks, and peer policy.

## Agent-to-agent services

An Outpost may ask an active peer for a structured, allowlisted service result when its own provider is unavailable. Initial services are weather, public alerts, and public knowledge lookup. A request includes a unique request ID, service name, bounded arguments, expiry, and maximum response size.

Peers return structured results with source provenance, fetch time, and cache age. Requests do not grant access to private mail, precise member positions, operator controls, arbitrary shell commands, prompts, or tools. Selection prefers an active peer advertising the required capability and a fresh successful heartbeat; failover is sequential rather than a mesh-wide broadcast.

## Loop and outage safety

- Relayed requests carry a TTL and origin identifier and may not be recursively relayed after TTL reaches zero.
- Seen request IDs are retained long enough to suppress loops across both transports.
- Outbound work is durable, expiring, bounded, and subject to federation airtime and per-peer quotas.
- Internet and provider availability are advertised separately so an online Outpost can assist an offline peer without implying broader trust.

## Delivery sequence

1. Persistent peers, authenticated framing, replay protection, and operator pairing.
2. Radio discovery and pairing transport.
3. Federation dashboard and peer controls.
4. Optional Meshtastic MQTT discovery and transport.
5. Bounded service queries, peer selection, provenance, and failover.
6. Board, incident, alert, and mail synchronization according to per-peer policy.
