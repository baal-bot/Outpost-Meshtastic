# SDR reception software qualification — September 9, 2026

Issue [#150](https://github.com/baal-bot/Outpost-Meshtastic/issues/150) identified
fresh receiver audio without proof of intelligible station reception or SAME
decoding. The Environment console now separates those observations and their age.
Read [SDR reception and station qualification](SDR-RECEPTION.md) for the operator
procedure, thresholds and remaining physical evidence.

## Software behavior and local evidence

The pipeline, fresh PCM, latest audio level, dated station/antenna statement and
verified decoder header are independent indicators. Decoder evidence is accepted
only from the supervised decoder, with complete parsing, relevant context and
clock/elapsed checks. Restart, configuration change, noncurrent header timing and
expiry remain visible. Historical records persist through restart without being
reclassified as current reception. Operator statements retain their original date,
one-day expiry, process/policy scope and audit record.

The receiver panel updates when WAN weather/forecast is unavailable, and clears
current status after a receiver-fetch failure. Tests/demos show **DRILL / TEST · log
only** and cannot be approved for broadcast. Existing county filtering, live
warning review, CAP deduplication, expiry and governed delivery remain enforced.
No schema migration, RF transmission or clock-setting privilege is introduced.

Local Linux ARM64 / Python 3.13 verification passed:

- **195** receiver, SAME review/deduplication, outage-readiness and configuration
  tests. Actual child-process tests repeat both `rtl_fm` and `samedec` failure,
  recover through bounded backoff, retain one drill and one pending warning, and
  preserve the reviewed warning without creating another actionable alert.
- **33** authenticated API/browser tests, including the new reception indicators
  at 320/1280 pixels in dark/daylight/night themes, lost/weak/stale evidence,
  observation freshness and existing readiness/clock behavior.
- **Four** existing Environment workflow and web-access compatibility tests.
- Ruff formatting/lint, mypy, the unchanged strict typing debt ratchet, command and
  requirement ledgers, static markup and capability validation.

Focused coverage is **89%** for `same.py` and **87%** for `same_receiver.py`.
Screenshots were inspected at phone/desktop sizes; no horizontal overflow or
browser script errors were observed in the new cases.

The installed `samedec` decoded the pinned public NPT audio fixture from an offline
copy with the expected SHA-256 and exact header. This opened no SDR, populated no
live application records and sent nothing. The hardware observation helper now
labels its default result as a pipeline check; requiring a test decode also checks
current, relevant test evidence and emits a timestamped summary for retention.

## CI, installation and remaining field gates

Exact-source CI and normal verified installation are pending. This record does not
yet claim the new source is installed; the final release and live preservation
checks will be added after completion.

The synthetic subprocess exercise is not a physical USB-disconnection test. No
legitimate antenna-received broadcast test, station speech/intelligibility check
or physical interruption campaign was performed in this session. Those #150
criteria remain open and the capability remains **hardware-gated**. No GitHub issue
state or live operator observation was changed.

Private evidence is retained under `.data/sdr-reception-2026-09-09/`; installed
preservation evidence will use
`/var/lib/outpost-qualification/150-20260909/sdr-reception-update/`.
