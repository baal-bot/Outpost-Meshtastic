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
- **Radio:** connection state, firmware/node details, telemetry, queue, airtime, reconnect, MQTT.
- **Federation:** peer directory, pairing, transport policy, sync policy/inbox, services, relay mail.
- **Backups/System/Settings:** health, retention, backup/restore, identity, emergency policy, AI, and
  other operator controls.

Discovered radios are not members. Member counts, welfare recipients, and member-map markers should
use admitted members only.

## Authentication

The first login requires the generated journal password and then a replacement of at least 12
characters. Sessions expire according to `web.auth.session_hours`. State-changing requests require
the session's CSRF token. Do not share browser sessions among operators.

## API

The JSON API is rooted at `/api/v1`. Important read surfaces include:

- `/api/v1/health` and `/api/v1/status`
- `/api/v1/config`
- `/api/v1/dashboard/overview`
- `/api/v1/members`, `/api/v1/members/map`, and `/api/v1/mesh/messages`
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
