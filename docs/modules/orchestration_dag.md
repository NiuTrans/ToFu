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
| Graph execution | `lib/orchestration_engine.py`, `lib/orchestration_execution_runtime.py` |
| Ephemeral and durable start | `lib/orchestration/runtime_start_service.py` |
| Durable run application API | `lib/orchestration/run_service.py` |
| Durable run repository | `lib/orchestration/sidecar_run_store.py` → Sidecar orchestration operations |
| Chat GoalRun contract and lifecycle | `lib/goal_runs/contract.py`, `service.py` |
| Chat GoalRun repository | `lib/goal_runs/repository.py` → Sidecar `goal.run.*` operations |
| Runtime event durability, reduction, and timeline policy | `lib/orchestration/events.py` |
| Mutations and human gates | `lib/orchestration/runtime_mutation_service.py`, `human_gate_service.py` |
| HTTP composition | `routes/api_v1/orchestrations.py` |
| Generated browser request contract and endpoint client | `frontend/src/features/orchestration/request-contracts.generated.ts`, `api-client.ts` |
| Bounded startup saved-Flow catalogue | `frontend/src/features/orchestration/flow-catalog.ts` |
| Studio and Task Mode typed modules | `frontend/src/features/orchestration/` |
| Demand-loaded retained Studio presentation | manifest bundle `orchestration-presenters` from `frontend/src/runtime/sections/orchestration*.js` |

`lib/orchestration/__init__.py` exports nothing. Import the focused owner; do
not create another convenience facade.

## Product exposure contract

Workflows, Agent/Goal Mode, and Tasks expose distinct authoring, next-turn, and
durable-run responsibilities. Their debug gating, selection fencing, and
visible-turn translation policy live in
[`../ORCHESTRATION_PRODUCT_SURFACES.md`](../ORCHESTRATION_PRODUCT_SURFACES.md).

## Application boundary

`OrchestrationApplicationServices` supplies late-bound ports for definitions,
authoring, durable runs, starts, mutations, and human gates. HTTP/chat adapters
only parse and project typed results; repositories, SQL, graphs, and outcome
classification remain behind those ports. Registration creates the bounded
`TaskRuntime`, container, and contract metadata; implementations load on the
first authorized operation.

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

The HTTP contract generator writes only
`frontend/src/features/orchestration/request-contracts.generated.ts`. It joins
canonical paths, verbs, path/query/body mappings, response metadata, and
browser method names into one immutable typed registry. Response adaptation is
not another generated runtime: `frontend/src/core/http-result.ts` owns the
status-preserving HTTP boundary and `api-client.ts` consumes it directly.

The generated snapshot named `compatibility-defaults` is a build-time copy of
current backend contracts. It is not permission to accept retired wire shapes.

## Runtime flow

1. The ingress resolves exactly one inline or stored definition.
2. `definition_inspection.py` validates and canonicalizes it before start.
3. `runtime_start_service.py` creates ephemeral or durable identity and hands
   the worker to the shared execution runtime.
4. The graph engine owns topology, control-node navigation, Typed I/O,
   subflows, loops, and parallel convergence.
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

The machine-readable policy in `lib/goal_runs/contract.py` requires a
long-term solution horizon, root-cause work, and verification evidence. The
canonical worker and virtual-user prompts consume the same directive. A
compatibility summary or marker-cleanup failure may not rewrite GoalRun truth;
a failed required GoalRun transition fails the chat terminal boundary closed.
The default Goal budget is 40 graph iterations, restoring the historical
long-horizon allowance; all executor loops share a non-disableable 64-iteration
hard ceiling. Invalid or extreme request overrides are normalized inside that
finite range.

Arming behind a live ordinary turn creates one deduplicated,
`goal_continuation` queue command. It is lower priority than an ordinary human
message, uses the same owner-scoped lane-idle fence as human input, and can be
removed by disarm without deleting other queued work. Queue dispatch restores
the server-stamped objective; a continuation missing that authority is retired
instead of treating “continue” as a new objective.

Ephemeral `TaskRuntime` state is process-local and bounded. Durable state and
replay survive restart because the Sidecar is authoritative. A durable start
failure must close the durable row; it must never leave an apparently active
run without a worker.

At worker/all-process startup, every non-terminal durable run is settled as
`worker_lost` before clients reconnect. GoalRuns also receive an atomic typed
`failed / worker_lost` transition event, so their semantic projection never
falls back to a bare physical `error`. This is an explicit cross-owner
Sidecar **maintenance** operation because all executor threads died with the
previous process; ordinary run repositories and mutations remain bound to an
explicit owner and tenant. Startup recovery must never impersonate the
personal owner or weaken request-time owner predicates.

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
- Repository methods are owner-bound and Sidecar-only.
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
| Execution semantics | `lib/orchestration_engine.py` and focused runtime module | engine, outcome, durable replay |
| HTTP path/body/query | `http_endpoint_registry.py` then route adapter | HTTP contract generator and parity tests |
| Mutation semantics | `mutation_result.py`, `mutation_contract.py` | mutation HTTP/OpenAPI/frontend reads |
| Studio behavior | `frontend/src/features/orchestration/` | focused TypeScript/Node workspace tests |
| Debug exposure or chat workflow selection | application-shell Agent Mode fragments, `main_toolbar_ui.js`, `settings/mcp.js` | `tests/test_orchestration_product_design.py`, Agent Mode tests |

## Test map

Start with the matching `tests/test_orchestration_{definition,service,engine,
outcome,mutation}*.py` owner, then HTTP/OpenAPI parity and the focused frontend
workspace/product tests. The domain gate is all `test_orchestration*.py` plus
frontend orchestration tests. Regenerate artifacts first; do not compose the
monolithic retained runtime while an unrelated section is dirty.
