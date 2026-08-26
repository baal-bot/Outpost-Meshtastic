# Federation

Federation connects independently operated Outposts without one central administrator. It remains
experimental until the two-node hardware backlog is complete.

## Trust model

- Radio and Meshtastic MQTT are transports for one peer trust record.
- Discovery creates a pending directory entry only.
- Pairing uses X25519 and a six-digit out-of-band confirmation code.
- Paired frames use canonical CBOR, bounded fragments, HMAC, and persistent replay counters.
- Federation frames use an RF/MQTT-compatible channel broadcast carrier; pair-specific encryption,
  authentication, replay protection, and application receipts provide confidentiality and delivery.
- The Federation transfer panel reports the path actually observed on inbound frames (LoRa or
  MQTT), authenticated counters, durable queue state, retries, recovered deliveries, and last
  successful activity. Outbound frames are labelled as mesh broadcasts because radio firmware may
  carry the same frame over RF, MQTT, or both.
- Per-peer policy controls boards, incidents, alerts, mail, quotas, and transport.
- Enabling federation on a board automatically adds its slug to every currently paired peer's
  policy; disabling it removes the slug. Per-peer settings can still narrow that selection.
- A new thread received from an allowed paired peer is imported automatically; later replies to
  that admitted thread continue without per-message approval. Disallowed boards remain rejected.
- Incident sharing is separately disabled by default. When enabled, located incidents are exported
  only within the configured peer-centered radius; without peer coordinates, only locationless
  incidents are eligible. Member positions, pending location reports, and waypoints are never
  federation streams.
- Imported items can be quarantined for operator review.

## Before pairing

Both nodes should pass independent health, backup, dashboard, and radio checks. Verify the other
operator outside the mesh pairing path. Agree on shared data, incident radius, retention, transport
cost, and revocation.

## Pairing workflow

1. Enable federation on both nodes.
2. Discover or identify the peer by mesh node ID.
3. Initiate pairing from one dashboard.
4. Review the pending request on the other.
5. Compare the six-digit code over an independent trusted channel.
6. Approve only when codes and node identities match.
7. Configure least-privilege sync/mail policy.
8. Send one small test and inspect status, inbox, audit, and airtime.

Never approve based only on a claimed Outpost name.

## MQTT

Meshtastic's default MQTT infrastructure may improve reach but is not a new trust domain. Discovery
is untrusted and paired payload authentication is still required. Broker availability/retention,
topic visibility, and radio uplink/downlink settings affect behavior. Keep it off unless understood.

## Peer services and mail

An offline node may request a bounded allowlisted service such as weather or public alerts from a
capable peer. IDs, expiry, limits, origin, and TTL prevent loops. Peers cannot invoke arbitrary
tools, shell commands, operator controls, private positions, or unconstrained prompts.

Federated mail uses a mail-specific key derived from peer trust material, with receipts, quotas,
expiry, and duplicate suppression. Both features have automated coverage but need two-node testing.

## Revocation

If identity, keys, or operation are in doubt, disable transports and remove trust. Confirm sync and
relay fail closed. Re-pair with fresh material only after investigation. Never publish pairing
secrets in screenshots, issues, logs, or peer exports.

Run the [acceptance backlog](FEDERATION-ACCEPTANCE-BACKLOG.md) when a second node is available.

## Content identity and radio replacement

Federated content is retained when a peer disconnects, is unpaired, or is forgotten. Trust removal
must not erase community records. Threads from an identity that is no longer in the peer directory
are labelled as former-peer content and remain readable.

Replacing a radio creates a new cryptographic peer identity. After pairing that identity normally,
an operator may use **Content identity → Adopt history** on the Federation page to declare it the
successor to a former content origin. This action is explicit and audited; matching Outpost names
never trigger an automatic merge. Original mesh IDs remain on stored records for provenance, while
the successor relationship supplies current attribution and prevents re-exported historical items
from being imported as duplicates. Trust and replay counters are never inherited.
