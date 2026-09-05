# Outpost adversarial review — 2026-09-05

Reviewed source revision: `49756805513ca3fe3e15cfe9c30569d49f4a63f5`.

This is the preserved pre-remediation review. Current remediation is tracked in
[resilience hardening](RESILIENCE-HARDENING.md) and the
[requirement disposition ledger](REQUIREMENT-DISPOSITIONS.md); historical findings below are
not assertions about the current source revision.

GitHub tracking: [resilience review #130](https://github.com/baal-bot/Outpost-Meshtastic/issues/130) links 29 tasks (#131–#159), with priorities, dependencies, acceptance criteria, and related earlier work. R1–R10 map to #131–#140. The existing physical power-loss and MQTT-fallback gates remain in #44. Logging is complete; implementation has not started.

Preserved on GitHub: [full review archive](https://github.com/baal-bot/Outpost-Meshtastic/issues/130#issuecomment-5553852356) and [runnable synthetic probes](https://github.com/baal-bot/Outpost-Meshtastic/issues/130#issuecomment-5553852199).
The [version-controlled probe artifact](review-artifacts/README.md) preserves the original suite
without turning known historical failures into current CI acceptance claims.

## Assessment

Outpost has a credible foundation for community communications without internet or cellular service. Local boards, mail, incident records, welfare workflows, radio commands, operator access, and direct radio federation are implemented. Its architecture does not require a cloud account or a central service to retain community information.

It is not yet ready to be relied upon as an unattended network during an extended outage. This review found reproducible failures in concurrent incident intake, local mail, queue scheduling, and clock-skewed federation. Routine incident propagation also falls short of the original latency requirement. The running development instance is healthy, but the installed boot service cannot open its newer database, and its configured offline map pack is absent.

The next milestone should be a resilience release followed by an independently operated outage exercise. More features would not compensate for these failures.

Working deployment assumption: several neighboring communities, approximately 3–10 Outposts with dozens of users each. Capacity depends on which radios share the same RF coverage area, terrain, antennas, message sizes, and operating discipline; this is a review target, not a supported capacity claim.

## Evidence and boundaries

The review compared the original specification set with current architecture, security, operation, retention, federation, acceptance, and capability documents; inspected the relevant implementation paths; checked the running appliance; and exercised isolated failure cases. Findings below distinguish implementation defects, problematic specification choices, deployment faults, and missing acceptance evidence.

- **182 existing targeted tests passed** in 53.32 seconds. These covered the governor, durable outbox, federation sync/radio/relay, incident reconciliation, alerts, welfare, web access policy, mesh PKI, transactions, backups, SAME, safety-floor admission, and inbound workers.
- **Nine additional assertions failed and one positive control passed.** The failing assertions cover six functional defect categories plus the configured durability policy. Some categories have multiple probes. The positive control successfully created 12 concurrent BBS threads, demonstrating that the transaction primitive itself can support this workload.
- The additional probes use temporary databases and simulated radios. They include the real command router for concurrent incident intake. Their assertions describe desired behavior and intentionally remain failing against this revision.
- The capability and command catalogue checks passed: 17 capabilities and 61 commands. These checks establish catalogue consistency, not fulfillment of every specification requirement.
- The strict installed-runtime lock check did not pass against the development environment because it also contains development and vendor packages. Its reported failures were extra packages without lock entries; this is not evidence that the production runtime dependency pins themselves are wrong.
- At the final live check, LoRa was up, core and optional tasks were healthy, AI was ready, and SDR audio was current with zero receiver restarts.

Reproduction suite: [local review probes](../.data/adversarial-review-2026-09-05/test_adversarial.py). Machine-readable result: [JUnit XML](../.data/adversarial-review-2026-09-05/results.xml). These two artifacts live under Git-ignored `.data`; copy them explicitly if sharing or preserving the report outside this checkout.

```sh
PYTHONPATH=src .venv/bin/pytest -q --tb=short \
  .data/adversarial-review-2026-09-05/test_adversarial.py
```

No production fixes are included. The review did not interrupt power, disconnect WAN, generate RF test traffic, or rerun the complete browser/hardware matrix. Prior field evidence is attributed to the repository records rather than presented as newly observed.

## Findings requiring remediation

P1 means a blocker for relying on the affected function during an outage. P2 means a material correction or preparation requirement before wider deployment.

### R1 — P1: Concurrent incident reports can fail during reference allocation

**Confirmed implementation defect.** `IncidentService.create()` reads the active reference set, selects the smallest unused number, and inserts in a separate committed write. Two requests can select the same number. The unique active-reference index then rejects one; serializing individual SQL writes does not serialize the whole operation.

In the final service-level burst, only 1 of 12 distinct reports was retained. More importantly, four ordinary guest `REPORT` commands through the actual router retained only two incidents; the other callers received `Err. Try again or send HELP.` Counts vary with scheduling. The configured router supports four workers, so concurrent intake is an intended execution path.

References: [incident creation](../src/outpost/watch/incidents.py#L230), [active-reference uniqueness](../src/outpost/store/migrations/0105_watch_incidents.sql#L32), [worker configuration](../src/outpost/config.py#L207).

**Correction:** Allocate the reference and insert the incident, origin record, and initial provenance within one transaction. Audit adjacent read-modify-write paths, particularly reaction sequence allocation and counters. Accept all distinct valid reports under concurrent member and dashboard activity; a losing race should never require a resident to resubmit a safety report.

### R2 — P1: Local mail has both a concurrency failure and a persistent interruption failure

**Confirmed implementation defect.** `MailService.send()` commits an insert with the globally unique UID `pending`, then commits a second write replacing that UID. Concurrent senders collide on the placeholder. In the final probe, only 2 of 12 sends were stored.

A fault injected after the committed insert but before UID finalization left `pending` in the database. Closing and reopening the database did not repair it; the next send failed with `UNIQUE constraint failed: mail.uid`. A process interruption in this window can therefore disable subsequent local mail until the orphan is repaired or removed.

References: [mail send](../src/outpost/bbs/mail.py#L36), [mail UID constraint](../src/outpost/store/migrations/0100_bbs_mail.sql). Compare the already transactional [BBS creation path](../src/outpost/bbs/service.py#L110), which passed the concurrent positive control.

**Correction:** Mint the permanent UID before insertion or wrap both operations in one transaction. Include recovery for existing placeholder rows, and test cancellation/interruption around every multi-statement mutation. A repair must preserve existing message and conversation identity.

### R3 — P1: A throttled urgent alert can block otherwise eligible replies

**Confirmed implementation defect.** The scheduler selects the alert class whenever an available alert exists. It checks the selected item's class allowance afterward. If the alert allowance is exhausted, it puts that alert back and returns without trying an eligible reply.

The durable-governor probe used the default 86.4-second alert allowance out of a 288-second ordinary hourly budget. With an urgent alert pending and the reply allowance unused, successive ticks sent nothing. Replies can expire after five minutes while the alert remains queued. Critical alerts have separate reserve handling; this finding concerns the pending noncritical alert blocking other eligible classes.

References: [class selection](../src/outpost/transport/governor.py#L745), [late class-budget check](../src/outpost/transport/governor.py#L878), [reply TTL](../src/outpost/transport/governor.py#L40).

**Correction:** Choose among items that are actually eligible under their class and global limits. Preserve critical priority, but skip temporarily ineligible work. Test that an exhausted class cannot starve another class with budget remaining.

### R4 — P1: Federation snapshots depend on the requesting node's wall clock

**Confirmed implementation defect and outage availability gap.** The requester sends a wall-clock snapshot. The producer filters its manifest to items with versions no later than that snapshot. An incident created on a node six hours ahead disappeared from the requesting node's manifest despite being allowed by peer policy. Without the snapshot filter, it was present.

This directly conflicts with REQ-FED-042 and the original ±6-hour acceptance case. The incident may eventually become visible as clocks catch up; it is not necessarily permanently deleted, but it can be absent when urgently needed. Timestamp-based incident conflict ordering deserves the same scrutiny.

The signed custody protocol also intentionally rejects creation times more than five minutes in the future. That protects validity boundaries, but its rejection tests establish fail-closed behavior, not continuity of legitimate communications between unsynchronized Outposts.

References: [manifest construction](../src/outpost/fed/sync.py#L181), [requester snapshot](../src/outpost/app.py#L2141), [federation specification](outpost-spec/docs/10-NODE-FEDERATION.md), [signed envelope time policy](FEDERATION-RELAY-PROTOCOL.md).

**Correction:** Use producer-owned sequence/snapshot identifiers for replication. Introduce explicit time-confidence diagnostics and a policy for clock uncertainty, backward jumps, forward jumps, expiry, and conflict ordering. Preserve replay and signature checks. Qualify RTC-backed operation and a local time source where appropriate. The Pi 5 has an RTC and supports a backup battery; the older blanket specification statement that a Pi has no RTC needs updating. [Raspberry Pi hardware documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#real-time-clock-rtc).

The live host currently reports synchronized system and RTC time. Battery-backed retention through a power cut was not verified.

### R5 — P1: Ordinary incident federation is not event-driven

**Confirmed requirement gap.** With Watch and federation enabled and a peer synced immediately before a new incident, repeated sync-loop calls through 60 seconds queued no federation work. The default and current appliance setting are a 60-minute sync interval. `IncidentService` has no corresponding change notification to promptly enqueue replication.

The normal reconciliation path uses the federation traffic class, which is also included in the default 22:00–06:00 quiet-hours policy. Delivery can therefore take considerably longer than the hourly interval. New remote incidents additionally enter human review before appearing as accepted Watch records. Review is a deliberate trust boundary, but its latency must be visible and accounted for.

References: [sync scheduling](../src/outpost/app.py#L2087), [default interval](../src/outpost/config.py#L365), [quiet hours](../src/outpost/config.py#L117), [REQ-FED-034 and phase acceptance](outpost-spec/docs/10-NODE-FEDERATION.md).

**Correction:** Record a durable replication event in the same transaction as an incident change. Give time-sensitive incident changes bounded priority distinct from bulk board catch-up, with dedupe and policy checks. Expose separately: stored locally, queued for peers, stored remotely, awaiting review, responder notified, and responder acknowledged. Keep explicit approval for alert broadcasts.

### R6 — P1 deployment fault: The installed service cannot recover this node after reboot

**Observed on the current host.** `outpost.service` is enabled but failed. Its journal records `database schema is newer than this Outpost binary`; after five attempts it reached the start limit. The running source-checkout process uses the newer code successfully, but does not repair the installed release selected at boot.

The schema guard is correct. The fault is release/database alignment, made easy to miss because the development process returns healthy HTTP responses.

References: [packaged service](../deploy/outpost.service), [schema downgrade guard](../src/outpost/store/database.py#L99), [existing handoff](../RESUME.md).

**Correction:** Deploy a compatible release through the normal verified upgrade path and verify the actual boot service. For ongoing development, isolate scratch development data or explicitly manage which release owns the live database. Acceptance must include rebooting the appliance and confirming radio, data, operator access, and outbox recovery without a terminal session.

### R7 — P1 specification choice: WAL with `synchronous=NORMAL` does not guarantee power-loss durability

**Confirmed configuration; loss not physically induced.** The writer uses WAL with synchronous level 1 (`NORMAL`). SQLite explicitly documents that committed WAL transactions can roll back after an operating-system crash or power loss in this mode. Passing `integrity_check` afterward does not establish that recently acknowledged reports, mail, or custody admissions survived. [SQLite synchronous documentation](https://www.sqlite.org/pragma.html#pragma_synchronous).

References: [database connection setup](../src/outpost/store/database.py#L60), [original mandated pragmas](outpost-spec/docs/05-DATA-MODEL.md).

This implements the original spec, whose comment describes it as durable enough. The off-grid use case is a reason to revisit that original choice rather than merely check compliance.

**Correction:** Establish an explicit durability contract for acknowledged data. Evaluate `synchronous=FULL` for the authoritative store, benchmark its cost on target storage, and combine it with an appropriate power system. Test retained acknowledged IDs and peer custody receipts after power interruption, not just database structural integrity. Storage hardware must honor flushes; a database setting alone is not whole-appliance qualification.

### R8 — P2: Short incident references are recycled while old information is still useful

**Confirmed behavior and design concern.** Once incident 1 becomes resolved, a new incident can receive reference 1 immediately. The probe showed a delayed `INC 1` selecting a different incident one second later. `CONFIRM` and `DISPUTE` resolve through the same short-reference lookup, so stale commands can act on the new record.

REQ-WATCH-011 calls for the smallest free active reference but also says it must not be recycled while visible in any listing. Retained history and long-lived radio messages make immediate reuse especially problematic during partitions.

References: [allocation](../src/outpost/watch/incidents.py#L275), [lookup](../src/outpost/watch/incidents.py#L424), [reactions](../src/outpost/watch/incidents.py#L843), [original rule](outpost-spec/docs/08-COMMUNITY-WATCH.md).

**Correction:** Prefer stable short identifiers with an Outpost prefix, or retain retired-reference tombstones for at least the relevant message/history lifetime. Confirmation prompts should show the incident title and origin. A delayed command must resolve to its original object or fail explicitly.

### R9 — P2 deployment gap: This host has no usable map pack at its configured path

**Observed on the current host.** `store.tiles_path` resolves to `/var/lib/outpost/.data/tiles`, and inspection as the service user returns `missing`. A different tile directory exists in the checkout, but the application is not configured to use it. The map controller requests online tiles first and falls back to local tiles after failure.

The dashboard shell, lists, and markers can still function without WAN; a usable offline basemap is not presently provisioned at the configured location. An online browser can conceal this problem.

References: [tile inspection](../src/outpost/web/tiles.py#L44), [online tile request and local fallback](../src/outpost/web/static/map-controller.js#L479), [installer's nonfatal map download](../deploy/install.sh#L266).

**Correction:** Provision the region/zoom pack in the service-readable location, verify its bounds and coverage, and test a fresh browser with external connectivity unavailable. A usable single tile is not sufficient evidence that the entire operating area is covered. Prefer local tiles in an explicit outage mode and make missing coverage prominent in readiness.

### R10 — P2: The incident acknowledgement instructs users to send an unregistered command

**Confirmed by source and catalogue inspection.** A report without coordinates tells the resident to send `UPD <number> <where>`. The current registered command set has no `UPD` command. This breaks the correction loop exactly when a responder needs a location.

Reference: [acknowledgement text](../src/outpost/commands/watch.py#L47) and the command registration in that module.

**Correction:** Implement an authorized member correction flow with provenance, or point the acknowledgement to a real supported flow. Add a check that actionable commands embedded in help and response text resolve in the registry. Test the complete report → missing-location prompt → correction journey on a handheld.

## What is worth preserving

- **Local-first domain model.** Boards, incidents, welfare records, and mail outlive a chat exchange. Non-AI features do not fundamentally require internet or the accelerator. SQLite remains a reasonable local authority; the demonstrated transaction failures call for better transaction boundaries, not a new database server.
- **Centralized outbound scheduling.** Durable admission, severity ordering, regional limits, pacing, retries, and receipt history provide the right place to enforce scarce-radio policy. The scheduler defect is serious but localized.
- **Explicit failure domains.** Core-task supervision and watchdog behavior are implemented; optional tasks have independent restart/backoff behavior. Preserve this separation while improving appliance-level readiness.
- **Federation trust and reconciliation.** Bilateral pairing, replay counters, policy filtering, quarantine, origin attribution, merge/unmerge history, and custody receipts are substantial capabilities. Existing two-node field records include real radio board sync, mail, rejection probes, revocation, and recovery.
- **Accountability and privacy.** Separate web and mesh identities, reviewed PKI for protected actions, default-deny wallboard access, step-up authentication, audit history, and location-retention controls are valuable. Human fingerprint verification and operator-readable storage remain explicit trust boundaries.
- **Human-controlled alerts.** Quarantine, acknowledgement tracking, zero-recipient handling, and guarded drills are preferable foundations for community coordination. Distinguishing queue admission from successful delivery remains essential.
- **Constrained AI.** Permission-scoped retrieval, deterministic refusals, evidence checks, and an extractive fallback reduce dependence on small-model judgment. Recorded raw-model failures make those guards necessary. A guarded evaluation pass measures the whole guarded service, not unrestricted model reliability.
- **Useful development evidence.** Packaged-wheel testing, two supported Python versions, browser/accessibility coverage, capability manifests, replay, and target-hardware records are strong assets. They need more scenarios that combine concurrency, failures, and network conditions.

## What is missing or should be stronger

### Network capacity and alternate paths

The default allowance is per Outpost, not a network-wide promise. Ten co-located transmitters each allowed 8% ordinary airtime could offer 80% before accounting for residents, relay repeats, and other traffic. That arithmetic is an offered-load warning, not a prediction that the governors will actually transmit that amount: channel-utilization gates will pause work first, creating latency and expiry.

The current governor estimates a complete 188-byte federation frame at **1.829 seconds** on LONG_FAST. The default federation share is **28.8 seconds/hour**, or approximately **15 full-sized frames/hour per Outpost**, before additional control frames, receipts, or retries. Smaller/compressed frames improve this figure, but bulk synchronization cannot be treated as cheap.

Private messaging channels and custom application port numbers organize traffic; they do not provide separate RF capacity. Meshtastic secondary channels share the primary channel's modem settings and frequency slot. [Meshtastic configuration documentation](https://meshtastic.org/docs/configuration/tips/#chat-channels-and-lora-frequency-slots).

Recommended architecture: resident traffic over local LoRa; bounded, selective replication to neighboring Outposts; a tested local IP/backhaul path for bulk transfer where available; and signed file transfer for periods when no radio route exists. A local MQTT broker/backhaul can operate without public internet if its entire path is locally powered and reachable, but a public-broker route cannot bridge an outage. Do not infer a direct RF route from MQTT discovery.

**Missing original deliverable:** no signed sneakernet sync export/import implementation was found for REQ-FED-041. Replay bundles and full database backups are different artifacts. A signed, policy-filtered, replay-resistant sync bundle is a particularly valuable next capability for a physically partitioned region.

The manifest implementation also reads and sorts whole eligible collections before slicing each network page. Network page bounds do not bound that database/memory work. Move filtering/pagination into indexed queries and measure catch-up with realistic retained history.

### Node loss, power, and replacement

Selective federation is not a hot standby. A neighboring Outpost does not automatically take over another node's private mail, welfare roster, member identity, or operator accounts. Define what survives loss of a particular node, which data is replicated by consent, and how a replacement resumes its identity without replay/counter or signing-key confusion.

Prepare a recoverable offline kit: tested boot media, a compatible release and dependency wheelhouse, vendor runtime/model artifacts if AI is used, radio firmware/client installation material, regional maps, configuration, keys, and encrypted off-device backups. The normal installer still depends on network downloads. Successful operation without WAN does not imply successful replacement installation without WAN.

The original encrypted-backup requirement, REQ-SEC-042, is not represented by an integrated recipient-encrypted backup path. Current snapshots are database files; the operator documentation recommends external encrypted storage. Document and test the actual encryption and restoration procedure, and include configuration/identity material as well as the database.

Measure whole-station watts, energy per day, battery autonomy, cold starts, and thermal behavior with LoRa, SDR, and AI active. The existing battery monitor concerns the attached mesh radio; it is not a Pi/UPS state-of-charge monitor. Add low-storage and low-energy operating policies that preserve urgent intake and authoritative records before discretionary workload. The current AI-enabled process was about 840 MiB RSS in a brief observation; this includes native inference and is not a valid isolated measurement of the original non-inference memory budget.

### Truthful readiness and operational workflow

Provide a distinct **outage readiness** view. HTTP health, an open USB radio, and an SDR PCM stream answer narrower questions than whether the community can operate after a power/network failure. Include boot-service compatibility, backup restorability, time confidence, map coverage, local access, storage reserve, power autonomy, peer-path evidence, and an available responder audience.

SDR audio above an RMS threshold establishes a working audio pipeline, not proof of intelligible weather reception or a successful SAME decode. The current session has no decoded header. Add dated station/antenna qualification and an expected test-message observation procedure, and distinguish signal present from decoder verified.

Preserve a supported operator LAN during an outage. The existing setup hotspot is intentionally temporary and expires; it should not be the only planned access method. Prepare radios and phone clients before networks fail and test common actions with nontechnical participants. Missing-location correction, missed acknowledgements, stale references, and multi-packet responses deserve particular attention.

A useful next coordination capability is explicit incident responsibility: assigned team, accepted assignment, last verified update, next action, and handoff. Responder groups and acknowledgements exist, but an acknowledgement alone does not establish ownership or completion. Cross-Outpost welfare/identity handling also needs an explicit policy for residents who move between communities; do not automatically replicate sensitive records to solve that problem.

Guest emergency admission is intentionally available under ordinary rate limiting, and changed safety details remain admissible. Preserve that accessibility while testing sustained malicious or accidental bursts, storage growth, queue headroom, and operator review load. Per-node identities and repeated confirmations are not proof of independent people.

### Maintainability and specification drift

The composition module is 3,547 lines and the web API module 5,119 lines at this revision. SQL and domain orchestration span multiple modules. This increases the chance that a correct transaction pattern in BBS is not applied to mail or incident intake. Extract cohesive services and route modules incrementally, with explicit transaction ownership and the new failure probes protecting behavior. A microservice rewrite is not needed.

The original spec now contains outdated or revised assumptions: direct federation delivery versus the implemented authenticated broadcast carrier, earlier model limitations, blanket RTC assumptions, raw tool-calling versus deterministic retrieval, and the initial durability choice. Keep a requirement-by-requirement disposition ledger with implemented/tested, accepted replacement, explicitly deferred, or withdrawn status. The 17-capability manifest is not that ledger. The older exit audit's conclusion that software remediation was complete should not be interpreted as closing the newly reproduced failures.

## Original outcome check

| Original goal | Assessment at this revision |
| --- | --- |
| G1: Phases 1–3 work with zero WAN | Core architecture supports this. Current transaction failures and missing configured maps prevent an unqualified pass. Complete WAN-down operator acceptance remains necessary. |
| G2: At least 80% of first-time users post within three messages | Menus and shortcuts exist. No representative user-study result was found; the unsupported correction hint is a concrete usability failure. |
| G3: Seven-day live-mesh airtime discipline | Governor has extensive software coverage. Network-wide capacity, firmware airtime calibration, busy-channel behavior, and the live soak are not established by this review. |
| G4: Useful guarded local AI with zero safety-category failures | Prior guarded 60-case target-hardware passes are documented. Preserve the guards; expand blinded/adversarial evaluation and measure value relative to deterministic retrieval and energy cost. |
| G5: Thirty-day unattended operation and automatic recovery | Not accepted. Recorded field gates remain open, the current boot service fails, local mail has a persistent interruption window, and power-loss durability is weaker than acknowledged-data expectations. |
| G6: Incident visible across the mesh and on maps within 60 seconds | Local routing/UI have implementation and simulated evidence. Routine cross-Outpost propagation does not satisfy the target; queue and clock defects further weaken it. Define separately local visibility, remote receipt, remote acceptance, and responder acknowledgement. |

## Outage scenarios

| Situation | Expected capability and limitation |
| --- | --- |
| Internet and cell networks fail; Outposts and RF links retain power | Local boards, mail, incident tracking, welfare, and radio commands can continue, subject to the confirmed defects. Internet-backed feeds eventually become stale/unavailable. |
| Neighboring Outposts retain a usable RF path | Selected records and mail can cross by radio. Timeliness, clock behavior, congestion, and operator review must be corrected and qualified. |
| Neighboring sites were connected only through an internet MQTT path | That connection is lost. Federation cannot create an RF path that does not exist. |
| RF coverage is partitioned | Each powered Outpost remains a local island. Existing queues and custody can support later transfer within their expiry/policy limits; signed physical sync remains missing. |
| An Outpost loses power or storage | Residents may retain ordinary radio communications, but that server's unreplicated records and services are unavailable. Neighboring instances are not automatic replacements. |
| A long outage removes all online weather sources | Local astronomy and retained local data continue. SDR may supply received warning headers while its external broadcast station remains operational; it does not replace internet forecasts. |

## Recommended sequence and release gates

1. **Repair data correctness:** R1/R2 transactions and recovery, R3 scheduler eligibility, R8 stable references, and R10 correction flow. Audit adjacent operations for the same patterns. Require concurrent requests and fault-injection probes to pass.
2. **Make replication meet the outage contract:** R4 producer-owned cursors/time policy and R5 durable prompt incident updates. Measure admitted → radio-sent → remotely-stored → reviewed → acknowledged separately. Test local resolve/reopen changes against remote stale updates and partitions.
3. **Align appliance durability and preparation:** R6 installed-release alignment, R7 durable commits/power policy, R9 map provisioning, encrypted off-device recovery, clock retention, and independent local access. Verify replacement from prepared media with WAN unavailable.
4. **Exercise an actual community network:** start with three physically separate Outposts and at least six participating handhelds. Remove all WAN/cellular paths from the exercise, including operator phones. Test radio-only board/mail exchange, simultaneous reports, delayed/duplicate/missing fragments, overlapping incidents, reassigned responders, local approval, all-clear, a partition, and catch-up. Include a route that requires an intermediate node.
5. **Qualify failures and capacity:** controlled power cuts during acknowledged writes and custody transfer on sacrificial/fully backed-up test hardware; repeated LoRa/SDR disconnect/reconnect; loss of a peer; clock offsets of ±6 hours and time jumps; nearly full storage; saturated classes and channel utilization. Verify retained message IDs, no unintended duplicate actions, bounded memory/queues, and visible failures.
6. **Close the original operating gates:** seven-day live airtime exercise, thirty-day unattended soak, and installation/recovery by a second operator. Record firmware, presets, power/storage hardware, packet counts, latency distributions, expiry/drop reasons, and configuration alongside results. Update the capability evidence and requirement ledger from those outcomes.

Acceptance should require zero lost acknowledged authoritative records in the declared power-failure contract; no silent wrong-incident action; no eligible urgent/human traffic starved by unrelated work; and an honest, tested recovery path for every unavailable dependency. Where the radio cannot meet a latency target under the declared load, show that limitation and change capacity/operating policy rather than reporting success at queue admission.
