# Task engine

`lib/tasks_pkg/` executes one model/tool attempt. It is an executor, not a conversation repository or browser transport.

## Boundary

Input is an API-ready message list plus task configuration. Output is a stream of typed task events and cumulative executor state. For conversation work, the task is bound to a durable `(turnId, attemptId, userId)` before execution.

The authoritative flow is:

```text
turn command
  -> pending generation attempt
  -> claim + bind executor task (pending/queued) -> worker entry + fenced start (running)
  -> model/tool loop
  -> append_event
  -> turn.event.record (revision patch + carried task event)
  -> v3 conversation sync
```

The task result table is recovery/diagnostic storage, not a second transcript; renderable content and settlement stay in turn authority. Normal and Flow chat share one finite FIFO Agent scheduler sized from effective CPU and memory/RSS headroom.
Waiting consumes a bounded queue entry, not an Agent slot. The reaper may quarantine a proven-wedged Python thread and admit a bounded replacement; the old thread retires if it returns, so recovery cannot create unbounded thread growth. While a task is resident in that FIFO, `spawn.py` emits the registered `executor_queued` phase with one-based position, total queued work, active/configured slots, and monotonic wait seconds, refreshing only when evidence changes or at sparse 20-second/one-minute milestones. New arrivals behind a task do not invalidate its phase, avoiding queue-wide repaint amplification. A shared per-task lock orders the last queued phase before physical worker entry so late persistence cannot overwrite `workerStarting`; provider 429/backoff remains `retrying`, letting clients distinguish host scheduling from model/API capacity without guessing from elapsed time.
After settlement, only terminal `round_committed` and `preference_learned`
observers use standalone task replay; their dedicated settled-Turn CAS owns
projection enrichment, while every other late frame retains the zombie fence.

Automatic translation is triggered only after Turn authority settles. Its
already-target skip is a whole-document identity decision: dominant-language
classification alone is insufficient because a target-language opening may
hide foreign-language sections later in the assistant content. Terminal root,
Flow-visible child, and incremental segment paths share the conservative
`lib/translate/skip_policy.py` predicate; mixed prose proceeds to translation,
while symbols, bare paths/URLs, and genuinely monolingual target prose may be
committed as an explicit no-op.

## Package map

| Area | Owner |
|---|---|
| task registry, lifecycle, event buffer and residency policy | `manager/`, `lib/agent_core/task_runtime.py`, `lib/agent_core/task_runtime_policy.py` |
| shared ReAct lifecycle | `lib/agent_loop.py` |
| root-chat ReAct policy/wire adapter | `orchestrator/_root_agent_loop.py` |
| tool parsing/execution | `tool_dispatch/`, `executor/` |
| model streaming and fallback | `stream_handler/`, `llm_fallback/` |
| prompt/context assembly | `conv_message_builder/`, `context_composer/` |
| provider-neutral PTC/Multi-agent task-shape policy | `tool_orchestration_policy.py` |
| orchestration projection/adoption evidence | `lib/orchestration_adoption.py` |
| compaction | `compaction/` |
| Flow-backed chat selection and delivery | `lib/orchestration_chat_flow_*.py` |
| Goal Mode lifecycle/policy | `lib/goal_runs/` + Flow-backed chat adapters |
| retired standalone-autopilot compatibility | `autopilot.py`, `autopilot_baton.py` |
| durable executor event log and terminal timing projection | `event_log.py`, `turn_trace.py` |

Facades may re-export stable entry points, but new implementation belongs in a cohesive leaf module. A facade must not preserve a retired behavior merely to keep an old monkeypatch path alive.

## Shared ReAct lifecycle

`run_agent_loop` is the sole LLM/tool round-loop authority for root chat, swarm workers, timer and research engines. It owns round numbering, continue/stop sites, three abort placements, timeout counting and the post-tool checkpoint. Root request building, anomaly policy, budget/protocol gates, event projection and semantic loop detection are typed `_root_agent_loop.py` hooks returning `LoopDirective`; they never own another LLM/tool `while` or infer completion from response text. FlowExecutor owns graph iteration only; role execution delegates through the shared agent runner.

Independent guards bound non-recovering tool loops. The shared no-progress guard runs only after tool execution and fingerprints the ordered calls, world version, and exact model-visible result snapshot. It accumulates only when all three are unchanged in consecutive completed rounds; a changed result, changed operation, changed world, verified success, or missing/incomplete result evidence resets and fails open. Read-only work and elapsed rounds alone are never stall evidence.
Stable `nonretryable_failure_signatures` accept only a canonical non-retryable typed error (the sparse model error or a legacy full `tofu.tool-result/v2` record) plus the concrete tool/argument identity. Swarm halts after three equal terminal-failure rounds with `exit_reason=nonretryable_tool_failure`, then performs one tool-less wrap-up. Any success, mixed/retryable/malformed or legacy result, or changed operation resets the streak; unknown results fail open.
Root post-dispatch policy also distinguishes byte-identical call/outcome loops from semantic no-progress no-ops and already-covered reads, gives one `_isMeta` safety correction, then stops continued waste.
For V2 partial results, the guard reconstructs its server observation from sparse `toolContent` plus the non-model `tofu.tool-result-evidence/v1` round sidecar (and still reads legacy full envelopes). Range/cursor/limit and presentation-only changes retain one resource identity across registered idempotent tools; only the model-visible summary/items/error projection counts as evidence, so new pages remain productive but new artifact IDs cannot disguise an identical projection.
The guard first directs the model to `read_tool_artifact`/`search_tool_artifact` or a visibly advancing narrower read, then stops one continued no-progress episode.
When local PTC was actually projected, three productive single eligible-read rounds may instead earn a non-forcing `_isMeta` adoption hint; a still-serial task may receive another only after 24 completed rounds, both efficiency lanes share a four-hint task cap, native/off lanes never receive the PTC-specific hint, a same-round safety correction preempts it, and real user steering remains a hard boundary.
Engine-authored stall/stream-continuation corrections and pure peer/swarm evidence are likewise transparent to current-user extraction; an inbox row containing human steering is not.

Flow convergence policy follows the same evidence hierarchy. Similar critic or virtual-user wording is an advisory signal only: it may inject one strategy-change directive but cannot terminate a run. The same is true when a complete structured `[PROGRESS]` signal stays flat across edit-shipping turns with overlapping targets: a complex fix may need several incremental edits before a criterion becomes fully resolved. The zero-deliverable hint explicitly permits focused read-only investigation and forbids mutation merely to satisfy the guard. Hard boundaries remain transparent finite iteration/turn budgets. The manager's `stuck_no_progress` reaper is separate infrastructure liveness policy: it requires both the real-event clock and dispatch heartbeat to be stale, exempts live human/model waits, and interrupts a silent subprocess before escalating to task failure.

One provider response is an ordered occurrence list. Every response position is an independent model action even when tool name, arguments, caller, or provider call ID are byte-identical.
Blank, recycled, or duplicate IDs are reminted before history/settlement so each assistant call retains exactly one adjacent result; equality never elects one physical owner for sibling positions.
Only a byte-identical transport retransmission proven to target the same stable stream slot may be suppressed before execution. Across provider responses the same payload is likewise a fresh model action governed by ordinary evidence/world-state guards.

Continue replay groups history by ordered `(attemptId/taskId, llmRound, contiguous occurrence)`, never a Turn-wide `llmRound` dictionary. Attempt counters restart; merging equal numbers fabricates a provider batch, reorders history, multiplies checklist/tool context and can induce repetition.
Legacy unstamped rows use contiguous order and observable `roundNum` resets without inventing ownership. Continue, checkpoint, segment, and cold-history reconstruction share one causal-prefix validator: explicitly superseded provider-attempt artifacts and identity-free display rows are transparent; any other identity-bearing malformed row stops replay before dependent calls.
Exact result text is the execution receipt regardless of `done`, `error`, `rejected`, or `aborted` verdict, and structured arguments are canonicalized without mutating durable audit rows. A malformed supplied `toolHistory` fails the request before any partial history reaches a model.
Resume text, tool rounds, checklist state, usage, and file metadata are validated and detached as one preflight before project setup or prefetch; hydration begins only after the whole snapshot is valid. The `todo_write` schema stays within 325 tokens and its current-state sidecar is bounded to 24 items per checklist, six nested levels, 64-character IDs, 512-character steps, 2,048-character replan reasons, an eight-entry history tail with explicit dropped count, and 1.5 MB serialized; the measured four-byte-Unicode maximum is 1,297,374 bytes, while raw tool rounds remain the complete durable audit. Every model-facing result, compaction attachment, and continuation reminder repeats each stable item ID, so folding the original call never asks the model to recreate checklist identity from prose.
Checkpoint resume reconstructs its interrupted assistant/tool rounds through the same canonical wire projector used by the next ordinary conversation rebuild, inserts that suffix before the optional resume prefill, and separately retains the raw rounds for durable settlement. The resumed request therefore keeps tool evidence and remains a byte-stable cache prefix for its successor. Resume snapshots carry no round-count or serialized-byte ceiling: oversized histories are folded by working-set compaction before dispatch, never rejected.


Swarm history is durable on the `spawn_agents` tool round: the returned handle identifies that wave, while `_swarmSnapshot` carries each agent's status, full result preview, token/timing/file counters, and bounded tool-call timeline. Agent callbacks stamp incremental/final snapshots when the handle is already visible. Tool settlement is the mandatory race-repair boundary: once it writes `toolContent`, it immediately reconciles the still-authoritative active session onto that exact round. This closes fast-agent completion before handle publication; the monotonic snapshot version and equality check make callback/settlement/replay writes idempotent. A detached CAS writer remains responsible only after the owning attempt settles.
The persisted `toolContent` is usually the SPARSE `summary_items` model projection (`{"summary", "items": [handle]}`), from which `lib/tools/result_envelope.py::_model_projection` intentionally drops `contractVersion`; every reader recovering the handle (snapshot matching, elision-stub salvage, frontend panel recovery) must unwrap through `sparse_result_items` / the swarm panel's equivalent — gating on the v2 marker alone recovers zero agents and renders an empty panel over fully-persisted data.
Every resume operation (`continue`, `checkpoint_resume`, `answer_guidance`) also inherits the settled projection's `modifiedFiles`/`modifiedFileList` as `checkpointModifiedFiles`/`checkpointModifiedFileList`, so the resumed attempt's commit merge unions pre-gap edits with its own instead of the settled card listing only resume-window files. When a restart settled the orphaned attempt before its live file-change stamps were folded (the durable projection has no list to carry), attempt dispatch derives the turn-scoped list from the conv-isolated modifications journal — entries at or after the turn's creation across the primary and extra workspace roots.

## Task carrier

The in-memory task dictionary is a process-local execution carrier. Important
field groups are:

- identity: `id`, `convId`, `_turnId`, `_attemptId`, `_userId`;
- configuration: `config`, `model`, feature/tool flags;
- request-local orchestration: `_toolOrchestration`, `_ptc_local` (recomputed before each wire request), bounded `_toolOrchestrationDecisions`, and one shared efficiency-hint budget retaining at most four total `_programmaticAdoptionNudges` / `_toolRoundTripNudges` witnesses with a 24-completed-round cooldown;
- cumulative projection: `content`, `thinking`, `segments`, `toolRounds`, `programRuns`; `modifiedFiles`/`modifiedFileList` are live-stamped from the modifications journal at record time (listener fold seeded from `_checkpointModifiedFileList`, terminal tasks refused) while settlement rebuilds the authoritative dedup;
- lifecycle: `status`, `finishReason`, `error`, `aborted`;
- accounting: `usage`, `apiRounds`, cost/fallback metadata;
- event delivery: `events`, sequence/cursor locks, and the launch-derived `TaskRuntime` terminal-record/event-count/serialized-byte policy. Probe failure retains 64 records per kind, 1,024 events and a 2 MiB ordinary tail/4 MiB complete event per task; the 8 GiB reference uses 128/2,048/4/8 and distributed 512/4,096/8/16, with hard caps. Explicit constructors only lower those ceilings. Byte/count pressure drops the oldest contiguous suffix and reports a cursor reset; one valid event may occupy the window alone, while a larger/unencodable event advances the absolute cursor and resets only reconstructible memory replay. Terminal chat dictionaries use a separate launch-derived 600..1,800-second personal TTL (600 seconds on the 8 GiB/probe-failure profiles, 3,600 distributed, 60..86,400 explicit bounds); active tasks are never TTL-evicted. `retention_stats` and Prometheus expose occupancy and every ceiling;
- cooperative controls: abort event, interaction requests, inbox injections.

Successful rounds build Request Inspector snapshots from canonical request-body messages, avoiding a second full-history sanitizer; body-build failures retain a separately sanitized diagnostic snapshot and re-raise the original typed error.
Private provider replay sidecars are excluded while the public event stays full in live memory. Its bounded storage-only v2 delta shares one message baseline per `(task, turn)` across request/state;
server rebuild preserves frozen v1 rows and returns full payloads to consumers.
Private fields need explicit owners/lifecycles. Provider-attempt `_wire_*` graphs remain only on the raw usage mapping through FloorRetry/cache accounting and in cache tracking's one previous-round baseline; retained `apiRounds` and `round_usage` events remove them plus the separately recorded nested billing carrier, preserving only a bounded static-prefix experiment join. When authoritative wire capture is unavailable, cache tracking builds one per-field fallback baseline through the larger prior/current immutable boundary, using slices for same-range comparison and next-round state instead of aggregate-plus-field rescans. Each message row is a fixed seven-slot tuple containing only process-local integer fingerprints/absence markers, so retained fallback state grows with message count rather than payload bytes. Stable tool catalogs reuse prior aggregate/per-tool source hashes when the validated final provider-bound tools-region digest matches; first use, digest change, or missing/malformed evidence reserializes schemas. The tool-result reuse FIFO is launch-probed at 64..256 personal entries (128 on the 8 GiB reference), falls back to 64, uses 512 distributed and a 1,024 hard ceiling; eviction safely re-executes, while terminal settlement releases the entire cache with `messages` and `_flow_turns` before the remaining hot TaskRuntime TTL. Tool settlement bodies are invocation-local; the separately bounded call-ID ledger stores only signature/name/status, never replay content, and both it and any live legacy settlement map join terminal heavy-field release. A running conversation attempt retains one last-applied public Turn projection/revision under a task-local lock so structural events send patches without repeated full reads; stale bases rebase once, and coalesced progress keeps a lightweight attempt fence. Once its terminal Turn and result metadata settle, the carrier releases that baseline with reconstructible `toolRounds`, `segments`, `programRuns`, and `_checkpointToolRounds`; inline/headless tasks retain their only structural copy. Commit admission snapshots its one tool-round-derived opaque-writer bit before release, and post-done preference CAS merges provenance only, so neither observer retains nor refolds the structural graph. Owner-scoped cold chat detail/replay reads `task_results` plus durable event bounds after hot eviction; sparse stored `seq` values remain authoritative, intermediate pages use a compact task-result projection, and cumulative content/thinking crosses the Sidecar boundary only on the caught-up terminal page. `run_command` caps settled and live reconnect output at 100,000 characters (prefix, tail, count, marker).
Completed rounds drop that buffer before durable projection; swarm cleanup is session-scoped, and durable behavior may not use unbounded globals. A swarm transcript directory is created only when its first real stream chunk arrives. After rehydration, one cancellable startup worker inspects at most `clamp(sessionCapacity*1024, 512, 16384)` immediate entries and atomically removes only empty directories; files, nested content, symlinks, and concurrent writers are preserved, and shutdown bounded-joins the worker. Startup recovery default-denies legacy swarm sessions without a positive owner and durably quarantines them without deleting child evidence, so their checkpoints are not repeatedly decoded or retried on later boots.

Incremental-translation accumulators and preview buffers use probed finite budgets: personal mode starts at 32 preview calls/Turn and 30-second deadlines (distributed 256/60 seconds), skips narration previews below 256 characters, and spends at most one upstream 429 response before yielding every later reconstructible preview in that Turn. The three-field task-local admission state survives five-minute idle accumulator/thread retirement without retaining the full task; admission skips worker recreation after the count/circuit closes. Finalize/stamp clears that state while preserving terminal reasoning with the ordinary translation retry budget and evicting reconstructible previews; cancel clears all previews, and final delivery stays outside every preview allowance.

Optional whole-output background translation and attended send-input translation enter one process-wide resource-probed lane: finite pending work is owner-round-robin, workers are lazy/idle-retiring, and durable tasks remain `pending` until entry. Send-input work may advance only within its own owner's queue; its request thread emits heartbeat status while waiting, queued timeout removes admission, and running timeout propagates cancellation into provider dispatch. Queue saturation sends the original input with typed `server_busy`; durable queued cancel and admission/thread failure remain typed/retryable. The same worker value caps actual MT/LLM calls from synchronous/incremental carriers, while the queue value caps provider waiters; waiting is cancellable, saturation never locally redispatches, and optional provider calls carry finite actual-429 attempts while attended Agent dispatch retains its default.
Background swarm sessions retain bounded dependency schedulers, agent results, and retries, while every actual SubAgent run crosses one process-wide owner-round-robin gate. Launch-probed ceilings independently bound live sessions, per-session threads, agents per wave/session, and retries; a new session may retire terminal memory backed by durable truth but never evicts productive work. One `await_agents` call waits at most 60 seconds. A repeated logically identical all/any wait that previously timed out and has observed no new terminal agent returns its no-progress receipt immediately; any completion delta rearms one real wait.
No-ID waits are consumptive: a result returned by await/get-result, injected from the background inbox, or restored with durable `delivered` evidence enters a session-local ledger bounded by completed result IDs and cannot satisfy later `mode=any` calls again. Explicit IDs and `get_agent_result` remain replayable for deliberate rereads.

Swarm-panel tool timelines share one presentation budget: the newest 30 rows, 2,000 characters per detail, and 32 KiB of conservative JSON per agent. New durable snapshots apply it at write time.
Push-owned live Turns do not issue fallback status reads. Detached/reloaded
active rounds reconcile only while visible and back off unchanged status from
20 to 120 seconds; ambiguous recovery observations keep the fast honesty gate,
and terminal state remains bounded by the 120-second ceiling.
The generated browser's v3 reference view applies the same budget request-locally to historical terminal Turns, inspecting only the retained tail; row/detail omissions remain explicit.
Durable Swarm snapshots, child transcripts, full agent results, active Turns, independent `full` responses, and recovery/provider evidence are never rewritten or discarded by this read projection.

## Persistence discipline

Outside an active provider dispatch, `append_event` persists before visibility and may carry the exact task event plus a revision patch in the same Sidecar transaction; the event envelope never repeats the cumulative projection. An oversized carried frame — the Sidecar payload cap or the 64 MiB wire frame — retries once with cumulative text and opens a 30-second full-projection probe circuit, keeping raw-event durability exact.
Writes without that carrier fail closed; a later full probe or terminal settlement converges the Turn document. Terminal events may also settle slim, and a toolRounds lane crossing its frame budget (`TOFU_TOOL_ROUNDS_FRAME_BUDGET_BYTES`, default 16 MiB) elides the oldest settled rounds' free-text payloads with replay identity intact. Projection normalization copies an explicit L1/frame placeholder into only its uniquely identity-compatible segment mirror, so a stale render copy cannot restore old bytes; ambiguity leaves evidence untouched. A `spawn_agents` round without a durable snapshot first donates its handle to a minimal roster snapshot (status `unknown`, version 0), so the reloaded swarm panel still expands to an honest roster instead of a dead header.
If even the text-only frame is rejected the worker is cooperatively aborted (`storage_frame_overflow`) instead of burning tokens on unpersistable events. A non-retryable `database_integrity` rejection at the same conversation-authority boundary likewise raises to the caller and stamps `storage_authority_integrity`, stopping provider/tool work at the next existing abort gate; it is never retried as if transient storage pressure could repair corrupt durable state.

Pure text progress may be coalesced. Structural events, interaction requests,
and terminal events persist immediately. The next structural/terminal write
carries cumulative text, so coalescing changes cadence, not final state.

Executor diagnostics use `tofu.task-results.checkpoint.guard/v1`: task birth retains the legacy parent/record preflights and caches the returned version only after an exact Sidecar echo; confirmed checkpoints become one command instead of two queries plus one command, while an old peer retains that safe three-RPC path.
Warm rounds stage at most two positive integer cache facts; an independent cache-settings-v1 echo folds them into that command, while an old peer retains the serialized per-fact settings fallback.
The guarded transaction takes the conversation delete/purge lock then the task-result key lock and enforces parent/task ownership, terminal regression, abort tombstones, and CAS. `False` is reserved for a proven fence; admission or CAS pressure raises instead of suppressing later durable work.
Reconstructible running checkpoints get one 500 ms maintenance-lane admission; task birth and terminal diagnostics retain user priority and five bounded attempts. Turn settlement remains render authority and commits before terminal diagnostics, so shedding running recovery state cannot lose a settled answer. A turn-native task therefore keeps content/thinking diagnostics in `task_results` but does not rewrite its growing segments/tool-result timeline there; the atomic Turn projection plus replay log already own it, while inline/headless tasks retain their only structural copy. Durable turn-source maintenance uses one exact read-pool expiry/list probe and enters the queue-repair writer only when a lease is actually expired; startup force recovery and old Sidecars retain the repair command.

Provider text ingress emits immediately and merges at most 100 ms or 256
characters per sequenced event; structural boundaries flush the memory replay.
While dispatch is active, Sidecar, push/webhooks, presence and DB abort probes
cannot run synchronously on upstream ingress. Task-memory SSE remains live;
the first post-provider event restores cumulative durable-before-visible state
and settles one sampled checkpoint. Its content-free `observerIsolation`
receipt exposes bounded counts. A process/host crash can lose this memory-only
window; provider return/failure or explicit abort converges normally. One
bounded coalescer worker exits at the provider boundary; resource tests live in
`test_stream_delta_coalescing.py` and `test_provider_ingress_isolation.py`.

Mid-turn injections (peer messages, operator steer, detached `run_command` completions) follow one dual-write delivery contract: the durable `message_queue` row is the delivery authority, while the conversation-keyed `agent_inbox` item is a volatile fast-path twin tagged with that row's `queueId`. A live turn drains the twin at its next round boundary and stashes it under `_peer_inject_pending` / `_steer_inject_pending` / `_bgcmd_inject_pending`; only after the LLM call returns does the deferred flush emit the arrival chip, accumulate the display-only `_peerInjects` / `_userSteerInjects` / `_bgCommandInjects` sidecar (never into `toolRounds`), and delete the durable row — chip-shown ⟺ model-consumed ⟺ durable-deleted is one atomic step. The forward race (inbox first) closes by that row deletion, the reverse race (turn ends first) closes when `dispatch_next_queued` pops the row and drops the twin by `queueId`. An abort before the flush leaves the row untouched, and `redispatch_orphaned_queue_on_startup` re-dispatches it after a restart, so delivery is exactly-once across crashes without persisting the inbox itself. Human steer carries no durable row of its own; finalize salvages undelivered steer back to the queue. Specs live in `test_background_command_injection_lane.py` and `test_peer_message_driver_loop.py`.

Terminal processing has one direction: stamp the task terminal once; settle its
private `ExecutionSession` resource stack; emit the typed terminal event; commit
turn settlement; persist bounded executor diagnostics; release heavy in-memory
fields; then notify projections and dispatch any durable queued successor. The
session is not a second task state: `TaskRuntime.status` remains authoritative.
It only proves that exact admission leases, request routes, billing holds, and
other live resources were released or handed to a declared durable/TTL recovery
owner. A non-recoverable cleanup failure converts a proposed success to a typed
terminal error before persistence/push. `TaskRuntime` raises a private
terminalization fence before running cleanup callbacks: a racing Stop cannot
rewrite completed work, a second terminalizer cannot publish another verdict,
and a session that already failed cannot later be projected as task success.
Because chat task acceptance is durable-at-birth, every pre-worker billing, admission, binding, and spawn rejection settles the session, writes a typed terminal task row, and only then unregisters it. A failed terminal write leaves an eviction-fenced, lightweight durability debt; bounded maintenance retries it before TTL, capacity, or memory-pressure cleanup may remove the task, so no accepted task can be stranded durably at `pending`/`running` merely because its worker never started.

The normal root-chat path uses the same immutable terminal stamp as Flow/error/queued-abort paths. A configured model fallback and its pool rescue each use a finite actual-429 budget (default 3, hard ceiling 16); after all recovery fails, every exception shape returns a normal loop break carrying typed `task.error` plus `autoRetryExhausted`, so finalization emits `done(error)` and does not reset the bound through whole-turn replay. `finished_at` is the TTL clock and cannot be
omitted or rewritten; cleanup never measures a successful long task from its
creation time after it finishes.

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

## Autonomous and collaboration drivers

Goal/Autopilot and Plan mode have distinct durable, model-routing, authority,
and terminal contracts. Their detailed lifecycle is specified in
[`../TASK_EXECUTION_MODES.md`](../TASK_EXECUTION_MODES.md).

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
- Never turn a provider/tool error into `done/stop`; an exhausted model fallback settles as `done/error` so browser, replay, and persistence consume the same terminal event.
- Never infer terminal retry policy from legacy text; only the typed result
  contract feeds the terminal tool-failure breaker.
- Recovery crosses the stream-handler API as `RecoveryDecision`; the root loop
  never branches on an open-ended dict.
- `TurnVerdict` derives durable completion fail-closed; missing evidence fails.
- Never push a frame whose authoritative write failed.
- Never retry a CAS conflict by rewriting a conversation-sized snapshot.
- Never bind after execution starts or report bound/queued work as running before physical worker entry.
- Never release an anonymous/LIFO admission slot from a production path; carry
  the exact lease token from acquisition through terminal settlement.
- Fence stale attempts cooperatively; never let them emit indefinitely.
- A safety-cap stop is incomplete, never verified completion.

## How to change it

Start with pure policy, event shape, command service, Sidecar contract, then browser projection. Source scans ratchet ownership; behavior needs execution.

Relevant gates include:

- Turn/event settlement: `tests/test_turn_event_carried_task_event.py`,
  `tests/test_turn_trace.py`, `tests/test_tool_settle_all_lanes.py`,
  `tests/test_orchestration_chat_completion.py`.
- Drivers/failures: `tests/test_finalize_persist_before_autopilot.py`,
  `tests/test_swarm_async.py`, `tests/test_agent_terminal_failure_breaker.py`,
  `tests/test_release_heavy_task_state.py`.

See [CONVERSATION_SYNC_V3.md](../CONVERSATION_SYNC_V3.md), [TURN_SETTLEMENT.md](../TURN_SETTLEMENT.md), and [EVENTS.md](../EVENTS.md) for durable/wire contracts.
