# tofu-ui adapter layer

## Scope

`frontend/src/ui/` is the ONLY supported way for application code to build
shared controls. Its contract is documented in `docs/DESIGN_SYSTEM.md`;
its styles live in `frontend/src/styles/application/30-ui-kit.css`, its
tokens in `00-design-tokens.css`.

## Rules

- Factories return DOM elements with `ui-*` classes pre-applied. Application
  code imports from `index.ts` only and never reaches into kit internals.
  Behavioral controls go through factories exclusively; purely presentational
  primitives with no variants or state (`.ui-table`, `.ui-divider`,
  `.ui-skeleton` markup) may be written as `ui-*` markup directly.
- One module per control family, dependency-free except sibling ui modules.
  Keep behavior on native elements (`<dialog>`, `<details>`, `<select>`)
  wherever the platform already provides focus, keyboard and dismissal
  semantics; add TS only for what the platform lacks.
- New controls land in three places together: factory here, styles in
  `30-ui-kit.css`, and an entry in `designs/ui-gallery/index.html`. A
  control without a gallery entry does not ship.
- Factories stay theme-agnostic; all theme adaptation flows through tokens.

## Verification

`npm run typecheck`, then regenerate the gallery previews
(`python3 designs/ui-gallery/capture.py`) and inspect the three-theme
screenshots for any new/changed control.
