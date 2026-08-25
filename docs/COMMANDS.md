# Mesh command reference

Commands are case-insensitive. Configured channels normally require `!`; direct messages accept an
optional prefix. `HELP` lists only commands available to the sender under current trust, module,
and channel policy.

## Basics and identity

| Command | Purpose |
| --- | --- |
| `PING` | Test reachability. |
| `ABOUT` | Show Outpost identity, operator, and disclaimer. |
| `HELP [command]` | List commands or show help. Aliases: `?`, `H`. |
| `WHOAMI` | Show identity and trust. Alias: `ME`. |
| `NAME <handle>` | Claim/change a handle. Alias: `HANDLE`. |
| `WHO` | List recently heard nodes. Alias: `NODES`. |
| `CHANS` / `CHAN <name>` | List channels or request details by DM. |
| `WHERE` | Show current navigation context. |
| `BACK` / `HOME` | Leave one context or clear it. |

## Bulletin boards

| Command | Purpose |
| --- | --- |
| `BOARDS` | List boards. Alias: `BL`. |
| `BOARD <name>` | Recent threads. Alias: `B`. |
| `POST <board> <text>` | Create a thread. Alias: `P`. |
| `READ <n>` | Open a numbered thread. Alias: `R`. |
| `REPLY [thread] <text>` | Reply. Alias: `RE`. |
| `SEARCH <terms>` | Search posts. Alias: `S`. |
| `NEW` / `MORE` | Unread digest / next page. |
| `SUB <board> [cadence]` | Subscribe. |
| `UNSUB <board>` | Unsubscribe. |
| `RMPOST <ref>` | Remove an eligible own post. |

## Private mail

| Command | Purpose |
| --- | --- |
| `SEND <handle> <text>` | Send stored mail. Alias: `SM`. |
| `MAIL` | List inbox. Alias: `MB`. |
| `READMAIL <n>` | Read mail. Alias: `RM`. |
| `REPLYMAIL <text>` | Reply in context. Alias: `RR`. |
| `DELMAIL <n>` | Delete mail. Alias: `DM`. |

Application privacy does not remove radio metadata or endpoint risk. This is not a formally audited
secure messenger.

## Environment and location

| Command | Purpose |
| --- | --- |
| `WX [TODAY|TOMORROW|HOURLY]` | Local weather. Alias: `WEATHER`. |
| `FC [1-5] [-long]` | Forecast. Alias: `FORECAST`. |
| `WARN [number]` | Official alerts/detail. Alias: `WARNINGS`. |
| `SUN` | Sunrise, twilight, moon. Alias: `ASTRO`. |
| `QUAKE [number]` | Nearby earthquakes/detail. |
| `POS [handle]` | Show an allowed member position. |
| `POS SHARE full|coarse|off` | Set sharing preference. |
| `WP [name]` | Waypoint detail. Alias: `WAYPOINT`. |
| `WP ADD <name>` | Add from an attached position. |
| `WPS [radius_km]` | Nearby public waypoints. |
| `DIST <waypoint>` | Range and bearing. Alias: `DISTANCE`. |

Coordinate results can link to a phone map. Member positions and public waypoints have different
privacy expectations.

## Incidents and alerts

| Command | Purpose |
| --- | --- |
| `REPORT <details>` | File a public incident, using attached position when available. |
| `REPORT -nopos <details>` | File without position. |
| `REPORT -wp <name> <details>` | Use a waypoint. |
| `REPORT! <details>` | File after an explicit duplicate warning. |
| `INCIDENTS` / `INC <number>` | List incidents / show detail. |
| `CONFIRM <inc>` | Independently confirm. |
| `DISPUTE <inc> [note]` | Flag concern. |
| `ALERT <severity> <inc> <headline>` | Responder alert. |
| `ACK <inc> [note]` | Acknowledge an active alert. |

Reports are community records, not emergency calls.

## Welfare

| Command | Purpose |
| --- | --- |
| `OK [note]` | Check in as okay. |
| `HELPME [note]` | Record need-help and notify responders. |
| `ROSTER` | Privacy-limited summary. |
| `ROSTER?` | Responder-visible names. |
| `EVENT OPEN <policy> <name>` | Open an event. |
| `EVENT CLOSE` | Close it. |

The dashboard provides a reviewed solicitation preview before sending to actual members.

## Behavior

- Long results are shortened or paged to protect airtime.
- Replies can be delayed by quiet hours, utilization, priority, or multipart pacing.
- Unknown commands receive a help hint rather than being treated as prose.
- `OP STATUS|RM` is restricted; dashboards are preferred for high-impact operations.
