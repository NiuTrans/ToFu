# Frontend style guidance

## Scope

Authored CSS lives under `application/` and `settings/`; manifests define
deterministic composition. `deferredSections` name feature-owned static CSS
imports; a `sourceEntry` and `featureEntry` generate ordered imports for Vite.
The composer rejects duplicate critical/deferred ownership and missing imports.
Root `static/` CSS and `*.generated.css` are generated delivery output.
`legacyTokenSha256` admits only unchanged copies of previously allowlisted
rules. An edited owner must pass tokens; never update its digest to allow new
raw declarations. Remove the digest when the owner migrates.

The application sheet is layered: `00-design-tokens.css` (vocabulary,
composed first) and `30-ui-kit.css` (shared controls, composed last) are
the design system — authority `docs/DESIGN_SYSTEM.md`. New feature CSS
consumes design tokens for all color/spacing/type values and must not
restyle `.ui-*` classes. `npm run check:styles` enforces this for every
file not on the legacy allowlist (`scripts/check_style_tokens.mjs`) plus
the composed-sheet size budgets.

## Editing rules

- Edit the smallest semantic source file and preserve manifest ordering.
  Never patch generated files under `static/`.
- Reuse established tokens, typography, spacing, color, elevation, and motion
  semantics. Avoid one-off overrides whose only purpose is to beat cascade
  order.
- Scope selectors to their component/feature and remove obsolete rules with the
  markup they served. Do not rely on generated hashes or incidental DOM depth.
- Conversation reading styles: `23-turn-reading-system.css` owns turn/round
  geometry; `23-reading-{prose,process,activity,evidence,actions,responsive,
  editor,background,execution}.css` own the named content. Manifest order is
  explicit; these owners pass the token gate without legacy exemptions.
- `02-*` owners separate composer geometry/field/controls/menus, shell actions,
  Update, persisted image receipts and message residency/navigation. Five
  `02-inspector-*` sheets import directly from diagnostics-presenters; the tool
  anchors remain critical. Field and edited Inspector sheets pass strict tokens.
- `08-*` owners separate Swarm, injections, signals, Scheduler/Timer panels,
  previews and round cards; the three `08-timer-receipt-*` owners pass strict
  tokens. `08-app-dialogs.css` belongs to the lazy dialog controller.
  `10-*` owners separate project selection, optimizer previews and theme rules.
  Unmigrated rule copies use exact manifest hashes, never a renewed allowlist.
- `08-tool-group-*` and `08-tool-row-*` separate batch/timeline/history and row
  intent/attribution/evidence. `08-tool-search-*` own search evidence and motion.
- Tool presentation styles: the ordered `09-*` owners separate command status,
  controls/output, approvals/diffs, memory/results/progress, project/conversation
  receipts, guidance, failures/activity and WorkUnits. `09-slide-editor.css`
  belongs to the lazy Slides entry; it never rides the conversation sheet.
- `03-pdf-reader.css` is imported by the shared PDF core; `24-paper-preview-*`
  separates shared reader chrome from preview sources/editors/proposals. The
  manifest names each direct component importer, covering Paper, chat preview
  and Research reuse without adding these sheets to startup.
- Preserve keyboard focus, contrast, zoom, reduced motion, touch targets,
  safe-area behavior, long localized text, and narrow/mobile layouts.
- Treat large images, fonts, animations, and CSS growth as bundle/resource
  budgets. Keep optional feature styles lazy where the architecture supports it.
- A visual change updates source markup/styles together; do not encode behavior
  solely through fragile CSS state.

## Verification

Run `npm run check:styles` and the focused style/frontend test. Use targeted
desktop and narrow-screen screenshots for layout changes, then the visual gate
when the affected behavior cannot be proven structurally.
