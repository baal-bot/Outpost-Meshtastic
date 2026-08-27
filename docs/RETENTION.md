# Data retention and storage

Outpost applies retention once per local day after `store.maintenance_hour`. The Backups page shows
the exact dry-run before any deletion, including eligible rows and an approximate byte count. A
manual run requires operator confirmation. Both scheduled and manual runs create a verified
pre-cleanup database snapshot, delete in small committed batches, perform bounded FTS/vacuum work,
and write an audit event.

Active safety and delivery state is never deleted merely because it is old. Audit evidence is
preserved for the life of the database and is included in rotated backups. Organizations that need
an external evidence archive should export encrypted, validated snapshots according to their own
policy; Outpost never silently replaces audit detail with a summary.

## Default windows

| Setting | Default | Applies to |
|---|---:|---|
| `posts_days` | 90 days | Unpinned threads; a board's `retention_days` overrides this. |
| `mail_days` | 180 days | Local/operator mail, or sooner at its explicit expiry. |
| `member_positions_hours` | 168 hours | Exact member POS shares. |
| `message_log_days` / `message_log_max_rows` | 30 days / 500,000 | Packet and command telemetry; whichever limit removes a row first. |
| `authentication_days` | 30 days | Login-attempt history; expired sessions are removed immediately. |
| `digest_days` | 90 days | Digest-delivery history. |
| `watch_history_days` | 365 days | Terminal incidents/alerts, closed events, and check-ins. |
| `environment_history_days` | 30 days | CAP, earthquake, and SAME review history. |
| `provider_cache_days` | 2 days | Environment caches and federation quota windows. |
| `federation_service_days` | 7 days | Completed, failed, or expired peer-service requests/results. |
| `federation_history_days` | 30 days | Reviewed inbox items, receipts, replay state, and terminal deliveries. |
| `outbound_history_days` | 30 days | Terminal durable outbound work and its attempts. |

The safety-floor replay window separately uses
`security.safety_attempt_retention_hours` (72 hours by default).

## Table policy

`Preserve` means no time-based deletion. `Cascade` means the record follows a parent that has its
own explicit policy. `Retain` means only the completed/historical subset ages out. `Expire` follows
an explicit deadline. `Compact` combines time/row limits or maintains an index.

| Domain | Tables | Policy |
|---|---|---|
| System/security | `schema_version`, `runtime_setting` | Preserve; migration evidence and current settings are naturally bounded. |
| System/security | `web_credential` | Preserve and protect; one current credential record. |
| System/security | `web_session` | Expire at its stored deadline. |
| System/security | `web_login_attempt` | Retain for `authentication_days`. |
| System/security | `message_log` | Compact by age and absolute row ceiling. |
| System/security | `outbound_work` | Retain terminal states; pending, held, sending, and acknowledgement work is protected. |
| System/security | `outbound_attempt` | Cascade with `outbound_work`. |
| System/security | `safety_floor_attempt` | Retain for the configured safety replay window. |
| System/security | `kv` | Expire only keys whose stored deadline elapsed. |
| System/security | `audit_log` | Preserve forever and protect. |
| Members/directory | `member` | Preserve identity/trust history until explicit operator lifecycle action. |
| Members/directory | `member_position` | Expire at its per-share deadline. |
| Members/directory | `channel_dir` | Preserve operator-managed directory. |
| BBS/mail | `board` | Preserve operator configuration. |
| BBS/mail | `thread` | Retain unpinned threads by board/global window; pinned threads are protected. |
| BBS/mail | `post`, `post_fts` and FTS shadow tables | Cascade/compact with their thread and bounded search-index merging. |
| BBS/mail | `read_marker`, `subscription`, `digest_state` | Preserve; bounded by members/scopes/cadences. |
| BBS/mail | `mail` | Retain by age or explicit expiry. |
| BBS/mail | `digest_delivery_log` | Retain for `digest_days`. |
| Watch | `pending_incident_location` | Expire at its workflow deadline. |
| Watch | `incident` | Retain only resolved, false-alarm, or expired incidents; open/monitoring are protected. |
| Watch | `incident_update` | Cascade with its incident. |
| Watch | `alert` | Retain concluded/expired alerts; active alerts are protected. |
| Watch | `alert_ack`, `alert_audience` | Cascade with their alert. |
| Watch | `watch_event` | Retain closed events; open events are protected. |
| Watch | `checkin` | Retain welfare history for `watch_history_days`. |
| Watch | `checkin_solicitation` | Cascade with its event. |
| Environment | `env_cache`, `cap_point_cache` | Expire after `provider_cache_days`. |
| Environment | `cap_alert`, `earthquake`, `same_event` | Retain review/history for `environment_history_days`. |
| Environment | `waypoint` | Preserve operator-managed reference data. |
| Federation | `fed_peer`, `fed_peer_successor` | Preserve trust, pairing, and identity-adoption evidence until operator action. |
| Federation | `fed_cursor`, `fed_service_circuit` | Preserve bounded state per peer/stream/service. |
| Federation | `fed_seen` | Retain replay/deduplication history for `federation_history_days`. |
| Federation | `fed_outbox` | Retain sent or long-expired frames; live frames are protected. |
| Federation | `fed_service_request` | Retain terminal/expired request results for `federation_service_days`; live requests are protected. |
| Federation | `fed_service_usage` | Retain short quota windows for `provider_cache_days`. |
| Federation | `fed_inbox_item` | Retain imported/rejected records; pending human approvals are protected. |
| Federation | `fed_mail_delivery` | Retain terminal deliveries; queued/sent relay state is protected. |
| Federation | `fed_post_delivery` | Retain delivered receipts; queued/sent reconciliation is protected. |

## Writer and recovery bounds

`maintenance_batch_rows` defaults to 250. Each batch is its own SQLite transaction and control is
yielded before another domain can run. Rules are processed round-robin so a large message log
cannot starve expired positions or sessions. `maintenance_max_rows` defaults to 10,000 per run; any
remainder stays visible in the next dry-run and is picked up by a later run. FTS merging is limited
to 16 pages and incremental vacuum to 200 free pages per run.

The snapshot is created before deletion and normal backup rotation then applies. A position that
has disappeared from the live database may therefore remain inside a sensitive backup until that
snapshot rotates out. Restrict backup access and use encrypted off-device storage.

## Capacity planning

Actual storage is displayed because message bodies, radio volume, incident history, and SQLite
page reuse vary. On the current schema, packet history with its indexes is roughly 180 bytes per
row on a representative node: the 500,000-row emergency ceiling is therefore about 90 MB. A
moderately active installation should normally remain below roughly 100–150 MB for the live
database under the default age windows, excluding offline map tiles. This is a planning estimate,
not a hard limit.

Fourteen full 150 MB snapshots require about 2.1 GB. The WAL is transient and may briefly add to
the live size. Deleted pages are reusable immediately but may return to the filesystem gradually
because vacuum work is intentionally bounded. Backups → Live data & retention reports database,
WAL, total backup, free-disk, and per-domain allocation so operators can replace estimates with
their node's measured growth.

The per-domain baseline is saved after each completed maintenance run. Until the first run, growth
is shown as pending. Byte eligibility is proportional to current SQLite page allocation and can
underestimate cascaded/index effects; it is explicitly a preview estimate rather than promised
filesystem recovery.
