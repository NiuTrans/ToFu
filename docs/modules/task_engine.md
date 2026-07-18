# Module Design Doc — Unit 1: Task Engine (`lib/tasks_pkg/`)

> Part of the per-module design-doc set (see `docs/ARCHITECTURE.md` for the
> 8-layer panorama). This unit is Layer ② (Core Engine) of that map: the
> ReAct/tool-calling loop, task lifecycle, context compaction, tool dispatch,
> and the three autonomous drivers (endpoint / autopilot / killed-recovery).
>
> **Grounding:** every line count below is `wc -l` on disk as of 2026-07-11,
> read directly — NOT copied from `docs/refactor_decomposition_proposal.md`,
> which is badly stale (it lists `manager.py` at 1848 and `orchestrator.py`
> at 1899; the real figures are 3236 and 2726). Do not trust that proposal's
> numbers; trust this file.
>
> **⚠️ `list_dir` line counts are wrong for this package.** The IDE `list_dir`
> tool reports inflated counts (it showed `manager.py`=5282, `orchestrator.py`=4716,
> `autopilot.py`=4124). Those are logical/wrapped-display lines, not newlines.
> `wc -l` and `read_files` (which numbers every physical line) agree with each
> other and are authoritative. All numbers here are `wc -l`.

---

## 1. Responsibility

`lib/tasks_pkg/` owns the **execution of a single agent turn** end-to-end:

1. Take an assembled `messages` list + a `cfg` dict → create a task.
2. Run the ReAct loop: LLM stream → parse tool calls → execute tools →
   feed results back → repeat until the model stops or errors.
3. Emit SSE events for the frontend; persist a durable event log + a
   result checkpoint; sync the finished turn back into the conversation.
4. Keep the context within the model's window (compaction).
5. Provide three **autonomous drivers** layered on top of the base loop:
   endpoint mode (Planner→Worker→Critic), autopilot (virtual-user
   self-drive), and killed-recovery (auto-resume OS-killed turns).

It does NOT own: message assembly from the DB conversation store *before* a
task (that's `conv_message_builder` here at the boundary, fed by
`routes/chat.py`), LLM transport (`lib/llm/`), model routing (`lib/llm_dispatch/`),
or the tool *implementations* (`lib/tools/`, `lib/project_mod/`, `lib/search/`…).
Tools are *dispatched* here; they're *defined* elsewhere.

**Package size:** 40 top-level modules + 2 sub-packages
(`compaction/` 16 files, `handlers/` 10 files) = **37,672 lines** total.

---

## 2. The central data structure: the `task` dict

Everything in this package operates on one mutable `dict` created by
`manager.create_task()` and passed by reference through the whole loop. It is
the single shared-state object. Key fields (not exhaustive — the dict is
open and modules stamp their own private `_`-prefixed keys):

| Field | Written by | Meaning |
|---|---|---|
| `id`, `convId`, `status` | manager | identity + lifecycle (`running`/`done`/`error`/`aborted`) |
| `config` (`cfg`) | caller | model, tools, feature toggles, project paths |
| `messages` | orchestrator | the live API message list (mutated every round) |
| `content`, `thinking` | orchestrator | terminal deliverable + reasoning (per-round accumulators) |
| `toolRounds` | tool_dispatch / executor | ordered per-tool-call round entries |
| `segments` | segments.py | the NEW ordered typed-segment SoT (ships dark; see §5) |
| `events`, `events_lock` | manager | in-memory SSE event buffer + lock |
| `aborted` | manager (abort path) | cooperative-stop flag polled everywhere |
| `error` | llm_fallback / stream_handler / orchestrator | typed error envelope |
| `usage`, `api_rounds` | llm_fallback | token accounting per round |
| `_endpoint_managed`, `_vu_subtask`, `_killed_recovery` | drivers | provenance markers gating cross-driver behaviour |

> **Design consequence:** because state is one shared dict rather than typed
> objects, module boundaries are enforced by *convention* (which keys a module
> reads/writes), not by types. This is the root of why the giant modules are
> hard to split — see §7.

---

## 3. Module inventory (real `wc -l`, with a size verdict)

Verdict legend: **OK** = correctly bounded; **BIG** = large but cohesive, a
split is optional; **MISCUT** = doing 2+ unrelated jobs, should be split.

Status legend: **HOT** = on the per-request/per-round hot path (many carry a
`# HOT_PATH` header); **live** = used in production, not hot; **leaf** =
small self-contained utility.

### 3.1 The base loop + lifecycle

| Module | LOC | Size verdict | Status | Tests |
|---|--:|---|---|---|
| `manager.py` | 3236 | **MISCUT** | HOT | `test_chat_manager_migration`, `test_upsert_task_row_orphan_guard`, `test_terminal_cas_retry`, `test_settle_time_reconcile_dropped_task`, `test_stuck_task_reaper`, `test_release_heavy_task_state` |
| `orchestrator.py` | 2726 | **MISCUT** | HOT | `test_orchestrator_pretool_prose_discard`, `test_orchestrator_pending_swarm_seam`, `test_run_task_sync_progress`, broad e2e |
| `tool_dispatch.py` | 2005 | **BIG→MISCUT** | HOT | `test_streaming_and_prefetch`, `test_abort_dangling_tool_round`, `test_dedup_cache_budget_sync` |
| `executor.py` | 729 | OK | HOT | `test_abort_dangling_tool_round`, `test_binary_blob_text_stream_guard` |
| `executor_image.py` | 802 | **BIG** | HOT | (via image-gen e2e; no dedicated unit) |
| `streaming_tool_executor.py` | 688 | OK | HOT | `test_streaming_websearch_delegation`, `test_streaming_fetch_url_delegation` |
| `stream_handler.py` | 625 | OK | HOT | `test_stream_anomaly_retry_widening`, `test_zero_byte_round0_retry` |
| `llm_fallback.py` | 648 | **BIG** | HOT | `test_compaction_improvements`, `test_compat_narrator_fix` |
| `entry.py` | 238 | OK | live | `test_chat_stream_direct` (kernel behind headless facade) |

`manager.py` — task CRUD, in-memory registry (aliases `TaskRuntime`), the
persistent event append (`append_event`), result persistence + DB upsert,
conversation sync (`_sync_result_to_conversation` alone is ~600 lines,
1268–1885), partial checkpointing, **stale-task recovery on startup**
(`recover_stale_tasks_on_startup`, 2168–2527), boot auto-dispatch, memory-pressure
shedding, and the **stuck-task reaper**. This is at least five distinct
responsibilities in one file → **MISCUT** (see §7 for the split).

`orchestrator.py` — the `run_task` loop (1128–2651, a single ~1500-line
function) plus `_run_single_turn` (the reusable one-turn primitive the drivers
call), finish handling, sources-footer, suspicious-completion detection,
pre-tool-prose discard, and per-turn auto-retry glue. The loop body itself is
the miscut: it inlines finalization, retry, and event emission.

`tool_dispatch.py` — parse → label → approval-gate → parallel-execute → append.
Cohesive as a pipeline, but it has grown a second cluster (the `_approval_meta_*`
family + `_handle_approval`, ~1736–1948) and cache/dedup key machinery that
could be its own module.

### 3.2 Context engineering (compaction + prompt + messages)

| Module | LOC | Size verdict | Status | Tests |
|---|--:|---|---|---|
| `compaction/` (pkg, 16 files) | ~7300 | OK (already split) | HOT | `test_compaction_*` (10 suites) |
| `system_context.py` | 1088 | **BIG** | HOT | `test_cc_alignment`, `test_context_trace`, `test_compaction_improvements` |
| `system_prompt_cc.py` | 851 | OK (mostly constants) | live | `test_cc_alignment` |
| `conv_message_builder.py` | 795 | **BIG** | HOT (turn boundary) | `test_conv_message_builder`, `test_conv_message_builder_async_safety` |
| `server_message_store.py` | 446 | OK | live (opt-in `keepToolHistory`) | `test_l2_cache_roi` (indirect) |
| `message_builder.py` | 302 | OK | HOT (Continue path) | via endpoint/continue e2e |
| `attachments.py` | 213 | OK | HOT | `test_cc_alignment` |
| `model_config.py` | 272 | OK | HOT | `test_cache_schema_stability`, `test_cc_alignment` |

`compaction/` is the **proof that decomposition works here** — the old
2620-line `compaction.py` was split (2026-06) into `_layer1/_layer2/_reactive/
_pipeline/_persist/_budget/_tokens/_steps/_constants/_archive/…`. `__init__.py`
is a pure re-export facade; the public API (`run_compaction_pipeline`,
`micro_compact`, `force_compact_if_needed`, …) is unchanged. `_layer2.py` (913)
is the biggest remaining sub-file but is cohesive (the LLM summary layer).

`system_context.py` is BIG because it interleaves two jobs: (a) the
`_inject_system_contexts` orchestration, and (b) the delta-attachment
hash/skip cache. `system_prompt_cc.py` is mostly static prompt-section
constants (ported from Claude Code) — large but low-complexity, leave it.

### 3.3 Autonomous drivers

| Module | LOC | Size verdict | Status | Tests |
|---|--:|---|---|---|
| `autopilot.py` | 2768 | **MISCUT** | live | `test_autopilot_*` (~18 suites) incl. `test_autopilot_verify`, `test_autopilot_arm`, `test_autopilot_resume_after_crash` |
| `endpoint.py` | 1562 | **BIG** | live | `test_endpoint_flow_parity`, `test_endpoint_messages`, `test_endpoint_finalize_status`, `test_endpoint_poll_recovery` |
| `endpoint_prompts.py` | 593 | OK (prompt constants) | live | `test_cc_alignment` (indirect) |
| `endpoint_review.py` | 426 | OK | live | `test_endpoint_flow_parity` |
| `killed_recovery.py` | 725 | OK | live | `test_killed_recovery`, `test_killed_recovery_integration` |

`autopilot.py` — MISCUT. It bundles: objective/run-id persistence, the
virtual-user turn (`run_virtual_user`), run-summary generation +
translation, the terminal verdicts (TASK_DONE close-out + manual-stop /
safety-cap conclude), follow-up task spawning, crash-resume arm/disarm, and
the kick/dispatch entry points. The **VU decision logic itself correctly lives
elsewhere** (`lib/agent_verdict.py`, the single source of truth for
`classify_verdict`/`parse_progress`) — autopilot only routes into it.
Split candidates: run-record persistence, summary generation, and crash-resume
are three separable concerns.

`endpoint.py` — the Planner→Worker→Critic loop. Prompts (`endpoint_prompts`)
and the planner/critic turns + verdict parsing (`endpoint_review`) are already
extracted; verdict logic delegates to `agent_verdict`. What remains in
`endpoint.py` is the loop orchestration + per-turn DB sync + auto-translate
triggers. BIG but coherent; a further split is optional.

`killed_recovery.py` — well-bounded. Pure decision functions (`decide`,
`next_attempt`) are unit-tested without a DB; the dispatch/drain machinery is
separate. Good example of the target shape.

### 3.4 Persistence / event / durability

| Module | LOC | Size verdict | Status | Tests |
|---|--:|---|---|---|
| `event_log.py` | 339 | OK | HOT | `test_event_log_orphan_prune`, `test_event_persist_before_push` |
| `event_fold.py` | 107 | OK | live (cold replay) | `test_event_fold_cold_replay` |
| `segments.py` | 534 | OK | live (ships dark) | `test_segment_model`, `test_edit_realigns_segments`, `test_frontend_segment_timeline` |
| `commit_round.py` | 600 | **BIG** | live (daemon-thread) | `test_file_history_compaction` |
| `persistence_store.py` | 270 | OK | live | via store seam e2e |
| `persist_registry.py` | 187 | leaf | live | `test_compaction_improvements` |
| `auto_translate.py` | 336 | OK | live | `test_autopilot_vu_auto_translate` |
| `activity_sink.py` | 39 | leaf | live | via project-brain e2e |

`event_log.py` + `event_fold.py` are the durable-replay foundation
(`task_events` table). `segments.py` is the **strangler-fig groundwork** for
the ordered typed-segment SoT (charter epic `pt_cb8f98b0cb9b47fb`): it ships
DARK — `assemble_segments` runs alongside the three legacy channels and is
proven a lossless projection by golden test. `commit_round.py` is BIG because
`_run_commit_round_async` carries the whole file-history attribution filter;
cohesive but at the split threshold.

### 3.5 Interactive I/O (blocking waits)

| Module | LOC | Size verdict | Status | Tests |
|---|--:|---|---|---|
| `approval.py` | 41 | leaf | live | via approval e2e |
| `human_guidance.py` | 134 | leaf | live | via `ask_human` e2e |
| `stdin_handler.py` | 112 | leaf | live | via run_command stdin e2e |

Three near-identical blocking-wait primitives (event + lock + poll-for-abort).
They share a pattern but are small enough that a shared base is optional, not
required.

### 3.6 Wire / cache / retry helpers

| Module | LOC | Size verdict | Status | Tests |
|---|--:|---|---|---|
| `cache_tracking.py` | 1415 | **BIG** | HOT | `test_cache_breakpoints`, `test_cache_improvements`, `test_cache_prefix_stability`, `test_cache_schema_stability` |
| `wire_fingerprint.py` | 369 | OK | HOT | `test_wire_fingerprint` |
| `wire_messages.py` | 195 | OK | live (debug panel) | `test_wire_messages_fidelity` |
| `write_breakdown.py` | 243 | OK | HOT | via cost-panel e2e |
| `turn_retry.py` | 160 | OK (pure) | live | `test_turn_auto_retry` |

`cache_tracking.py` is BIG: prompt-cache break detection + cache-aware
micro-compact prefix gating + TTL latch + tool-result ordering + L2 ROI +
per-model concurrency counting. Several of these (L2 ROI, concurrency counting)
are diagnostics that could move out, but the core is a tight cohesive concern.

### 3.7 Tool display + hooks

| Module | LOC | Size verdict | Status | Tests |
|---|--:|---|---|---|
| `tool_display.py` | 1261 | **BIG** | HOT | via streaming/render e2e |
| `tool_hooks.py` | 269 | OK | HOT | via dispatch e2e |

`tool_display.py` is a large dispatch dict of per-tool `_tool_display_*`
formatters. BIG but the pattern is uniform (one formatter per tool family);
low complexity per unit. A split by tool family is possible but low-value.

### 3.8 `handlers/` sub-package (per-tool execution)

| Module | LOC | Size verdict | Status |
|---|--:|---|---|
| `misc.py` | 716 | **BIG** | HOT — ask_human/scheduler/desktop/swarm/conv_ref/**charter/board/peer** |
| `search.py` | 671 | OK | HOT — web_search + fetch_url (single + batch) |
| `project.py` | 476 | OK | HOT — read/write/grep/run + read-before-edit gate + artifact promotion |
| `_read_gate.py` | 352 | OK | HOT — read-before-edit policy |
| `code_exec.py` | 222 | OK | HOT |
| `_adapter.py` | 195 | OK (shared) | HOT — `simple_call` + `run_batch_concurrent` |
| `memory.py` | 147 | OK | HOT |
| `mcp.py` | 145 | OK | HOT |
| `browser.py` | 137 | OK | HOT |

`misc.py` is BIG because it hosts the **Project-Brain tool handlers**
(charter / board / peer) alongside the older misc tools. Those are functionally
a distinct family (cross-conversation coordination) and are a clean split
candidate → `handlers/coordination.py`.

---

## 4. Dependencies (in / out)

**Inbound (who calls the task engine):**
- `routes/chat.py` → `manager.create_task()` + `spawn_task()` (the primary path).
- `routes/api_v1/chat.py`, `routes/api_v1/agent_run.py`, compat surfaces,
  and the in-process `tofu.chat` facade → all converge on `entry.py`
  (`run_chat_sync`/`run_chat_stream`), the transport-agnostic kernel.
- `routes/endpoint.py` → `endpoint.run_endpoint_task`.
- Schedulers / boot → `manager.recover_stale_tasks_on_startup` →
  `killed_recovery.run_killed_recovery`.

**Outbound (what the engine depends on):**
- `lib/llm/` (stream/build_body), `lib/llm_dispatch/` (routing) — via
  `manager.stream_llm_response` + `llm_fallback`.
- `lib/agent_verdict.py` — SoT for STOP/CONTINUE verdicts
  (endpoint + autopilot both delegate here; they do NOT re-implement it).
- `lib/agent_core/` — `TaskRuntime` (backs `manager`), `events` (EventType),
  `push` (SSE fan-out), `store` (persistence seam), `personal_scope`
  (headless fail-closed in `entry`).
- `lib/tools/` + `lib/project_mod/` + `lib/search/` + `lib/browser/` +
  `lib/mcp/` — tool *implementations*, reached via `handlers/`.
- `lib/database/` — via `manager`, `event_log`, `persistence_store`,
  `auto_translate`, `killed_recovery`, `server_message_store`.
- `lib/conversations/` (Project Brain) — reached only through the
  `activity_sink` adapter and the `handlers/misc` charter/board/peer handlers
  (the CORE_MODULES boundary forbids `agent_core` from importing it directly).

**Import-cycle discipline:** `manager` is eagerly imported by the package
`__init__`; heavy modules (orchestrator/endpoint/executor/compaction) are
lazy (`_LAZY_MAP`) to save ~600ms startup. Several modules use function-body
(lazy) imports specifically to break cycles (`segments`↔`manager`,
`streaming_tool_executor`↔`tool_dispatch`, `activity_sink`→`conversations`).

---

## 5. Invariants (must not be broken by a refactor)

1. **`spawn_task` is the single task-spawn entry point** (`__init__.py`). All
   callers use it; it picks event-loop executor vs daemon thread.
2. **`task` is passed by reference**; modules communicate by stamping keys on
   it. A split module must keep reading/writing the same keys.
3. **`append_event` mirrors every event to `task_events`** (durable replay).
   The cold-replay fold (`event_fold`) must mirror the frontend's accumulation
   semantics (`delta` / `delta_reset` / `retry_reset`) EXACTLY.
4. **Cache-prefix stability is load-bearing.** `cache_tracking`,
   `server_message_store._truncate_old_tool_results`, and
   `attachments.inject_attachments` all avoid mutating bytes inside the cached
   prefix. This is subtle and heavily commented — a naive edit re-bills the
   whole context uncached every round. (See the extensive comments + the
   `test_cache_prefix_stability` suite.)
5. **`_run_single_turn` is the shared one-turn primitive.** endpoint's
   planner/worker/critic and autopilot's VU all drive through it — its
   signature/behaviour is a contract for the drivers.
6. **Verdict logic is centralized in `lib/agent_verdict.py`.** endpoint +
   autopilot must NOT fork it (it was hand-copied 4× before centralization).
   The `parse_progress` remaining>0 cross-check is backend-authoritative there.
7. **`segments` is a strangler-fig SoT that ships dark.** The three legacy
   channels (`content`/`thinking`/`toolRounds`) remain DERIVED projections
   proven byte-identical by golden test; do not retire them until each of the
   ~40 measured readers is migrated.
8. **Loop-protection caps are hyperparameters (CLAUDE.md §10).** The retry caps
   (16× stream anomaly, 2× empty-stop, 3× auto-turn-retry, 3× killed-recovery
   attempts, 2× reactive-compact) require sign-off to change; a split must
   preserve them verbatim.
9. **`max_tool_rounds` is intentionally unbounded** (`model_config`, explicit
   Chinese comment forbidding a cap). Do not add a tool-round limit.

---

## 6. Known debt (grounded, not speculative)

- **`manager.py` mixes 5 concerns** (CRUD / event log / persistence+sync /
  startup recovery / reaper) — the single biggest file in the package.
- **`orchestrator.run_task` is one ~1500-line function** (1128–2651). Testable
  seams (`_run_single_turn`, `stream_handler`, `llm_fallback`,
  `commit_round`, `write_breakdown`, `attachments`, `model_config`) have
  already been extracted *around* it, but the loop body itself is monolithic.
- **`autopilot.py` bundles 6 concerns** (see §3.3).
- **`handlers/misc.py` hosts the Project-Brain coordination handlers** mixed
  with legacy misc tools.
- **Three duplicated blocking-wait primitives** (`approval` / `human_guidance`
  / `stdin_handler`) — low priority.
- **The stale `docs/refactor_decomposition_proposal.md`** cites obsolete LOC and
  a `compaction.py` single-file split that is already done. This doc supersedes
  its `tasks_pkg` rows (see §8).

---

## 7. Why the giants are hard to split (and the safe way to do it)

The blocker is the shared `task` dict (§2). Any split of `manager` /
`orchestrator` / `autopilot` moves code that reads and writes dozens of
`task[...]` keys, so the "interface" between the new modules is the untyped
dict — easy to get subtly wrong (e.g. an accumulator reset ordering, a
cache-prefix mutation).

The **proven safe pattern in this very package** is the `compaction/` split:
extract a *cohesive sub-concern* into its own module, keep a re-export facade
in the original module so no caller changes, and gate the split behind the
existing test suite. `commit_round`, `stream_handler`, `llm_fallback`,
`write_breakdown`, `attachments`, `model_config`, `endpoint_prompts`,
`endpoint_review`, and `segments` were all extracted from
`orchestrator`/`endpoint`/`manager` exactly this way. The remaining giants are
the cores that were left behind after those extractions.

---

## 8. Segmentation verdict (supersedes `refactor_decomposition_proposal.md`)

**Correctly bounded — leave as-is:**
`executor`, `streaming_tool_executor`, `stream_handler`, `entry`, `event_log`,
`event_fold`, `segments`, `killed_recovery`, `endpoint_review`,
`endpoint_prompts`, `model_config`, `message_builder`, `attachments`,
`turn_retry`, `wire_fingerprint`, `wire_messages`, `write_breakdown`,
`persistence_store`, `persist_registry`, `activity_sink`, `approval`,
`human_guidance`, `stdin_handler`, `tool_hooks`, `server_message_store`,
`auto_translate`, `compaction/` (already correctly a package), and all of
`handlers/` except `misc.py`.

**Miscut — should be split (priority order):**

1. **`manager.py` (3236) → package `manager/`.** Highest value. Split by the
   5 concerns it already has clean internal boundaries for:
   `_crud.py` (create/discard/abort/registry), `_events.py`
   (`append_event` + `stream_llm_response`), `_persistence.py`
   (`persist_task_result` + `_upsert_task_row` + `_sync_*` — the ~600-line
   conversation-sync is the bulk), `_recovery.py`
   (`recover_stale_tasks_on_startup` + boot dispatch + reaper +
   memory-shedding). Re-export facade in `manager/__init__.py`; the 47
   `tasks`/`tasks_lock` import sites stay valid. RISK: hot path + the
   cache-prefix and CAS-retry invariants live here → do it behind
   `test_chat_manager_migration` + `test_terminal_cas_retry` +
   `test_settle_time_reconcile_dropped_task`.

2. **`orchestrator.py` (2726).** Extract `_finalize_and_emit_done` (460–1127,
   ~660 lines) into `orchestrator_finalize.py` and the suspicious-completion /
   sources-footer / dangling-round helpers into `orchestrator_finish.py`. The
   `run_task` loop body stays, but shrinks to control flow. RISK: highest
   (deepest hot path); owner-in-the-loop, Stop-actually-stops test first.

3. **`autopilot.py` (2768) → split off 3 modules:**
   `autopilot_runrecord.py` (`_store_run_record` + `_store_run_summary` +
   run-id persistence), `autopilot_summary.py` (summary generation +
   translation + reporter), `autopilot_resume.py` (arm/disarm/crash-resume).
   The VU turn + conclude + kick stay in `autopilot.py`. Verdict logic
   already correctly external (`agent_verdict`).

4. **`handlers/misc.py` (716) → extract `handlers/coordination.py`** for the
   charter / board / peer handlers (Project-Brain family). Clean seam — they
   already share only `simple_call` + the registry decorator.

**Big but optional (defer unless touched):**
`tool_dispatch` (2005 — could shed the `_approval_meta_*` cluster),
`cache_tracking` (1415 — could shed L2-ROI + concurrency diagnostics),
`tool_display` (1261 — split by tool family is low-value),
`system_context` (1088 — separate the delta-attachment cache),
`conv_message_builder` (795), `executor_image` (802), `endpoint` (1562),
`llm_fallback` (648), `commit_round` (600).

**Do NOT split:** `system_prompt_cc` (851, mostly constants),
`endpoint_prompts` (593, mostly constants), `compaction/_layer2` (913,
cohesive).

---

*Next unit to document: Unit 2 (LLM I/O — `llm/`, `llm_dispatch/`,
`model_info`, `context_limits`, `llm_sanitize`).*
