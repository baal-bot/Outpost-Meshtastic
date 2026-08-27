# Dashboard and API

The dashboard is a local operator console served by the Outpost process. It is responsive for
phones and desktops and uses bundled assets so its interface remains available without WAN access.

## Sections

- **Overview:** identity, service/radio state, message activity, weather, and operational summary.
- **Members:** actual members, discovered radios, trust/approval controls, and member map.
- **BBS and Mail:** boards, threads, moderation, and a conversation-based operations inbox with
  member/system identity, route previews, delivery state, search, unread state, and archiving.
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

Section headings share one responsive action-bar treatment. Filters come first, status follows,
and action buttons remain in their authored primary/secondary order. The action bar wraps on
intermediate widths and stacks below its heading on narrow or enlarged-text displays rather than
clipping controls at the viewport edge.

Disabled optional modules remain visible as policy state, not as apparently broken features. Their
navigation entries and matching capability cards read **Disabled**; a direct page visit shows the
configuration key and restart requirement while making the inactive controls inert. The API source
of truth is `/api/v1/modules`.

Recurring refreshes are visibility-aware: hidden tabs stop nonessential API work and resume with
jitter rather than producing a request burst. Navigation consumes the compact, ETag-enabled
`/api/v1/dashboard/poll` response for module state, pending federation reviews, and actionable
mail. The contributor-facing request, CPU, memory, provider, and database limits are recorded in
the [dashboard performance budget](PERFORMANCE.md).

The Members workspace includes a responsive audit inspector. Desktop keeps action, actor, target,
outcome, and time in dense scanning columns; narrow displays use complete stacked event cards.
Time, actor, action, target, and outcome filters run on the server and remain active while paging.
Structured details are formatted behind a disclosure control, and **Copy details** copies the same
credential-redacted representation shown on screen.

The Mail workspace is an operator-only operations inbox. Local and federated messages group into
conversations without treating the catch-all `@operator` address as a member. Each conversation
shows the named member or remote Outpost operator, peer, observed LoRa/MQTT paths, message state,
and receipts. A federated reply uses the conversation's stored peer, wire conversation ID, and
reply address; the UI previews all three before sending. Search includes message text but list
responses do not expose bodies. Opening a conversation, changing read/archive state, and replying
are audited. Messages explicitly addressed to a named member remain available through that
member's mesh mailbox; system messages addressed to `@operator` remain web-only.

## API

The JSON API is rooted at `/api/v1`. Important read surfaces include:

- `/api/v1/health` and `/api/v1/status`
- `/api/v1/config`
- `/api/v1/dashboard/overview` and the ETag-enabled `/api/v1/dashboard/poll`
- `/api/v1/members`, `/api/v1/members/map`, and `/api/v1/mesh/messages`
- `/api/v1/mesh/queue` and `/api/v1/mesh/airtime`
- `/api/v1/boards`, `/api/v1/mail`, `/api/v1/mail/conversations`, `/api/v1/incidents`,
  `/api/v1/events`
- `/api/v1/environment/weather`, `/forecast`, `/alerts`, `/earthquakes`, `/waypoints`
- `/api/v1/backups`, `/api/v1/audit`, and federation endpoints under `/api/v1/federation`
- `/api/v1/maintenance/storage`, `/api/v1/maintenance/preview`, and
  `/api/v1/maintenance/run`

Backups → Live data & retention reports the live database, WAL, verified backups, available disk,
per-domain rows/allocation, and growth since the last maintenance baseline. Its cleanup preview is
read-only. Applying the preview requires the displayed confirmation, takes a verified snapshot
first, and runs bounded deletion batches. See [Data retention and storage](RETENTION.md).

`/api/v1/audit` accepts `from_time`, `until`, `actor`, `action`, `target`, `outcome`, `cursor`, and
`limit`. Outcomes are `success`, `denied`, or `failure`; existing privileged-action writers record
`success` by default. The response includes the filtered `total` and redacts credential-shaped
fields before returning or formatting detail.

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
