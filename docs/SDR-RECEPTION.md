# SDR reception and station qualification

The Environment page separates the running receiver, fresh audio, audio level,
station/antenna observation and the last verified decoder header. A connected
USB device, high audio RMS or an end marker cannot establish intelligible weather
reception. Noise can exceed the audio threshold. A required test is a **DRILL / TEST**,
logged without an approval button; live relevant warnings retain human approval,
CAP deduplication, expiry and the normal governed delivery path.

## Reading the indicators

- **Receiver pipeline:** the supervised `rtl_fm` / `samedec` process pair. Fresh
  PCM is measured separately and becomes stale at `audio_stall_seconds`.
- **Audio level:** the latest finite, positive PCM RMS compared with
  `signal_rms_threshold`. Below-threshold or absent audio never inherits a prior
  above-threshold claim. This is not calibrated RF strength or intelligibility.
- **Station / antenna qualification:** the existing dated SAME readiness
  observation. An operator statement is displayed as **Operator attested**, not
  automatic certification. It expires after one day, or needs review after a
  process restart, relevant policy change or clock uncertainty. Opening Environment
  does not renew it or run a hardware exercise.
- **Last verified decoder header:** a complete header parsed from the supervised
  decoder, with event/test classification, relevance and elapsed age. Direct
  fixture/import ingestion cannot establish receiver evidence. An expired/future
  header does not establish current reception. Evidence expires at
  `env.same.decode_stale_hours` (default **192 hours / eight days**, range 1–720).
  This review window allows a weekly test with some delay; it is not a broadcast
  guarantee. Elapsed age does not use wall-clock subtraction.

A restarted pipeline clears its audio evidence and marks the previous decode as
before the restart. Changing receiver settings invalidates the decode's context.
A full Outpost restart begins new current-process evidence; historical SAME
messages remain in the durable event log below the indicators. The console does
not infer current antenna reception from those old records. A failed receiver
refresh clears its current-status display. Weather/forecast failure does not hide
successful independent receiver updates.

## Prepare a dated station and antenna record

Record the release/schema, decoder version, receiver model/serial, antenna and
feedline, placement, tuned frequency, gain/PPM/sample settings, configured SAME
areas, station identity and programming office. Keep device identifiers and exact
locations in protected local evidence. Check station identification and sustained
speech intelligibility using a separate receiver or a deliberately scheduled
receive-only listening session. Only one program can own the SDR at a time.

Check the station's current test schedule and outage information before scheduling
an observation. NWS describes a usual Wednesday 10 a.m.–noon local test window,
with postponements or other schedules possible; confirm with the programming
office for the selected station. Reception can vary by location and conditions.
See [NWS test guidance](https://www.weather.gov/nwr/nwrtest) and
[NWS transmitter outages](https://www.weather.gov/nwr/outages). A missed scheduled
test is an unresolved observation until the station's actual broadcast is checked.

During the legitimate broadcast test, retain the received header/event identifier,
receive time, local area match, test classification and reception/error/restart
indicators. Confirm the message is visibly a drill, remains log-only and creates
no actionable alert or outbound work. Preserve the evidence before updating the
SAME observation in the readiness menu. Do not mark a station qualified from the
software fixture alone.

## Offline regression and interruption checks

`tools/verify_same_audio.py` checks the pinned upstream public NPT sample against
`samedec`; it never opens the SDR or ingests into the running application. Its
checksum is `65c58a6c3e34fa5ed68f7288b6f10369bfb73034e5f12da5e8e63671dcf15b88`.
Prepare the file while connected, then verify its offline copy:

```sh
python3 tools/verify_same_audio.py --fixture /path/to/npt.22050.s16le.bin
```

The filename/rate and sample format are documented by the
[upstream decoder](https://github.com/cbs228/sameold/tree/samedec-0.4.2/sample).
The sample is public test material, not captured private station data. The check
proves decoding that recording only; it does not qualify the antenna or station.

For an explicitly scheduled hardware session, stop the normal receiver before
using `tools/check_same_hardware.py`. Its default pass proves the process/audio
pipeline only. `--require-test-decode` additionally waits for a complete, current,
relevant test header from the current process pair. `--require-restart` requires a
restart as well; combine them to require a decode after that restart. The tool uses
a temporary isolated database and has no transmitter or alert dispatcher.

```sh
/opt/outpost/current/bin/python tools/check_same_hardware.py --device SDR_SERIAL --frequency 162.550 \
  --county PSSCCC --timeout 7200 --require-test-decode
```

Use actual prepared settings in place of the placeholders. Preserve the tool's
console output, including its timestamped decode summary, in the session evidence
before the temporary database is removed. Record a separate
physical USB-disconnect/reconnect and process-interruption exercise, including
repeated failures, bounded backoff, recovery and process cleanup. Compare original
and resulting event/alert/outbound identifiers to detect duplicate actionable
warnings or unintended sends. Restore the normal service and verify it owns the
SDR and receives audio again. Synthetic process interruption regressions are
preparation for this hardware witness, not a replacement for it.

SAME depends on an operating, receivable broadcast transmitter and the installed
antenna path. It carries alert headers and does not generate a local offline
forecast. Forecast availability and age remain separately labeled.
