# Connected-radio power

A radio powered by the Pi's USB supply can report external power without a battery
percentage. Outpost shows **External power** on the Radio page and in the situation
briefing. A fresh external-power report passes the `radio_power` readiness check.
Other readiness checks retain their own results, including whole-station power.

## Interpreting telemetry

The [Meshtastic device-metrics protocol](https://github.com/meshtastic/protobufs/blob/master/meshtastic/telemetry.proto)
defines battery percentages from 0 through 100 and values above 100 as powered.
The adapter retains valid unsigned external-power values as the internal sentinel
101; the governor forwards that report to power monitoring before discarding the
sentinel from percentage-only metrics and low-battery shedding.

| Report | Display and readiness |
| --- | --- |
| External power, fresh sample | External power; radio-power check passes |
| Battery above warning threshold, fresh sample | Percentage; radio-power check passes |
| Low or critical battery, including 0% | Percentage and warning/critical condition; check fails |
| Missing, malformed or unavailable report | Not reported; check remains unknown |
| Future-dated sample | Check remains unknown |
| Expired sample | Check becomes stale |

External power supplies no battery percentage or battery trend. Percentage metrics
remain NaN/unreported, and battery-based discretionary shedding remains inactive.
Returning to a low battery or losing the power report immediately updates the
condition and invokes the existing readiness trigger. No new polling or radio
transmissions are added, and airtime limits and traffic priorities are unchanged.

The existing sample-expiry and report-expiry policies still apply. A serial, TCP or
BLE connection alone does not establish a power source. This is the connected
radio's reported supply state; it does not measure the Pi/AP/storage's usable energy,
UPS runtime, charging capability or survival after power interruption.

## Storage and compatibility

Migration 186 adds a checked `external_power` flag to the existing bounded power
history. Actual battery percentages remain constrained to 0–100. The flag defaults
to false for historical records: old NULL battery samples cannot distinguish
external power from missing telemetry, so they retain unknown status until a new
report arrives. Sample IDs and timestamps remain intact.

The power API adds `external_power` to its snapshot/history and the `external`
condition. `reported` continues to mean that a battery percentage is available.
No wire-format, configuration, identity or retention-policy changes are introduced.
Deploy through the normal verified updater; schema downgrade guards remain active.

## Verification

`test_local_power_telemetry_preserves_external_supply` exercises external-power
values, actual 0/100 percentages and invalid/missing values through the radio adapter.
`test_external_power_survives_adapter_governor_storage_and_readiness` uses the real
adapter, governed observation, database, restored monitor, power API, briefing and
readiness, including external/missing/critical transitions and expiry.
`test_external_power_migration_preserves_legacy_unknown_samples` checks upgrade
retention without reclassifying old NULL samples. The outage-readiness cases retain
future/stale and whole-station boundaries. Chromium tests exercise external, missing
and low-battery labels on 320px and 1280px layouts in all three themes.

[The September 8 local Pi observation](RADIO-POWER-QUALIFICATION-2026-09-08.md)
records the verified schema-186 deployment, actual external-power readiness pass,
served dashboard assets and retained records. Whole-station power remains separate.
