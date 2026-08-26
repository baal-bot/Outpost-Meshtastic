# Dashboard and API

The dashboard is a local operator console served by the Outpost process. It is responsive for
phones and desktops and uses bundled assets so its interface remains available without WAN access.

## Sections

- **Overview:** identity, service/radio state, message activity, weather, and operational summary.
- **Members:** actual members, discovered radios, trust/approval controls, and member map.
- **BBS and Mail:** boards, threads, moderation, stored mail, and message detail.
- **Watch:** incident map/list, monitoring state, alerts, acknowledgements, and welfare events.
- **Environment:** separate environmental map, weather/forecast, official alerts, earthquakes,
  astronomy, and editable waypoints.
- **Radio:** connection state, firmware/node details, telemetry, durable outbound state and
  cancellation, airtime, reconnect, and MQTT.
- **Federation:** peer directory, pairing, transport policy, per-path transfer telemetry, durable
  retry/recovery health, sync policy/inbox, services, and relay mail.
- **Backups/System/Settings:** health, retention, backup/restore, identity, emergency policy, AI, and
  other operator controls.

Discovered radios are not members. Member counts, welfare recipients, and member-map markers should
use admitted members only.

## Weather provenance

Weather cards label station observations, near-term forecasts, and model estimates separately.
They show the provider, valid time/age, and cached state; missing measurements render as unavailable
rather than as zero. Peer-service weather is additionally marked as peer-provided while preserving
the original provider and observation/forecast kind. The weather API includes a `measurements`
object with the availability and provenance metadata for each value.

## Authentication

The first login requires the short-lived token shown by `sudo outpost-setup-token show`, followed
by a permanent password of at least 12 characters. The token is consumed by that login. Completing
setup invalidates the bootstrap session and requires a clean sign-in. Sessions expire according to
`web.auth.session_hours`; state-changing requests require the session's CSRF token.

## Appearance

Settings → Appearance offers Outpost Dark, high-contrast Daylight, low-light Night Ops, and a
Follow System option. The preference is stored in the current browser rather than the Outpost
database so a wall display, phone, and operator workstation can each use the appropriate mode.
Night Ops reduces map brightness; it does not change incident severity or provider data.

## API

The JSON API is rooted at `/api/v1`. Important read surfaces include:

- `/api/v1/health` and `/api/v1/status`
- `/api/v1/config`
- `/api/v1/dashboard/overview`
- `/api/v1/members`, `/api/v1/members/map`, and `/api/v1/mesh/messages`
- `/api/v1/mesh/queue` and `/api/v1/mesh/airtime`
- `/api/v1/boards`, `/api/v1/mail`, `/api/v1/incidents`, `/api/v1/events`
- `/api/v1/environment/weather`, `/forecast`, `/alerts`, `/earthquakes`, `/waypoints`
- `/api/v1/backups`, `/api/v1/audit`, and federation endpoints under `/api/v1/federation`

The source of truth for paths and request models is `src/outpost/web/api.py`; the API is pre-release
and has no compatibility promise. Browser authentication uses an HTTP-only cookie. Clients making
state changes must first establish a session and send the CSRF token expected by the API. Do not
build unattended automation by embedding the operator password.

## Maps

Incident, environment/waypoint, and member maps are intentionally separate. Online interactive
tiles allow exploration; `/tiles/...` serves the local regional fallback. Markers expose bounded
info cards appropriate to their domain. Member sharing preferences still govern member positions.

## Operator safety

High-impact flows use previews, confirmation phrases, disabled acknowledged states, or explicit
approval. Preserve those safeguards when changing the UI. A successful HTTP response should also
produce visible state change or feedback; silent actions are operationally unsafe.
