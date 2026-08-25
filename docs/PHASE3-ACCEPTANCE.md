# Phase 3 — Community Watch acceptance evidence

Automated on 2026-08-24 with `pytest`, the virtual clock, simulated radio, and a fresh
SQLite database. Hardware-only gates remain explicitly open until the tabletop exercise is
run; this document does not treat simulation as proof of real-mesh timing.

| # | Gate | Evidence | Status |
|---|---|---|---|
| 1 | One-message typed, geotagged report | `test_watch_incidents.py` | Pass |
| 2 | ≥90% inference on 40 phrases | `test_phase3_acceptance.py` | Pass |
| 3 | Duplicate offered as `CONFIRM` | `test_watch_incidents.py` | Pass |
| 4 | Responder alert preempts digest and broadcasts within 5 s | `test_governor.py`; real timing in tabletop | Sim pass / hardware open |
| 5 | Durable escalation and ack threshold | `test_watch_alerts.py` | Pass |
| 6 | All-clear supersedes queued repeat | `test_governor.py`, `test_watch_alerts.py` | Pass |
| 7 | `OK`, roster, derived unaccounted | `test_watch_checkin.py` | Pass |
| 8 | Need-help reaches responders and dashboard within 5 s | Integration path passes; real timing in tabletop | Sim pass / hardware open |
| 9 | Severity then haversine ordering | `test_watch_incidents.py` | Pass |
| 10 | Offline map: incidents, nodes, alert areas, roster | Browser check; WAN-down hardware check remains | Sim pass / hardware open |
| 11 | Three-alert storm stays within airtime ceiling | `test_governor.py` | Pass |
| 12 | Emergency keywords default off; responder-only | `test_watch_incidents.py`, configuration tests | Pass |
| 13 | Valid roster CSV | `test_watch_checkin.py` | Pass |
| 14 | AI cannot author or raise alerts | `test_phase3_acceptance.py` | Pass |

The remaining phase gate is the six-person real-radio tabletop in
`tests/hardware/PHASE3_TABLETOP.md`.
