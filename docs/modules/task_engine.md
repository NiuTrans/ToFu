# Task engine

`lib/tasks_pkg/` executes one model/tool attempt. It is an executor, not a
conversation repository and not a browser transport.

## Boundary

Input is an API-ready message list plus task configuration. Output is a stream
of typed task events and cumulative executor state. For conversation work, the
task is bound to a durable `(turnId, attemptId, userId)` before execution.

The authoritative flow is:

```text
turn command
  -> pending generation attempt
  -> claim + bind executor task
  -> model/tool loop
  -> append_event
  -> turn.event.record (projection + carried task event)
  -> v3 conversation sync
```

The task result table is recovery/diagnostic storage for executor state. It is
not a second transcript. Conversation attempts keep renderable content, tool
rounds, Flow node phases, and terminal settlement in turn authority.

## Package map

| Area | Owner |
|---|---|
| task registry, lifecycle, event buffer | `manager/` |
| shared ReAct lifecycle | `lib/agent_loop.py` |
| root-chat ReAct policy/wire adapter | `orchestrator/_root_agent_loop.py` |
| tool parsing/execution | `tool_dispatch/`, `executor/` |
| model streaming and fallback | `stream_handler/`, `llm_fallback/` |
| prompt/context assembly | `conv_message_builder/`, `context_composer/` |
| provider-neutral PTC/Multi-agent task-shape policy | `tool_orchestration_policy.py` |
| orchestration projection/adoption evidence | `lib/orchestration_adoption.py` |
| compaction | `compaction/` |
| Flow-backed chat selection and delivery | `lib/orchestration_chat_flow_*.py` |
| autonomous virtual-user policy | `autopilot.py`, `autopilot_baton.py` |
| durable executor event log | `event_log.py` |

Facades may re-export stable entry points, but new implementation belongs in a
cohesive leaf module. A facade must not preserve a retired behavior merely to
keep an old monkeypatch path alive.

## Shared ReAct lifecycle

`run_agent_loop` is the sole LLM/tool round-loop authority for root chat,
swarm workers, timer and research engines. It owns round numbering,
continue/stop sites, the three abort placements, timeout counting and the
post-tool checkpoint boundary. Root-specific request building, stream anomaly
policy, budget/protocol gates, event projection and semantic loop detection live
in `_root_agent_loop.py` as typed hooks. They return `LoopDirective`; they do
not own a second LLM/tool `while` or infer completion from response text.
FlowExecutor owns graph-level iteration only; role execution delegates through
the shared agent runner.

Two complementary guards bound non-recovering tool loops. Before execution,
the exact-call guard fingerprints tool name, arguments, world version, and new
evidence so repeated side effects stop without running again. After execution,
a batch adapter may report stable `nonretryable_failure_signatures` only when
every tool result is a canonical `tofu.tool-result/v2` error with
`retryable=false`. The chassis ignores arguments for this second signal: a
changed tab ID, selector, or path cannot make an unchanged capability denial
recoverable. Swarm halts after three consecutive equal terminal-failure rounds
with `exit_reason=nonretryable_tool_failure`, then performs one tool-less
wrap-up. Any success, mixed result, retryable error, malformed envelope, legacy
string, or changed code resets the streak; unknown results therefore fail open.

## Model-round budget

Root execution has a finite model-API-round budget when `maxApiRounds` is unset
or zero: 192 in personal mode and 512 in distributed mode. Operators may change
the inherited default with `TOFU_TASK_MAX_API_ROUNDS`. Positive request values
may raise or lower it but cannot exceed 1,024; malformed, zero, and negative
values inherit the profile. Other task budgets remain opt-in.

Before each provider call, root checks the completed `apiRounds` ledger. With
`min(64, floor(limit / 3))` rounds left, it emits one `budget_warning` and one
model-visible `_isMeta` reminder to finish and verify. At the hard limit it
makes no provider call and settles with `task_budget_exceeded`, never verified
completion. Other adopters enforce their own declared outer budgets.

## Task carrier

The in-memory task dictionary is a process-local execution carrier. Important
field groups are:

- identity: `id`, `convId`, `_turnId`, `_attemptId`, `_userId`;
- configuration: `config`, `model`, feature/tool flags;
- request-local orchestration: `_toolOrchestration`, `_ptc_local` (recomputed
  before each wire request), plus bounded `_toolOrchestrationDecisions`;
- cumulative projection: `content`, `thinking`, `segments`, `toolRounds`,
  `programRuns`;
- lifecycle: `status`, `finishReason`, `error`, `aborted`;
- accounting: `usage`, `apiRounds`, cost/fallback metadata;
- event delivery: `events`, sequence/cursor locks;
- cooperative controls: abort event, interaction requests, inbox injections.

Private fields need explicit owners/lifecycles; `run_command` caps settled and live reconnect output at 100,000 characters (prefix, tail, count, marker).
Completed rounds drop that buffer before durable projection; swarm cleanup is session-scoped, and durable behavior may not use unbounded globals.

Incremental translation follows that rule explicitly: active accumulators and
each accumulator's preview-operation buffer use probed finite budgets. Preview
segments are bounded/coalescible; terminal finalize, stamp, and cancel handoffs
reserve capacity and are never discarded.

## Persistence discipline

`append_event` persists before visibility and may carry the exact task event in the same Sidecar transaction. An oversized carried frame retries once with
cumulative text and opens a 30-second full-projection probe circuit, preventing a deterministic retry storm while raw-event durability remains exact.
Writes without that carrier fail closed; a later full probe or terminal settlement converges the Turn document.

Pure text progress may be coalesced. Structural events, interaction requests,
and terminal events persist immediately. The next structural/terminal write
carries cumulative text, so coalescing changes cadence, not final state.

Provider text ingress emits its first chunk immediately, then merges at most
100 ms or 256 characters before assigning the next event sequence. Retry,
diagnostic, tool-ready, error, and provider-return boundaries synchronously
flush the tail, preserving durable-before-visible ordering. Each active model
stream owns at most one daemon coalescer worker; it exits when that provider
call returns or raises, and the buffer never exceeds the character ceiling.
Production replay samples (4-character median chunks) project 55.5–64.4%
fewer text-event transactions at this window. The executable resource budget
is pinned by `tests/test_stream_delta_coalescing.py`.

Terminal processing has one direction:

1. stamp the task terminal once;
2. emit the typed terminal event;
3. commit turn settlement;
4. persist bounded executor diagnostics;
5. release heavy in-memory fields;
6. notify projections and dispatch any durable queued successor.

Task manager code must never append/replace conversation messages. Queue
dispatch, autopilot, timers, proactive work, and swarm continuation all create
new turn commands.

Orchestration persistence is truth-preserving: `projectionEvidence` means a
backend reached the model wire, while `adoptionEvidence` accepts only an actual
program run, native multi-agent response item, launched local agent wave, or
completed model round. `adoptionStatus` and `actualShape` are derived from those
runtime ledgers at projection time; mutable latches and non-empty prose cannot
upgrade an offered lane into an adopted one. Legacy v1 decision rows remain a
read-only compact projection without retrofitted adoption claims.

## Autonomous drivers

Flow-backed chat stores visible role messages as explicit related turns.
Autopilot creates an atomic virtual-user/input plus assistant/output pair and
then claims the successor attempt. Swarm and scheduler continuation use the
shared scheduled-turn dispatch service with explicit owner identity and stable
command ids.

Every unattended loop needs:

- a durable command id;
- an owner-scoped lane-busy decision;
- a finite chain/budget policy;
- honest visible attribution;
- a terminal error when executor startup fails.

## Plan collaboration mode

`planMode: true` is one attended, read-only model/tool loop. Config-resolution
and runtime normalization enable `ask_human` automatically and disable
autopilot, direct image generation, and selected orchestration flows. This
allows multiple clarification exchanges inside the same turn without asking
the user to discover a second toggle.

The model proposes rather than executes. Only the successful Plan-task terminal
boundary may mint a complete tagged plan into the typed turn sidecar owned by
`lib/plan_contract.py`; arbitrary assistant text is never upgraded into
execution authority. Leaving Plan mode manually does not synthesize an
execution prompt; execution starts only through the v3 exact-plan command
described in `CONVERSATION_SYNC_V3.md`.

## Failure rules

`status: rejected` means the tool did not execute; the public `rejection`
descriptor carries its stable kind, tool, reason, and known retryability.
`_rejected` and result-level `rejected` are compatibility aliases. The descriptor
survives the round, tool events, cold Turn projection, and activity timeline.
If it is the last terminal tool act, finalization emits a typed task error:
`hallucinated` maps to `tool_not_available`; all other refusal kinds map to
`tool_call_rejected` with original detail. Abort/interruption wins. Activity
keeps status `skipped`, summary `blocked`/`unavailable`, and kind as `reasonCode`.

- Never infer success from non-empty content.
- Never turn a provider/tool error into `done/stop`.
- Never infer terminal retry policy from legacy text; only the typed result
  contract feeds the terminal tool-failure breaker.
- Recovery crosses the stream-handler API as `RecoveryDecision`; the root loop
  never branches on an open-ended dict.
- `TurnVerdict` derives durable completion fail-closed; missing evidence fails.
- Never push a frame whose authoritative write failed.
- Never retry a CAS conflict by rewriting a conversation-sized snapshot.
- Never bind an executor after it has already started.
- Fence stale attempts cooperatively; never let them emit indefinitely.
- A safety-cap stop is incomplete, never verified completion.

## How to change it

Start with pure policy, event shape, command service, Sidecar contract, then
browser projection. Source scans ratchet ownership; behavior needs execution.

Relevant gates include:

- Turn/event settlement: `tests/test_turn_event_carried_task_event.py`,
  `tests/test_tool_settle_all_lanes.py`, `tests/test_orchestration_chat_completion.py`.
- Drivers/failures: `tests/test_finalize_persist_before_autopilot.py`,
  `tests/test_swarm_async.py`, `tests/test_agent_terminal_failure_breaker.py`,
  `tests/test_release_heavy_task_state.py`.

See [CONVERSATION_SYNC_V3.md](../CONVERSATION_SYNC_V3.md), [TURN_SETTLEMENT.md](../TURN_SETTLEMENT.md), and [EVENTS.md](../EVENTS.md) for durable/wire contracts.
