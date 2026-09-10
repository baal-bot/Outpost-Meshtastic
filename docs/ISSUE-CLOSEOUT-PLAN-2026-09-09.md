# Open-issue closeout plan — September 9, 2026

The live GitHub inventory contains **16 open issues**, excluding pull requests:
13 September resilience tasks, the September tracker, the August tracker, and the
parked native-client discovery task. This plan preserves their existing acceptance
criteria. A software fix, an installed release, an operator observation and a
completed field exercise provide different evidence.

Automatic recovery from delayed startup synchronization under #141 is now installed
and verified; see the [qualification record](STARTUP-TIME-QUALIFICATION-2026-09-09.md).
#150 is ready for closure using the existing installed-antenna reception, broadcast
tests, hardware recovery and software regression evidence. Following the owner's
request to move on from power qualification, **#156 guarded-AI evaluation and
service-isolation software is now installed and verified**. #148 is deferred in
the work order; its remaining resource-policy and measurement criteria are not
silently marked complete.

The owner authorized #156 and explicitly prioritized completing software before
physical qualification. Software implementation and automated validation proceed
independently of the field sessions below. #156 now has a frozen real-service
holdout, deterministic comparison, blinding exports and fixes for grounding,
revision/access changes and AI worker starvation. All four exact-source CI jobs
and live installation/preservation checks pass. See
[the software evidence](AI-EVALUATION-2026-09-09.md) for the installed release,
results and the remaining comparative evaluation work.
See the [SDR qualification record](SDR-RECEPTION-QUALIFICATION-2026-09-09.md).

The next authorized deliverable, **#159's bulk-synchronization design and coding
scope**, is documented in the
[architecture decision](BULK-BACKHAUL-DECISION-2026-09-09.md). The recommended
software candidate is optional direct peer HTTPS over a configured local network.
The IP protocol, trust, configuration and storage contract **B1 is now implemented**;
see [the frozen contract](BULK-PROTOCOL-V1.md). **B2 shared ingress is next**, followed
by HTTPS, durable reconciliation, operator controls and software qualification.
Operational selection is deferred while the actual
local inter-station IP route is unconfirmed; only LoRa is confirmed for this
design. This does not block development using isolated software peers. The new
transport is not implemented and #159 remains open.

#139's physical offline-map check and the longer exercises below remain later
qualification work. Finish remaining software work before scheduling those
sessions; their equipment and participant requirements do not block software
implementation. Freeze a candidate when ready for the seven-day and thirty-day
acceptance intervals.

## Every open issue

| Issue | Current position | Work and evidence needed to close | Equipment or people |
| --- | --- | --- | --- |
| [#141 — Offline time (P1)](https://github.com/baal-bot/Outpost-Meshtastic/issues/141) | Native safeguards, trusted-peer checks and automatic startup correction recovery are installed; exact-source CI and live checks pass. | Observe an actual delayed-sync boot. Measure continuously powered UTC error and availability through a multi-day WAN/time-source outage, including beyond six-hour estimated holdover. Exercise fresh authenticated peer evidence, missing/disagreeing/stale sources and recovery on two stations. Separately measure RTC retention after complete main-power loss and no-WAN cold boot. Record any needed source/policy follow-up instead of extending confidence without measurements. | Two updated stations, independent trusted clock/reference, sustained station power; RTC/battery inventory for the separate complete-power-loss test. |
| [#139 — Offline maps (P2)](https://github.com/baal-bot/Outpost-Meshtastic/issues/139) | Regional detail and overview are installed and verified. Map observations now preserve measured PASS. | Keep the local LAN, physically remove WAN access and disable client cellular fallback; use a fresh browser without cached map assets. Render the selected region across supported zooms, verify labels and list/incident fallback, and record release, pack digest and results. Review the operator observation after the actual check. Existing browser request blocking does not close this field criterion. | Current station and phone/laptop; local access while WAN is disconnected. |
| [#150 — SDR reception (P2)](https://github.com/baal-bot/Outpost-Meshtastic/issues/150) | Ready to close. Connected-antenna reception is owner-confirmed and corroborated by decoded weekly tests and weather messages. Installed indicators, exact-source CI, recorded fixture, August USB-driver recovery and repeated process-failure/deduplication tests cover the issue criteria. | Attach the existing qualification evidence and close the issue. No additional antenna, listening session or reception test is needed. | No additional equipment or operator exercise. |
| [#148 — Energy/storage/thermal policy (P2)](https://github.com/baal-bot/Outpost-Meshtastic/issues/148) | Deferred in the work order. The owner confirms the Pi runs from a solar battery capable of many days of operation. The LoRa radio has its own battery and stays powered and receiving during a Pi outage. Existing power backup is the deployment baseline. | When resumed, address remaining low-energy/storage/thermal software behavior and the issue's measurements using the existing setup. Preserve urgent intake and authoritative records; report unavailable Pi/UPS telemetry independently of radio battery telemetry. Reuse #44's interruption evidence. | Existing solar battery for the Pi and separate radio battery. No replacement power arrangement is requested. Measurement interfaces depend on existing equipment. |
| [#145 — Encrypted recovery (P2)](https://github.com/baal-bot/Outpost-Meshtastic/issues/145) | Encryption, validation, fenced restore, real copied-runtime smoke and browser access tests pass. Encrypted USB copies exist. | Secure recovery-key custody outside the original Pi and prepare a current compatible off-device generation. A second operator restores on a fresh compatible offline target, authenticates, compares retained records and historical identity/provenance, and records timing/missing assets. Exercise the documented return-to-service identity procedure; a historical clone remains fenced and must not reuse active signing authority. Resolve any unmet field identity criterion explicitly. | Separate target/boot media, verified current kit and encrypted archive, independently held key/digest, second authorized operator. |
| [#147 — Replacement kit/local access (P2)](https://github.com/baal-bot/Outpost-Meshtastic/issues/147) | Kit builder/verifier, locked runtime and isolated copied-kit smoke exist. A complete field replacement remains open. | Refresh the kit for the chosen green candidate; inventory boot OS, vendor/model/device assets, regional maps, client installers, hashes, schema, storage and redistribution rights. Have a second operator install/restore service using only prepared media. Demonstrate phone/radio/dashboard access after temporary hotspot expiry and reboot, without public DNS/cloud/cellular. Record elapsed recovery and repeatable refresh instructions. | Fresh target/boot media, powered permanent local LAN, offline clients, second operator; coordinate with #139/#145. |
| [#135 — Prompt incident federation/G6 (P1)](https://github.com/baal-bot/Outpost-Meshtastic/issues/135) | Automatic sender and real-browser simulation are complete. Physical G6 remains open. | On a declared supported payload/load/topology, measure report intake through remote map display, with remote storage, human review, notification and ACK timings separate. Preserve provenance, review and congestion/expiry behavior. Use the #157 session for overlapping three-node evidence; results outside the supported envelope must remain visible. | Updated paired stations, actual radios, operators watching remote inboxes/maps; three nodes for the joint exercise. |
| [#142 — Shared RF capacity/seven days (P1)](https://github.com/baal-bot/Outpost-Meshtastic/issues/142) | Software budgeting exists; simulated airtime is not a measured community capacity envelope. | Declare a supported workload/topology within the requested 3–10-station scope, measure firmware airtime against estimates and record offered versus transmitted traffic. Publish per-class/node and aggregate airtime, latency distributions, retries, queue growth, expiry and overload limits. Complete the literal seven-day live-mesh gate within declared budgets. | At least three stations for the initial declared envelope, resident traffic/handhelds, durable measurements for seven days. Wider capacity claims need their own measurements. |
| [#157 — Three-station outage exercise (P1)](https://github.com/baal-bot/Outpost-Meshtastic/issues/157) | Scenario/acceptance task; no physical pass has been recorded. | Run at least three physically separate Outposts and six handheld participants with WAN/cellular/public-broker paths excluded and an intermediate RF route. Exercise boards/mail, simultaneous incidents, corrections, assignment/review/all-clear, lost/duplicate/reordered traffic, partition, isolated operation and bounded catch-up. Record each delivery stage, G6, retained acknowledged records and wrong/duplicate-action checks. | Three complete stations, six handheld participants, operators and a documented topology. #141/#147 readiness and completed software prerequisites first; #135's shared final gate can be measured in this same session. |
| [#158 — Thirty-day unattended soak (P1)](https://github.com/baal-bot/Outpost-Meshtastic/issues/158) | A full uninterrupted acceptance interval has not begun on a fixed qualified candidate. | After startup, energy/storage and SDR prerequisites pass, freeze release/configuration and run thirty days of representative traffic/data. Include planned radio/SDR disconnects, provider failure, storage pressure and optional-service recovery. Record every intervention, task failure, starvation and acknowledged-record outcome; reset or explicitly qualify the interval after material changes. Consume #44's power evidence. | Stable deployed candidate, reliable evidence storage, realistic workload and scheduled fault sessions. Ordinary read-only observation can continue during the interval. |
| [#44 — August physical exit gates (P1)](https://github.com/baal-bot/Outpost-Meshtastic/issues/44) | Original software findings are closed; two physical exit gates remain. | Run the prepared power-interruption/recovery campaign with before/after acknowledged incident, mail, outbound and custody IDs on intended storage/radio hardware. Separately demonstrate physical two-node MQTT-only operation and automatic LoRa/MQTT fallback through partition/reconnect, deduplication and application receipts. Report every loss/intervention. Reuse these results in #148/#157/#158. | Backed-up designated test hardware and controlled power setup; two nodes and the declared broker/network paths. Schedule the disruptive exercise explicitly. |
| [#155 — First-time usability/G2 (P2)](https://github.com/baal-bot/Outpost-Meshtastic/issues/155) | Command/accessibility software exists; representative first-time-user evidence is missing. | Predefine participant count/method. Measure the literal G2 target: at least 80% post to a board within three messages, unprompted. Also test corrections, incident identity, mail/ACK/check-in, delayed/multipart replies, stale references and applicable accessibility needs on fresh offline clients. Record assistance/failures and repair concrete blockers before closure. | Consenting nontechnical first-time participants, actual handhelds/phones, prepared permanent access from #147; synthetic scenarios and anonymized observations. |
| [#156 — Guarded AI holdout (P3)](https://github.com/baal-bot/Outpost-Meshtastic/issues/156) | Software installed and verified on `a68baa8`. Frozen 28-case guarded/deterministic controls and separate legacy 60-case harness pass. Grounding, permission/revision checks and non-AI worker capacity are fixed; four CI jobs and live preservation checks pass. | Complete independent blinded usefulness scoring and a configured-native-provider comparison using the frozen synthetic corpus. Record model/runtime identity and native latency/memory/energy; distinguish estimated from measured airtime and leave unavailable measurements explicitly pending. These remaining evaluation results do not block this installed software or other software work. | Existing Hailo appliance/model and independent reviewer. Coordinate native device access for the comparison; no new power setup or model migration is required. |
| [#159 — Independent bulk backhaul (P2)](https://github.com/baal-bot/Outpost-Meshtastic/issues/159) | [Design and coding scope recorded](BULK-BACKHAUL-DECISION-2026-09-09.md): optional direct peer HTTPS is the software candidate; deployment selection is explicitly deferred while the local IP route and shared-capacity envelope are unconfirmed. B1 codec/configuration and bounded replay/work ledger are implemented; the operational transport remains incomplete. | Continue with B2 shared transaction-owned ingress, then B3–B6 HTTPS, scheduling, operator controls and software qualification. Preserve current policy, independent replay state, finite reconnect and zero bulk fallback onto LoRa. Qualify in software before later deployment checks, reusing #44 where applicable. Retain a supported-path demonstration or explicit accepted deployment deferral for closure. | Isolated software peers suffice for development. Actual inter-station LAN/routing remains unconfirmed; no new power or antenna exercise is required. Network equipment is a later deployment decision. |
| [#130 — September resilience tracker (P1)](https://github.com/baal-bot/Outpost-Meshtastic/issues/130) | Umbrella over the September work; many software children are complete. | Reconcile every remaining child with its closing evidence or explicit accepted disposition, the G1–G6 requirement ledger and capability matrix. Preserve #44's separate physical gates. Close the tracker only after its remaining scope is accounted for. | Evidence from the rows above and final owner/reviewer disposition for any deferred requirement. |
| [#61 — Native iOS/Android discovery (P3)](https://github.com/baal-bot/Outpost-Meshtastic/issues/61) | Deliberately parked outside current hardening. | After core field qualification and a stable scoped API, produce an architecture decision and narrow proof-of-concept plan covering LAN/BLE/hybrid transport, trust, permissions, offline storage/sync, accessibility, platform constraints and maintenance. Implementation is separate. Keep parked unless the owner changes its priority. | Product/platform decisions after the core acceptance work; native apps are not prerequisites for current offline clients. |

## Work sequence and shared sessions

Software completion has priority. Steps 2–8 describe later sessions and decisions
to schedule when ready; they are not prerequisites for finishing software.

1. **Continue #159 with B2 shared domain ingress.**
   B1's versioned IP protocol, active-peer trust, independent replay state and
   finite durable storage contract are implemented. Follow B2–B6 in the architecture
   decision; a local test network can exercise them before field deployment.
   #156's software is installed with exact-source CI and preservation checks;
   keep its remaining native comparison and independent review separate. The
   #141 startup fix and #150 reception indicators are also installed; close #150
   using its existing evidence. Keep #148 out of the immediate work order as
   requested, using the confirmed battery setup when it resumes.
2. **Close the smallest field gap: #139.** Prepare permanent local access first,
   then schedule a fresh-browser WAN-disconnected map session. Capture #147 local
   access evidence in the same session when the hotspot-expiry/reboot checks are
   actually performed. Do not call a partial #147 pass a replacement test.
3. **Run one coordinated recovery/access session for #145/#147.** Prepare the
   spare target, current complete kit, archive, independently held key/digest and
   second operator before disconnecting WAN. Preserve the old verified kit and
   backup generations until the replacement passes. Verify historical restoration
   separately from a fresh serving identity and any authorized data adoption.
4. **Qualify time, reception and power on the installed stations.** Use a shared
   dated baseline for #141/#148: keep the main station powered during the multi-day
   WAN outage and compare UTC against an independent reference. Peer fallback
   requires a peer with a valid independent reference; two equally stale clocks
   cannot keep certifying each other. #150's reception/recovery evidence is already
   sufficient for its closeout; do not add another reception exercise to this
   session. RTC retention is a separate power-loss
   scenario, not a prerequisite to the continuously powered test.
5. **Complete the controlled two-node gates under #44.** Prepare current backups,
   exact record witnesses and the intended interruption/fallback procedure before
   the disruptive session. Record one power campaign and reuse its evidence.
6. **Run #135/#157 together once three stations and six handheld participants are
   available.** Exercise the stated intermediate-route, partition and human-review
   conditions. Identify the supported G6 envelope; expand it only from results.
   Establish #142's initial live workload/capacity baseline in the same setup.
7. **Freeze the candidate and start the long gates.** The seven-day RF interval
   (#142) can overlap the thirty-day appliance interval (#158) when the same
   candidate/configuration, relevant prerequisites and workloads satisfy both.
   Start formal acceptance only after prerequisites pass. Development/diagnostic
   soak before that is useful preparation, not credited acceptance time. Keep
   timestamps, observations and failures through the complete intervals.
8. **Schedule usability and comparative AI work alongside available field access.**
   #155 needs first-time participants; #156 needs a blinded scoring method. Resolve
   #159's deployment selection from the actual local route, measured capacity and
   completed software evidence, or retain an explicitly accepted deferral.
   Reconcile #130 after the child decisions; #61 remains outside this milestone.

An afternoon of successful checks cannot substitute for the seven-day or thirty-day
interval. The schedule is relative to a prepared candidate and available equipment;
no unperformed field session or future completion date is claimed here.

## Preparation and closing record

Current confirmed context is this station and a second station with its owner.
Availability of a third station, a separate restore target and a second operator
needs confirmation before assigning field dates. A separate boot drive may support
a same-hardware offline rebuild exercise, but does not by itself demonstrate recovery
from loss of the entire appliance. The owner confirms a solar battery capable of
powering the Pi for many days and a separate battery in the LoRa radio, so the
radio remains powered and receiving through a Pi outage. This supersedes the
earlier description of one shared battery as the radio's only power source.
Continued RF reception and later delivery of buffered messages to the Pi are
different software/firmware behaviors; this inventory records the power setup
without claiming an unlimited or durable radio backlog. Dedicated RTC battery
presence remains unspecified.

Prepare a small evidence record for each session: issue and exact acceptance
criterion, date/duration, release and CI identity, schema/firmware/preset and effective
configuration, participating hardware/roles, actual WAN/RF/power boundaries,
before/after expected outcomes, timings and every failure/intervention. Keep raw
credentials, private content, exact positions and record identifiers in protected
local evidence; publish only reviewed summaries and necessary aggregate results.

For each closure, attach the relevant evidence, check only the satisfied original
criteria, update capability/requirement records when warranted, then reconcile the
tracker. A remaining physical criterion keeps the issue open. If a requirement is
intentionally changed or deferred, record the explicit owner disposition and any
follow-up before closing it; do not substitute a weaker test silently. This plan
does not change GitHub issue states or record operator attestations.

Related procedures: [offline time](OFFLINE-TIME.md),
[regional maps](WORLDWIDE-MAP-SETUP.md),
[encrypted recovery](ENCRYPTED-RECOVERY.md),
[replacement kit and access](OFFLINE-REPLACEMENT.md),
[node loss and identity](NODE-LOSS-AND-MOBILITY.md),
[federation acceptance](FEDERATION-ACCEPTANCE-BACKLOG.md), and
[requirement dispositions](REQUIREMENT-RECONCILIATION.md).
