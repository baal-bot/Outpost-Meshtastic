# Federation

Federation connects independently operated Outposts without one central administrator. Two physical
Outposts have exercised pairing, board/incident catch-up, peer services, MQTT discovery, and mail;
the feature remains experimental until the complete acceptance backlog is run.

## Trust model

- Radio and Meshtastic MQTT are transports for one peer trust record.
- Discovery creates a pending directory entry only.
- Pairing uses X25519 and a six-digit out-of-band confirmation code.
- Paired frames use canonical CBOR, bounded fragments, HMAC, and persistent replay counters.
- Federation frames use an RF/MQTT-compatible channel broadcast carrier; pair-specific HMAC,
  replay protection, and application receipts provide authentication and delivery. Confidentiality
  on the carrier depends on the configured Meshtastic channel; encrypted relay mail adds its own
  end-to-end protection.
- The Federation transfer panel reports the path actually observed on inbound frames (LoRa or
  MQTT), authenticated counters, durable queue state, retries, recovered deliveries, and last
  successful activity. Outbound frames are labelled as mesh broadcasts because radio firmware may
  carry the same frame over RF, MQTT, or both.
- Tampered authentication tags, unknown peer secrets, replayed counters, expired messages, identity
  mismatches, and policy violations are rejected before import. The transfer panel reports bounded
  counts and fixed safe reason labels; it never displays payloads, keys, or raw exception details.
- Reconciliation after an outage uses a stable timestamped snapshot and keyset cursor. Pages are
  limited to eight manifest entries and each catch-up cycle is bounded by
  `fed.max_items_per_cycle`; unfinished snapshots resume in later airtime-governed batches. New
  records cannot shift the cursor and cause older records to be skipped.
- Durable board items retain one authenticated wire counter across retries. This lets multipart
  fragments received on different attempts complete the same logical item; a duplicate of an
  already imported item can regenerate only its receipt and cannot import content twice.
- Per-peer policy controls boards, incidents, alerts, mail, quotas, peer information services, and
  an optional operator review date. The peer card records who last applied the policy and flags an
  overdue review without automatically interrupting a field link.
- Pairing completion opens the same sharing wizard used for later edits. Discovery-only, BBS-only,
  mutual-aid, and full-partner presets fill the editable policy. The final step shows every
  before/after value and a plain-language data-sharing summary before anything changes.
- Enabling federation from a board's BBS control automatically adds its slug to every currently
  paired peer's policy; disabling it removes the slug. Selecting a local-only board inside the
  per-peer wizard instead requires an explicit global-eligibility confirmation and assigns that
  board only to the peer being edited. The board and peer changes commit atomically.
- A new thread received from an allowed paired peer is imported automatically; later replies to
  that admitted thread continue without per-message approval. Disallowed boards remain rejected.
- Incident sharing is separately disabled by default. When enabled, located incidents are exported
  only within the configured peer-centered radius; without peer coordinates, only locationless
  incidents are eligible. Member positions, pending location reports, and waypoints are never
  federation streams.
- Imported items can be quarantined for operator review.
- Incident imports preserve every contributing origin UID and append each accepted, stale,
  conflicting, or advisory update to an immutable provenance timeline. Bounded duplicate
  suggestions always require a human decision. A remote resolution cannot silently close an
  incident that this Outpost is monitoring. See [incident reconciliation](INCIDENT-RECONCILIATION.md).
- Signed store-and-forward can move bounded envelopes through explicitly authorized paired peers.
  Direct destinations are preferred, each peer has separate scope/storage/rate/airtime policy, and
  relay-origin keys learned indirectly require operator review. At the destination, incidents enter
  normal reconciliation, authorized read-only service requests return signed receipts, and failed
  local dispatch remains visible and retryable. Ordinary relay payloads and routing metadata are
  visible to custodians; only the `opaque` scope is suitable for ciphertext. See the [relay protocol
  and threat model](FEDERATION-RELAY-PROTOCOL.md).
- The topology health view maps only active peers that explicitly sent an authenticated coarse
  location. Unsigned discovery, incident-sharing coordinates, and names never imply map consent.
  List-only identity states, transport health, backlog, policy/audit context, optional incident
  layering, and tile-free fallback are documented in [federation topology](FEDERATION-TOPOLOGY.md).

## Before pairing

Both nodes should pass independent health, backup, dashboard, and radio checks. Verify the other
operator outside the mesh pairing path. Agree on shared data, incident radius, retention, transport
cost, and revocation.

For a physical acceptance host, keep test dependencies out of the production service environment:

```sh
sudo ./deploy/install.sh
./deploy/install-test-host.sh
```

The supported helper and optional browser setup are documented in
[Installation](INSTALLATION.md#federation-acceptance-host).

## Pairing workflow

1. Enable federation on both nodes.
2. Discover or identify the peer by mesh node ID.
3. Initiate pairing from one dashboard.
4. Review the pending request on the other.
5. Compare the six-digit code over an independent trusted channel.
6. Approve only when codes and node identities match.
7. Use the sharing wizard to choose a least-privilege preset or custom policy, review its exact
   diff, and optionally schedule a review date.
8. Send one small test and inspect status, inbox, audit, and airtime.

Never approve based only on a claimed Outpost name.

## MQTT

Meshtastic's default MQTT infrastructure may improve reach but is not a new trust domain. Discovery
is untrusted and paired payload authentication is still required. Broker availability/retention,
topic visibility, and radio uplink/downlink settings affect behavior. Keep it off unless understood.
The limited controls on this page and the full configurator on **Radio** share live firmware state;
changes made in either view are reflected in the other without clearing advanced settings or stored
credentials.

## Peer services and mail

An offline node may request a bounded allowlisted service such as weather or public alerts from a
capable peer. IDs, expiry, limits, origin, and TTL prevent loops. Peers cannot invoke arbitrary
tools, shell commands, operator controls, private positions, or unconstrained prompts.

Federated mail uses a mail-specific key derived from peer trust material, with receipts, quotas,
expiry, and duplicate suppression. These paths have automated and two-Outpost field coverage; the
long-duration acceptance backlog still remains. The encrypted envelope also carries a bounded opaque
conversation ID, message kind, participant, and reply address. Those fields preserve the exact
peer/member return route without inventing a member named `operator` or parsing a display label.

The dashboard's operations inbox groups this traffic into audited conversations. Outpost-to-Outpost
messages addressed to `@operator` are web-only system traffic. Mail addressed to a named member is
still member mail; when that member uses `RR`, their handle remains the sender and the response
returns to the initiating operator conversation. Operator replies preview the stored address and
observed LoRa/MQTT paths before sending. Read/unread and archive state are local operator workflow
metadata and are never federated.

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
