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
  restart evidence against raw execution snapshots; `invocation_recovery.py`
  bridges completed registered children through their owned task-result CAS;
  optional private native fees share that CAS and never enter the public receipt;
  `invocation_frontier.py` reads exact dead-writer actor evidence without leases;
  aggregate preflight compares the complete active set to a shared account cut.
  Charged pending files require the original complete batch and owned publication
  proof through `native_file_evidence`/`invocation_effects`; counts alone never
  authorize replay. See `docs/contracts/EXECUTION_ACCOUNT_CONTINUITY.md`.
  `inspect_journal` keeps pending admissions separate from completed records;
  its explicit account-handoff port serializes native ACKs with ordinary writes
  and fences reentry/unknown delivery. Goal publication owns physical positions;
  verify test_goal_native_journal and existing effect/retry specifications.
  `continuity.py` acknowledges source-cursor-bound audit before the domain's
  explicit submission hook. After issuance, only claim/consumption may append;
  unknown submission keeps election pending without moving that cursor.
  Verify `test_continuity_source_cursor` and public Goal restart on every backend.
  `recovery_frontier.py` inspects pending inputs through the same interpreter
  and a read-only journal port; exact keys/digests and complete inventory are
  required. Verify test_orchestration_recovery_frontier plus native Flow parity.
  `agent_runner.prepare` scopes ordinary child preparation without dispatch;
  unused children settle through the child owner, active scope close cancels and
  joins. Verify prepared-invocation and ordinary runner/child tests.
  `prepared_recipients.py` binds inspected occurrences through the root journal's
  explicit dispatch port; actual branch runners retain context/events/Stop and
  normal child completion. Verify prepared-recipient and native-cursor parity
  plus registered completion/lost-ACK specifications. Preparation grants no
  checkpoint takeover; goal_runs/native_recovery composes accepted Goal takeover.
  only the settled reader grants ordinary replay. Missing completion is distinct
  from corrupt/inaccessible evidence. `recovery_account.py` validates/selects cumulative observations for the existing
  execution account, never sums them or grants takeover. `continuation.py` owns
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

Native Flow control is optional explicit composition through native_flow.py.
The Rust cursor owns traversal and topology; existing callback/journal owners
retain effects and recovery. Verify native engine/topology/lifetime/recovery
specifications plus real v4 fixtures. No selected-native failure may fall back.
