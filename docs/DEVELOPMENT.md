# Development and contributing

## Setup

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
| `tests` | Unit, integration, acceptance, hardware procedures |
| `deploy` | Installer, service, package smoke test |

## Quality gates

```sh
.venv/bin/ruff format src tests
.venv/bin/ruff check src tests
.venv/bin/mypy
.venv/bin/pytest --cov=outpost --cov-report=term
sh -n deploy/install.sh deploy/smoke-package.sh
sh deploy/smoke-package.sh
.venv/bin/pip-audit
```

## Change rules

- Add a new ordered migration; never rewrite one used by deployed databases.
- Test fresh and upgraded databases for schema changes.
- Commands declare module, trust, traffic class, maximum parts, rate key, and help.
- Keep radio output concise, bounded, and direct rather than broadcast when possible.
- Preserve auth/CSRF on web state changes and all offline dashboard assets.
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
