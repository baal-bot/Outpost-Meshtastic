# Phase 3 real-radio tabletop

This exercise intentionally transmits alerts. Run it only with operator approval, a declared
test window, and six participating radios. Prefix every headline with `DRILL`.

## Before transmitting

- Announce the start/end time and channels to participants.
- Confirm all six radios are approved members; assign at least two responders.
- Confirm no unrelated emergency traffic is active and channel utilisation is safe.
- Open the Watch dashboard, Radio page, and message log.
- Record the starting alert airtime, queue depth, and Outpost process start time.

## Exercise

1. Participant A shares GPS, then sends `REPORT tree down blocking drill route`.
2. Verify one geotagged incident exists and the member receives its incident number.
3. Participant B files the same report; verify Outpost offers `CONFIRM` instead of duplicating.
4. A responder raises `ALERT urgent <inc> DRILL route blocked; avoid area`.
5. Record radio receipt latency; target is no more than five seconds when the channel is clear.
6. Verify the map footprint, stage, next action, and acknowledgement progress.
7. One responder sends `ACK <inc>`; verify the named acknowledgement appears.
8. Let the next accelerated drill stage become due, or temporarily use a reviewed short policy.
9. Second responder sends `ACK <inc>`; verify escalation stops at threshold.
10. Open a drill welfare event, review recipients, and approve one solicitation per member.
11. Participants send `OK`, `HELPME`, and evacuation states; verify roster derivation and CSV.
12. Resolve the incident with `DRILL complete`; verify one all-clear and no stale repeat follows.

## Pass record

- Six participants completed the flow without out-of-band instructions beyond `?`.
- Alert and need-help p95 receipt latency: ______ seconds.
- Peak channel utilisation: ______%; Outpost alert airtime: ______ seconds.
- No nonmember received a direct solicitation.
- Restart during a pending stage resumed the correct next stage: pass / fail.
- WAN disabled map and roster remained functional: pass / fail.
- All-clear removed queued repeats: pass / fail.
- Operator, date, firmware, region, modem preset, notes: ________________________________.
