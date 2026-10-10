# Tofu UI Kit gallery

Living preview of the design system (authority: `docs/DESIGN_SYSTEM.md`).
Every shared control must be registered on this page.

- `index.html` — kitchen-sink page. `?theme=dark|light|tofu` selects the
  theme, `&dialog=open` renders the modal state. It consumes the real
  composed stylesheet (`static/styles.css`), never a copy.
- `capture.py` — regenerates `previews/*.png` (3 themes × desktop/mobile +
  dialog states) and `previews/contrast-report.json` (feedback-text
  WCAG 4.5:1 gate; legacy on-accent cases warn).
- `verify.py` — 18 structural checks in a real Chromium: control height
  ramp, theme-invariant geometry, 320px overflow, dialog focus-trap /
  Escape / backdrop / action resolution, field error wiring, focus ring,
  per-theme token resolution.

Workflow for a new/changed control:

```bash
node scripts/compose_frontend_styles.mjs   # refresh static/styles.css
python3 designs/ui-gallery/verify.py       # structural proof
python3 designs/ui-gallery/capture.py      # previews + contrast gate
```

Then inspect the PNGs in `previews/` (they are the review artifact posted
in the change). Both scripts self-serve the repository root on a local
port and reuse `chromium_env` for the rootless-container library path.
