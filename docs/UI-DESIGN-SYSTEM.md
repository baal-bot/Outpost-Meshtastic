# Dashboard design system

Outpost's operator interface is a local, offline-capable application. Its visual system must stay
legible in a dark operations room, outdoors in Daylight mode, and on a wallboard without requiring
remote fonts, scripts, or styles.

## Stylesheet contract

Every operator page loads styles in this order:

1. `base.css` — structural fallback rules for first paint and older pages.
2. `layout.css` — bundled Figtree font, shell geometry, responsive behavior, and theme tokens.
3. The page's domain stylesheet, plus an optional domain or shared-map stylesheet.
4. `components.css` — reusable visual primitives and temporary adapters for legacy class names.

Stylesheets are declared in the document head. JavaScript must not inject stylesheets at runtime;
doing so makes cascade order depend on network and execution timing. `components.css` is always
last so semantic state wins over domain layout without specificity escalation.

## Semantic tokens

Reusable components consume `--ui-*` variables from `layout.css`. The Dark, Daylight, and Night
Ops theme blocks only assign tokens; they do not restyle individual widgets.

| Role | Tokens |
| --- | --- |
| Canvas and surfaces | `--ui-canvas`, `--ui-surface`, `--ui-surface-raised`, `--ui-surface-subtle`, `--ui-surface-inset` |
| Content and edges | `--ui-text`, `--ui-text-muted`, `--ui-border`, `--ui-border-soft`, `--ui-focus` |
| Primary action | `--ui-primary`, `--ui-on-primary` |
| Semantic states | `--ui-success-*`, `--ui-warning-*`, `--ui-danger-*`, `--ui-info-*`, `--ui-disabled-*` |
| Application chrome | `--ui-rail*`, `--ui-header*`, `--ui-canvas-*`, `--ui-overlay` |

Do not put a literal theme color in a reusable component. Add or reuse a semantic token, verify it
in all three themes, and keep state meaning constant: green/success is not a substitute for a
neutral selected state, and danger styling is reserved for destructive or unsafe actions.

## Primitives

New markup uses these classes directly:

| Primitive | Purpose |
| --- | --- |
| `.ui-card` | Bounded content surface |
| `.ui-button`, `.ui-button--primary`, `.ui-button--danger` | Neutral and semantic actions |
| `.ui-icon-button` | Square icon-only action with a text alternative |
| `.ui-pill` and semantic variants | Compact status or lifecycle state |
| `.ui-notice` and semantic variants | Persistent contextual message |
| `.ui-dialog` | Modal content surface |
| `.ui-action-bar` | Wrapping action/filter row |
| `.ui-empty` | Empty, loading, or unavailable copy |
| `.ui-map-controls` | Shared map instrument group |

Legacy selectors grouped below the adapter comment in `components.css` exist only to migrate old
markup safely. Do not add page-specific theme corrections there. A domain stylesheet owns geometry
and information layout; the shared component owns color, borders, interaction state, and focus.

Disabled controls must remain readable and visibly unavailable. Keyboard focus uses `--ui-focus`
and must never be removed. `!important` is limited to genuinely invariant utilities such as
`[hidden]`, screen-reader-only content, and reduced-motion enforcement.

## Visual regression contract

The Playwright suite captures every operator page at mobile (390 × 844), tablet (768 × 1024), and
desktop (1280 × 900) in Dark, Daylight, and Night Ops: 90 approved states. It also checks visible
keyboard focus, horizontal overflow, JavaScript/console errors, failed API requests, and failed API
responses. A separate axe pass checks every page in every theme against WCAG 2 A/AA rules.

Run the approved baselines normally:

```sh
.venv/bin/pytest tests/browser/test_mobile_navigation.py
```

Only update baselines after reviewing an intentional visual change in every affected theme and
viewport:

```sh
OUTPOST_UPDATE_VISUAL_BASELINES=1 \
  .venv/bin/pytest tests/browser/test_mobile_navigation.py \
  -k operator_page_visual_baseline
```

`tests/browser/visual_baselines.json` contains small perceptual signatures rather than
renderer-specific full screenshots. Commit it with the visual change. Do not update a baseline to
hide overflow, contrast, focus, request, or console failures.

Set `OUTPOST_VISUAL_ARTIFACT_DIR=/tmp/outpost-visuals` to retain full PNGs from a local run for
human review. Generated screenshots can contain operational data when tests are replaced with a
live backend; never commit them without sanitizing them.

## Review checklist

- Use the existing primitive and semantic tokens before introducing a class or color.
- Preserve the fixed stylesheet order and keep all runtime assets bundled.
- Verify loading, empty, degraded, success, disabled, and error states.
- Check keyboard operation, 200% zoom, 320 px reflow, and reduced motion.
- Run the browser suite and the repository quality gates in `DEVELOPMENT.md`.
