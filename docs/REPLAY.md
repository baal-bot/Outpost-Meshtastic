# Mesh traffic replay and operator drills

`outpost-replay` runs retained inbound mesh traffic through the production command graph without
opening a Meshtastic connection or writing to the source database. It is intended for regression
comparison, tabletop practice, airtime-policy checks, and sanitized defect reproduction.

## Safety boundary

Replay is a separate executable; `radio.transport` cannot select it. Every run creates a new
scratch SQLite database, injects packets through `SimulatedRadioLink`, and uses `VirtualClock`.
The status API, situation briefing, and every dashboard page identify replay/drill mode. The
dashboard banner states that RF and MQTT transmission are disabled and that mutations affect only
scratch state.

The source SQLite connection uses query-only mode and closes before replay begins. `--scratch-db`
must name a file that does not exist. Without that option the scratch database is temporary and is
deleted after the command or drill ends. The harness refuses an existing database rather than
risking overwrite.

Configured environment and AI providers are disabled by default so retained message content cannot
leave the host. The result records that limitation. `--allow-provider-access` is an explicit opt-in
for a controlled exercise whose provider boundary has already been reviewed.

## Deterministic regression run

Select by packet-log ID, time, or both. Time accepts a Unix timestamp or ISO-8601 instant. At most
the newest 1,000 matching inbound packets are selected by default; `--limit` is bounded at 100,000.

```sh
sudo outpost-replay run /var/lib/outpost/outpost.db \
  --since 2026-08-01T00:00:00Z --until 2026-08-02T00:00:00Z \
  --preset LONG_FAST --region US --output /var/lib/outpost/replay-result.json
```

Each message result includes:

- input and resolved command, including tolerant-resolution mode;
- effective member trust and the router/admission decision;
- rendered response, traffic class, part count, and drop reason;
- simulated text/data sends, destinations, and payload hashes; and
- source record/time plus the virtual-clock radio profile.

The JSON contains no wall-clock generation time or scratch path, so repeating the same corpus,
configuration, region, and preset produces comparable output. It includes the Outpost version and
a SHA-256 fingerprint of the effective source configuration. Compare two revisions with a normal
JSON-aware diff tool.

Older retained rows remain replayable for text commands. Results list missing schema fields and
packet limitations. Routing acknowledgements cannot correlate unless their original outbound state
is represented in scratch data. Provider results and stateful BBS/mail results can also differ from
the historical response because replay starts from a clean store seeded only with member trust,
handle, and reviewed PKI state.

## Redacted bundles

Never attach a live database to an issue. Export the smallest relevant range:

```sh
sudo outpost-replay export /var/lib/outpost/outpost.db \
  --start-id 8100 --end-id 8180 \
  --output /var/lib/outpost/issue-74.replay.json
```

Exports use mode `0600`, pseudonymize sender/destination IDs, replace handles with synthetic labels,
coarsen positions to roughly 1 km by default, and always strip binary payloads and PKI public keys.
Use `--strip-bodies` to remove message text as well. Increase location coarsening with
`--coarsen-meters`; values below 100 metres are rejected. Review the resulting JSON before sharing
because traffic timing, channel choice, command text, trust levels, and radio measurements may
still be sensitive.

The bundle carries explicit redaction/fidelity limitations and can be replayed directly:

```sh
outpost-replay run issue-74.replay.json --config config/config.local.yaml
```

## Dashboard drill

Drills bind only to loopback by default and generate a random, ephemeral Administrator credential
inside the scratch database:

```sh
sudo outpost-replay drill /var/lib/outpost/outpost.db \
  --start-id 8100 --end-id 8180 --speed 60 --port 8081
```

Open the printed URL and sign in as `drill` with the printed password. `--speed 60` compresses one
hour of recorded spacing into one minute. When injection finishes, the dashboard remains available
for acknowledgement, update, resolution, alert, welfare, and after-action practice until the
process is stopped. Those actions remain inside the scratch database and all outbound packets stay
in the simulated link.

A non-loopback bind requires `--allow-remote` and uses trusted local HTTP; restrict it to an
isolated exercise LAN or encrypted VPN. Use `--output` to save the deterministic replay result when
the drill stops, and `--scratch-db` only when retained drill state is deliberately required.
