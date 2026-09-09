# Open-issue closeout plan — September 9, 2026

The live GitHub inventory contains **16 open issues**, excluding pull requests:
13 September resilience tasks, the September tracker, the August tracker, and the
parked native-client discovery task. This plan preserves their existing acceptance
criteria. A software fix, an installed release, an operator observation and a
completed field exercise provide different evidence.

The immediate software change addresses delayed startup synchronization under #141.
The next substantial software work is #150 reception status and #148 degraded
operation. The earliest likely complete closure is #139's physical offline-map
check. Prepare the longer exercises while doing these smaller tasks, then freeze
one candidate for the seven-day and thirty-day gates.

## Every open issue

| Issue | Current position | Work and evidence needed to close | Equipment or people |
| --- | --- | --- | --- |
| [#141 — Offline time (P1)](https://github.com/baal-bot/Outpost-Meshtastic/issues/141) | Native safeguards and trusted-peer checks are implemented; the reboot exposed a startup correction hold. | Verify/install automatic recovery before the first trusted UTC use. Observe an actual delayed-sync boot. Measure continuously powered UTC error and availability through a multi-day WAN/time-source outage, including beyond six-hour estimated holdover. Exercise fresh authenticated peer evidence, missing/disagreeing/stale sources and recovery on two stations. Separately measure RTC retention after complete main-power loss and no-WAN cold boot. Record any needed source/policy follow-up instead of extending confidence without measurements. | Two updated stations, independent trusted clock/reference, sustained station power; RTC/battery inventory for the separate complete-power-loss test. |
| [#139 — Offline maps (P2)](https://github.com/baal-bot/Outpost-Meshtastic/issues/139) | Regional detail and overview are installed and verified. Map observations now preserve measured PASS. | Keep the local LAN, physically remove WAN access and disable client cellular fallback; use a fresh browser without cached map assets. Render the selected region across supported zooms, verify labels and list/incident fallback, and record release, pack digest and results. Review the operator observation after the actual check. Existing browser request blocking does not close this field criterion. | Current station and phone/laptop; local access while WAN is disconnected. |
| [#150 — SDR reception (P2)](https://github.com/baal-bot/Outpost-Meshtastic/issues/150) | Receiver supervision exists; audio alone has not qualified decoding. | Finish distinct pipeline, signal, station-qualification and last-verified-decode status with freshness. Add a recorded/synthetic decode fixture and interruption regressions. Observe a legitimate broadcast test on the installed SDR/antenna, then verify repeated USB/process recovery without duplicate actionable alerts or unintended sends. Document dependence on a receivable broadcast station. | SDR/antenna, receivable station and its actual test schedule; an operator for the reception/interruption session. |
| [#148 — Energy/storage/thermal policy (P2)](https://github.com/baal-bot/Outpost-Meshtastic/issues/148) | Radio external power is correctly recognized. Whole-station telemetry and operating envelope remain unqualified. | Inventory the actual supply/telemetry interface, implement and test explicit reversible low-energy/storage/thermal behavior, and preserve urgent intake and authoritative records. Measure idle/typical/burst watts, energy/day, autonomy, charging assumptions, temperature and memory with AI off/on. Missing UPS telemetry stays unknown. Reuse #44's controlled shutdown/interruption evidence. | Existing shared Pi/SDR/LoRa battery and solar supply; suitable energy measurement and any supported UPS telemetry. Battery runtime is currently owner-reported. |
| [#145 — Encrypted recovery (P2)](https://github.com/baal-bot/Outpost-Meshtastic/issues/145) | Encryption, validation, fenced restore, real copied-runtime smoke and browser access tests pass. Encrypted USB copies exist. | Secure recovery-key custody outside the original Pi and prepare a current compatible off-device generation. A second operator restores on a fresh compatible offline target, authenticates, compares retained records and historical identity/provenance, and records timing/missing assets. Exercise the documented return-to-service identity procedure; a historical clone remains fenced and must not reuse active signing authority. Resolve any unmet field identity criterion explicitly. | Separate target/boot media, verified current kit and encrypted archive, independently held key/digest, second authorized operator. |
| [#147 — Replacement kit/local access (P2)](https://github.com/baal-bot/Outpost-Meshtastic/issues/147) | Kit builder/verifier, locked runtime and isolated copied-kit smoke exist. A complete field replacement remains open. | Refresh the kit for the chosen green candidate; inventory boot OS, vendor/model/device assets, regional maps, client installers, hashes, schema, storage and redistribution rights. Have a second operator install/restore service using only prepared media. Demonstrate phone/radio/dashboard access after temporary hotspot expiry and reboot, without public DNS/cloud/cellular. Record elapsed recovery and repeatable refresh instructions. | Fresh target/boot media, powered permanent local LAN, offline clients, second operator; coordinate with #139/#145. |
| [#135 — Prompt incident federation/G6 (P1)](https://github.com/baal-bot/Outpost-Meshtastic/issues/135) | Automatic sender and real-browser simulation are complete. Physical G6 remains open. | On a declared supported payload/load/topology, measure report intake through remote map display, with remote storage, human review, notification and ACK timings separate. Preserve provenance, review and congestion/expiry behavior. Use the #157 session for overlapping three-node evidence; results outside the supported envelope must remain visible. | Updated paired stations, actual radios, operators watching remote inboxes/maps; three nodes for the joint exercise. |
| [#142 — Shared RF capacity/seven days (P1)](https://github.com/baal-bot/Outpost-Meshtastic/issues/142) | Software budgeting exists; simulated airtime is not a measured community capacity envelope. | Declare a supported workload/topology within the requested 3–10-station scope, measure firmware airtime against estimates and record offered versus transmitted traffic. Publish per-class/node and aggregate airtime, latency distributions, retries, queue growth, expiry and overload limits. Complete the literal seven-day live-mesh gate within declared budgets. | At least three stations for the initial declared envelope, resident traffic/handhelds, durable measurements for seven days. Wider capacity claims need their own measurements. |
| [#157 — Three-station outage exercise (P1)](https://github.com/baal-bot/Outpost-Meshtastic/issues/157) | Scenario/acceptance task; no physical pass has been recorded. | Run at least three physically separate Outposts and six handheld participants with WAN/cellular/public-broker paths excluded and an intermediate RF route. Exercise boards/mail, simultaneous incidents, corrections, assignment/review/all-clear, lost/duplicate/reordered traffic, partition, isolated operation and bounded catch-up. Record each delivery stage, G6, retained acknowledged records and wrong/duplicate-action checks. | Three complete stations, six handheld participants, operators and a documented topology. #141/#147 readiness and completed software prerequisites first; #135's shared final gate can be measured in this same session. |
| [#158 — Thirty-day unattended soak (P1)](https://github.com/baal-bot/Outpost-Meshtastic/issues/158) | A full uninterrupted acceptance interval has not begun on a fixed qualified candidate. | After startup, energy/storage and SDR prerequisites pass, freeze release/configuration and run thirty days of representative traffic/data. Include planned radio/SDR disconnects, provider failure, storage pressure and optional-service recovery. Record every intervention, task failure, starvation and acknowledged-record outcome; reset or explicitly qualify the interval after material changes. Consume #44's power evidence. | Stable deployed candidate, reliable evidence storage, realistic workload and scheduled fault sessions. Ordinary read-only observation can continue during the interval. |
| [#44 — August physical exit gates (P1)](https://github.com/baal-bot/Outpost-Meshtastic/issues/44) | Original software findings are closed; two physical exit gates remain. | Run the prepared power-interruption/recovery campaign with before/after acknowledged incident, mail, outbound and custody IDs on intended storage/radio hardware. Separately demonstrate physical two-node MQTT-only operation and automatic LoRa/MQTT fallback through partition/reconnect, deduplication and application receipts. Report every loss/intervention. Reuse these results in #148/#157/#158. | Backed-up designated test hardware and controlled power setup; two nodes and the declared broker/network paths. Schedule the disruptive exercise explicitly. |
| [#155 — First-time usability/G2 (P2)](https://github.com/baal-bot/Outpost-Meshtastic/issues/155) | Command/accessibility software exists; representative first-time-user evidence is missing. | Predefine participant count/method. Measure the literal G2 target: at least 80% post to a board within three messages, unprompted. Also test corrections, incident identity, mail/ACK/check-in, delayed/multipart replies, stale references and applicable accessibility needs on fresh offline clients. Record assistance/failures and repair concrete blockers before closure. | Consenting nontechnical first-time participants, actual handhelds/phones, prepared permanent access from #147; synthetic scenarios and anonymized observations. |
| [#156 — Guarded AI holdout (P3)](https://github.com/baal-bot/Outpost-Meshtastic/issues/156) | Earlier guarded 60-case evidence exists; new blinded comparative evaluation remains. | Freeze a new synthetic blinded/adversarial holdout; report original G4 separately. Compare guarded AI with deterministic retrieval for usefulness, latency, airtime, energy and memory. Verify permissions/provenance and zero safety-category failures; measure other services with AI disabled/busy/failed on the declared model/runtime. | Current Hailo appliance, independent scoring/review and power measurements shared with #148. No model migration is implied. |
| [#159 — Independent bulk backhaul (P2)](https://github.com/baal-bot/Outpost-Meshtastic/issues/159) | An architecture decision remains; this is not an instruction to build another transport immediately. | Compare locally powered IP/MQTT/backhaul options using terrain, power, trust, maintenance and #142 capacity evidence. Record a supported choice or explicit deferral. If selected, demonstrate WAN-independent bounded reconnect/fallback and no policy bypass or LoRa flood, reusing #44 where applicable. Scope implementation only after the decision. | Deployment constraints and owner decision; candidate network hardware only if selected. An explicit documented deferral can conclude the design scope. |
| [#130 — September resilience tracker (P1)](https://github.com/baal-bot/Outpost-Meshtastic/issues/130) | Umbrella over the September work; many software children are complete. | Reconcile every remaining child with its closing evidence or explicit accepted disposition, the G1–G6 requirement ledger and capability matrix. Preserve #44's separate physical gates. Close the tracker only after its remaining scope is accounted for. | Evidence from the rows above and final owner/reviewer disposition for any deferred requirement. |
| [#61 — Native iOS/Android discovery (P3)](https://github.com/baal-bot/Outpost-Meshtastic/issues/61) | Deliberately parked outside current hardening. | After core field qualification and a stable scoped API, produce an architecture decision and narrow proof-of-concept plan covering LAN/BLE/hybrid transport, trust, permissions, offline storage/sync, accessibility, platform constraints and maintenance. Implementation is separate. Keep parked unless the owner changes its priority. | Product/platform decisions after the core acceptance work; native apps are not prerequisites for current offline clients. |

## Work sequence and shared sessions

1. **Finish #141 startup recovery, then #150 and #148 software.** Use synthetic
   clock/source, receiver and resource-pressure faults in development. Install only
   a revision with all required exact-source CI jobs passing. Keep whole-station
   energy choices tied to the actual supply and storage inventory.
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
   cannot keep certifying each other. Schedule #150's legitimate broadcast test
   around the actual station schedule. RTC retention is a separate power-loss
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
   #159 from measured capacity and deployment constraints. Reconcile #130 after
   the child decisions; #61 remains outside this milestone.

An afternoon of successful checks cannot substitute for the seven-day or thirty-day
interval. The schedule is relative to a prepared candidate and available equipment;
no unperformed field session or future completion date is claimed here.

## Preparation and closing record

Current confirmed context is this station and a second station with its owner.
Availability of a third station, a separate restore target and a second operator
needs confirmation before assigning field dates. A separate boot drive may support
a same-hardware offline rebuild exercise, but does not by itself demonstrate recovery
from loss of the entire appliance. The owner reports a shared backup battery with
days of runtime and solar backup; measurement and dedicated RTC battery presence
remain outstanding.

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
