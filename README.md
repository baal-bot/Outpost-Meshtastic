# Outpost

Outpost is an offline-first community server for Meshtastic networks. It currently provides
radio-aware BBS, private mail, member identity, digests, moderation, backups, and an operator
dashboard while enforcing a shared airtime budget.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,radio]'
cp config/config.example.yaml config/config.local.yaml
# For a source checkout, change router.intents_file to config/intents.yaml.
OUTPOST_CONFIG=config/config.local.yaml .venv/bin/python -m outpost
```

The service deliberately starts without a radio. Check `http://127.0.0.1:8080/api/v1/health`.
The complete requirements are in `docs/outpost-spec/`.

## Raspberry Pi installation

On a Raspberry Pi OS or Debian host with Python 3.12+, `venv`, `pip`, and a supported
Meshtastic radio attached:

```sh
sudo ./deploy/install.sh
systemctl status outpost
```

The installer creates the restricted `outpost` service account, installs Outpost into
`/opt/outpost`, places configuration in `/etc/outpost`, and starts the hardened systemd unit.
Edit `/etc/outpost/config.yaml` to select the radio and instance identity, then run
`sudo systemctl restart outpost`. Upgrades preserve that file and write current defaults to
`/etc/outpost/config.yaml.dist` for comparison. Runtime data and backups live under
`/var/lib/outpost`.

For a second Outpost, clone this repository on the target, install its OS prerequisites, and
run the same installer. Node-specific configuration, local overrides, databases, map tiles,
and environment files are excluded from Git. `pyproject.toml` is the authoritative dependency
manifest; the requirements files provide conventional pip entry points.

The dashboard is available on port 8080. On the first boot, retrieve the one-time initial
operator password from `journalctl -u outpost`, sign in, and replace it immediately. Do not
expose the dashboard directly to the public internet.
