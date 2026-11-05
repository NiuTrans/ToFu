# Tofu UI Kit gallery

Living preview of the design system (authority: `docs/DESIGN_SYSTEM.md`).
Every shared control must be registered on this page.

- `index.html` — kitchen-sink page. `?theme=dark|light|tofu` selects the
  theme, `&dialog=open` renders the modal state. It consumes the real
  composed stylesheet (`static/styles.css`), never a copy.
- `capture.py` — regenerates `previews/*.png` (3 themes × desktop/mobile +
  dialog states) and `previews/contrast-report.json` (feedback-text
  WCAG 4.5:1 gate; legacy on-accent cases warn). It also refreshes
  `previews/rich-components-*.png` and `previews/dynamic-*.png` by running
  the visual tests that render them; those tests write only to their
  `tmp_path`.
- `verify.py` — 18 structural checks in a real Chromium: control height
  ramp, theme-invariant geometry, 320px overflow, dialog focus-trap /
  Escape / backdrop / action resolution, field error wiring, focus ring,
  per-theme token resolution.
- `previews/timeline-*.png` — mixed conversation chronology from the production
  Surface and tool presenters: continuous activity surfaces, answer typography,
  desktop, phone, warm Tofu branding and the nonmodal process reader. `timeline-shell-*.png` places the same
  mounted renderer in the real `index.html` shell markup, with application
  scripts absent (layout review, not a backend/boot test).
  Regenerate with `tests/visual/test_visual_timeline_reading.py`; its JSON
  measurements check label alignment and contrast against the actual activity
  surface across all themes. The rationale and visual references are in
  `docs/DESIGN_SYSTEM.md#conversation-reading-grammar`.
- `previews/context-rail-*.png` — collapsed/expanded context sections from
  `tests/visual/test_visual_context_rail_reading.py`: production renderer,
  fixed heading controls, localized counts and real pointer hit testing.
  `context-rail-live-tofu.png` also boots the actual application shell and checks
  its adaptive rail grant against the reading column and composer.

Workflow for a new/changed control:

```bash
node scripts/compose_frontend_styles.mjs   # refresh static/styles.css
python3 designs/ui-gallery/verify.py       # structural proof
python3 designs/ui-gallery/capture.py      # previews + contrast gate
```

Then inspect the PNGs in `previews/` (they are the review artifact posted
in the change). Both scripts self-serve the repository root on a local
port and reuse `chromium_env` for the rootless-container library path.

## Settings audit

`settings_audit.py` reviews every Settings tab on the same footing. It boots
the real app against disposable storage (`app_harness.py`, shared with
`migration/capture_app.py`), opens each tab in the navigation and writes a
first-screen and a fully expanded screenshot per tab and theme, plus
`report.md`/`report.json`: visible controls, collapsed sections, how much of
the first screen the tab fills, and every explanatory text block, longest
first. Screenshots and reports are review artifacts; do not commit them.

```bash
python3 designs/ui-gallery/settings_audit.py /tmp/settings-audit \
    --theme tofu --theme light --theme dark      # optional: --lang en, --tab general
```

In a worktree whose checkout differs from the deployed native storage engine,
prefix `TOFU_PERSONAL_STORAGE_BACKEND=sqlite` (the same lane the test suite
names). Chinese text needs a CJK font: the harness picks an interpreter-prefix
fontconfig that has one; otherwise set `FONTCONFIG_FILE`.

Review checklist, applied to every tab:

1. **First screen.** Everyday controls are visible without expanding
   anything; a tab that fills little of the screen hides nothing in a
   disclosure.
2. **One explanation per control.** A label plus at most one short hint.
   A hint never restates the label, the section description, a status
   banner or the empty state.
3. **No implementation narration.** Storage engines, header and vault entry
   names, measurement anecdotes and architecture boundaries belong in docs,
   not in Settings.
4. **No decoration that controls nothing.** Flow diagrams, responsibility
   boxes, legends, eyebrow labels and watermarks go.
5. **One visual vocabulary.** Section title, short description, card or list
   rows, the same empty-state card; native controls keep their native size.
6. **One language per locale.** Chinese copy uses Chinese terms and
   punctuation except for product and protocol names.
