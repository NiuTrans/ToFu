---
name: tofu-design-system
description: Build NEW pages, features, or standalone project UIs (翻译项目/企业项目/新页面/新项目 UI, translation tools, enterprise apps, dashboards) on the Tofu design system — every control from frontend/src/ui factories and every value from design tokens; never hand-rolled CSS, one-off buttons, or raw hex/px spacing.
---

# Build UI on the Tofu design system

Use this workflow whenever a request asks for a new page, feature surface,
or a whole new project UI (including translation/enterprise tools that
reuse Tofu's agent runtime). The authority contract is
`docs/DESIGN_SYSTEM.md` — read it first; do not duplicate its tables here.

## Workflow

1. **Start from the scaffold**, not a blank file:
   `designs/ui-gallery/scaffold.html` is a complete app shell (sidebar +
   toolbar + content) built only from the kit. Copy it, then replace its
   demo content with the feature's real structure.
2. **Every control comes from a factory** in `frontend/src/ui/index.ts`
   (button, field, dialog, drawer, tabs, dropdown, toast, alert, table,
   choice, progress, empty state…). Do not hand-write `ui-*` class strings
   for behavioral controls, and NEVER invent a new button/input/checkbox
   style — extend the kit instead (factory + `30-ui-kit.css` + gallery
   entry, all three together).
3. **Feature CSS only for genuinely novel visuals**, and then tokens only:
   spacing from `--space-*`, font sizes from `--font-size-*`, colors from
   the token contract in `00-design-tokens.css`. Raw hex colors and raw px
   margins/paddings fail `npm run check:styles` — that gate is the
   definition of done.
4. **Layout by primitives** (`.ui-stack/.ui-row/.ui-grid/.ui-toolbar`) so
   alignment is a construction property, not manual margin tuning.
5. **Register new shared controls** in `designs/ui-gallery/index.html`.

## "Not ugly" is machine-checkable — run the loop

```bash
python3 designs/ui-gallery/verify.py   # 37 structural/behavioral checks
python3 designs/ui-gallery/capture.py  # 3-theme screenshots + WCAG contrast gate
npm run check:styles                   # token gate + size budgets
```

Post the regenerated `designs/ui-gallery/previews/*.png` for user review;
do not claim visual quality without these artifacts.

## Agent capability

The agent side of the project (tasks, tools, streaming) reuses Tofu's
existing surfaces — see `docs/AGENT_CAPABILITY_GUIDE.md` and
`docs/FRONTEND_ARCHITECTURE.md`. Do not build a parallel agent runtime.
