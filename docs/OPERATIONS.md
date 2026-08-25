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

Before restore, validate the exact file, understand newer data loss, notify operators, use the
displayed confirmation phrase, and verify afterward. Restore creates a safety backup and audit row.

## Upgrades

Prefer a tag or known commit over a moving branch. Back up, review changes, rerun the installer,
compare `/etc/outpost/config.yaml` with `.dist`, and perform a dashboard and mesh round trip. Retain
the prior source revision and backup until verification is complete.

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

During WAN outages, avoid repeated forced refreshes. Cached data may be stale—use its timestamp.
Radio federation can still work. Peer/AI fallback must remain bounded and source-attributed.
