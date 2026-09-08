# Repairing an incompatible boot release

An enabled service can fail at boot when a source checkout has migrated its database
beyond the release selected by `/opt/outpost/current`. The schema downgrade guard
must remain enabled. A working checkout process does not repair the boot selection.

## Preserve current data and inspect the intended release

Retain and validate an independent recovery copy of the current database and its
configuration before changing the installation. Use SQLite's backup API or the
supported backup tools; copying a running database file without its WAL is insufficient.
Keep backup contents, credentials and record identifiers private.

Record the selected release, its migration capacity, the current database schema and
integrity, and the target's successful exact-commit CI evidence. Confirm that the
target supports the current schema and retains the required radio, native libraries
and model assets. Stop any checkout process using the installed database.

## Explicit forward recovery

From a clean checkout containing the repair support, run the normal verified updater
as the checkout owner, selecting a full commit whose CI has passed:

```sh
OUTPOST_RECOVER_INCOMPATIBLE_BOOT=1 ./deploy/update.sh <verified-full-commit>
```

Include the usual hardware-specific installer inputs, such as `OUTPOST_HAILORT_WHEEL`
for Hailo. The recovery option does not bypass exact-commit CI, database integrity,
schema capacity, package, configuration or service health checks. It is used only
when the current database is already newer than the previous release. Without this
explicit option the updater refuses that situation before changing the boot selection.
It always rejects a target older than the current database.

The installer takes a verified snapshot, records that the previous release cannot
read it, marks database ownership, selects the compatible release atomically, clears
the old systemd failure limit and starts the packaged service. Successful recovery
keeps the current data and applies only forward migrations.

If startup or health fails, the new release remains selected but stopped. The installer
preserves the live database and captures a forensic snapshot where possible. Repair
the startup fault or install another verified compatible release. Automatic and manual
rollback to the incompatible predecessor are refused; no older snapshot is restored
over newer acknowledged data. Ordinary upgrades with compatible predecessors retain
their existing rollback behavior.

If the failed service cannot be stopped, the installer reports its state as
unconfirmed and preserves the new selection and live data without attempting rollback.

## Development ownership and reboot evidence

Database opening checks ownership before creating a SQLite connection. Standard
`/var/lib/outpost` stores are reserved even before the first marker; the installer
also creates `<database>.deployment.json` beside custom stores. Both the interpreter
and imported package must belong to the selected release. Use separate configuration,
state and simulated radio for development. The marker is an accidental-use guard,
not a security boundary against an administrator or an older binary.

After recovery, record service health, the selected and running release, schema,
web reachability, actual radio state and retained authoritative records/durable work.
Run a fresh readiness check: its boot-schema result must show sufficient capacity
and the same package location. Other unmet field checks retain their own status.

A controlled reboot must then start the enabled packaged service without a terminal
session. Compare boot IDs and capture the same observations after reboot, including
any expired or delivered durable work. A service restart, simulated test or static
schema pass does not satisfy that physical reboot criterion. #136 remains open until
the actual deployment and reboot evidence has been recorded; physical power-loss
qualification stays with #44.
