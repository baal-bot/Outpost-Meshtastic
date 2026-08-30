# Development and contributing

## Setup

Outpost explicitly supports Python 3.12 and 3.13. CI runs the complete unit, integration,
acceptance, browser, coverage, and packaging suite on both versions. A new Python minor is not
supported until it is added to that matrix; the package metadata intentionally rejects versions
outside the tested `>=3.12,<3.14` range.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,radio]'
cp config/config.example.yaml config/config.local.yaml
```

Set `router.intents_file: config/intents.yaml` and a writable store such as `.data/outpost.db`:

```sh
OUTPOST_CONFIG=config/config.local.yaml .venv/bin/python -m outpost
```

The app runs without hardware; use simulated transports in automated tests. Never commit a real
config, database, API/channel key, member export, precise location, or tile cache.

## Layout

| Path | Purpose |
| --- | --- |
| `src/outpost/app.py` | Composition and lifecycle |
| `src/outpost/config.py` | Strict config and environment overlay |
| `src/outpost/commands` | Mesh commands |
| `src/outpost/transport` | Radio, queues, airtime, supervision |
| `src/outpost/store` | Database and migrations |
| `src/outpost/web` | API, auth, settings, dashboard |
| `src/outpost/fed` | Federation framing, peers, sync, mail |
| `src/outpost/ai` | Provider adapters, budgets, retrieval, safety and agent runtime |
| `tests` | Unit, integration, acceptance, hardware procedures |
| `deploy` | Installer, service, package smoke test |

## Quality gates

Before pushing, run the lightweight formatting helper. Its scope is kept identical to the first CI
gate so formatting drift cannot hide the later checks:

```sh
./tools/pre-push.sh
```

```sh
.venv/bin/ruff format --check src tests tools/build_release_metadata.py tools/check_capabilities.py \
  tools/check_ci_evidence.py tools/check_dependency_lock.py tools/check_mypy_ratchet.py \
  tools/verify_release.py deploy/configure.py deploy/render_avahi.py
.venv/bin/ruff check src tests tools/build_release_metadata.py tools/check_capabilities.py \
  tools/check_ci_evidence.py tools/check_dependency_lock.py tools/check_mypy_ratchet.py \
  tools/verify_release.py deploy/configure.py deploy/render_avahi.py
.venv/bin/mypy
.venv/bin/python tools/check_capabilities.py --check
.venv/bin/pytest --cov=outpost --cov-report=term --cov-report=json:coverage.json
.venv/bin/python tools/check_critical_coverage.py coverage.json
sh -n deploy/install.sh deploy/install-test-host.sh deploy/rollback.sh deploy/update.sh \
  deploy/smoke-package.sh tools/pre-push.sh
sh deploy/smoke-package.sh
.venv/bin/pip-audit
```

Provider adapter tests use recorded/mock HTTP responses and never contact a live model. Hardware
quality runs are explicit and excluded from normal CI:

```sh
.venv/bin/python tools/bench_inference.py --provider hailo_vlm --runs 3
.venv/bin/python tools/bench_inference.py --provider hailo_vlm --runs 5 --eval
```

The global coverage floor is supplemented by weighted line-coverage floors in
`coverage-gates.toml` for safety, federation framing/import, authentication, backup/restore,
radio supervision, and startup/shutdown. A configured path that matches no source file fails the
gate, preventing renames from silently removing a critical subsystem from enforcement.

### Failure-injection matrix

| Failure | Automated coverage |
| --- | --- |
| Radio loss and reconnect backoff | `tests/unit/test_supervisor.py` |
| Provider timeout and fallback health | `tests/integration/test_weather.py` |
| SQLite write failure and rollback | `tests/integration/test_transactions.py` |
| Background cancellation and task failure | `tests/unit/test_background_tasks.py` |
| Process restart and durable outbox recovery | `tests/integration/test_durable_outbox.py` |
| Federation clock skew and expiry | `tests/integration/test_federation_radio.py` |
| Duplicate/replayed frames | `tests/unit/test_inbound.py`, `tests/integration/test_federation_radio.py` |
| Partial federation assembly and retry | `tests/unit/test_federation_framing.py`, `tests/integration/test_federation_radio.py` |

The Playwright suite performs functional operator flows for authentication, Settings, BBS, Mail,
Watch, Environment, Federation, and Backups. Critical mutation flows fail on uncaught JavaScript,
console errors, failed API requests, non-success API responses, or horizontal viewport overflow.
Every operator page is also scanned against WCAG 2 A/AA rules in all three display themes. Its
visual matrix covers every page at mobile, tablet, and desktop sizes in all three themes; follow the
[dashboard design system](UI-DESIGN-SYSTEM.md) before approving a baseline update.

Dashboard refresh changes must also remain within the Raspberry Pi
[performance budget](PERFORMANCE.md). Run `tools/dashboard_idle_probe.py --seconds 300` on target
hardware when adding a timer, provider-backed surface, or recurring database query.

## Change rules

- Add a new ordered migration; never rewrite one used by deployed databases.
- Test fresh and upgraded databases for schema changes.
- Commands declare module, trust, traffic class, maximum parts, rate key, and help.
- Keep radio output concise, bounded, and direct rather than broadcast when possible.
- Preserve auth/CSRF on web state changes and all offline dashboard assets.
- Preserve the static stylesheet order and use shared UI primitives and semantic theme tokens.
- Register recurring dashboard work with `refresh-scheduler.js`; do not add page-local intervals.
- Show clear loading, empty, degraded, success, and error states on mobile screens.
- Add tests and update public docs/spec for behavior or protocol changes.

## Accessibility smoke test

Before merging dashboard changes, run the Playwright browser suite and complete this short manual
pass in Outpost Dark, Daylight, and Night Ops:

1. Navigate every page using only Tab, Shift+Tab, Enter, Space, and Escape. Focus must remain visible.
2. Open settings and a destructive confirmation. Focus must stay inside the dialog, Escape must
   close dismissible dialogs, and focus must return to the control that opened it.
3. With a screen reader, confirm page and dialog headings, current navigation state, async
   success/error announcements, and pending-review badges are announced once and in context.
4. Operate member, waypoint, and incident workflows from their list controls without using the map.
5. At 200% zoom and a 320 px viewport, confirm controls do not overlap and every action remains
   reachable.

Automated axe scans cover every operator page and display theme, but they complement rather than
replace this keyboard and screen-reader check.

Pull requests should state checks, hardware, migrations, compatibility, airtime impact, and external
providers. Sanitize screenshots. If specification and implementation disagree, document it rather
than claiming completion.
