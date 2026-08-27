# Features and maturity

Outpost is pre-release. This table distinguishes implementation from field validation.

| Area | Implemented | Validation status |
| --- | --- | --- |
| Meshtastic transport | Serial, TCP, BLE; reconnect and liveness supervision | Serial exercised; broader hardware matrix pending |
| Command routing | DM/channel parsing, sessions, paging, trust and module gates | Automated integration and acceptance coverage |
| Airtime control | Durable priority queue, budgets, reserve, quiet hours, limits, dedupe, restart recovery | Automated restart and failure-injection tests; extended channel calibration pending |
| Identity | Handles, trust, approvals, recently heard discovery | Implemented and tested |
| BBS and mail | Boards, threads, posts, subscriptions, digests, stored mail, operator conversation inbox | Implemented and tested |
| Community Watch | Incidents, maps, confirmation/dispute, responder alerts | Implemented; tabletop scale exercise recommended |
| Welfare | Events, roster, check-ins, reviewed solicitation, CSV | Implemented and tested |
| Environment | Weather, CAP, astronomy, earthquakes, maps, waypoints | Implemented; external providers required after cache expiry |
| Dashboard access | Responsive pages/API; named roles, TOTP/recovery, revocable sessions, protected-action step-up | Implemented and automated; trusted-LAN or TLS/VPN deployment assumed |
| Backups | Online backup, rotation, validation, guarded restore | Implemented and tested |
| Federation | Pairing, authenticated framing, policy, sync, services, mail | Automated tests and two physical Outposts exercised; full acceptance backlog pending |
| Meshtastic MQTT | Optional discovery/transport through radio firmware | Implemented controls; multi-node validation pending |
| SAME/RTL-SDR | Supervised decoder, county/review gates, CAP dedupe, health UI/API, scoped installation | Audio fixture, live carrier, process failure, USB loss, and automatic recovery validated on Pi hardware; extended field observation pending |
| Local AI | Provider adapters and policy surface | Optional; quality is model/hardware-specific |

## Boundaries

- No 1.0 compatibility promise exists yet.
- Federation is experimental until two-node acceptance is complete.
- Environmental feeds are informational and retain source timestamps.
- Emergency keyword matching is off by default due to false-positive and missed-detection risk.
- Automated tests do not replace local radio-range, power-loss, or disaster exercises.
