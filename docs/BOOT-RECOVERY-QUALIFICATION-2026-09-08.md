# Controlled reboot recovery — September 8, 2026

The local Raspberry Pi passed #136's installed-release alignment and unattended
controlled reboot acceptance. The enabled packaged service recovered web access,
radio connectivity, required native Hailo AI, schema integrity and retained data
without an interactive service start. [Repair procedure](BOOT-RECOVERY.md).

## Release and deployment

| Item | Verified value |
| --- | --- |
| Selected and running release | `20260908T160830Z-045130381360` |
| Installed source | `045130381360e23aa89e093b8afb0c87613cef6d` |
| Database schema / packaged migration capacity | 185 / 185 |
| Previous failed boot release capacity / original live schema | 153 / 172 |
| Deployment | Normal verified updater with explicit incompatible-boot recovery and the native HailoRT wheel |
| CI | [Run 34242955400](https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/34242955400), all four jobs successful for the installed source |

Each full CI suite passed 2,261 tests; each Python version's production-mode suite
passed 1,209 tests. Recovery/ownership regressions, installed-wheel smoke and the
actual checkout refusal before opening the installed database also passed before
reboot. These identify the deployed software; later documentation commits are
separate from its release and CI identity.

Encrypted pre-repair schema-172 and post-repair schema-185 recovery archives were
read back from the owner's USB, decrypted and checked against source and component
hashes. Both databases passed integrity and foreign-key checks. The USB was safely
unmounted. The recovery key is separate from the USB; owner custody outside the Pi
remains outstanding. Private snapshots and observations are retained locally.

## Actual reboot observations

The pre-reboot baseline was captured at **16:14:31 UTC**. A temporary systemd
collector, armed before reboot and ordered after Outpost, captured the passing
post-boot witness at **16:17:29 UTC**, at **99.62 seconds of uptime**. Its journal
records automatic execution and successful completion. It only observed recovery
and requested diagnostics; it did not start Outpost. The boot ID changed and matches
the running system. Raw boot IDs, process identifiers and record identifiers remain
in private evidence.

An independent follow-up at **16:19:26 UTC** confirmed the same boot, selected
release and original service process, with **zero automatic restarts**.

| Check | Post-boot result |
| --- | --- |
| Packaged systemd service | Enabled, active, running; intended packaged command |
| Health endpoint / web page | HTTP 200 / HTTP 200 |
| Radio / core tasks | Up / healthy |
| Required AI | Hailo VLM ready and healthy |
| SQLite | Schema 185, integrity `ok`, zero foreign-key errors |
| Fresh readiness | Completed; `boot_schema: pass`, running and boot package at the same location |
| Release selection | Unchanged across reboot and follow-up; recorded CI remains successful |
| Record retention | No missing pre-reboot authoritative records or durable work |

## Retained records and work

Private comparisons used primary-key sets, rather than aggregate counts alone.
Every record present before repair remained after the follow-up; every record in
the later pre-reboot baseline remained in both post-boot observations.

| Record family | Before reboot | Automatic post-boot witness | Follow-up |
| --- | ---: | ---: | ---: |
| Incident | 9 | 9 | 9 |
| Incident update | 11 | 11 | 11 |
| Mail | 7 | 7 | 7 |
| Federation mail delivery | 0 | 0 | 0 |
| Outbound work | 1,558 | 1,558 | 1,559 |
| Federation relay envelope | 0 | 0 | 0 |

The initial pre-repair outbox contained terminal history only. Normal operation
created **five pending items before reboot**; all five survived and remained pending
at both observations. Follow-up included one additional pending item. Remote
delivery and acknowledgement of these items were not measured. No missing work
required a terminal-history retention exception, and no older snapshot was restored
over newer acknowledged data.

## Scope and cleanup

This result qualifies one controlled reboot on the repaired local Pi. Abrupt
power-loss durability (#44), battery runtime/storage/shutdown qualification (#148),
whole-appliance replacement and second-operator access (#145/#147), WAN-down and
multi-node delivery, and the thirty-day soak (#158) retain their own acceptance
gates. The separate second machine is updated by its owner.

Overall field readiness remains **degraded**: offline maps fail their check, while
power, offline time confidence, restore, local-access, peer-path and SAME observations
remain unknown. The boot result does not change those statuses.

After reviewing the passing witness and live process, the temporary qualification
unit was disabled and removed. Its armed file is absent, private observations and
backups remain, and the original enabled Outpost service continued running.
