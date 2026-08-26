# Troubleshooting

Collect service state, recent journal, health response, device path, and whether failure affects
mesh, dashboard, or only a WAN provider.

## Service will not start

```sh
sudo systemctl status outpost --no-pager
sudo journalctl -u outpost -n 150 --no-pager
/opt/outpost/venv/bin/python -c \
  "from outpost.config import load_config; print(load_config('/etc/outpost/config.yaml'))"
```

Common causes: YAML errors, unknown strict keys, unchanged environment user agent, unsafe no-auth
bind, unwritable database directory, or Python older than 3.12.

## Dashboard unavailable

```sh
curl -v http://127.0.0.1:8080/api/v1/health
ss -ltnp | grep ':8080'
```

If local works, inspect firewall, VLAN/client isolation, bind address, and URL. Do not fix this by
opening the dashboard directly to the internet.

## Login failure

Confirm system time and recent login failures. After repeated failures, wait before retrying. Use a
private browser window to isolate stale cookie/CSRF state. During first setup, inspect the token
state with `sudo outpost-setup-token status`; use `sudo outpost-setup-token reset` if it expired or
was consumed before a permanent password was saved. This local recovery revokes existing sessions.
Do not delete the database to reset auth.

## Radio disconnected

```sh
ls -l /dev/serial/by-id/ /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
id outpost
sudo journalctl -u outpost --since '15 minutes ago' --no-pager
```

Ensure no other client owns serial, the path exists, `outpost` belongs to `dialout`, and the cable
carries data. Firmware can change USB identity or radio channels. Reconnect after fixing the cause.

## `PING` has no response

Try DM with and without `!`. Verify destination, channel, LoRa region/preset, and Radio dashboard.
Busy channels or the governor may delay output. Inspect inbound history and queue before retrying.

## Unknown command

Use `HELP` in DM. A command may be hidden because its module is off, trust is insufficient, or the
channel forbids it. DMs strip an optional prefix; arbitrary prose is not a command.

## Environment unavailable

Check provider status/timestamps, WAN/DNS, clock, node location, user agent, and timeout. Provider
failure should not break BBS or local mesh. A stale-cache success is not current conditions.

## Blank map area

Exploration needs online tiles. The USGS pack covers only configured bounds/zooms. Check
`/tiles/manifest.json`, permissions, browser connectivity, and attribution. Rebuild only within
source usage rules.

## Duplicate incidents

Review the suggested incident before `REPORT!`. If position/report split incorrectly, capture
timestamps, member ID, sequence, and sanitized logs without publishing exact coordinates.

## Federation failure

Verify each node independently, peer/pairing state, transport policy, radio/MQTT, replay errors,
quota, expiry, and clock. Discovery is not pairing. Never bypass authentication to make tests pass.
