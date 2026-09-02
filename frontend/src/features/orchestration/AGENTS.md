# Orchestration Studio guidance

## Scope and first read

This tree owns the TypeScript authoring workspace and run projection for the
orchestration product. Read `docs/modules/orchestration_dag.md`; backend
definition/runtime contracts remain authoritative.

## Editing rules

- Build editor, inspector, validation, layout, mutation, run, and outcome views
  from generated/typed contract projections. Do not recreate backend defaults or
  validation rules in ad hoc UI conditionals.
- Keep authored definition state, persisted server version, dry-run plan, live
  run state, and presentation selection distinct. Conflicts refresh/merge or
  fail visibly; they never silently overwrite.
- Route all mutations through the named command service and canonical mutation
  result. Optimistic UI records rollback state and settles once.
- Graph editing preserves stable node/edge identities, deterministic layout,
  keyboard access, focus, undo/redo bounds, and readable narrow-screen fallback.
- Run streams are projections with explicit disconnect/replay/cancel/terminal
  behavior. Do not create a browser execution engine.
- Register through the feature/action owners and dispose subscriptions,
  observers, workers, timers, and large graph caches on exit.

## Verification

Run focused orchestration workspace/authoring frontend tests,
`npm run typecheck:modules`, and the generated orchestration contract checks.
Add visual/E2E coverage for graph interactions or live-run journeys.
