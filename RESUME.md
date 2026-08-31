# Session resume point — 2026-08-30

## Repository state

- Repository: `baal-bot/Outpost-Meshtastic`
- Branch: `main`
- Last completed feature commit: `6397fbc2904a679f0fd7e97ca06062e5a763e173`
- Commit title: `feat: add recurring welfare drills (#124)`
- `main` and `origin/main` matched and the worktree was clean before this handoff.
- GitHub CI run `33342427525` passed all four Python 3.12/3.13 and locked-runtime jobs.
- Issue #124 is closed with its implementation and verification record.

## Live state before reboot

- The development server was running directly from this checkout as the `outpost` user.
- Health returned HTTP 200 with `{"status":"ok","version":"0.1.0"}`.
- `/var/lib/outpost/outpost.db` migrated successfully through schema version 172.
- The attached Meshtastic radio reported healthy.
- Migration 0172 created the responder-group and recurring-welfare tables.
- No responder groups or recurring schedules were created in live data during implementation.
- The manual development process will not survive a reboot. Check whether another service owns port 8080 before starting it again.

## What #124 added

- Named responder groups with general, medical, fire, search and rescue, logistics,
  communications, and public-safety types.
- Weekly, biweekly, and monthly welfare drills in configured local time.
- Frozen drill rosters, explicit `DRILL` markings, automatic response windows, and retained run history.
- Exact recipient/message/airtime preview with hard recipient and airtime ceilings.
- Stale previews and later roster/airtime growth stop a send pending fresh operator review.
- Drills yield to real watch events and digest quiet hours.
- Members can use `DRILLS ON|OFF` without changing eligibility for real welfare checks.
- Participation reporting includes per-net response rate, never responded, and not heard since the latest net.
- Real `HELPME` reports remain urgent even when received during a drill.

## Verification completed

- Ruff formatting and lint, strict source mypy, the mypy ratchet, capability and command catalogues,
  markup safety, shell syntax, Python compilation, and diff checks passed.
- Focused safety, check-in, situation, maintenance, governor, mesh-command, and browser tests passed.
- The complete responsive Chromium matrix passed.
- Critical coverage passed: global 80.8%, safety 88.0%, safety commands 97.5%, and all production floors.
- Production-wiring coverage and the packaged-wheel smoke installation passed.
- The live schema, protected API boundary, Watch assets, radio health, and process health were checked after restart.

## First steps after reboot

1. Confirm the checkout is still on `main` at or beyond `6397fbc`, the worktree is clean, and fetch/pull with fast-forward only if needed.
2. Check for an existing Outpost process or system service on port 8080 before launching a development process.
3. If no instance is running, start the source checkout with:

   ```sh
   sudo -n -u outpost env OUTPOST_CONFIG=/etc/outpost/config.yaml \
     PYTHONPATH=/home/brendtpe/Documents/coding_projects/mesh-outpost/src \
     /home/brendtpe/Documents/coding_projects/mesh-outpost/.venv/bin/python -m outpost
   ```

4. Verify `http://127.0.0.1:8080/api/v1/health` returns HTTP 200 and confirm the radio is up before testing mesh behavior.
5. Open the Watch page and visually confirm the welfare schedule, responder groups, and participation-history panels.

## Remaining GitHub queue

- #44 is the P1 red-team remediation tracker. All software work is complete; it intentionally remains open for
  two physical field gates: destructive power-cut/recovery evidence and a physical two-node MQTT-only plus
  automatic LoRa/MQTT fallback campaign. Do not close it based on simulation.
- #61 is the only other open issue and is a P3 future native iOS/Android companion architecture.
- The next session should choose between executing/documenting a #44 hardware campaign or beginning #61 product
  architecture; there is no remaining open software-defect ticket in the current queue.
