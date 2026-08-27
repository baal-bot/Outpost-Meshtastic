# Dashboard and API

The dashboard is a local operator console served by the Outpost process. It is responsive for
phones and desktops and uses bundled assets so its interface remains available without WAN access.

## Sections

- **Overview:** identity, service/radio state, message activity, weather, and operational summary.
- **Members:** actual members, discovered radios, trust/approval controls, and member map.
- **BBS and Mail:** boards, threads, moderation, and a conversation-based operations inbox with
  member/system identity, route previews, delivery state, search, unread state, and archiving.
- **Watch:** incident map/list, monitoring state, alerts, acknowledgements, welfare events, and
  human-reviewed cross-Outpost incident reconciliation with origin/provenance inspection.
- **Environment:** separate environmental map, weather/forecast, official alerts, earthquakes,
  astronomy, editable waypoints, and the reviewed RTL-SDR/SAME inbox and receiver health.
- **Radio:** connection state, firmware/node details, telemetry, durable outbound state and
  cancellation, airtime, reconnect, and MQTT.
- **Federation:** peer directory, pairing, transport policy, per-path transfer telemetry, durable
  retry/recovery health, sync policy/inbox, services, relay mail, and signed multi-hop custody queues
  with per-peer limits and operator origin-key review. Its topology workspace maps only active peers
  with explicit coarse-location consent, keeps every other identity in a health list, and adds
  incidents only after the operator selects that layer.
- **Access:** named web accounts, roles, authenticator enrollment, one-use recovery codes, and
  active-session inventory.
- **Backups/System/Settings:** health, retention, backup/restore, identity, emergency policy, AI, and
  other operator controls.

Discovered radios are not members. Member counts, welfare recipients, and member-map markers should
use admitted members only.

### Member and radio triage

The Members workspace separates enrolled identities from radios merely heard on the mesh. Saved
queues cover new discoveries, recently heard identities, stale discoveries, members, responders,
and radios requiring operator review. Search covers mesh ID, radio names, handle, notes, and
hardware metadata.

Open **Review** to see why the identity is in its current category, first/last heard time, latest
signal and hops, position consent and retention state, recent activity, operator notes, and the
permanent trust-change history. A trust change requires a reason and shows what the target level
enables before it is saved. Granting member-level trust admits the identity to member-only
workflows; claiming a handle through the mesh also restores a previously archived discovery as an
active enrolled identity.

Archive and Ignore apply only to unenrolled discovered radios:

- **Archive** removes stale discovery noise from active queues while retaining the identity,
  activity, and audit evidence for later restoration.
- **Ignore** additionally keeps future heard traffic from reopening operator review. Traffic is
  still logged, and the identity remains restorable.
- Neither action blocks radio commands or deletes evidence. Use the reviewed `blocked` trust level
  when command suppression is intended.

Bulk archive/ignore safely skips enrolled identities even if a stale selection is submitted. CSV
export is limited to 200 selected identities, is audit logged, neutralizes spreadsheet formulas,
and deliberately omits exact coordinates. Exact retained coordinates remain operator-only in the
review and member map.

## Weather provenance

Weather cards label station observations, near-term forecasts, and model estimates separately.
They show the provider, valid time/age, and cached state; missing measurements render as unavailable
rather than as zero. Peer-service weather is additionally marked as peer-provided while preserving
the original provider and observation/forecast kind. The weather API includes a `measurements`
object with the availability and provenance metadata for each value.

## Authentication

The first login uses account name `operator` and the short-lived token shown by
`sudo outpost-setup-token show`, followed by a permanent password of at least 12 characters. The
token is consumed by that login. Completing setup invalidates the bootstrap session and requires a
clean sign-in. Existing single-password databases migrate that credential and its audit continuity
to the named `operator` Administrator account.

Access supports Administrator, Operator, and Read-only / wallboard roles. Initial passwords for
new accounts must be changed at first sign-in. TOTP enrollment shows a standards-compatible secret
and then eight one-use recovery codes exactly once. The session inventory shows source, client,
last activity, and expiry, and can revoke one sign-in or all sign-ins for the current account.
Sessions expire according to `web.auth.session_hours`; state-changing requests require CSRF.
Protected actions additionally use a 10-minute password/TOTP step-up window. The browser asks only
when that window has expired and safely retries the original request after successful confirmation.

## Appearance

Settings → Appearance offers Outpost Dark, high-contrast Daylight, low-light Night Ops, and a
Follow System option. The preference is stored in the current browser rather than the Outpost
database so a wall display, phone, and operator workstation can each use the appropriate mode.
Night Ops reduces map brightness; it does not change incident severity or provider data.
Shared cards, buttons, pills, notices, dialogs, action bars, empty states, and map controls consume
the same semantic surface, text, border, focus, state, and disabled tokens in every theme. The
[dashboard design system](UI-DESIGN-SYSTEM.md) documents the contributor contract and visual
regression matrix.

## Map controls

Watch, Members, and Environment share the same map controls and interaction model. Drag with a
mouse or one finger to pan, use the wheel or **+ / −** controls to zoom, and activate a marker to
open its domain detail card. With the map focused, arrow keys pan, **+ / −** zoom, **0** fits visible
markers where the page offers fit, **Home** returns an Environment map to the Outpost, and
**Escape** closes the active selection. Marker hit areas remain 36 px square while their visual
symbol changes state, preventing the shape and click-target shifts seen in the earlier maps.

OpenStreetMap is the online default. If a regional tile bundle is installed, failed online tiles
fall back to it automatically. When neither source can provide a tile, the map explicitly reports
that the basemap is unavailable while coordinates, markers, controls, and detail cards continue to
work. Attribution and fallback state are rendered consistently on every map.

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
- `/api/v1/members`, `/api/v1/members/{id}`, `/api/v1/members/map`,
  `/api/v1/members/export`, `/api/v1/members/bulk`, and `/api/v1/mesh/messages`
- `/api/v1/mesh/queue` and `/api/v1/mesh/airtime`
- `/api/v1/boards`, `/api/v1/mail`, `/api/v1/mail/conversations`, `/api/v1/incidents`,
  `/api/v1/incidents/{id}/merge`, `/unmerge`, `/reject-match`, and `/api/v1/events`
- `/api/v1/environment/weather`, `/forecast`, `/alerts`, `/same`, `/earthquakes`, `/waypoints`
- `/api/v1/backups`, `/api/v1/audit`, and federation endpoints under `/api/v1/federation`
- `/api/v1/maintenance/storage`, `/api/v1/maintenance/preview`, and
  `/api/v1/maintenance/run`
- `/api/v1/auth/accounts`, `/api/v1/auth/sessions`, `/api/v1/auth/mfa/*`, and
  `/api/v1/auth/step-up`

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
