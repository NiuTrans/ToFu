# Task engine guidance

## Scope and first reads

This package owns task-turn execution, orchestration of model/tool rounds,
context composition, compaction, event folding, and settlement. Read
`docs/modules/task_engine.md`, `docs/EVENTS.md`, and
`docs/TURN_SETTLEMENT.md`.

## Editing rules

- Keep one root run loop and one settlement path. Managers, handlers, and
  compatibility entry points delegate; they do not fork lifecycle state.
- Build model context through `context_composer/` and its explicit budgets.
  Compaction preserves required anchors, tool-call/result pairs, provenance,
  usage accounting, and durable receipts.
- Tool handlers adapt domain tools to the unified gateway. Registration,
  approval, execution, and result settlement retain distinct owners.
- Persist only through declared task/conversation repository operations after
  the relevant commit boundary. A wake/push hint never substitutes for commit.
- Preserve explicit owner cancellation through model streams, tools,
  subprocesses, child agents, persistence, and final event emission. An
  HTTP/SSE/push observer disconnect never cancels an already-started provider
  dispatch; see `docs/API_CONTRACT.md` §2.11.
- Bound rounds, fan-out, retries, context, tool output, queues, server messages,
  and retained stream state. Fault injection and rollback paths remain usable.

## Verification

Start with the focused task/round/handler/compaction test. Then run event-fold,
turn-settlement, cancellation, and owner-isolation neighbors named in the task
and context domain maps. Use `make test-unit` only after those pass.
