# Module Design Doc — Unit 4: Orchestration / DAG (`orchestration*.py`, `swarm/`)

> Part of the per-module design-doc set (see `docs/ARCHITECTURE.md`). This unit
> covers the two multi-agent execution systems: the top-level
> `orchestration*.py` graph layer and the `lib/swarm/` async fan-out system.
>
> **Grounding:** every line count is `wc -l` on disk 2026-07-11. `list_dir`
> overcounts — all numbers are `wc -l`. Every MISCUT/BIG verdict cites competing
> responsibilities or line ranges; size alone is never the argument.

---

## 1. The analytical payload: duplication or layering? (and are both alive?)

The unit's central question: `orchestration*.py` and `swarm/` both "run multiple
agents" — is that a **genuine duplication** (two engines solving the same problem
that should be unified) or **correct layering** (one composes the other)?

**Verdict: it is CORRECT LAYERING — `orchestration` COMPOSES `swarm`, one
direction, no duplication of task-graph execution. Both are alive.** This is the
`compaction/`-clean outcome at the subsystem scale, not a `tool_env`-style defect.
Evidence, traced from every cross-system import edge:

### 1a. The edges are single-directional (orchestration → swarm)

- **`orchestration_engine.py:1343-1344`** — `FlowExecutor._default_runner` does
  `from lib.swarm.agent import SubAgent` + `from lib.swarm.protocol import
  SubTaskSpec`. The graph engine's default node-runner *builds a swarm SubAgent
  and runs it*. The engine's own docstring says so: "Default runner builds a
  `SubTaskSpec` + `SubAgent` from the swarm substrate (`lib/swarm`) … same agents
  the swarm uses, with the same role→tool scoping + model tiers."
- **`orchestration.py:452`** — imports `AGENT_ROLES` from `swarm.registry` (the
  role catalogue is shared, not re-declared).
- **`swarm/` → `orchestration`: ZERO code edges.** The only match is a *doc
  comment* at `swarm/agent.py:119` ("the caller (the orchestration engine) stream
  this sub-agent's output live"). The `stream_sink` seam it describes is a
  generic callback — swarm has no import of, or dependency on, orchestration.

So the dependency graph is strictly `orchestration → swarm`. The graph engine is
the higher layer; swarm is the reusable agent-execution substrate it drives.

### 1b. They do NOT duplicate task-graph execution — they solve DIFFERENT graph problems

This is the key distinction. Both have a "scheduler," but they schedule
different things:

| | `swarm` (`StreamingScheduler`) | `orchestration` (`FlowExecutor`) |
|---|---|---|
| Graph shape | **dependency DAG** (`depends_on` edges) | **control-flow graph** (start/role/parallel/barrier/loop/branch/stop) |
| Scheduling | dep-ready streaming: an agent starts the instant its `depends_on` complete | topology walk: interpret control nodes, run role nodes via a runner |
| Iteration | none (fire once, retry on fail) | **loops with verifier verdicts** (endpoint-mode-as-data) |
| Trigger | LLM tool call `spawn_agents` (fire-and-forget) | a user-authored `tofu.orchestration/v1` graph, or a canonical endpoint/autopilot graph |
| The agent | owns `SubAgent` (the actual LLM+tools worker) | **borrows** `SubAgent` as its `_default_runner` |

The engine's docstring frames it exactly: it is "the piece that finally unifies
the two hand-built orchestrators — endpoint mode (loop + verifier) and the swarm
(fan-out) — under one declarative engine." So `FlowExecutor` expresses BOTH
endpoint's loop AND swarm's fan-out **as graph data**, and delegates the leaf
agent execution DOWN to the swarm substrate. That is composition, not
duplication: the fan-out topology in a graph is interpreted by `FlowExecutor`,
but each node still runs as a swarm `SubAgent`. There is exactly ONE agent
implementation (`swarm/agent.py`), consumed by three drivers (the swarm master,
the flow engine, and — indirectly — endpoint/autopilot).

### 1c. Liveness — BOTH are on live paths, neither is dead

- **swarm is HOT and default-on.** Reached via the `spawn_agents`/`await_agents`/
  `get_agent_result` tools (Unit 3), routed through `swarm/integration.py`, driven
  by `orchestrator.py`'s between-round drain hook. `routes/api_v1/swarm.py` +
  `agents.py` expose it. Confirmed live consumers: `routes/chat.py`,
  `orchestrator`, `agent_verdict`, `conv_config`.
- **orchestration is live but its chat-mode paths are FLAG-GATED (deliberate,
  not dead).** Two distinct liveness tiers:
  1. **Always-live:** the Studio authoring surface — `orchestration.py`
     (schema+validate), `orchestration_composer.py` (NL→graph), `orchestration_runs.py`
     (durable run instances) — all reachable *today* via
     `routes/api_v1/orchestrations.py`. A user can author, validate, compose, and
     run a graph now.
  2. **Flag-gated convergence path:** `orchestration_endpoint_runner.py` routes
     endpoint/autopilot chat modes THROUGH `FlowExecutor` only when
     `TOFU_ENDPOINT_VIA_FLOW=1` / `TOFU_AUTOPILOT_VIA_FLOW=1`. Its own docstring:
     "The live `lib/tasks_pkg/endpoint.py` / `autopilot.py` paths remain the
     default + authoritative until each flagged path is validated on real tasks."
     A user-SELECTED flow is always honored (the selection is the opt-in).

**This is the most important finding of the unit:** `FlowExecutor` is NOT a dead
engine masquerading as active, but it is also NOT yet the authoritative path for
endpoint/autopilot — those still run through the hand-built Unit-1 modules
(`endpoint.py`, `autopilot.py`) by default. So there are currently **TWO live
implementations of endpoint/autopilot** (the Unit-1 hand-built loop AND the
`FlowExecutor` graph), gated by an env flag, mid-migration. That is a *transient*
duplication with an explicit strangler-fig plan, not a permanent segmentation
defect — but it IS real duplication that should not be left half-finished (§6).

---

## 2. Module inventory — `orchestration*.py` (4236 LOC, 6 files)

Verdict: **OK** / **BIG** / **MISCUT**. Status: **HOT** / **live** / **flag-gated**.

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `orchestration_engine.py` | 1593 | **BIG** | live (flag-gated for chat) | `test_orchestration_engine`, `test_orchestration_io`, `test_orchestration_emits_subflow`, `test_orchestration_nested_canvas`, `test_orchestration_phase_and_output` |
| `orchestration.py` | 1323 | **BIG** | live (always — schema) | `test_orchestrations`, `test_orchestration_role_params` |
| `orchestration_endpoint_runner.py` | 455 | OK | flag-gated | `test_orchestration_endpoint_runner` |
| `orchestration_endpoint_adapter.py` | 318 | OK | flag-gated | `test_orchestration_endpoint_adapter` |
| `orchestration_runs.py` | 300 | OK | live (durable runs) | `test_orchestrations` |
| `orchestration_composer.py` | 247 | OK | live (Studio) | `test_frontend_composer_*` |

`orchestration_engine.py` — **BIG, and it bundles 2 concerns.** (a) The graph
interpreter proper: `FlowExecutor` walking start/role/parallel/barrier/loop/
branch/stop, the concurrency (ThreadPoolExecutor for parallel branches), the
loop+verifier control flow. (b) The `_default_runner` (1340+) that adapts a graph
node into a swarm `SubTaskSpec`/`SubAgent` + the live token-streaming plumbing
(`step_delta` events). The runner is a *separable adapter* — the whole point of
the `agent_runner` injection seam is that the interpreter doesn't need to know
about swarm. Split candidate: `orchestration_engine_runner.py` (the SubAgent-backed
default runner), leaving the pure interpreter in `orchestration_engine.py`.
Verdict grounded in the docstring's own architecture note ("agent execution is
abstracted behind a single injectable `agent_runner`").

`orchestration.py` — **BIG but cohesive.** It is the schema+validator+graph-algebra
module: `validate_definition`, `expand_subflows`, `resolve_emits`/`resolve_scope`,
`layout_definition`, `render_role_brief`, the canonical `build_endpoint_definition`/
`build_autopilot_definition` graph builders, `KNOWN_ROLES`/`CONTROL_KINDS`. All one
concern (the `tofu.orchestration/v1` contract + pure operations on it). Large
because the graph algebra is genuinely rich (subflows, I/O refs, emits axis). The
builders could split to `orchestration_builtin_graphs.py` but it's low-value.
Classified BIG, defer.

The other four are correctly bounded: `composer` (NL→graph, mirrors the optimizer
proposer pattern), `runs` (durable DB-backed run instances, mirrors
`swarm/persistence.py`, best-effort never-raises), `endpoint_runner` (the chat-mode
convergence entry) + `endpoint_adapter` (FlowExecutor-event → endpoint-UI-schema
translator). The adapter is a clean stateful translator — its whole existence is
to let the graph engine drive the *existing* endpoint UI with zero frontend change.

---

## 3. Module inventory — `swarm/` (7370 LOC, 17 files)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `integration.py` | 1396 | **BIG** | HOT | `test_swarm_async`, `test_orchestrator_pending_swarm_seam`, `test_swarm_pending_tool_force` |
| `master.py` | 1278 | **BIG** | HOT | `test_swarm_async` |
| `agent.py` | 1252 | **BIG** | HOT | `test_swarm_async`, `test_swarm_tool_scoping`, `test_presence_subagent_integration` |
| `scheduler.py` | 639 | OK | HOT | `test_swarm_async` |
| `registry.py` | 519 | OK | HOT | `test_swarm_tool_scoping` |
| `tools.py` | 444 | OK | HOT | `test_swarm_tool_scoping` |
| `persistence.py` | 352 | OK | live (durable) | `test_swarm_snapshot_persist` |
| `artifact_store.py` | 300 | OK | HOT | `test_swarm_async` |
| `snapshot.py` | 288 | OK | live | `test_swarm_snapshot_persist` |
| `events.py` | 162 | OK | HOT | via swarm e2e |
| `result_format.py` | 152 | OK | HOT | via swarm e2e |
| `rate_limiter.py` | 138 | OK | HOT | `test_swarm_async` |
| `types.py` | 128 | OK | HOT | — |
| `__init__.py` | 126 | OK (facade) | — | — |
| `protocol.py` | 81 | leaf | HOT | — |
| `planner.py` | 73 | leaf | HOT | — |
| `messages.py` | 42 | leaf | HOT | — |

`agent.py` — **BIG but one cohesive concern:** `SubAgent` is the actual multi-round
LLM+tool worker (build_body → dispatch_stream → parse tools → execute → repeat),
with DI seams for `build_body`/`dispatch_stream`/`stream_sink`. It is essentially a
*second, self-contained ReAct loop* parallel to `orchestrator.run_task` — but
scoped to a sub-agent (no user interaction, denylisted tools). That it duplicates
the *loop shape* of `orchestrator` is a known architectural fact (the shared
`lib/agent_loop.py` was created to eventually unify them — see CLAUDE.md §1); for
now `agent.py` is cohesive-and-BIG, not miscut. Defer.

`master.py` — **BIG, bundles 3 concerns:** (a) `MasterOrchestrator` lifecycle
(run_in_background daemon thread, abort), (b) the await/get-result inbox
integration, (c) result plumbing. The scheduler was already extracted (`scheduler.py`).
Split candidate but shared state makes it BIG-defer.

`integration.py` — **BIG, and it is the closest to miscut:** it routes the swarm
tools (`spawn_agents`/`await_agents`/`get_agent_result`/artifact tools) AND owns
session bookkeeping (TTL eviction, concurrent-session ceiling) AND
`rehydrate_swarms_on_startup` (crash recovery) AND `has_live_or_pending_swarm`. The
session-registry concern (bookkeeping + rehydrate) is separable from the
tool-routing concern. Split candidate: `swarm/session_registry.py`.

`scheduler.py` — OK, and a *reference-quality* extraction: it was pulled out of
`master.py` (docstring says so) and holds `StreamingScheduler` (the dep-DAG
streaming executor, with the carefully-commented TOCTOU-safe queue/lock discipline)
+ its `AsyncStreamingScheduler` asyncio wrapper. One concern, heavily tested.

The 10 small modules (`registry`, `tools`, `persistence`, `artifact_store`,
`snapshot`, `events`, `result_format`, `rate_limiter`, `types`, `protocol`,
`planner`, `messages`) are all well-bounded single-concern files — swarm is a
*better-decomposed* package than `tasks_pkg`, with clean protocol/registry/tools/
persistence separation.

---

## 4. Dependencies (in / out)

**orchestration inbound:** `routes/api_v1/orchestrations.py` (CRUD + run the
Studio graphs), `routes/chat.py` (via `resolve_chat_flow_entry` — the flag-gated
chat convergence). Internal: `endpoint_runner` → `engine` + `adapter` + the LIVE
`tasks_pkg.endpoint` DB-sync functions (reuses endpoint persistence verbatim for
reload parity).

**swarm inbound:** the `spawn_agents` tool handler (`tasks_pkg/handlers/misc.py` →
`swarm/integration.execute_swarm_tool`), `orchestrator.py` (between-round inbox
drain), startup rehydrate. `routes/api_v1/swarm.py` + `agents.py`.

**The composition edge:** `orchestration_engine._default_runner` →
`swarm.SubAgent` + `swarm.SubTaskSpec` (lazy import at 1343). `orchestration.py` →
`swarm.registry.AGENT_ROLES` (shared role catalogue).

**Shared substrate both use:** `lib/agent_verdict.py` (verdict classification —
`FlowExecutor._classify_verdict` and endpoint both delegate here, NO engine-local
copy — the docstring explicitly says "there is no longer an engine-local copy to
drift"); `lib/agent_loop.py` (the abort seam / round loop that `SubAgent` and the
paper engines share); `TaskRuntime` events; `lib/agent_inbox` (swarm-update queue).

**No back-edges:** swarm does not import orchestration; neither imports up into
`routes`; `orchestration_runs`/`swarm/persistence` reach DOWN into `lib/database`
only (best-effort, never-raise).

---

## 5. Invariants (must not be broken by a refactor)

1. **The `agent_runner` injection seam is load-bearing.** `FlowExecutor` takes
   `agent_runner(node, context, iteration)`; the default builds a swarm SubAgent,
   tests inject a mock. The interpreter's control-flow logic is fully covered in
   CI with NO LLM call *because* of this seam — do not inline the swarm runner.
2. **Verdict logic is centralized in `agent_verdict`** (shared with Units 1/8).
   `FlowExecutor._classify_verdict`/`_detect_stuck` delegate to the core; the
   endpoint-local copy was removed to stop drift. Do not fork it.
3. **Every loop has a hard `max_iterations` + total `max_agents` cap.** A
   malformed graph can never spin forever. §10 hyperparameters.
4. **swarm `StreamingScheduler` queue/lock discipline is TOCTOU-critical.**
   `_results_queue.put()` happens INSIDE `_lock` in `_run_one`; `iter_completions`
   drains + idle-checks atomically under the same lock. A naive refactor
   reintroduces the "result slips through between drain and idle-check" race
   (heavily commented — respect it).
5. **Sub-agents cannot spawn/await/get_result/ask_human** (`SUB_AGENT_DENYLIST`).
   No recursive swarms, no sub-agent user interaction.
6. **The FlowExecutor→endpoint-UI adapter must preserve the exact message schema
   AND SSE sequence** (`_isEndpointPlanner`/`_epIteration`/`endpoint_iteration`/
   `endpoint_critic_msg`) so the existing frontend renders flag-path runs
   unchanged. The `emits` axis (user|assistant) is orthogonal to role.
7. **Flag defaults are OFF and symmetric** (`TOFU_ENDPOINT_VIA_FLOW`/
   `TOFU_AUTOPILOT_VIA_FLOW`). The live `tasks_pkg` paths stay authoritative until
   validated; single-box behaviour is byte-identical with flags off.
8. **`orchestration_runs`/`swarm persistence` are best-effort, never raise into a
   running flow** — durability is a safety net, not a critical path.

---

## 6. Known debt (grounded)

- **The transient endpoint/autopilot duplication is the unit's real debt** (§1c):
  two live implementations (Unit-1 hand-built `endpoint.py`/`autopilot.py` AND the
  `FlowExecutor` graph path), gated by `TOFU_*_VIA_FLOW`. This is a deliberate
  strangler-fig migration, but it is unfinished — until the flag paths are
  validated and made default, the codebase maintains two engines for the same
  modes. The finish line (per the engine's own docstring) is "unify the two
  hand-built orchestrators under one declarative engine." **This should be
  flagged as loudly as any structural miscut** — not because the layering is
  wrong, but because a half-finished migration is its own liability.
- **`agent.py` (1252) is a second ReAct loop** parallel to `orchestrator.run_task`;
  `lib/agent_loop.py` exists to eventually unify them but the migration is partial
  (only the paper engines adopted it — CLAUDE.md §1).
- **`orchestration_engine.py` bundles interpreter + SubAgent runner** (§2).
- **`swarm/integration.py` bundles tool-routing + session-registry** (§3).
- Both `master.py` and `integration.py` are >1200 lines with separable concerns.

---

## 7. Segmentation verdict (this unit)

**Correctly bounded — leave as-is:**
`orchestration_composer`, `orchestration_runs`, `orchestration_endpoint_adapter`,
`orchestration_endpoint_runner`; and in swarm: `scheduler` (a reference extraction),
`registry`, `tools`, `persistence`, `artifact_store`, `snapshot`, `events`,
`result_format`, `rate_limiter`, `types`, `protocol`, `planner`, `messages`. The
whole `swarm/` package is *better* decomposed than `tasks_pkg`.

**Miscut — should split (priority order):**

1. **`orchestration_engine.py` (1593) → extract the SubAgent runner.** Pull
   `_default_runner` + its swarm-adapter/streaming plumbing (~1340+) into
   `orchestration_engine_runner.py`, leaving the pure graph interpreter. The
   `agent_runner` injection seam already defines the boundary; this makes the
   physical split match the logical one. Behind `test_orchestration_engine`
   (mock-runner tests already isolate the interpreter).
2. **`swarm/integration.py` (1396) → extract `swarm/session_registry.py`** for
   session bookkeeping (TTL eviction, ceiling, `rehydrate_swarms_on_startup`,
   `has_live_or_pending_swarm`), leaving tool-routing in `integration.py`. Behind
   `test_swarm_async` + `test_swarm_snapshot_persist`.

**Big but optional (defer unless touched):**
`orchestration.py` (1323 — the graph builders could split), `swarm/master.py`
(1278), `swarm/agent.py` (1252 — the deeper `agent_loop` unification is a separate
program).

**Do NOT split:** the small swarm modules, `orchestration_endpoint_adapter`
(cohesive translator).

**NOT a segmentation fix — a migration to FINISH (§6):** resolve the
endpoint/autopilot dual-implementation. Either complete the `FlowExecutor`
convergence (validate the flag paths, make them default, retire the hand-built
loops) or explicitly decide the hand-built paths stay and the flag paths are a
research branch. Leaving it half-flagged indefinitely is the liability.

---

## 8. Comparison to Units 1–3 (the running thesis)

- **This is the first unit where two subsystems genuinely overlap — and the
  overlap is correct layering, not duplication.** `orchestration` composes
  `swarm` via a clean injection seam; there is ONE agent implementation, three
  consumers. That's the subsystem-scale version of the `compaction/` clean split.
- **But it also surfaced the first LIVE-DUPLICATION finding** (§1c/§6): a
  half-finished strangler-fig migration means endpoint/autopilot exist twice
  (hand-built + graph), env-flag-gated. Distinct from the `tool_env` misplacement
  (Unit 3) and the `manager.py`/`api.py` miscuts (Units 1–2): those are
  *structural* defects in one file; this is a *process* defect (an unfinished
  migration) spanning two subsystems. The refactor plan must treat it as
  "finish the migration," not "split a file."
- **swarm is the best-decomposed package documented so far** — cleaner than
  `tasks_pkg` (Unit 1) — which reinforces that good decomposition is achievable
  here and the giants are the exception, not the norm.

---

*Next unit: Unit 5 (Context engineering — `system_context`, `compaction`,
`memory/`, `conv_message_builder`, `token_counter/`).*
