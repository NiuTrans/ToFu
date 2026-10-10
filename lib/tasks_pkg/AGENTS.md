# Task engine guidance

## Scope and first reads

This package owns task-turn execution, orchestration of model/tool rounds,
context composition, compaction, event folding, and settlement. Read
`docs/modules/task_engine.md`, `docs/contracts/EVENTS.md`, and
`docs/contracts/TURN_SETTLEMENT.md`.

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
  dispatch; see `docs/contracts/API_CONTRACT.md` §2.11.
- Bound explicit authorization, retries, context, tool output, server messages,
  and retained stream state. Fault injection and rollback paths remain usable.

Parallel tools and streaming prefetch use the shared demand executor in
`lib/observability.py`. Worker knobs size idle reuse only; prefetch preserves
qualified occurrences and never restores a local count gate.

Native context admission uses NativeContextPolicy and native_context_scope.
Verify generated native tool projection, route-window/output isolation, joined
preparation, unauthorized-summary refusal and installed context application tests.
Use the existing compactor/accounting owners; never create slots or fallback routes.

NativeGatewayProgress owns one current settled batch of hashes; run_native_task
binds it from explicit breaker limits. Verify native-progress/gateway tests and
installed context execution. Match native call counts, withdraw incomplete or
oversized evidence, and release borrowed gateway references on close.
Project preparation/consumption enters through ConversationStore. The default
adapter owns host project validation; native and Python verified returns share
confirm_consumed_project_context. Verify project-store/native-context tests and
current-coordinator consumption; transient tasks must not activate app storage.
NativeCheckpointPolicy delegates to selected embedded storage, with explicit
financial/time recovery and supplied executor. Verify callback cancellation,
unknown acquisition, native finish retry and real-library pause/reopen. Release
paused recovery only after NativeTaskTerminalPolicy.accepted; cleanup preserves
primary failure. No Python journal or selected-backend fallback.

NativeModelAccountPolicy uses an existing owned tree account and executor.
Verify native_model_control installed parent cap/zero-request refusal, unknown
usage, exact settlement and real native_model_account application persistence.
ModelPolicy owns counted finance; RoundPolicy must not reserve the same account.

## Verification

Start with the focused task/round/handler/compaction test. Then run event-fold,
turn-settlement, cancellation, and owner-isolation neighbors named in the task
and context domain maps. Use `make test-unit` only after those pass.
