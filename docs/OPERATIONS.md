# Operations

## Routine checks

- System and Radio dashboards show healthy service and current connectivity.
- The durable outbound queue is bounded and drains when utilization permits. Radio → Outbound queue
  shows queued, transmitting, acknowledgement-waiting, expired, and failed work. Failed work and a
  stale acknowledgement wait can be cancelled without editing SQLite.
- Provider status shows recent success or a clear degraded state.
- Disk space accommodates database, backups, and tiles.
- Journal shows no restart loop, migration/integrity error, or repeated radio flap.

```sh
systemctl is-active outpost
systemctl status outpost --no-pager
journalctl -u outpost --since today --no-pager
curl -fsS http://127.0.0.1:8080/api/v1/health
```

Prometheus metrics are mounted at `/metrics`. Restrict them to trusted networks because labels and
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

The archive is mode 0600. Review it before sharing; secret redaction does not make ordinary radio or
community activity non-sensitive.

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

Prefer a tag or known commit over a moving branch. `deploy/update.sh` stages an isolated release;
the installer takes a verified database snapshot and rolls back automatically if health fails.
Compare `/etc/outpost/config.yaml` with `.dist`, then perform dashboard and mesh round trips. Retain
the prior release and backup until verification is complete. Use `sudo outpost-rollback` for a
compatibility-checked rollback. The command verifies its release metadata before downtime and
automatically restores the matching pre-upgrade database when the older code cannot read the live
schema. It also keeps a pre-attempt safety snapshot and restores it if rollback health checks fail.

## Radio maintenance

Record region, modem preset, channels/keys, identity, and MQTT state before firmware updates. Stop
Outpost while another application needs exclusive serial access:

```sh
sudo systemctl stop outpost
# radio maintenance
sudo systemctl start outpost
```

Then verify serial path, node ID, DM `PING`, channel handling, positions, and telemetry.

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
