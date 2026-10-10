# Retained runtime guidance

## Scope

This tree is the retained classic browser runtime. Authored code lives under
`sections/`; `app-runtime.js` is composed output. Read
`docs/FRONTEND_ARCHITECTURE.md` before editing.

## Editing rules

- Edit the smallest owning file under `sections/` and update its manifest only
  when composition order or membership changes. Never hand-edit
  `app-runtime.js`.
- Do not add a new retained section for new behavior; implement a TypeScript
  feature and use a narrow bridge when migration cannot be completed at once.
- A bridge delegates to one typed owner and includes a deletion path. It must
  not duplicate state, event dispatch, transport, or rendering logic.
- Preserve established boot order, readiness, action registration, and teardown
  contracts. Avoid undeclared cross-section globals and import-time network
  work.
- Keep conversation and tool rendering as projections of canonical events.
  Bound retained nodes, stream buffers, timers, and listeners.
- When extracting code to TypeScript, remove the superseded section behavior
  and its compatibility tests in the same increment.

## Verification

Run the focused retained-runtime harness, `npm run check:runtime`,
`npm run check:actions`, and `npm run typecheck:modules`. Regenerate only after
source checks pass; do not overwrite an unrelated dirty composed output.
