# Tofu design system

Status: **phases 0–4 landed** (token layer + full component catalog +
style gate + value-identical legacy sweep). This document is the authority
for the design-token contract and the shared-control catalog; the living
preview is `designs/ui-gallery/`.

## Why this exists

The pre-system frontend grew 2,506 hand-written margins/paddings with no
spacing scale, ~15 independent button implementations (`myday-add-btn`,
`opt-btn`, `orch-btn`, `paper-upload-btn`, …), and 2,736 hard-coded hex
colors bypassing the theme tokens. Every new project on the Tofu base
re-inherited that vacuum: misaligned elements and one-off ugly controls.
The design system removes the vacuum instead of policing taste:

- **Tokens** (`00-design-tokens.css`) make spacing/typography/geometry a
  fixed vocabulary — misalignment becomes unrepresentable.
- **The UI kit** (`30-ui-kit.css` + `frontend/src/ui/`) provides one
  implementation per control, consumed through factories.
- **The gallery** (`designs/ui-gallery/`) makes every control inspectable in
  all three themes at desktop and mobile widths, with structural and
  contrast proofs (`verify.py`, `capture.py`).

## Architecture

```
00-design-tokens.css   vocabulary: space/type/color/geometry/motion/z
30-ui-kit.css          controls, tokens only, .ui-* classes, zero [data-theme]
frontend/src/ui/       TS factories — the ONLY app-facing entry (index.ts)
designs/ui-gallery/    kitchen-sink page + capture/verify scripts + previews
```

Composed FIRST/LAST in `frontend/src/styles/application/manifest.json`;
both ship inside `static/styles.css` like every other section.

## Token contract

Token names are a stable contract: add freely, **never rename or remove**.
New feature CSS consumes tokens only — no raw hex colors, no raw px
margins/paddings (gate: phase 3).

| Scale | Tokens | Notes |
|---|---|---|
| Spacing | `--space-0`…`--space-16` (4px base) | margins/padding/gaps; theme-invariant |
| Font size | `--font-size-xs`…`--font-size-4xl` | consolidated from the legacy 9–16px half-step sprawl |
| Leading/weight | `--leading-*`, `--weight-*` | tight/title/body; regular→bold |
| Conversation measure | `--conversation-reading-measure` = 720px | Default input to the existing `--msg-measure` authority; explicit overrides continue to size shell, prose, composer and context rails together. |
| Control height | `--control-h-sm/md/lg` = 28/34/42 | one height ramp → rows align by construction |
| Motion | `--motion-fast/base/slow`, `--ease-*` | collapses to 0.01s under reduced-motion |
| Z-index | `--z-dropdown`…`--z-tooltip` | new code only; native `<dialog>` needs none |
| Color (legacy, kept) | `--bg-*`, `--text-*`, `--border*`, `--accent*`, `--radius*`, `--shadow`, `--error-*` | defined per theme in 01/10/11 files |
| Feedback (new) | `--success-*`, `--warning-*`, `--info-*` (`-text/-bg/-border`) | text ≥4.5:1 on primary/secondary surfaces; Inspector browser proof includes tinted evidence |
| Chrome | `--backdrop`, `--focus-ring`, `--font-ui`, `--text-on-accent` | per-theme values |
| Code typeface | `--font-mono` | shared monospace stack for source and tool evidence |
| Status chip | `--chip-{ok,warn,err,info,neutral}-{color,bg,border}` | per-theme blocks (01 dark / 10 light / 11 tofu); skins every status pill (`.ptool-badge-*`, `.sw-card-role`, `.sw-note-reason-*`). Class names are stable aliases; re-skin = retune 15 values/theme. Tofu unifies on the kinako/azuki casts (the old one-off khaki override in 18 was removed). |

Problem: light feedback inks cleared the primary canvas but missed AA on secondary/tinted evidence. Decision: darken the existing three text roles; retain fills/borders. Rejected: feature-specific raw syntax paints or lowering the 4.5:1 floor. Inspector contrast is verified on actual browser backgrounds.

Theme values live in per-theme blocks at the bottom of
`00-design-tokens.css`. Geometry (spacing/type/control heights) is
theme-INVARIANT by design; the tofu theme's distinct body typeface may
shift text-flow container heights and text-bearing control widths by ±1px —
asserted scope of that exception: `verify.py` check #2.

### Conversation typography

Problem: chat content selected unrelated type sizes and font stacks; task
items competed with prose, and headings jumped directly to 28px.
Decision: `--conversation-font-size-*` in `00-design-tokens.css` is the shared
CSS interface. Reading owners and checklist/wait cards consume semantic roles:
body 16px, operational content 13px, metadata 12px, captions 11px, tables 14px,
and h1/h2/h3–h6 22/18/16px. Use `--font-ui` for natural language and
`--font-mono` for code; theme changes select the face, not the size hierarchy.
Task states use regular/semibold weights, glyphs and strike-through without
increasing size. Text inherits the task row's font and wraps long paths.
Alternatives rejected: isolated task overrides leave the next card inconsistent;
a blanket font reduction loses the distinction between answers and process.

### Ghost tokens (discovered in the wild, deliberate decisions)

- `--success-color`, `--text-muted`, `--text-quaternary`: used with
  fallbacks, never defined — left untouched (fallbacks keep winning).
- `--success-border`: used WITHOUT fallback by `.branch-approve-btn`
  (silently broken since introduction). Now defined by the token layer;
  the button finally renders its designed intent, mirroring
  `.branch-reject-btn`'s `--error-border` usage. This is the single
  deliberate legacy-visual change of the spike.
- `--text-on-accent`: centralized as `#fff` (identical to every existing
  ghost fallback → zero visual change). Known legacy debt: white on the
  light/tofu accents measures 4.47:1 / 3.34:1 (pre-existing primary-button
  paint, reported as WARN by `capture.py`; phase-4 decision — deepening
  the accent hues or darkening labels changes shipped product surfaces and
  needs a product call, not a spike call).

## Component catalog

Application code uses ONLY the factories in `frontend/src/ui/index.ts`;
it never hand-writes `ui-*` class strings. (Static design prototypes under
`designs/` may write markup directly — they are the kit's documentation,
not application code.)

`createButton(options, ownerDocument?)` and the existing menu-row primitive `createMenuItem(entry, ownerDocument?)` default to the browser document; independent list popups reuse menu-row controls without creating a second control family.
Mounted surfaces pass their container's `ownerDocument` so controls remain in
that DOM realm (including embedded views and isolated test documents); styling
and native keyboard behavior are identical.
| Control | Markup / factory | Status |
|---|---|---|
| Layout: stack / row / grid / toolbar | `.ui-stack` `.ui-row` `.ui-grid` `.ui-toolbar` (+`--tight/--loose/--between/--wrap`) | ✅ |
| Button | `createButton` — primary/secondary/ghost/danger × sm/md/lg | ✅ |
| Icon button | `createIconButton` — aria-label required | ✅ |
| Field (label+hint+error) | `createField`, `setFieldError` | ✅ |
| Text input / textarea | `.ui-input` | ✅ |
| Select | `.ui-select` wrapping native `<select>` | ✅ |
| Dialog | `createDialog` on native `<dialog>` (focus trap, Esc, backdrop from the platform) | ✅ |
| Drawer | `createDrawer` — right-docked `<dialog>` variant | ✅ |
| Card / badge | `.ui-card`, `.ui-badge` (+feedback variants) | ✅ |
| Checkbox / radio / switch | `createChoice` on native inputs | ✅ |
| Tabs | `createTabs` — roving tabindex, arrows/Home/End, automatic activation | ✅ |
| Dropdown menu | `createDropdown` — menu semantics, full keyboard map, outside-click | ✅ |
| Tooltip | `.ui-tooltip-anchor[data-tooltip]` — CSS-only, hover + focus-within | ✅ |
| Toast | `toast(...)` — bounded aria-live region (max 4), auto-dismiss | ✅ |
| Alert | `createAlert` — 4 feedback variants, dismissible | ✅ |
| Table | `.ui-table` (+`__cell--num`) — presentational markup | ✅ |
| Spinner / skeleton | `createSpinner`, `createSkeleton` | ✅ |
| Progress | `createProgress` — determinate + indeterminate | ✅ |
| Divider | `hr.ui-divider` / `--vertical` — margin:0, parent stack owns spacing | ✅ |
| Empty state | `createEmptyState` — icon/title/description/action | ✅ |

### Adding a control

Three artifacts land together, or the control does not ship:

1. Factory in `frontend/src/ui/` (one module per family, native elements
   first — add TS only for what the platform lacks).
2. Styles in `30-ui-kit.css` (tokens only; no `[data-theme]` selectors;
   `.ui-*` scoped).
3. Gallery entry in `designs/ui-gallery/index.html`, then regenerate
   `previews/` and run `verify.py`.

## Usage rules for feature CSS

- New feature CSS consumes tokens for ALL color/spacing/type values.
- Feature CSS must not restyle `.ui-*` classes; extend the kit instead.
- Ghost tokens must not be introduced: a `var(--name)` without a fallback
  that is defined nowhere is a latent bug (see `.branch-approve-btn`).

## Style gate and budgets

`npm run check:styles` = composed-sheet sync check + `scripts/check_style_tokens.mjs`:

- Every CSS file NOT on the script's explicit legacy allowlist is gated:
  PostCSS parses declarations: no hex colors, no `rgba()/hsla()` paints, no raw px in
  margin/padding/gap/inset properties (0 exempt, including signed/fractional and logical values), and `30-ui-kit.css`
  additionally bans `[data-theme]` selectors (themes adapt via tokens).
  `00-design-tokens.css` is exempt from the color rules — defining colors
  is its job. A newly added file is gated automatically; the allowlist
  only shrinks.
- A byte-moved legacy owner may declare `legacyTokenSha256` in the ordered
  style manifest. Only identical bytes keep their old declarations; any edit
  returns the owner to strict tokens. Do not refresh a digest to admit new raw
  values. Migrate the edited owner and remove its digest. This replaces an
  open-ended giant-file exemption with closed copies; generated CSS is excluded.
- Budgets: `static/styles.css` ≤ 1,500,000 B, `static/settings.css` ≤
  300,000 B (personal-computer resource budget: CSS ships to every
  session).

## Phase-4 token sweep (landed)

A value-identical codemod normalized the legacy sheets in one pass:

| Substitution | Count | Identity guarantee |
|---|---|---|
| standalone scale-step px in margin/padding/gap/inset → `var(--space-N)` | 2,293 | tokens are theme-invariant, same px |
| whole-value scale px in `font-size` → `var(--font-size-*)` | 1,156 | same px |
| exact hex == token inside the matching `[data-theme]` block → `var(--token)` | 29 | same value in that scope; unthemed rules untouched (per-theme vars differ) |

Evidence (`designs/ui-gallery/migration/`): welcome + settings surfaces in
all three themes screenshotted before/after; pixel drift ≤0.01% on 5/6
surfaces and 0.33% on settings-tofu, which equals that surface's
dynamic-content noise floor measured by an identical-code control run
(0.34%) — i.e. zero codemod-induced drift.

Residual long tail (tracked debt, shrinks via the gate going forward):
~3,486 off-scale spacing values (10/14/6/2px…), ~470 off-ramp font sizes
(9–12.5px half-steps), ~1,199 themed one-off hex colors (largely the tofu
theme's warm palette extensions), ~1,139 unthemed hex colors. These need
per-component design decisions — snap-to-scale changes pixels — so they
land through normal feature work with gallery review, not blind codemods.

## Migration policy

New code is held to the token gate; legacy files are allow-listed and the
allow-list only shrinks. The value-identical sweep above is done. Control-
level replacement of legacy markup (each `*-btn` family, settings forms,
legacy modals → ui-kit factories) continues per component with gallery
review: when touching a feature, migrate its controls to
`frontend/src/ui/` factories in the same change.

## Agent-driven development ("一句话起项目")

The skill `.agents/skills/tofu-design-system` routes any "build me a new
project/page" request (翻译项目、企业项目、new feature UI) into this
workflow, so an agent lands on the kit instead of a blank stylesheet:

1. Clone `designs/ui-gallery/scaffold.html` (app shell built only from the
   kit; rebrand = retune token values, zero markup change).
2. Controls come from `frontend/src/ui/` factories exclusively; new shared
   controls land as factory + `30-ui-kit.css` + gallery entry together.
3. Feature CSS is tokens-only; `npm run check:styles` (token gate + size
   budgets) is the definition of done.
4. "Not ugly" is machine-checkable: `verify.py` (structural/behavioral) +
   `capture.py` (three-theme screenshots + WCAG contrast). The agent posts
   `previews/*.png` for user review instead of claiming quality.

New-project checklist: scaffold cloned □ factories only □ no raw hex/px □
gallery updated □ verify+capture green □ screenshots posted.

## Verification

- `python3 designs/ui-gallery/verify.py` — 37 structural checks (height
  ramp, theme-invariant geometry, 320px overflow, dialog/drawer focus trap
  and dismissal paths, tabs roving tabindex, dropdown keyboard map, toast
  region bounds, alert dismiss, progress, tooltip reveal, field error
  wiring, focus ring, token contract resolution in all three themes).
- `python3 designs/ui-gallery/capture.py` — regenerates
  `previews/{desktop,mobile,dialog}-{tofu,dark,light}.png` +
  `contrast-report.json` (feedback text ≥4.5:1 gate; legacy on-accent
  cases WARN).
- `npm run check:styles` — composed stylesheet stays in sync.
- `npm run typecheck` — factories type-check under the repo tsconfig.
