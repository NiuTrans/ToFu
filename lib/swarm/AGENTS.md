# Multi-agent coordination guidance

## Scope

This package coordinates parent/child agent work on top of the canonical task
engine. It is not a second model loop, event taxonomy, persistence authority, or
project board.

`agent.py` adapts scheduling to `lib/tasks_pkg/child_invocation.py`, the shared
Goal/Swarm factory. Its child artifact tools ride the typed `RunArtifactTools`
port, which project dispatch admits by exact type; never install artifact
handlers as an ad-hoc `_tool_env` object. `master.bind_assignment_contexts` owns wave preparation and current-caller grants
for retained runs and follow-up waves; persisted field names remain compatible.
`lib/tasks_pkg/child_invocation.py` owns execution; see
`docs/TASK_EXECUTION_MODES.md` for input, cancellation and evidence contracts.
Ordinary and retained parent proxies preserve the read-only requirement source
and, like wave-preparation policy copies, borrow live native identity through
`tasks_pkg/native_parent.py` instead of holding a copied port. Durable
session metadata retains source IDs and native recovery admission, never ports.
Verify `test_native_delegation_recovery.py` and the production delegation test.
Under a durable spawning attempt, parent carriers borrow that task's worker
claim (`worker_binding.share_conversation_worker`) and `turn_handoff`
keeps the attempt's job claimed while a session `holds_parent` it. Verify
`tests/conversation/test_durable_swarm_descendants.py` on all three authorities.

## Editing rules

- Carry root task, parent/child, owner, role, objective, and budget identity
  explicitly through every spawn, route, message, snapshot, and settlement.
- Delegate execution to task/agent-core ports and persist through declared task
  or conversation operations. Do not call provider transports or SQL directly.
- Define one terminal outcome for each child and deterministic aggregation for
  the parent. Late messages, duplicate settlement, cancellation, and partial
  failure remain distinguishable.
- Dependencies, identity, cancellation and inherited consumption authority own
  execution; local pool/slot/count limits must not reject authorized work.
  Bound reconstructible messages/snapshots and retries; reject cycles and orphan work.
- The shared execution-budget account's root must outlive every agent it
  admits. A spawned swarm is fire-and-forget, so `MasterOrchestrator`
  re-roots the account onto its session-lifetime `_parent_task_proxy`
  (`_promote_proxy_to_budget_root`), which stays `status='running'` until
  `abort()`/`suspend_for_restart()` mark it terminal. Never let the account
  root be the short-lived spawning chat task: a dependency-blocked agent
  admitted after that task settled must not hit `execution_owner_cancelled`
  at admission. Covered by
  `test_swarm_session_root_outlives_spawning_turn_for_blocked_dependents`.
  Exception: a natively executed parent keeps the root. Native model
  selection and fee transfer admit only that exact live task, and its issued
  port retires with the turn, so no native child is issued after it settles
  (`docs/TASK_EXECUTION_MODES.md`, `tests/tasks_pkg/test_native_parent.py`).
  A worker that would start later settles failed as `native_parent_settled`
  before allocation; wave preparation captures the caller's binding while live.
- Cancellation and shutdown propagate down the tree; cleanup cannot erase
  committed child evidence needed for parent recovery.
- A worker is an ordinary agent beside its parent: its silence is judged only
  by `lib/tasks_pkg/liveness.py` (the root's clocks, explained waits, command
  ladder and threshold). `ProgressBeacon` is the swarm's view of that verdict
  for bound workers; never add a swarm-private stall clock or threshold.
- Integration adapters project canonical events and cannot own hidden global
  coordinator state.
- A role that holds `run_command` also holds `poll`/`kill`; a leaf worker
  (`routing.is_leaf_worker`) reaches only the `bg_*` commands it started, and
  `lib/tasks_pkg/handlers/poll.py` refuses every other handle. A worker message
  never wakes an idle root; the rejection directs it to finish with a blocker
  result (`docs/AGENT_LIFECYCLE.md`).
- Worker message construction routes through `lib.tasks_pkg.context_composer`:
  `worker_policy` is the only system-authority contract, a bare `system`
  carrier is emitted only when the inherited prompt is non-empty, and the
  shared ambient blocks compose with the parent's resolved config. Checkpoint
  resume refreshes by registered injection ID, never by message position. Do
  not hand-build prompt dicts in this package.

## Verification

Run focused `test_swarm_*` routing, snapshot, cancellation, budget, and
integration tests, plus task settlement/event tests when shared lifecycle
behavior changes.
`test_swarm_invocation.py` exercises the real common pipeline;
`test_swarm_agent_loop_chassis.py` forbids private execution paths.
