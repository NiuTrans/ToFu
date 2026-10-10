# Task orchestrator guidance

## Scope

This package owns one root agent loop and its per-turn/per-round coordination.
Model transport, tools, compaction, persistence, and event vocabularies retain
their separate owners.

> **Name disambiguation**: this is the *task-turn root loop*
> (`lib/tasks_pkg/orchestrator/`). It is NOT the durable orchestration-graph
> owner — that is `lib/orchestration/` (definitions, runs, DAG runtime), a
> different domain with its own domain map (`docs/modules/orchestration_dag.md`).
> When a change mentions "orchestration", decide which owner you mean before
> editing; see `docs/modules/task_engine.md` for this package.

## Editing rules

- Keep the root loop, turn state, round state, one model attempt, tool dispatch,
  post-loop work, and finalization as explicit phases with a single transition
  owner.
- Emit ordered canonical events only after their facts are known. Persistence
  commit precedes wake/push; final settlement occurs exactly once.
- Retries and fallbacks start a distinguishable attempt and preserve usage,
  provider, error, and cancellation attribution. They do not replay committed
  side effects.
- Tool calls flow through the unified gateway; child agents and human guidance
  use declared ports. Never call a domain private implementation directly.
- Propagate explicit owner cancellation through queued model slots, streams,
  tools, child agents, persistence, and post-loop hooks while preserving
  recoverable state. HTTP/SSE/push observer loss does not own cancellation of
  an already-started provider dispatch.
- Bound rounds, tool batches, child work, server messages, pending events, and
  finalization time. Helper extraction must not create shadow state.

## Verification

Run focused orchestrator round/turn/finalization tests, then retry/fallback,
tool settlement, event order/fold, cancellation, and turn-settlement neighbors.

prepare_round_context/admit_round_context are shared by the default adapter and
NativeContextPolicy. Verify ordered injection, child inbox scope, post-notice
admission and verified/cancelled delivery confirmation; never fork this policy.
