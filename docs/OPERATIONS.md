# Operations

## Routine checks

- System and Radio dashboards show healthy service and current connectivity.
- The durable outbound queue is bounded and drains when utilization permits. Radio → Outbound queue
  shows queued, transmitting, acknowledgement-waiting, expired, and failed work. Failed work and a
  stale acknowledgement wait can be cancelled without editing SQLite.
- Provider status shows recent success or a clear degraded state.
- **Overview → Subsystem health** shows each task's failure domain, last progress or error, total
  failures/restarts, open-circuit state, and next retry. A degraded optional task does not imply the
  radio router is down.
- Disk space accommodates database, backups, and tiles.
- Journal shows no restart loop, migration/integrity error, or repeated radio flap.

```sh
systemctl is-active outpost
systemctl status outpost --no-pager
journalctl -u outpost --since today --no-pager
curl -fsS http://127.0.0.1:8080/api/v1/health
curl -fsS http://127.0.0.1:8080/api/v1/diagnostics/status
```

The minimal health endpoint and systemd watchdog reflect core offline progress. Optional provider
and restartable-local failures remain visible in the authenticated dashboard and loopback-only
diagnostics while the core continues serving mesh traffic.

Prometheus metrics use canonical `/metrics` (HTTP 200); `/metrics/` remains compatible. Restrict
them to trusted networks because labels and
traffic characteristics can reveal operational patterns.

## Operator access and shift changes

Use one named account per human operator. Administrators should review **Access** periodically for
disabled staff, unexpected source addresses or clients, stale sessions, and accounts without MFA.
Do not use mesh member handles as shared dashboard credentials; mesh trust and web authority are
separate controls.

For a shift handoff, create or enable the incoming operator's own account and have them change the
temporary password. When access ends, disable that account; doing so revokes all of its sessions.
Use **Sign out everywhere** after a lost terminal, and reset the affected password. Keep recovery
codes offline and cross them off after use. If every dashboard credential is lost, a local root
operator can run `sudo outpost-setup-token reset` to recover the original `operator` administrator;
this invalidates dashboard sessions and clears that bootstrap account's MFA enrollment. Other named
accounts, their MFA credentials, and audit history are preserved.

Create a credential-redacted diagnostic archive with:

```sh
sudo outpost-diagnostics --output /var/lib/outpost/outpost-diagnostics.zip
```

The default mode-0600 archive contains platform/schema/storage evidence, systemd and loopback-only
task/radio/provider health, and recent warning/error lines. It omits the broader journal and never
queries message bodies. Use `--include-journal` only when necessary. Review every archive member
before sharing; redaction does not make ordinary radio or community activity non-sensitive.

## Backups

The dashboard can create, validate, download, and restore backups. Rotation protects disk space;
local copies do not protect against device loss. Periodically store a validated backup in encrypted
off-device storage.

Before restore, validate the exact file, understand newer data loss, notify operators, and use the
displayed confirmation phrase. Outpost then enters visible maintenance mode, blocks and drains API,
radio, transport, and scheduled work, creates a verified safety snapshot, restores, and restarts.
The Backups page tracks the recovery through restart even if the restored snapshot invalidates the
operator's session. A failed restore automatically returns to the pre-restore snapshot.

## Upgrades

Use a verified `v*` tag for production. `deploy/update.sh` authenticates the release artifacts and
exact source commit before staging an isolated release; the installer takes a verified database
snapshot and rolls back automatically if health fails. Development refs are explicitly unsigned.
Compare `/etc/outpost/config.yaml` with `.dist`, then perform dashboard and mesh round trips. Retain
the prior release and backup until verification is complete. Use `sudo outpost-rollback` for a
compatibility-checked rollback. The command verifies its release metadata before downtime and
automatically restores the matching pre-upgrade database when the older code cannot read the live
schema; code-only failures do not replace live data. It also keeps a pre-attempt safety snapshot
and restores it after a failed schema rollback. HTTP, trusted-proxy, and direct-HTTPS deployments
share one loopback probe implementation, and probe configuration errors fail before downtime. The
verification and compromise-response procedures are in [Releases](RELEASES.md).

## Radio maintenance

Record region, modem preset, channels/keys, identity, and MQTT state before firmware updates. Stop
Outpost while another application needs exclusive serial access:

```sh
sudo systemctl stop outpost
# radio maintenance
sudo systemctl start outpost
```

Then verify serial path, node ID, DM `PING`, channel handling, positions, and telemetry.

For normal changes, use **Radio → Configure radio**. Before confirmation, Outpost reconnects to read
fresh device state and shows a redacted field diff plus operational impact. Apply is bound to that
exact preflight for ten minutes and follows `preflight → applying → reconnecting → verifying →
verified/failed`; a write is successful only after a new SDK connection reads the expected values
back from the radio. Concurrent configurator writes are rejected. Outpost limits device roles to
`CLIENT` and `CLIENT_BASE`, keeps serial enabled, removes unsafe frequency/duty-cycle overrides when
saving a LoRa profile, and will not disable a channel referenced by Outpost policy. Generated
channel keys are shown once and are never retained in the database or audit log.
The LoRa Frequency Slot is separate from messaging channel slots 0–7: `0` uses Meshtastic's
primary-channel-name calculation, while an explicit slot selects the shared RF frequency for every
messaging channel. Preflight validates the region/preset slot count and shows the automatic or
explicit effective slot and center frequency before any write.

Outpost keeps a redacted pre-change snapshot and lifecycle record in SQLite. If firmware rejects a
field, a multi-write operation fails partway, or fresh readback differs, it attempts to restore the
in-memory pre-change radio configuration while the transport remains reachable. A failed or
interrupted operation remains visible with recovery instructions. Connect directly over USB or
Bluetooth with a Meshtastic client when the radio moved off-network; restore the displayed
non-secret values and restore channel keys or MQTT credentials from the operator's separate secret
store. Outpost deliberately cannot recover secrets that were never persisted.

The compact MQTT controls under **Federation** and the full MQTT form under **Radio** operate on the
same radio state. Compact edits preserve credentials and advanced flags. Outpost always enables
Meshtastic MQTT channel encryption; JSON and map reporting can disclose identity or location and
should remain disabled unless the broker and operating policy explicitly require them.

Queued work survives a normal service restart or host power loss until its traffic-class TTL. An
item interrupted at the radio-call boundary may be transmitted again after recovery because the
radio cannot provide a transactional exactly-once boundary; its outbox ID still produces only one
packet-history row. Direct messages already awaiting a radio acknowledgement are not resent on
restart. Safety alerts recover before lower-priority bulk work and remain subject to the airtime
budget and emergency reserve.

## Incident and welfare use

Review welfare recipients before sending; only actual members should be eligible. Do not imply
professional dispatch. Close test events. For alerts, verify severity, linked incident, channels,
escalation stages, and acknowledgement threshold before approval.

## SAME receiver operations

The Environment page is the normal receiver console. `listening` means both decoder processes are
running. `up` additionally means audio crossed the signal threshold. `no_signal` means the silence
window was exceeded; `backoff` means a failed or stalled pipeline is waiting for its bounded
restart. The status API includes `same_receiver`, with last audio/signal/decode times, last process
error, restart count, next restart, and a bounded stderr tail.

```sh
curl -fsS http://127.0.0.1:8080/api/v1/status
sudo journalctl -u outpost --since '30 minutes ago' --no-pager | grep 'SAME receiver\|SAME new'
rtl_test -d 51231467 -t
```

Stop Outpost before opening the dongle with `rtl_test` or another SDR application. If the carrier
is absent, verify antenna, device serial, NWR frequency, automatic/manual gain, and PPM correction.
If audio stalls, the pipeline restarts after `audio_stall_seconds`; repeated failures back off to
`restart_max_seconds` and remain visible instead of silently disabling the receiver.

Every decoded header is deduplicated. Out-of-area messages and required tests/demos are retained as
non-actionable evidence. County-matched live warnings require explicit approval and are
deduplicated against NWS CAP by event, SAME location code, and expiry. Never approve a warning
solely because it appeared through a second source.

## Privacy and retention

Retention covers completed operational, provider, federation, authentication, BBS, mail, and watch
history. It does not replace an organizational policy. Review the dry-run and per-domain growth in
Backups → Live data & retention; the complete rules and storage estimates are in
[Data retention and storage](RETENTION.md). Scheduled cleanup takes a verified pre-cleanup snapshot
and releases the SQLite writer between small batches. Audit evidence, active workflows, pairing
state, federation approval queues, and unsent deliveries are protected.

Document who can approve members, view locations, export rosters, access backups, and pair peers.
Exact member POS shares expire after `store.retention.member_positions_hours` (168 hours by
default). The Members map shows the source, share time, visibility, and scheduled deletion, with
audited controls to delete one share or purge expired rows. Expired positions remain excluded after
restore, and member positions are not federated. Backups and welfare CSV exports must be treated as
sensitive because they may contain location data captured before expiry.

During WAN outages, avoid repeated forced refreshes. Cached data may be stale—use its timestamp.
Radio federation can still work. Peer/AI fallback must remain bounded and source-attributed.
