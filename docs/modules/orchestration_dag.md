# Orchestration and DAG runtime

This map describes the orchestration system that runs now. The domain owns
Studio authoring, stored definitions, graph execution, durable runs, replay,
human gates, and state-changing run operations. Historical migrations and
removed response aliases live only in Git history.

## Ownership

| Concern | Owner |
|---|---|
| Definition schema, defaults, validation, layout | `lib/orchestration/_definition_contract.py`, `_defaults.py`, `_validate.py`, `_layout.py` |
| Authoring catalogue, Composer, built-ins, dry-run plans | `lib/orchestration/authoring_*.py` |
| Stored-definition use cases | `lib/orchestration/definition_service.py` |
| Stored-definition repository | `lib/orchestration/store.py` → Sidecar `orchestration.definition.*` operations |
| Graph execution | `lib/orchestration/engine.py`, `execution_runtime.py`, optional `native_flow.py` → Rust `flow_graph.rs` ([contract](../NATIVE_FLOW_CONTROL.md)) |
| Role invocation execution | `lib/orchestration/agent_runner.py` → shared `lib/tasks_pkg/invocation.py` |
| Ephemeral and durable start, boot continuity auto-resume | `lib/orchestration/runtime_start_service.py`, `continuity.py` |
| Durable run application API | `lib/orchestration/run_service.py` |
| Durable run repository | `lib/orchestration/sidecar_run_store.py` → Sidecar orchestration operations |
| Chat GoalRun contract and lifecycle | `lib/goal_runs/contract.py`, `service.py` |
| Chat GoalRun repository | `lib/goal_runs/repository.py` → Sidecar `goal.run.*` operations |
| Runtime event durability, reduction, and timeline policy | `lib/orchestration/events.py` |
| Mutations, human gates and continuations | `lib/orchestration/runtime_mutation_service.py`, `human_gate_service.py`, `continuation.py`; Goal checkpoints: `lib/goal_runs/context_recovery.py` |
| HTTP composition | `routes/api_v1/orchestrations.py` |
| Generated browser request contract and endpoint client | `frontend/src/features/orchestration/request-contracts.generated.ts`, `api-client.ts` |
| Bounded startup saved-Flow catalogue | `frontend/src/features/orchestration/flow-catalog.ts` |
| Studio typed modules | `frontend/src/features/orchestration/` |
| Demand-loaded retained Studio presentation | manifest bundle `orchestration-presenters` from `frontend/src/runtime/sections/orchestration*.js` |

`lib/orchestration/__init__.py` exports nothing. Import the focused owner; do
not create another convenience facade.

## Product exposure contract

Workflows and Agent/Goal Mode expose distinct authoring and next-turn
responsibilities; durable runs are observed through the chat projection and
the run APIs. Gating, fencing, and translation policy:
[`../ORCHESTRATION_PRODUCT_SURFACES.md`](../ORCHESTRATION_PRODUCT_SURFACES.md).

## Application boundary

`OrchestrationApplicationServices` supplies late-bound ports for definitions, authoring, durable runs, starts, mutations, and human gates. HTTP/chat adapters only parse and project typed results; repositories, SQL, graphs, and outcome classification remain behind those ports. Registration creates the bounded `TaskRuntime`, container, and contract metadata; implementations load on the first authorized operation.

Use-case routes live in `orchestration_{definition,authoring,runtime,task,
mutation}_routes.py`; sibling `*_http.py` modules own shared parsing and
projection. OpenAPI comes from `lib/orchestration/` registries. Registration
may read lightweight role metadata but must not initialize agents, schedulers,
integrations, task handlers, or project tools; those APIs remain lazy.

## Persistence and identity

Definitions, runs, and run events are owner-scoped Sidecar data. Every
repository instance receives `owner_user_id` and the tenant evolution seam.
The Sidecar semantic operation is the transaction boundary; routes contain no
SQL and no filesystem persistence path.

Definition replacement and deletion always use compare-and-set:

1. reads and successful writes return `ETag: "<updatedAt>"`;
2. replace/delete require that token in `If-Match`;
3. a missing or malformed token is `400` before the service call;
4. a stale token is `409 stale_definition` with non-null expected/current
   versions;
5. the Sidecar checks the version and mutates in one locked transaction.

There is no unguarded repository method and no missing-header compatibility
mode. A Studio session that loses its version refreshes metadata instead of
issuing an unconditional write.

## Wire contracts

Wire formats and field registries are defined once under
`lib/orchestration/`. Important owners are:

- `definition_contract_registry.py` and `definition_wire_projection.py`;
- `runtime_wire_contracts.py`;
- `mutation_contract.py` and `mutation_payload_fields.py`;
- `outcome_domain.py` and `durable_projection.py`;
- `inspection_wire_contract.py`;
- `http_endpoint_registry.py`.

Responses expose one canonical envelope. Runtime start identity lives under
`start`; mutation state lives under `mutation`; definition conflicts live
under `write`. Do not add top-level aliases or frontend fallback scanners.
Unknown explicit formats fail closed.

Generated browser artifacts are rebuilt with:

```bash
python3 scripts/gen_orchestration_http_contract.py
python3 scripts/gen_orchestration_authoring_metadata.py
python3 scripts/gen_orchestration_compatibility_defaults.py
```

The HTTP contract generator writes only `frontend/src/features/orchestration/request-contracts.generated.ts`. It joins canonical paths, verbs, path/query/body mappings, response metadata, and browser method names into one immutable typed registry. Response adaptation is not another generated runtime: `frontend/src/core/http-result.ts` owns the status-preserving HTTP boundary and `api-client.ts` consumes it directly.

The generated snapshot named `compatibility-defaults` is a build-time copy of
current backend contracts. It is not permission to accept retired wire shapes.

## Runtime flow

1. The ingress resolves exactly one inline or stored definition.
2. `definition_inspection.py` validates and canonicalizes it before start.
3. `runtime_start_service.py` creates ephemeral or durable identity and hands
   the worker to the shared execution runtime.
4. The graph engine owns topology, Typed I/O, subflows and bounded convergence;
   role nodes enter the ordinary invocation owner, not a second agent pipeline.
5. Runtime events are projected once and appended to the durable event log
   when the run is durable.
6. `outcome_domain.py` classifies terminal meaning once; chat, polling, replay,
   and run headers consume that result.
7. Mutations return the versioned `tofu.orchestration.mutation/v1` envelope.

For the `autopilot` projection, the chat adapter adds one required lifecycle
around that flow:

1. derive the objective from the current accepted human turn (never the first
   message in the conversation and never a synthetic VU/review row);
2. atomically start an owner/tenant/conversation-scoped GoalRun, superseding a
   prior active goal with the typed reason `superseded_by_new_goal`;
3. execute the graph and stamp every VU turn with that GoalRun id;
4. accept a VU completion only with its parseable `remaining=0` progress
    receipt; a bare/malformed done sentinel is not verification evidence;
    `goal_completion_evidence_missing` and `goal_stop_rejected` remain durable
    audit facts explaining why execution continued, without mutating generic
    Studio state or adding end-user timeline noise;
5. map the shared `TerminalOutcome` once to `completed`, `blocked`, `failed`,
   or `cancelled`, then persist the transition before chat emits `done`.

The machine-readable policy in `lib/goal_runs/contract.py` requires a long-term solution horizon, root-cause work, verification evidence, and forbids shrinking the original objective scope; the canonical worker and virtual-user prompts consume the same directive. A compatibility summary or marker-cleanup failure may not rewrite GoalRun truth; a failed required GoalRun transition fails the chat terminal boundary closed. Goal budget is a checkpoint **window** (default 40 iterations), never a stop reason: a closing window that still advances is extended; only a full window with zero state changes and zero verified progress ends the loop as `stalled` (`blocked/no_verified_progress`), and a gated VU `[VERDICT: BLOCKED]` ends it as `vu_blocked` (`blocked/verifier_declared_blocked`).

Arming behind a live ordinary turn creates one deduplicated,
`goal_continuation` queue command. It is lower priority than an ordinary human
message, uses the same owner-scoped lane-idle fence as human input, and can be
removed by disarm without deleting other queued work. Queue dispatch restores
the server-stamped objective; a continuation missing that authority is retired
instead of treating “continue” as a new objective.

Ephemeral `TaskRuntime` state is process-local and bounded. Durable state and
replay survive restart because the Sidecar is authoritative. A durable start
failure must close the durable row, never leaving an apparently active run
without a worker. Post-ready, the [continuity pass](../RUN_CONTINUITY.md)
auto-resumes the boot's retired `auto`-policy runs.

At worker/all-process startup, every non-terminal durable run is settled as `worker_lost` before clients reconnect. GoalRuns also receive an atomic typed `failed / worker_lost` transition event, so their semantic projection never falls back to a bare physical `error`. This is an explicit cross-owner Sidecar **maintenance** operation because all executor threads died with the previous process; ordinary run repositories and mutations remain bound to an explicit owner and tenant. [Flow recovery](../TASK_EXECUTION_MODES.md#durable-flow-restart-recovery) creates an owner-scoped successor from committed effect receipts. Startup recovery must never impersonate the personal owner or weaken request-time owner predicates.

## Invariants

- One graph interpreter; plans and UI projections may inspect but not execute a
  second graph semantics.
- One GoalRun lifecycle owner; queue markers and prompt sentinels are
  projections/control tokens, never lifecycle authority.
- One outer `_flow_managed` ownership marker survives every inner role turn;
  scoped role execution may not erase it and reactivate compatibility hooks.
- One terminal-outcome classifier across live, chat, durable, and replay
  surfaces.
- One mutation result taxonomy and HTTP mapping.
- One endpoint registry shared by backend validation, OpenAPI, and browser
  generation.
- Definition validation happens before persistence or execution.
- Repository methods are owner-bound and use the selected storage client.
- CAS is mandatory for every replacement and deletion.
- Application services return typed results/errors; delivery adapters do not
  catch arbitrary programmer errors.
- Frontend modules render declared semantics and never infer a role or outcome
  from a transport event name.

- Flow role tools use one occurrence identity across live task events,
  reconnect snapshots, and settled `toolRounds`; terminal projection may not
  re-number or synthesize a second timeline.
- Fault injection, worker-handoff cleanup, terminal fences, and replay cursor
  correction remain executable behavior.

## Change routing

| Change | Start here | Then verify |
|---|---|---|
| Node/control/role field | `_definition_contract.py`, `_defaults.py`, field-spec owners | authoring schema and metadata generators |
| Validation rule | `_node_validation.py`, `_edge_validation.py`, `_validate.py` | inspection, save, plan, and run tests |
| Definition persistence | `definition_service.py`, `store.py`, Sidecar operation | owner isolation and CAS integration |
| Execution semantics | `lib/orchestration/engine.py` and focused runtime module | engine, outcome, durable replay |
| HTTP path/body/query | `http_endpoint_registry.py` then route adapter | HTTP contract generator and parity tests |
| Mutation semantics | `mutation_result.py`, `mutation_contract.py` | mutation HTTP/OpenAPI/frontend reads |
| Studio behavior | `frontend/src/features/orchestration/` | focused TypeScript/Node workspace tests |
| Debug exposure or chat workflow selection | application-shell Agent Mode fragments, `main_toolbar_ui.js`, `settings/mcp.js` | `tests/orchestration/test_orchestration_product_design.py`, Agent Mode tests |

## Test map

Run the matching orchestration owner suite and Goal invocation integration, then
HTTP/OpenAPI and frontend parity. The domain gate is `test_orchestration*.py`
plus frontend orchestration tests; regenerate artifacts without overwriting unrelated work.
Dynamic fanout, node model/tool policy and recovery boundaries: [execution modes](../TASK_EXECUTION_MODES.md#dynamic-fanout-and-node-execution-policy).

## Flow child accounting boundary

**Problem.** Ordinary child finalization settles provider usage and compaction
into the ephemeral child, but the runner and chat adapter previously dropped
`apiRounds`, cost snapshots, and compaction detail. Counting only root calls or
visible phase text is incomplete; absent historical details cannot be
reconstructed into a bill.

**Decision.** `usage_accounting.py` projects settled accounting through both whitelists. Each compact API row retains the original round/model/provider and available request IDs, plus canonical child `invocationId` and positional `ledgerIndex`. Repeated round numbers (including discarded/retried calls) remain distinct requests. `_finalize.py` stamps child per-request prices through the existing `lib.cost.compute_cost` authority before child release; existing snapshots are never repriced. Missing usage or unavailable pricing stays missing, not an invented zero-cost call. Task totals already include compaction; the compaction breakdown and API rows are **not** added to those totals again.

`FlowEventAdapter.usage_summary` is a **child-only snapshot**, not an additive delta or complete run bill. It folds one terminal receipt per canonical child, normalizes cache/token conventions per request, and reports coverage separately for usage, recorded token totals, and stamped costs. Exact replay (including a lost persistence ACK or replay after `drain()`) reuses the original phase ID; conflicting terminal output/accounting is rejected. Empty, failed, cancelled, and empty-output children preserve any reported paid usage. Partial costs remain under `usageAccounting.recordedCost` and per-call snapshots rather than being misrepresented as a complete phase `cost`.

Bounds: one fixed-vocabulary row per recorded paid call; no request prompts, wire messages, timing payloads, or nested compaction call copies. The ledger is O(child invocations) and holds only compact receipts, while the adapter reuses existing phase-message references for replay. The executable payload bound uses multi-megabyte synthetic prompts/timing and asserts a projection below 1.6 KB; the three-call finalizer/runner/adapter fixture remains below 6 KB. Call counts are not arbitrarily truncated by Flow. The upstream compaction accumulator does cap detail at 16 rows; `n_calls` versus retained rows explicitly marks that gap.

**Remaining durable boundary.** Public `TurnProjection` normalization retains `usage`, compact `apiRounds`, and complete `cost`; compact compaction rows have `kind=compaction`. It does **not** retain `usageAccounting` or `compactionUsage`, and `TurnOrchestration` is a closed contract, not an arbitrary metadata bag. The child summary is not yet consumed by `chat_flow_runtime` / `chat_completion` or persisted as a distinct run/header accounting receipt. Contract, settlement, and storage owners must integrate that field and coverage before claiming a complete durable run bill (especially when compaction detail is truncated). No historical backfill or native/SQL persistence success is claimed here. Fatal/preparation paths bypassing ordinary finalization can retain usage without price snapshots and are reported as unpriced rather than guessed.

**Alternatives rejected.** Copying children into root `task.usage` / `apiRounds` would overwrite phase zero with the run total and double count root plus child phases. Repricing merged usage under the terminal model misbills mixed-model and mixed-cache conventions. Hiding unknown receipts under orchestration metadata violates the closed durable contract. Dropping older paid calls to achieve a constant-size bill conceals usage rather than bounding raw diagnostic payloads.

Executable boundary specification:
`tests/orchestration/test_flow_child_accounting.py` (real child finalizer → runner
→ adapter → public projection, snapshot aggregation, coverage, and replay).

Problem: the parallel interpreter retained a fixed worker count after the authoring concurrency quota was retired, leaving independent branches queued. Decision: `parallel_runtime.py` uses the shared demand owner; the legacy `max_parallel` argument sizes idle reuse only. Failed unstarted submissions become the existing branch failure outcome, while already published branches drain. Graph-order merging, common barriers, typed aborts and event-durability failures retain their authority. Alternatives: raising the count still queues; detached branch threads lose joined ownership. Real rendezvous and startup-fault tests verify delivery and settlement. Measurement: 32 branches rendezvous with idle hint 1 in 10.12 ms, with zero workers after joined shutdown.
