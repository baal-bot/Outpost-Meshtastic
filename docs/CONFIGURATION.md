# Configuration

Outpost reads YAML from `OUTPOST_CONFIG`, defaulting to `config/config.yaml` for source runs. The
system service sets `/etc/outpost/config.yaml`. Models reject unknown keys and invalid ranges.

## Safe editing

```sh
sudo cp /etc/outpost/config.yaml /etc/outpost/config.yaml.before-edit
sudoedit /etc/outpost/config.yaml
sudo systemctl restart outpost
sudo journalctl -u outpost -n 80 --no-pager
```

The dashboard exposes selected settings; YAML remains the complete configuration surface. Runtime
settings saved in the dashboard should be reviewed alongside the file.

## Sections

### `node`

- `name`: instance name such as `Pittsburgh Outpost`.
- `short_name`: 1–4 UTF-8 bytes.
- `operator_contact`: public operational contact.
- `timezone`: IANA zone such as `America/New_York`.
- `units`: `metric` or `imperial`.
- `location`: optional `{lat, lon}` for maps and providers.
- `disclaimer`: safety text included where appropriate.

Avoid publishing a private residential coordinate for a personal installation.

### `radio`

Select `serial`, `tcp`, or `ble` and configure the matching subsection. Serial is preferred for a
fixed node. `federation_portnum` is a private Meshtastic application port from 256–511. Only
explicitly trusted bridge node IDs belong in `bridge_node_ids`.

### `channels`

Keys are Meshtastic channel indices 0–7. Each controls display name, AI access, BBS policy (`none`,
`read_only`, `full`), official alerts, and report acceptance. This does not replace the radio's
region, modem preset, PSK, or channel setup.

### `modules`

Disable unused modules. Environment and AI have additional validation: environment needs a real
user agent, and an enabled non-null AI provider needs a base URL.

Module flags are startup policy, not live switches. Change `modules.<name>.enabled` in the YAML (or
the matching environment override) and restart `outpost.service`. The dashboard marks disabled
navigation and capability cards, direct page visits explain the disabled state, and module APIs
return `409 module_disabled` with `restart_required_to_change: true`.

| Module | Disabled behavior |
|---|---|
| `bbs` | Board commands, moderation, digests, board APIs, and federated board exchange stop. |
| `watch` | Incident/check-in/alert commands, schedulers, APIs, and federated safety records stop. |
| `env` | Weather/forecast/geo commands, provider polling, APIs, and peer provider service stop. |
| `fed` | Discovery, pairing, sync, relay mail/services, background work, and federation APIs stop. |
| `ai` | AI navigation/API capability is unavailable; core deterministic features are unaffected. |

Mail, identity, member directory, radio safety/governance, backups, and system operations are core
surfaces and do not depend on the BBS flag.

### `airtime`

The governor enforces an Outpost-originated budget, reserve, channel-utilization ceiling, minimum
gap, multipart pacing, queue capacity, and traffic-class shares. Budget plus reserve may not exceed
20% and must stay below the utilization ceiling. Conservative settings protect other mesh users.

### `env`

Set a descriptive `user_agent` with operator contact. Refresh, cache age, timeout, earthquake
radius, and review magnitude are bounded. SAME is disabled unless configured with RTL-SDR tools
and county codes.

### `watch`

Controls position freshness, duplicate radius/window, self-resolution, alert repetition, and
escalation stages. Emergency keyword matching is off by default. Enabling it requires a response
policy, false-positive review, and operator training.

### `fed`

Controls frame limits, discovery/sync cadence, batch size, stale peers, incident radius, and MQTT.
Discovery never grants trust. See [Federation](FEDERATION.md).

### `web`

Password authentication is the expected LAN mode. `auth.mode: none` is valid only on loopback. Do
not expose port 8080 directly to the internet; use an authenticated VPN or carefully configured TLS
proxy for remote administration.

### `store`

Controls SQLite path, maintenance hour, retention, and backup count. The service account needs
write access to the database parent directory. `retention.member_positions_hours` controls how
long the latest exact POS share is retained (default 168 hours, range 1–720). Each new share stores
its own deletion time. Past-due positions are excluded immediately from maps, commands, welfare,
and safety processing; daily maintenance physically deletes them. Existing positions from before
the expiry migration are expired on upgrade and require a fresh member share to reappear.

## Environment overrides

Nested keys use double underscores; values are JSON-decoded when possible:

```sh
OUTPOST__WEB__PORT=8081
OUTPOST__NODE__UNITS='"imperial"'
OUTPOST__MODULES__FED__ENABLED=true
OUTPOST__NODE__LOCATION='{"lat":40.4406,"lon":-79.9959}'
```

Use a protected systemd environment file or override. Never commit credentials, channel keys, or
precise private positions.

## High-risk settings

- MQTT carries Meshtastic traffic through infrastructure outside local control.
- AI can be incorrect and must not autonomously trigger emergency action.
- Provider data can be delayed; preserve timestamps and provenance.
- Federation trust permits bounded exchange and must be reviewed per peer.
