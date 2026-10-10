# Orchestration definition guidance

## Scope

This package owns versioned orchestration definitions, validation, authoring
metadata, persistence services, and run APIs. Execution modules named
`lib/orchestration_*.py` are the graph runtime. Read
`docs/modules/orchestration_dag.md`.

> **Name disambiguation**: this is the durable *orchestration-graph* owner.
> The task-turn root loop lives in `lib/tasks_pkg/orchestrator/` (see
> `docs/modules/task_engine.md`) — a different domain despite the near-identical
> name. Decide which owner a change belongs to before editing.

## Editing rules

- Define node, edge, control, role, default, and validation semantics once in
  the contract owners. Inspection, save, plan, execution, HTTP, and frontend
  projections must agree.
- Persist definitions through the owner-aware store and Sidecar semantic
  operations with CAS/version behavior. Never select another owner's definition.
- Keep definition validation pure and deterministic. Runtime state, retries,
  mutation, replay, and outcome settlement belong to their explicit services.
- `execution_journal.py` owns effect receipts; `run_recovery.py` validates
  restart evidence against raw execution snapshots; `continuation.py` owns
  worker-free human suspension and decision admission on that same log. Browser layout projections
  never become recovery authority. Read `docs/TASK_EXECUTION_MODES.md`.
- Independent branches start through the shared demand executor; parallelism
  hints size idle reuse only. Graph execution has explicit nodes, depth, retries, budgets,
  transcripts, and artifacts, with cancellation propagated to every branch.
- Generated authoring metadata, compatibility defaults, HTTP contracts, and
  wire formats are regenerated from their source owners, not hand-edited.
- Frontend Studio behavior lives under `frontend/src/features/orchestration/`;
  do not encode UI policy in the definition package.

## Verification

Run the smallest `tests/orchestration/test_orchestration_*.py` owner suite and regenerate/check
the affected artifacts. Add service, engine/outcome, API parity, and frontend
workspace tests as the change crosses those boundaries.
