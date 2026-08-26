# Operations

## Routine checks

- System and Radio dashboards show healthy service and current connectivity.
- The outbound queue is bounded and drains when utilization permits.
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

## Incident and welfare use

Review welfare recipients before sending; only actual members should be eligible. Do not imply
professional dispatch. Close test events. For alerts, verify severity, linked incident, channels,
escalation stages, and acknowledgement threshold before approval.

## Privacy and retention

Retention reduces old posts, mail, and message logs; it does not create an organizational policy.
Document who can approve members, view locations, export rosters, access backups, and pair peers.
Exact member POS shares expire after `store.retention.member_positions_hours` (168 hours by
default). The Members map shows the source, share time, visibility, and scheduled deletion, with
audited controls to delete one share or purge expired rows. Expired positions remain excluded after
restore, and member positions are not federated. Backups and welfare CSV exports must be treated as
sensitive because they may contain location data captured before expiry.

During WAN outages, avoid repeated forced refreshes. Cached data may be stale—use its timestamp.
Radio federation can still work. Peer/AI fallback must remain bounded and source-attributed.
