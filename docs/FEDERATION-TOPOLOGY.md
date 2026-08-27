# Federation topology and health

The Federation page combines peer identity, authenticated transport observations, sync/delivery
state, and optional coarse locations into one regional map and list. The list is authoritative when
tiles or locations are unavailable; the basemap is never required for health decisions.

## Location privacy

Location is denied by default and is not part of unsigned discovery. An operator must enable it
separately for each active or paused paired peer, enter the local coordinates, and select a
precision from 1–100 km. Outpost rounds the coordinates to that precision before placing them in an
authenticated `TOPOLOGY_UPDATE` frame. Enabling incident-radius sharing does not enable topology
location sharing, and its coordinates are never reused for this purpose.

The receiver stores whether the remote peer explicitly shared a location, the coarse coordinates,
precision, generation time, and receipt time. Only a peer whose trust state is still `active` can
appear as a marker. Disabling sharing sends a paired-peer HMAC-authenticated update with no location,
which removes the remote marker. Unpairing, rejecting, or forgetting a peer also removes it from the
map. The local sharing change is recorded in the audit log without putting coordinates in the audit
detail.

`TOPOLOGY_UPDATE` uses the paired peer's HMAC and persistent replay counter. It is sent directly to
one target identity over the normal airtime-governed federation carrier. Updates with unknown
fields, future timestamps, invalid coordinates/precision, stale versions, or a non-active sender
fail closed. A current update is sent after a policy change and then at most once every six hours.

Coarse sharing reduces precision but does not make a location anonymous. Operators should use a
regional center rather than a sensitive facility coordinate and grant it only to peers that need
regional topology awareness.

## Identity states

The topology list retains identities even when they cannot be mapped:

- `discovered` is an unsigned directory observation with no trust or location access;
- `pairing` is exchanging keys or waiting for operator confirmation;
- `active` is mutually approved and recently observed;
- `stale` is active trust with no observation for 24 hours;
- `paused` and `rejected` reflect explicit operator controls;
- `successor` labels an active identity that adopted one or more former content origins;
- `adopted` is the retained predecessor identity and points to its successor; and
- `forgotten` is a minimal tombstone created when a rejected peer record is deliberately removed.

Tombstones contain only mesh ID, last node name, time, and actor. They prevent the overview from
pretending that a deliberately forgotten identity never existed. A later rediscovery creates a new
pending current record; it does not restore keys or trust.

## Health projection

Each current identity reports available observed transports, the last successful inbound LoRa or
MQTT path, the path preferred from that evidence, last seen/sync times, and one combined backlog for
quarantined sync, durable board/mail delivery, pending peer services, and signed relay custody.
Degraded reasons are explicit: stale observation, overdue configured sync, delivery errors, or
recent rejected authenticated traffic. The peer detail exposes only safe delivery counts, service
names, sharing policy, relay state, and audit action metadata. Shared secrets, pairing material,
payloads, raw errors, and audit details are never returned by the topology API.

Clicking a map marker or list identity opens the same detail. The list includes discovered,
pairing, rejected, adopted, and forgotten identities that must never become markers. Community Watch
incidents are excluded by default; the operator must select **Show incidents** to fetch and add that
temporary layer. Incident details remain in Community Watch.

## Offline behavior

The shared map controller first uses installed local tiles and otherwise attempts its configured
online basemap. When both are unavailable, coordinates, markers, keyboard controls, selection, the
identity list, and peer detail remain functional. Attribution and the offline-basemap state stay
visible.

## API and verification

- `GET /api/v1/federation/topology` returns the safe identity/health projection.
- `PUT /api/v1/federation/topology/peers/{mesh_id}` changes that peer's local location-sharing
  policy and queues a fresh advertisement.

`tests/integration/test_federation_topology.py` covers default denial, coarse opt-in sharing,
authenticated receipt/revocation, safe API output, paths, backlog, stale health, successor/adopted
records, and forgotten tombstones. The federation browser test covers trusted-only markers, the
list alternative, incident opt-in, detail context, and browser health. Physical multi-node location
exchange and long-duration stale/recovery behavior remain field-acceptance work.
