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
| Mutations and human gates | `lib/orchestration/runtime_mutation_service.py`, `human_gate_service.py` |
| HTTP composition | `routes/api_v1/orchestrations.py` |
| Studio modules | `frontend/src/features/orchestration/` |
| Retained browser delivery sections | `frontend/src/runtime/sections/orchestration*.js`, `api/orchestration*.js` |

`lib/orchestration/__init__.py` exports nothing. Import the focused owner; do
not create another convenience facade.

## Product exposure contract

Orchestration is experimental and remains behind `debug_mode`. With Debug
Mode off, the Workflows and Tasks navigation entries, saved-workflow choices,
and their mobile counterparts are hidden. Restoring a conversation does not
project its stored `activeFlow` into the composer while the flag is off; turning
the flag off also clears the painted selection for future turns. An already
accepted turn keeps its immutable execution snapshot.

The three user concepts have distinct jobs:

- **Workflows** opens the Orchestration Studio authoring surface;
- **Agent Mode** selects how the next accepted chat turn runs;
- **Tasks** observes, approves, aborts, and reopens durable runs.

Saved Studio definitions appear inside the Debug-only section of Agent Mode,
not as a second toolbar selector. Autopilot appears there only once as a chat
mode; its engine graph remains available as a Studio template but is not
duplicated as a built-in workflow choice.

“Save & use” is an editor-owned transaction. A successful save may select the
definition for the current chat only when the same document token and revision
are still current, the chat can change modes, and Studio closes successfully.
A CAS conflict, failed save, intervening edit/document switch, busy chat, or
failed close leaves Studio open and never changes `activeFlow`.

## Application boundary

`OrchestrationApplicationServices` is the delivery-layer composition object.
It supplies late-bound structural ports for definitions, authoring, durable
runs, runtime starts, mutations, and human gates. HTTP and chat adapters parse
transport input and project results; they do not select repositories, execute
SQL, rebuild graphs, or classify domain outcomes.

The HTTP blueprint is split by use case:

- `orchestration_definition_routes.py`: list/read/create/replace/delete;
- `orchestration_authoring_routes.py`: validate, layout, compose, built-ins,
  authoring contract, and plan;
- `orchestration_runtime_routes.py`: ephemeral start and poll;
- `orchestration_task_routes.py`: durable create/read/list/replay;
- `orchestration_mutation_routes.py`: abort, delete, approval, and input.

Shared parsing and projection belong in the sibling `*_http.py` modules.
OpenAPI is projected from the same registries in `lib/orchestration/`; a route
must not hand-author a competing schema.

Authoring metadata may read the lightweight swarm role registry, but route and
OpenAPI registration must not initialize swarm agents, schedulers, integration
state, task handlers, or project tools. `lib.swarm` preserves its package-level
API through lazy exports; execution modules load only when their symbol is used.

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

The HTTP contract generator owns both the endpoint artifact and the canonical
response-contract artifact; there is no second response-contract generator.

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

Ephemeral `TaskRuntime` state is process-local and bounded. Durable state and
replay survive restart because the Sidecar is authoritative. A durable start
failure must close the durable row; it must never leave an apparently active
run without a worker.

At worker/all-process startup, every non-terminal durable run is settled as
`worker_lost` before clients reconnect. This is an explicit cross-owner
Sidecar **maintenance** operation because all executor threads died with the
previous process; ordinary run repositories and mutations remain bound to an
explicit owner and tenant. Startup recovery must never impersonate the
personal owner or weaken request-time owner predicates.

## Invariants

- One graph interpreter; plans and UI projections may inspect but not execute a
  second graph semantics.
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

Run the smallest relevant set first:

```bash
pytest -q tests/test_orchestration_definition_http.py \
  tests/test_orchestration_definition_request_http.py \
  tests/test_orchestration_definition_openapi.py
pytest -q tests/test_orchestration_service.py \
  tests/test_orchestration_run_service.py
pytest -q tests/test_orchestration_engine.py \
  tests/test_orchestration_outcome.py
pytest -q tests/test_orchestration_mutation.py \
  tests/test_orchestration_mutation_http.py
pytest -q tests/test_api_contract_orchestrations_parity.py \
  tests/test_frontend_orchestration_workspace.py \
  tests/test_orchestration_product_design.py
```

For a complete domain gate, run all `tests/test_orchestration*.py` plus the
frontend orchestration tests. Regenerate artifacts before the broad gate and
do not compose the monolithic retained runtime when an unrelated section is
dirty.
