# Phase gate hardware checklist

- Fresh Pi 5 install from `deploy/install.sh`
- Serial connect and unplug/replug recovery
- BLE connect, flap recovery, and serial recommendation after five failures
- `PING`, `ABOUT`, `HELP`, `WHOAMI` round trips from a handheld
- Multi-part ordering and 12-second pacing
- 24-hour `airUtilTx` comparison against the ToA model (recalibrate if error exceeds 15%)
- Governor throttling on a busy channel
- Dashboard on a phone with WAN disconnected
- Ten power removals during writes, each followed by `PRAGMA integrity_check`
- 72-hour Phase 0 unattended soak with stable RSS and no restarts
- Six-participant Community Watch exercise in `PHASE3_TABLETOP.md`
- `python tools/verify_same_audio.py` decodes the checksum-pinned NPT fixture
- RTL-SDR is identified by serial and opens without root
- Correct local NOAA Weather Radio frequency produces sustained PCM audio above the silence floor
- Unplugging the RTL-SDR produces bounded backoff; reconnecting restores `listening` without a service restart
- A received RWT/RMT/NPT is visible as log-only and never enters an outbound queue
