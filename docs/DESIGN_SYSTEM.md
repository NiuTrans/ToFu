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
| Font size | `--font-size-xs`…`--font-size-4xl` | consolidated from the legacy 9–16px half-step sprawl; `--font-size-base` (15px) is the conversation prose step only |
| Leading/weight | `--leading-*`, `--weight-*` | tight/title/body; regular→bold |
| Conversation measure | `--conversation-reading-measure` = 800px | Shared `--msg-measure` authority for prose, composer and rails, inside the 700–800px reading contract. The context track is reserved per conversation once any of its Turns shows a rail; new chats and rail-less conversations keep the centered column. The composer keeps its left-edge lock on the same grant. |
| Conversation process | `--conversation-process-*`, `--conversation-flow-gap`, `--conversation-prose-gap`, `--conversation-leading-*` | 15px prose (1.65), 14px tables, 13px rows and evidence, 12px metadata. A grouped reply's calls sit in one tool-panel paper card: one 28px row per call with its tool glyph, a one-line intent in secondary ink and one outcome at a shared end column (quiet telemetry, a bare check for routine success, a chip only for an exception); folds, anchors and carets on hover/focus; exact identity inline once a step is open or needs attention; every round's reasoning merges into one disclosure in the same activity card: the reasoning and tool headers share full-width rows, glyph tiles and trailing chevrons, and expand independently. Native evidence controls retain keyboard/touch behavior. |
| Tool rows | `--tool-row-intent-ink(-live)`, `--tool-row-meta-ink`, `--tool-row-ok-ink`, `--tool-row-glyph-ink(-live/-attention)` | The one role vocabulary for every tool family's call row, aliased onto the palette per theme. `23-reading-tool-summary.css` draws every row from it; the panel adds only the card. Accent = liveness on the glyph; identity, telemetry and routine outcomes share the meta ink. Retune tool rows here, never per family or per theme selector. |
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

The warm dark palette (V5, 2026-10-27) replaces the dark violet/green accents
with charcoal `#24221f`, cream `#f0e7d8`, warm secondary `#b9ae9c` and coral
error `#e4a193`. Tofu's coral error text is `#9E4838`: the previous `#B05B48`
measured 4.21:1 on the paper receipt canvas, below the shared 4.5:1 text floor.
The deeper ink retains the existing error fill and border roles. Light/paper palettes remain independent. The V5 sizing (18px
rows, two-line 64px headers, 960px measure) is superseded by compact action
reading. `23-reading-tool-summary.css` owns the shared one-line action row
and the reasoning fold; existing specialized sheets own evidence bodies.
`ui/tool-summary.ts` places exact identity right after the native intent and
keeps the full intent as its tooltip, including lazy hydration;
`ui/round-outline.ts` owns only reader expansion/search over the immutable
ordered projection. Rationale is in
[RENDER_CONTRACT.md](RENDER_CONTRACT.md#compact-action-reading-2026-10-27).

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
  ghost fallback → zero visual change). Remaining legacy debt is the LIGHT
  accent (pre-existing primary-button paint, reported as WARN by
  `capture.py`); deepening it changes shipped product surfaces and needs a
  product call, not a spike call. Tofu no longer owes this: its accent was
  deepened to `#8F5126` on 2026-10-20, which also fixed accent-as-text
  (4.35:1 → 5.5:1 on the ground) and puts white labels at 6.2:1.

## Component catalog

Application code uses ONLY the factories in `frontend/src/ui/index.ts`;
it never hand-writes `ui-*` class strings. (Static design prototypes under
`designs/` may write markup directly — they are the kit's documentation,
not application code.)

`createButton(options, ownerDocument?)` and the existing menu-row primitive `createMenuItem(entry, ownerDocument?)` default to the browser document; independent list popups reuse menu-row controls without creating a second control family.
Mounted surfaces pass their container's `ownerDocument` so controls remain in
that DOM realm (including embedded views and isolated test documents); styling
and native keyboard behavior are identical.

Shared buttons honor native `hidden`: author display rules must not restore
invisible retry/reload actions to layout or the keyboard focus order.
| Control | Markup / factory | Status |
|---|---|---|
| Layout: stack / row / grid / toolbar | `.ui-stack` `.ui-row` `.ui-grid` `.ui-toolbar` (+`--tight/--loose/--between/--wrap`) | ✅ |
| Button | `createButton` — primary/secondary/ghost/danger × sm/md/lg | ✅ |
| Icon button | `createIconButton` — aria-label required; `pressed` exposes an on/off toggle through `aria-pressed` | ✅ |
| Field (label+hint+error) | `createField`, `setFieldError` | ✅ |
| Text input / textarea | `.ui-input` | ✅ |
| Select | `.ui-select` wrapping native `<select>` | ✅ |
| Dialog | `createDialog` on native `<dialog>` (focus trap, Esc, backdrop from the platform); `open()` mounts a detached dialog on `<body>` until it closes | ✅ |
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

Tabs preserve native `hidden` even when a panel uses a layout primitive such
as `.ui-stack`; inactive panels leave layout and keyboard navigation. Tab labels
retain one line in their existing horizontal scrolling container on narrow screens.
The strip stays inside its dialog or split-pane bounds; keyboard and pointer
scrolling reveal off-screen labels without scrolling the whole dialog.

### Native dialog backdrop ownership

Problem: modal and drawer factories duplicated the same gesture logic.
Decision: the kit's internal `dialog-backdrop.ts` binds one implementation to
each native dialog. Dismissal requires press and release outside the current
box; selecting text across its edge, an unpaired click, or a release on its
border keeps it open. Gesture state stays local to each element, and an inside
press needs no layout read on release. The factories retain their own close
callbacks and promise-settlement ordering. Sharing their whole lifecycle was
rejected because dialog actions and drawer dismissal have distinct results.
`dialog.test.ts` covers both controls, changing geometry and independent mounts.

### Project workbench dialog appearance

`createDialog({ appearance: 'project', ... })` is the opt-in native project
shell. `--project-*` tokens supply the ink frame, paper/header surfaces and gold
accent in Tofu; dark/light retain their palettes. Geometry is theme-invariant.
The kit owns descendant field/button treatment; feature CSS owns only layout.
Default dialogs and Settings chrome are unaffected. Native focus trapping,
Escape and backdrop dismissal remain the only modal behavior owner.

Problem: a generic cold/hairline dialog did not match the established CO-PILOT
workbench. Decision: extend the existing factory/kit with semantic tokens.
Rejected: adding legacy `.modal` classes or feature-level `.ui-*` overrides
would duplicate chrome authority and couple native dialogs to legacy skins.

An open native `<dialog>` owns Escape. Problem: the document-level shell
shortcuts (`application-shell/interactions/shortcuts.ts`) closed the Settings
overlay underneath and `preventDefault()`ed the keydown, which cancels the
dialog's platform close request and strands it on screen. Decision: the shell
handles no Escape while any `dialog[open]` exists; every dialog owner opens
with `showModal()`, so an open dialog is always the topmost layer. Rejected:
stopping propagation inside each dialog misses keydowns targeted at `<body>`
(Chromium's modal focus cycle can land there) and must be repeated by every
dialog owner.

### Settings status colors

Settings status colors follow the Settings surface. Problem: Settings retains
its paper background in dark chat, but inherited dark-page success, warning and
information colors could fall below 4.5:1 there (the Database Running badge was
2.06:1). Decision: `styles/settings/00-status-colors.css` binds the shared status
roles to existing Settings ink, paper, gold and danger tokens at `.settings-panel`.
Success and information use quiet ink; warnings and failures keep distinct
accent treatment. This also covers OAuth feedback and field errors. Alternatives
rejected: per-badge color overrides would repeat the same surface mismatch;
changing the global dark palette would alter chat to repair a local surface.
The Database browser contract measures all four status roles across all themes
and retains its actual text, keyboard and narrow-layout checks. Database's
required-index badge also pairs Settings ink with Settings paper; inheriting a
dark chat background behind that ink had produced 1.15:1 contrast.

### Settings density and copy

Problem: Settings accumulated explanation instead of controls. Everyday
switches folded into disclosures left General mostly blank, while other tabs
repeated one sentence as description, hint and empty state, narrated
implementation details, and drew diagrams that controlled nothing.
Decision: every tab passes the same audit (`designs/ui-gallery/settings_audit.py`
and its checklist): everyday controls on the first screen, at most one short
hint per control, no implementation narration or decoration, one visual
vocabulary, one language per locale. Disclosures hold only rarely used or
bulky groups. Alternatives rejected: a character budget enforced by a test
(an arbitrary threshold that cannot judge redundancy) and per-tab restyling
without a shared checklist, which is how the tabs drifted apart.

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

The installed `tofu-design-system` skill package (owner-installed like the
other retired `.agents/skills` development guides) routes any "build me a new
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

## Conversation reading grammar

**Problem:** a flat list of equally styled prose and disclosures makes a mixed
chronology read like logs. Uniform spacing alone cannot distinguish the user's
brief, the assistant's work, and its eventual answer. A separate card for every
operation creates the opposite problem: too many competing boundaries.

**Decision:** compose each Turn as an editorial reading sequence. Human input is
a compact, right-aligned note; assistant identity is a small accent mark and
byline. Authored narration stays open on the page. A reply's calls share one
rounded paper card under its tool-panel header (`23-reading-tool-panel.css`);
without calls, consecutive operational leaves form one quiet run. Narration,
deliverables and the current page/window boundary interrupt that run. The `RoundOutline` derives start/middle/end/single decoration from the
mounted sequence without regrouping, reparenting or creating disclosures. Its
small map is replaced on each bounded projection. Accepted WorkUnits keep their
own delivery surface, as do artifacts and other durable content.

`23-turn-reading-system.css` owns these boundaries. The shared process slot,
label column, type scale and evidence inset live in `23-reading-process.css`
and `23-reading-execution.css`. Tool identity is leading; disclosure is trailing.
That trailing caret is a pseudo-element of the disclosure ROW, never inline text
inside the wrapping label: a label-suffix caret is ordinary inline content, so a
full last line pushes it alone onto the next line. As a row-level flex item it is
atomic — it cannot wrap, and multi-line labels keep one caret at the row's end.
Reasoning, search/read results, context, checklists, programs and fallback tools
use the same row rhythm. Code/output controls remain independent, with selectable
intent. The program summary keeps recorded activity, counts and semantic receipts;
arbitrary return-value previews live in expanded evidence. Failure/attention
receipts remain visible. Routine success is quiet; pending actions retain their
own controls and status.

Tofu is a **paper room**: a warm washi ground (`--bg-primary #F7F1E6`,
`--bg-secondary #F1EADC`) with near-neutral paper sheets (`#FFFEFA`) stepped up
on top, warm ink, and one roasted-caramel accent (`--accent #8F5126`). Warmth
lives in hue, not in chroma; saturation is reserved for the two filled marks
(New Chat's plus, the Send button). **Problem:** the palette had drifted to a
cool grey-white canvas statistically indistinguishable from the light theme
(#F8FAFC vs #f4f2ed), so Tofu read as "light with an orange accent"; an earlier
iteration had over-corrected into a beige canvas plus a yellow gradient CTA. The
first warm pass then over-corrected a second time: every surface sat at OKLCH
C 0.024–0.043 with the hue pinned at 85–88°, so the whole viewport was one cream
field with no neutral anywhere — reported as "too warm and uncomfortable", which
is the eye finding no resting point rather than a hue complaint. It also put a
full-viewport gradient (top-left highlight → caramel falloff) under every pixel,
and left `--text-tertiary #9A8F79` at 2.78:1 on the ground. **Decision:** warm
in HUE at low chroma — ground C 0.016, panels ≤0.020, sheets near-neutral (C
0.005) so exactly one surface is clean; the ground is FLAT (no ambient ramp);
sheets step up in L by ~0.04 over the ground; and every ink role clears AA
(`--text-tertiary #7F7360` 4.1:1, `--accent #8F5126` 5.5:1 text / 6.2:1 with a
white label). Depth is ONE hairline plus a soft warm shadow — never per-side
bevels. **Rejected:** a saturated yellow CTA (competes with the conversation), a
uniform ivory wash (no hierarchy), an ambient full-screen gradient (low-frequency
luminance ramp reads as unevenly lit and leaves no flat field), a cream-on-cream
ramp at high chroma (the stained-screen effect above), and embossed 2px bevels on
popovers (read as a foreign plastic object inside an editorial UI).

One chip family owns every composer control (`.preset-toggle`,
`.submenu-trigger`, `.search-mode-toggle`): 32px tall, 10px radius, transparent
at rest, caramel wash on hover, paper fill when open, no offset shadow. The
model selector is a *field* (paper fill + hairline, brand hue confined to its
12px glyph); the rest are *actions*. Menus and the model picker share one
floating sheet: 14px radius, one hairline, layered warm shadow, 10px rows, and
a single-select state of caramel wash plus a 2px left spine — the same grammar as
a selected conversation row. Capability toggles are independent and often on
together, so an "on" toggle keeps the wash and its filled seal but no spine,
consecutive toggle rows sit one `--space-1` apart, and every toggle shares the
theme's on-state (no per-capability hue). **Rejected:** flush toggle rows with
spines (concurrent rows fused into one slab whose stacked spines read as a curly
bracket) and dropping the wash ("on" would show only at the row's far edge).
New Chat is a paper slip with a leading filled caramel plus; the label stays left-aligned and keyboard focus stays visible.
Message actions retain their icon shelf, localized accessible names and native
tooltips; Review keeps its visible label. Shared controls remain keyboard and
touch accessible.

The assistant byline, narration, process labels, localized **Answer** title and
answer text share `--conversation-reading-inset`; the avatar occupies the gutter
instead of shifting a separate grid column. For at least three direct Markdown
headings the answer toolbar carries a bounded **On this page** menu (at most 32
headings). Navigation focuses the actual
current heading; replay retains menu focus. The first answer heading is 28px on
desktop / 22px on phones, with 16px prose. The Answer title identifies settled
authored content and never declares success. During generation every content block keeps
the narration treatment, regardless of translation progress; only authoritative
Turn settlement enables the answer title, hierarchy and bilingual disclosure.

**Grouped reply card (2026-10-31).** Problem (owner report): once calls were
grouped, the panel still used the compact action grammar — one glyph-less
grey sentence per call with a plain "exit 0" — so a long reply read as a wall
of text with no design, unlike the original tool panel the owner preferred.
Decision: the panel is one paper card (`23-reading-tool-panel.css`) drawn over
sibling keyed blocks, so streaming never reparents a leaf. The header tops it
with a caramel glyph tile, the count and an error chip; each call is a 28px row
with its tool's own line glyph, a one-line intent that wraps only at words, and
one outcome under the calm row grammar below. Hidden calls are announced
by an in-card "N earlier tool calls" row. Reasoning is a pill above the card.
The card uses the reading system's process palette, so each theme keeps its
own chip decisions (the warm dark palette keeps routine success quiet). Phones
keep one row per call; a narrow pointer window takes invisible controls out of
layout until hover or focus. Alternatives rejected: restyling the quiet rows
globally (every non-grouped surface would change); a wrapper element per panel
(reparents live leaves on toggle and windowing); new colour values (the chip
and process palettes already carry per-theme decisions; the tool-row roles
below only alias them).

**Calm tool rows (2026-11-01).** Problem (owner report): the rows still
repeated themselves in the accent. A running step printed its exact identity
("run_command cd /mnt/…") in caramel beside an intent that already said what
ran, and every routine call carried a tinted mono pill ("31 tok", "1 result")
whose Tofu info cast was the same `#7A441F` as the warning chip, so telemetry
read like a warning. Control was diffuse: one command status was skinned by
six stylesheets, and the panel re-pilled what the shared summary flattened.
Decision: the accent marks liveness only, on a running step's glyph. Exact
identity, telemetry, counts and status words are metadata in one quiet ink at
text contrast, in the UI face with tabular numerals (monospace is for code:
the identity); a routine success is a bare check. Chip chrome is reserved for
failures, warnings, skips and refusals, so a chip always means "look here".
Identity shows only for an open step or one needing attention, never while it
runs and never beside an open command pane that already prints the command;
an attention step's identity line hangs on the intent column at every width.
`23-reading-tool-summary.css` owns this outcome/identity grammar for every
family, `23-reading-tool-panel.css` only the card and glyph column, and both
read the `--tool-row-*` roles in `00-design-tokens.css`. Alternatives
rejected: recolouring the panel's pills (keeps a chip on every row, so a chip
stops meaning anything); retuning `--chip-info-*` (also skins swarm roles and
notes outside tool rows); dropping telemetry (token cost is evidence readers
use); dimming metadata to `--text-tertiary` (3.4–4.0:1 on the light and Tofu
panels, below text contrast). Spec: `tests/visual/test_visual_tool_row_roles.py`
(three themes, 1180/390px, including a one-token retune).

The context rail uses the same paper/ink palette and readable 12px facts,
with one section anatomy for every fact (2026-11-01 owner report: each piece
of the rail was styled on its own). The identity line carries the
brand mark, the model in primary ink and effort as the rail's one accent pill;
run modes are neutral pills. Every other section is a heading (an uppercase
tertiary label, a count pill and, at its end, the disclosure) above its facts:
tools as quiet chips with their registry glyph (MCP chips show the server
beside a plug glyph, the full label in the tooltip), workspace roots as
monospace fact lines with folder or lock glyphs, each ending with its branch
after a colon (`tofu:main`), and the
checklist as a thin progress bar, its live edge in primary ink, then the rest
of the plan in order (completed items fade, blocked ones stay in the error
ink). Sections are separated by whitespace, not rules; the background-agents
section joins the same heading voice and lists each agent the way the
checklist lists items: a state glyph in the tool rows' outcome inks, what the
agent does in at most two lines (primary ink while live), then its state word,
round count and quiet monospace ID. All values come from the rail's
`--tctx-*` vocabulary in `23-reading-context-rail.css`, built on the shared
tokens. The disclosure stays at the heading while entries expand below it.
A branch is written after its root, `root:branch`, in the root's own line
(2026-11-01 owner follow-up: a branch line with its own glyph under the roots
spent a line and a glyph on a fact of the root above it). Capture records each
root's own branch; snapshots recorded before that collapsed roots on one
branch into the first root, which alone shows it. A turn without roots shows
no branch. Each name is one line-breaking unit: a pair too long for its line
breaks only after the colon (`your-username/tofu:` over
`eval/opencode-glm53-tb21`), and a name wraps inside itself only when it
alone is wider than the line; the root keeps a column for its colon, so the
colon never starts a line. **Alternatives rejected:** branch lines closing the
workspace list (the previous form); a separate Branch section (split one
workspace fact in two); a branch pill beside the path (a second chip style
inside a fact line); a branch-only Workspace section for root-less turns (their
branch is the live fallback or an inactive project's saved path, never a
workspace the turn ran in); `<wbr>` after the colon in one text run (every
hyphen in a branch is also a break, so 1100-1160px panes showed
`eval/opencode-` over `glm53-tb21`); only the branch as a unit (a root that
exactly fills a line pushed the colon to the start of the next).
Four initial tool chips and three paths form the preview; all remaining facts
are reachable by click or keyboard. The rail remains out of message flow;
its card is sticky within its round's track, never taller than the pane,
and scrolls within itself when its facts outgrow the pane.
Populated rail hosts sit above later transparent Turn boxes so real pointer
clicks reach the controls. The compact context line remains available when
the pane cannot grant a rail track. Alternatives rejected: a boxed card per
rail competes with the prose it annotates; per-tool colours compete with
status semantics; hairlines between sections tripled the rail's lines without
adding structure.

Visual references reviewed: [Linear Agent](https://linear.app/docs/linear-agent)
for quiet activity and content hierarchy; [Cursor's agents workspace](https://cursor.com/blog/cursor-3)
for outcome emphasis and inspectable detail; [Raycast's redesign](https://www.raycast.com/blog/the-new-raycast)
for coherent control geometry and restrained surfaces. These inform Tofu's
composition, not its brand, content order or interaction authority.

**Alternatives considered:** regrouping all tools or reasoning loses causality;
a flat disclosure list lacks hierarchy; independent cards fragment the stream;
a permanent second column reduces prose width and fails on phones. Adjacency
surfaces and an explicit answer destination preserve one responsive reading
column, stable leaves, reader choices and existing lazy owners. A floating
inspector preserves answer geometry without permanently reserving another
column. A brand-neutral palette was rejected because Tofu's warmth is a product
constraint. No duplicated
transcript, additional timers, transport or animation loop is introduced.

`tests/visual/test_visual_timeline_reading.py` mounts the real selector, Surface,
Markdown and tool presenters, then also places the same Surface in application
shell markup. It checks chronological boundaries, label alignment, actual-surface
contrast, 320/390px wrapping, native keyboard disclosure, replay focus and history
settlement in three themes. Shell captures verify layout only; live-app boot is
owned by the existing conversation-reading shell test. Reviewed images live in
`designs/ui-gallery/previews/timeline-*.png`.
`tests/visual/test_visual_context_rail_reading.py` checks real pointer and keyboard
expansion, stationary controls, complete facts, pane containment and narrow-pane
fallback in three themes and both languages. Its live-shell case checks the
actual adaptive rail grant, prose width and composer left-edge alignment.
Grouped-turn-layout, intent-first
and program-surface suites own neighboring interaction and lifecycle contracts.

## Verification

- `python3 designs/ui-gallery/verify.py` — structural checks (height
  ramp, theme-invariant geometry, 320px overflow, dialog/drawer focus trap
  and dismissal paths, tabs roving tabindex, dropdown keyboard map, toast
  region bounds, alert dismiss, progress, tooltip reveal, field error
  wiring, focus ring, token contract resolution in all three themes).
  The gallery locally restores document scrolling on phones because application
  viewport containment clipped its long captures; the check verifies footer
  scrolling and actual footer pixels in the full-page PNG without changing
  production shell styles.
- `python3 designs/ui-gallery/capture.py` — regenerates
  `previews/{desktop,mobile,dialog}-{tofu,dark,light}.png` +
  `contrast-report.json` (feedback text ≥4.5:1 gate; legacy on-accent
  cases WARN), and refreshes `previews/rich-components-*.png` and
  `previews/dynamic-*.png` by running the visual tests that render them.
- `npm run check:styles` — composed stylesheet stays in sync.
- `npm run typecheck` — factories type-check under the repo tsconfig.
- `python3 designs/ui-gallery/settings_audit.py <out>` — screenshots and a
  copy inventory for every Settings tab, reviewed with the checklist in
  `designs/ui-gallery/README.md` ("Settings audit").

**Test-rendered previews.** Problem: the rich-response and dynamic-surface
visual tests screenshotted straight into the tracked `previews/`, so every
suite run dirtied the checkout and unreviewed rendering drift was later swept
into unrelated commits. Decision: visual tests write screenshots only to their
`tmp_path`; `capture.py` is the one writer of the tracked copies, running those
tests under a temporary pytest root and failing if a preview is missing or
produced twice. Alternatives rejected: an environment switch that lets tests
write `previews/` keeps a test as the writer of shipped files; copying images
by hand from pytest's temporary directories has no stable, reviewable owner.
