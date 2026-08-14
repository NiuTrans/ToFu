# Module Design Doc — Unit 4: Orchestration / DAG (`orchestration*.py`, `swarm/`)

> Part of the per-module design-doc set (see `docs/ARCHITECTURE.md`). This unit
> covers the two multi-agent execution systems: the top-level
> `orchestration*.py` graph layer and the `lib/swarm/` async fan-out system.
>
> **Grounding:** every line count is `wc -l` on disk 2026-07-11. `list_dir`
> overcounts — all numbers are `wc -l`. Every MISCUT/BIG verdict cites competing
> responsibilities or line ranges; size alone is never the argument.

## 2026-08-08 alignment update: Flow Autopilot parity tranche

The graph interpreter was not the source of the observed Autopilot mismatch.
The defect was at the chat boundary: `orchestration_endpoint_adapter.py` treated
the Endpoint wire vocabulary as both **transport** and **presentation meaning**.
Consequently a graph `virtual_user` streamed live as an Endpoint Critic, even
though its durable row was later marked `_isVirtualUser`; refresh changed the
same turn's identity. The bridge also seeded a graph with only the latest user
string, dropped the resolved system/project prompt at the `SubAgent` boundary,
and persisted `[VU: TASK_DONE]` as if it were user-visible speech.

The first parity tranche separates those axes without breaking rolling-client
compatibility:

| Axis | Status | Contract |
|---|---|---|
| Graph topology | green | `FlowExecutor` remains the single interpreter; no Autopilot-only graph engine was added. |
| Chat projection | green | `chat_projection_for_flow()` derives `autopilot` / `endpoint` / `flow` from graph roles. `endpoint_mode` remains the compatible multi-turn transport flag; `flowMode` + `flowProjection` carry meaning. |
| VU identity | green | Start/delta/finalize/reconnect frames carry `turnRole`, `emits`, stable `vuMsgId`, and `autopilotRunId`; a VU row never receives `_isEndpointReview`. |
| Context and policy | green | Chat-launched flows receive bounded full conversation history, the resolved system/project prompt, and the chat thinking preference. Isolated subflows inherit the same policy. |
| Terminal semantics | green | `[VU: TASK_DONE]` is control-plane data: it stops the graph, removes the eager placeholder, creates no transcript row, and emits the normal `autopilot_run_concluded` lifecycle fact. |
| Recovery | green | SSE state, in-memory poll, and durable task metadata preserve projection/current-turn identity; a terminal snapshot no longer creates a ghost Worker. |
| Engine cutover | amber | Standalone `tasks_pkg.autopilot` and graph Autopilot are still two loop drivers. Behavioural contracts now align, but retiring either driver remains a separate measured migration. |

The durable boundary rule going forward is:

1. graph roles/topology define execution;
2. the backend derives an explicit chat projection;
3. transport carries that projection and stable turn identity;
4. the frontend renders declared semantics and must not infer "Critic" merely
   because an event uses the legacy `endpoint_iteration` envelope.

Regression coverage: `test_orchestration_endpoint_adapter.py` pins projection,
stable VU identity, token stripping and terminal discard;
`test_chat_flow_dispatch.py` pins full-history/system-policy inheritance and
end-to-end terminal behaviour; `test_frontend_sse_dispatch.py` pins live,
reconnect and discard rendering.

### 2026-08-08 Studio and application-service tranche

The next defect class was not in graph interpretation either: application
logic had accumulated in adapters. The REST route, durable Task Mode route and
chat launcher each knew how to locate stored definitions, select built-ins,
construct `FlowExecutor`, fan out events, and decide whether a non-converged
result counted as done. The Studio simultaneously had no document lifecycle:
an invalid or edited graph looked identical to a saved graph until an action
failed.

The reusable backend boundary is now:

1. `lib/orchestration/*` — pure graph schema, validation, builders and layout;
   `_defaults.py` is the canonical constructor for new role/control/subflow
   params and is shared by Studio contracts and built-in graph builders;
2. `lib/orchestration/store.py` — the only owner of the JSON repository shape
   and atomic definition CRUD;
3. `lib/orchestration/definition_service.py` —
   `OrchestrationDefinitionService` is the
   single list/get/resolve/validated-create/update/delete and subflow-resolver
   interface above that repository. Typed Definition/Run/Runtime Start failures
   share the stable `lib/orchestration/errors.py` application-error contract;
   concrete services re-export their historical exception names.
   `lib/orchestration/definition_inspection.py` is the repository-free shared
   owner of inspection, dry-run preview attachment and field-value
   canonicalization. `service.py` is now a compatibility re-export facade
   only;
4. `lib/orchestration/authoring_contract.py` — the focused application owner
   of role/control catalogues, new-node defaults, built-in graphs and the
   detached Studio authoring-contract document;
   `lib/orchestration/authoring_service.py` exposes validation, Composer,
   built-ins, contract lookup and copy-before-layout through one
   `OrchestrationAuthoringService` interface. HTTP adapters inject that
   interface instead of assembling implementation functions; `service.py`
   only re-exports them for rolling compatibility;
5. `lib/orchestration/wire_contracts.py` — the framework-free owner of
   authoring/inspection/definition list-entry-write and runtime-start format
   identifiers, request schema, detached response projection and
   optimistic-write token parsing;
6. `lib/orchestration/runtime_service.py` — the execution-only boundary for
   executor construction, result normalization, live+durable event projection,
   terminal fences and TaskRuntime completion;
7. `lib/orchestration/runtime_start_service.py` — the application facade that
   creates ephemeral and durable runtime tasks, injects late-bound subflow
   resolution and closes both runtime projections if worker handoff fails;
8. `lib/orchestration_graph.py` — the pure topology boundary for entry lookup,
   reachability, loop body/exit partitioning and parallel barrier convergence;
   it has no agent, runtime-state or persistence dependency;
9. `lib/orchestration_plan.py` — the execution-free dry-run compiler used by
   Studio inspection. It shares ordered adjacency and navigation with the live
   executor rather than rebuilding a second graph interpretation;
10. `lib/orchestration_dataflow.py` — the thread-safe runtime data plane for
   initial seed resolution, named/implicit output publication, strict Typed-I/O
   input composition and artifact change manifests;
11. `lib/orchestration_trace.py` — the bounded, thread-safe per-node trace store,
   sole projector of durable `step_trace` events and owner of the versioned
   cross-surface text-limit/truncation contract;
12. `lib/orchestration_feedback.py` — the shared-context feedback channel,
   bounded node-attempt memory and per-loop stuck/VU convergence ledger;
13. `lib/orchestration_progress.py` — the deterministic producer ledger used by
   parallel loop guards and verifier deliverables injection;
14. `lib/orchestration_outcome.py` — the versioned terminal-outcome classifier,
   loop/node/artifact ledger and the sole durable/chat lifecycle projector;
15. `lib/orchestration_mutation.py` — the versioned state-change result for
   durable abort/delete and human-gate resolution, including shared reason,
   retryability, HTTP classification and compatibility projection;
16. `lib/orchestration_agent_runner.py` — the production implementation of the
   interpreter's injected leaf-runner port. It alone maps role nodes to lazy
   `SubTaskSpec`/`SubAgent` construction and maps SubAgent streaming back to
   engine events;
17. `lib/orchestration_runner_result.py` — the typed leaf-result port. The
    production adapter returns its immutable value directly, while legacy
    custom Runner mappings are normalized once; invalid top-level shapes
    become explicit failed-node outcomes instead of scattered `.get` errors;
18. `lib/orchestration_tool_usage.py` — the immutable compatibility boundary
    for Runner `tool_names`/`tool_log` telemetry. Deliverable, exploratory and
    unreported-tool semantics are normalized once before execution progress,
    trace or loop guards consume them;
19. `lib/orchestration/human_gate_service.py` — the application port that
    resolves approval/input against the existing shared chat registries and
    returns the canonical orchestration mutation result. HTTP adapters no
    longer import `tasks_pkg` or classify gate presence themselves;
20. Flask/chat adapters — parse authentication/transport input and provide
    callbacks, but do not reimplement graph selection or run semantics.

The HTTP adapter is physically split along the same boundary:
`routes/api_v1/orchestrations.py` is the composition root,
`orchestration_definition_routes.py` registers definition persistence,
`orchestration_authoring_routes.py` registers repository-free Studio
validation/Composer/built-ins/contracts/layout,
`orchestration_runtime_routes.py` registers ephemeral plan/start/poll,
`orchestration_task_routes.py` registers durable create/list/get/replay, and
`orchestration_mutation_routes.py` registers human gates plus ephemeral and
durable abort/delete. `orchestration_task_http.py` owns durable list-filter
parsing, replay-cursor normalization, list/item/replay response projection and
the one durable-service exception-to-HTTP boundary shared by read and mutation
adapters; the durable route no longer interprets these wire details itself.
The framework-neutral replay vocabulary remains in `lib/task_replay.py`, while
`routes/task_http.py` is now the single live/durable HTTP cursor, OpenAPI query
and replay-status adapter consumed by both the generic task-route factory and
durable orchestration reads. The generic v1 task long-poll and SSE endpoints
also consume its query schema and parser, so negative/malformed cursors and
OpenAPI metadata no longer have a third implementation. Factory-generated
orchestration poll/abort routes
also receive the same `require_auth` decorator explicitly, keeping executable
access control aligned with their OpenAPI security declaration.
`orchestration_definition_http.py` similarly owns definition service-failure,
list/item/write/delete, ETag and validation/CAS projection; CRUD routes retain
only request parsing, service invocation and success logging.
Its failure wrapper, the authoring wrapper, the durable-run wrapper and the
shared start wrapper all delegate to
`orchestration_service_http.orchestration_service_call()`. Validate, Composer,
built-in/contract/layout reads and Plan therefore expose the same declared
application-failure envelope as definition and runtime operations. The adapter
catches only `OrchestrationServiceError`, attaches the operation context and
invokes the injected 500 projector; unexpected programmer errors still escape.
HTTP helpers therefore no longer import concrete service modules or replicate
exception-mapping templates.
`orchestration_authoring_http.py` owns the shared Builtin/Layout
definition-action envelope, typed Composer request preparation and logical
result passthrough, plus the detached Plan/inspection response projection,
matching the browser's single authoring/runtime projector shapes. Plan
compilation itself now goes through `OrchestrationAuthoringService.plan()`;
the runtime route no longer imports or interprets definition inspection.
`orchestration_definition_request_http.py` is the one `definition`/stored `id`
ingress used by Layout, Plan and `orchestration_run_http.prepare_run_request()`;
the latter adds inspection, canonicalization and input extraction for both
ephemeral and durable starts, then projects their shared provenance/inspection
response fields through `run_start_response_fields()`. Missing selection,
provenance and run-start inspection fields therefore have one backend meaning
across all four endpoints.
The two start routes no longer assemble workers independently: both call
`OrchestrationRuntimeStartService`, which owns metadata, subflow lookup,
durable identity and pre-worker failure closure. Their responses contain the
same `tofu.orchestration.runtime-start/v1` `start` envelope while preserving
legacy `task_id`/`run_id` aliases. `orchestration-runtime-read.js` consumes that
envelope through one projector and only falls back to aliases when the envelope
is absent; an explicit unknown version fails closed.
`orchestration_mutation_http.py` similarly owns requestId/approval/input typing
and the only `mutation_response → api_payload` status projection used by human
gates plus ephemeral/durable abort/delete; mutation routes now retain only
service invocation, success logging and compatibility fields.
Those ingress owners also publish the JSON Schemas consumed by `api_meta`:
Layout/Plan share one selection schema, ephemeral/durable starts share its
input-bearing variant, and Composer/approval/input limits come from the same
constants as their executable parsers. The generated OpenAPI document is
therefore tested against runtime request contracts instead of falling back to
an untyped generic object. The compatibility role lookup follows the same rule:
its optional `role` query is parsed and documented by
`orchestration_authoring_http.py`, and the frontend endpoint registry exposes
the corresponding optional argument rather than requiring callers to build a
query string.
All focused adapters receive late-bound authoring/definition/run service
providers plus the shared resolver/runtime as needed. Those ports are
structural `Protocol` capabilities and import no concrete service at runtime;
reloads, replacement implementations, production and tests therefore use the
same seams without a route-local persistence path.

`inspect_definition()` is also the Studio contract. In one versioned
`tofu.orchestration.inspection/v1` response it returns structured diagnostics,
legacy error/warning arrays and the graph projection contract. Save, run and
Task Mode responses derive their compatibility fields through
`wire_contracts.inspection_response_fields()` rather than rebuilding the
shape. The authoring catalogue itself is now identified as
`tofu.orchestration.authoring-contract/v1`. The frontend keeps the matching
protocol registry in `orchestration-wire-contract.js`: missing `format` stays
the single rolling-server compatibility path, while an explicitly different
version is classified as `unsupported-format` and never guessed into the
current model. `orchestration-result.js` owns only generic error/HTTP-result
primitives; `orchestration-inspection-result.js`,
`orchestration-outcome-result.js`, `orchestration-mutation-result.js` and
`orchestration-definition-write-result.js` mirror their four backend domain
owners. `orchestration-read-core.js` owns shared envelope/action classification.
`orchestration-definition-read.js` mirrors persisted-definition routes,
`orchestration-authoring-read.js` mirrors repository-free authoring routes,
and `orchestration-runtime-read.js` mirrors ephemeral and durable runtime
routes. Each domain explicitly registers its projectors;
`orchestration-http-read.js` is only their unified lookup/injection seam, so
Document, Workspace, Composer, Run and Task Mode share the same interpretation
without duplicating transport policy or loading unrelated domains.
The low-level browser transport is physically separate too:
`api.js` owns request execution and publishes a stable empty
`Api.orchestrations` object. `api/http-result.js` is the only raw `Response`
to `{ok,status,data}` adapter; its `normalize` path guarantees a transport
envelope while `adapt` preserves legacy direct JSON bodies.
`api/orchestration-endpoints.js` is the canonical registry for all 23 semantic
endpoint ids: each entry owns its HTTP verb, backend-style route template,
path/query/body shaping, optimistic `If-Match` policy, public result/direct API
method names and response-projector metadata. `api/orchestrations.js` is now a
thin compatibility facade over that registry; normalized/direct method pairs
therefore cannot drift on URL, body keys, query parameters or error options,
and deferred Studio/Task clients consume the same method/projector contract
instead of maintaining a second table.
Both production and development load orders pin
`api.js → api/http-result.js → api/orchestration-endpoints.js →
api/orchestrations.js → push.js`. The generic frontend/backend scanner reads
the registry as part of the API surface, while the orchestration-specific
guard compares its 23 route/verb pairs exactly against the live backend
`url_map` and executes every registered request shape at runtime.
Terminal execution uses the matching `tofu.orchestration.outcome/v1` contract.
The engine records loop exits, node failures and declared artifacts through one
ledger, then classifies success/incomplete/failure/aborted once. Durable Task
Mode and chat tasks consume its explicit lifecycle/chat/finish projections;
an unverified cap stop can no longer become `error` in one surface and a clean
`finishReason=stop` in another. Structured durable errors retain this outcome,
and `OrchestrationRunService.get/list` publish it as a top-level run-header
field (including derived success/aborted outcomes), so reload, rail/title chips
and the live `flow_complete` timeline render the same meaning.
State-changing endpoints use the sibling
`tofu.orchestration.mutation/v1` contract. Durable abort/delete and ephemeral
or durable human-gate resolution expose one nested `mutation` envelope with an
action, machine reason, target/current status, retryability and an explicit
`reconcile_required` hint. The three-state `target_exists` field separately
declares whether the addressed run/gate is known present, known absent or
unknown after the operation, while `resource_terminal` projects the backend's
canonical durable lifecycle fact without asking clients to classify status
strings. Routes retain
their previous HTTP codes and compatibility fields, but no longer hand-map the
same run race independently. Task Mode no longer hard-codes rejection reason
lists or terminal-status literals to decide whether list/header state must be
re-read. During a rolling deployment, a mutation that supplies a resource
status without `resource_terminal` triggers an authoritative refresh instead
of a browser-side guess.
Studio and Task Mode consume this envelope through
one `orchestration-mutation-request.js` client backed by the shared API invoker
and `orchestration-mutation-result.js`. `orchestration-mutation-command.js` turns every
request into the same immutable accepted/rejected/target-presence outcome for
both surfaces; stale/expired gates are removed only when
confirmed, conflicted runs are reloaded, and both gate surfaces disable their
full control group while one resolution is pending. The shared approval/guidance registries
also enforce first-resolution-wins under their lock, including timeout/abort
boundaries, so two tabs cannot overwrite an already accepted human decision.
Definition replacement and deletion have their own narrower
`tofu.orchestration.definition-write/v1` contract. Stored entries expose their
monotonic `updatedAt` validator in the response body and `ETag`; Studio sends
that token in `If-Match` on every update/delete for which it owns a version.
The JSON repository compares and mutates inside the same cross-process atomic
read-modify-write lock, so the check has no time-of-check/time-of-use window.
A stale tab receives a typed `stale_definition` 409 carrying expected/current
versions; its local draft, dirty baseline and last known version remain intact
instead of overwriting or deleting the newer server definition. Header parsing,
conflict projection and the advertised authoring contract are framework-free
service helpers; `orchestration_definition_http.py` is the sole Flask adapter
for request headers, ETags and 400/409 responses. Missing preconditions remain
an explicit rolling-client compatibility path, not a second update
implementation.
Collection reads use the separate
`tofu.orchestration.definition-list/v1` metadata projection owned by
`OrchestrationDefinitionService.list_summaries()`. The list route returns only
`id`, `name`, `nodeCount`, `createdAt` and `updatedAt`; complete DAG snapshots
stay behind the item endpoint. Studio, desktop and mobile flow pickers therefore
share one lightweight list seam instead of downloading every saved graph or
depending on the JSON repository's entry shape. The same contract declares and
the service enforces newest-first `updatedAt` ordering (then `createdAt`, then
stable ID), so every picker presents one consistent order; the Store Browser's
client sort exists only for rolling servers predating this contract.
Full item reads and successful create/update responses use the sibling
`tofu.orchestration.definition-entry/v1` document. The shared
`project_definition_list()` / `project_definition_entry()` service functions
now own these wire documents, including detached snapshots and write-time
inspection fields; GET, POST and PUT routes no longer rebuild stored entries
independently.
Studio also records a rejected save in the document lifecycle rather than only
showing an expiring toast. The header badge remains in a localized
“save conflict · draft safe” state across later local edits and validation
passes. Clicking it opens the single-flight
`orchestration-write-recovery.js` choice: keep the draft (the safe dismissal),
export the root snapshot and then load the latest server version (recommended),
or deliberately load without export. The recovery rechecks document identity
after the choice, so an old dialog cannot replace a newer canvas. Only an
authoritative reload/new baseline, a successful guarded save, or detaching the
persisted copy clears that state.
`/api/v1/orchestrations/authoring-contract` exposes backend-known roles,
controls, built-ins, default
`emits`, `roleSchemas`, `controlSchemas`, Typed I/O, `nodeDefaults` and ordered
`executionOptions` (tier/isolation/scope/emits); the browser adds only
labels/icons and renders role tasks and control settings from those `FieldSpec`
lists. New role/control/Group nodes therefore receive the same backend-owned
defaults and accepted option axes that validation and execution expect.
The versioned browser reader validates this complete shape, reports missing
top-level or nested fields through one typed `missingFields` list, and refuses
to enable authoring when the response is structurally incomplete. The
controller carries no semantic role, persona, execution-option, node-default,
field-value or definition-write fallback. While the contract is pending the
Palette exposes a loading state; after a malformed or unavailable response it
fails closed with a single unavailable state and no draggable node chips. An
inline retry calls the same contract loader port and restores authoring only
after a complete response, so recovery does not require closing the Studio.
The older `/role-schema` route remains a compatibility alias (and keeps its
single-role query), while both responses are built by the same framework-free
`service.authoring_contract()` boundary.
`lib/orchestration/_field_specs.py` is the one value-checking implementation
for both node families, so frontend rendering and backend validation cannot
grow parallel type systems. The AI Composer catalogue is generated from these
same schemas as well; it cannot teach the model stale shadow params such as
`max_concurrent`, `per_item` or `branches`.
All five Studio templates (`endpoint`, `autopilot`, `fanout`, `adversarial`,
`blank`) now resolve through `service.build_builtin_definition()` and the same
authenticated builtin API. The browser no longer carries a second topology,
parameter or baked-coordinate catalogue; the backend builders apply the
canonical layout before returning each definition.

| Studio concern | Status | Contract |
|---|---|---|
| Document lifecycle | green | Draft, checking, invalid, unsaved and saved/warning states are explicit; close/template/load actions guard unsaved changes. |
| Validation | green | Revisioned, debounced backend inspection; stale responses cannot overwrite newer edits; save/plan/run all use the same validation gate. |
| Node authoring | green | Role Task and Control Settings are generated from backend `FieldSpec`s and checked by one shared validator; execution/I/O/persona are separate layers. |
| Nested editing | green | Validation/export/save/run use a pure root snapshot and do not pop the author out of the active Group canvas. |
| Save/compose races | green | A late save acknowledges only its submitted snapshot; AI composition never overwrites edits made while it was running. |
| Edit history | green | One bounded snapshot controller owns undo/redo for graph, FieldSpec and Typed-I/O mutations; continuous text input coalesces by field, nested navigation is preserved, and the persisted fingerprint drives the dirty badge. |
| Canvas viewport | green | Zoom/fit is presentation-only; one viewport transform feeds drop/drag/port/edge geometry, while saved node coordinates and undo history stay in model space. |
| Frontend module size | amber | Ordered modules own the shell, panel-layout state, palette, Composer, backend contract, document/run/workspace state, FieldSpec and Typed I/O editors, graph/navigation/canvas mechanics, node cards, SVG edges, Inspector DOM/content, canvas-view composition and authoritative editor state. `orchestration.js` remains the integration coordinator and compatibility facade. |

The compact-shell pass also closes two viewport/accessibility gaps. At
769–1250px, low-priority toolbar copy collapses to accessible icon buttons,
the flow name/status footprint contracts, and actions remain a single row
instead of consuming the canvas with wrapped header lines. At phone widths,
the header is split into a fixed identity row and a left-origin, horizontally
scrollable action row. Nodes, Edit, Run and Save are the first reachable
actions; the remaining tools stay reachable by touch scrolling instead of
being stranded at a negative horizontal offset. Palette and Inspector sheets
no longer rely on off-screen transforms alone:
one Studio controller synchronizes their class, `aria-expanded`,
`aria-hidden`, `inert`, mutual exclusion and close-focus restoration. A sheet
that is visually closed therefore cannot retain keyboard focus or remain in
the modal's Tab sequence. Mobile sheet classes are discarded when returning
to desktop, so an old transient sheet cannot unexpectedly reopen after a
later resize. Sheet headers are explicitly mobile-only (the shared header rule
cannot override that visibility contract); the long Inspector keeps its close
header sticky while scrolling, and Palette guidance spans the sheet width.
Both Studio and Task Mode also reserve device safe-area insets on phone-width
headers, scroll surfaces, bottom sheets and floating Canvas controls. Their
compact icon controls and mobile tabs keep a 40px minimum target while the SVG
glyph stays visually small, and every generated shell button declares
`type="button"` so reuse inside a form cannot trigger an unrelated submit.
On desktop, `orchestration-panel-layout.js` alone owns Palette, Inspector and
Focus Canvas rail state. The toolbar can collapse either rail independently;
Focus Canvas remembers the previous one- or two-rail combination and restores
that combination on exit. The controller synchronizes
`aria-pressed`/`aria-expanded`/`aria-hidden`/`inert`, restores focus when needed
and asks the injected viewport adapter to resync; it never mutates graph,
document or undo state. Across the 768/769px boundary, ownership is explicit:
panel-layout writes rail accessibility state only on desktop, while Studio
writes it only on mobile and asks panel-layout to re-project desktop state
after a breakpoint transition. Either controller may therefore resync first
without reopening a rail the operator deliberately closed.
`orchestration-panel-state.js` is the lower-level presentation boundary shared
by that controller, Canvas Focus, Composer and the Run Drawer. It atomically
projects one expanded decision into class, `aria-hidden`, `inert`, trigger and
focus-restoration state; surface controllers now own policy without each
reimplementing accessibility mechanics.
`orchestration-scroll-state.js` is the matching bounded reading-position
primitive. Studio Inspector scopes offsets by workspace plus node/edge, while
Task Mode scopes them by durable run, active/pinned node and pending gate set.
Both surfaces therefore preserve position on routine projection refreshes,
start new contexts at the top, and share one capacity/eviction policy.
`orchestration-draft-state.js` applies the same bounded ownership model to
unsent human-gate text without placing request IDs or drafts in markup. Studio
survives a replayed request; Task Mode scopes drafts by durable run and gate.
Accepted, expired or removed gates clear their text, while a failed mutation
keeps it available for retry. If a synchronous projection replaces the active
textarea, the same binding contract restores focus and the bounded caret
selection without scrolling the Inspector away from its reading position.
`orchestration-single-flight.js` is the matching async-command ownership
primitive. Save, contract loading and write recovery share an active Promise;
keyed Task Mode writes reject duplicate callers with a neutral result. Promise
registration and `finally` cleanup therefore have one implementation while
each domain controller retains its own validation, result and UI policy.
Composer and Run expose the same transient-surface lifecycle port
(`isOpen`, `close`, plus `toggle` or `open`) to panel-layout. Mutual exclusion
and layered Escape dismissal therefore consume controller facts rather than
querying another module's CSS classes. The Run trigger participates in the
same projection with `aria-controls`/`aria-expanded`, and closing the drawer
restores the focus captured by the Run controller.

The 2026-08-10 extraction makes the document boundary physical, not merely a
commented convention. `createOrchestrationDocumentController()` owns its state
and receives snapshot/API/notification adapters from the editor. A thin global
compatibility facade preserves existing extension entry points without copying
the implementation. Bundle and development loading are both pinned to
`catalog → rich-copy → request-limits → popup-menu → shell → dialog → panel-state → scroll-state → draft-state → single-flight → studio → panel-layout → palette → write-recovery → wire-contract → result-core → inspection-result → outcome-result → mutation-result → definition-write-result → run-status → trace-contract → read-core → definition-read → authoring-read → runtime-read → http-read → api-request → request-contract → mutation-request → mutation-command → task-request → validation-request → document → history → session → feedback → export → composer-view → composer-request → composer → workspace-request → definition-request → store-browser → workspace → events → event-format → run-session → cursor-poller → run-request → run-drawer-view → human-gate-view → run → run-overlay → field-value → inspector → io-tools → io → contract-sections → contract-loader → contract → graph → graph-actions → navigation → viewport → canvas → canvas-interaction → edge-view → node-view → node-editor → inspector-content → inspector-view → canvas-view → editor-state → studio-api → editor → task-mode-shell → task-mode-run-store → task-mode-actions → task-mode-command-controller → task-mode-run-controller → task-mode-event-controller → task-mode-panel-layout → task-mode-list → task-mode-run-view → task-mode-node-presentation → task-mode-timeline → task-mode-graph → task-mode-inspector → task-mode`.
The inactive 300-line Studio CSS literal and the later 170-line Task Mode
runtime injector were removed from their controllers; `static/styles.css` is
now the only source as well as the only runtime owner for both operating
surfaces. Task Mode also honors reduced-motion preferences without a
JavaScript styling path.

The next 2026-08-10 tranche adds three more physical boundaries:

- `orchestration-events.js` is the pure event reducer shared by ephemeral
  Studio runs and durable Task Mode; active node, completed nodes, human gates
  and per-node traces no longer have two semantic implementations. As of
  2026-08-12 it also executes the backend `eventContract` capability policy:
  registered `reduce` flags gate state projection, while unknown future event
  types fail open so rolling upgrades remain observable.
  The same contract publishes distinct bounded wire/timeline preview limits;
  role, subflow and human-gate producers share `event_preview()`, while the
  reducer and formatter consume the matching browser projector instead of
  carrying their own 200/120 literals.
- `orchestration-event-format.js` is the matching pure presentation projector.
  The two run surfaces now share one event-to-localized-line vocabulary and
  one escaping boundary; they retain only their surface-specific DOM effects
  such as the Studio gate row and Task Mode graph/Inspector refreshes. Its
  timeline membership is likewise driven by the backend `timeline` flag, so a
  newly introduced detail-only frame cannot become an “unknown event” error
  row merely because an older frontend has no dedicated switch case. Studio's
  live controller adopts refreshed capabilities through one setter; Task Mode
  reads the same contract dynamically after its independent refresh.
- `orchestration-run-status.js` is the pure consumer of the backend
  `runContract`. Explicit `terminal` booleans on current snapshots remain
  authoritative; older persisted snapshots without that field fall back to
  the published terminal-status list. Missing contracts and unknown future
  statuses remain live, preventing an older browser from stopping its poller
  prematurely. Task Mode list rows, title actions, duration calculation and
  final resync therefore share one lifecycle predicate.
- `orchestration-trace-contract.js` is the pure consumer of the backend
  `traceContract`. The recorder publishes one status projection plus limit and
  truncation-flag maps for brief/input/output/thinking/error; the shared event
  reducer, Studio and Task Mode Inspectors now normalize
  `running/completed/failed` through that same policy instead of each mapping
  them to `running/done/error`. Both surfaces also use the same defensive text
  projector and no longer carry surface-specific clipping numbers.
  A bounded local map remains only as a rolling-deploy safety net.
- `orchestration-cursor-poller.js` owns cursor advancement, bounded retry,
  stale-response rejection, hidden-tab pausing and timer teardown for both
  ephemeral and durable viewers. A transient Studio polling failure no longer
  detaches from a still-running backend task after one request; both surfaces
  expose the same reconnect/recovered/offline lifecycle.
- `orchestration-run-session.js` owns the matching run identity, operation/read
  generations and poll-active state for both viewers. Studio late-start cleanup,
  Task Mode reopen/final-read races, close/delete invalidation and poll callback
  acceptance now use the same immutable ownership tokens instead of parallel
  sets of booleans and counters.
- `task-mode-run-store.js` owns durable list refresh generations, explicit
  load-error state, the selected full header and row lifecycle projection.
  Task Mode no longer lets list refresh, replay completion and mutation
  recovery write four independent globals. Stale list responses are rejected
  by store-issued owners, terminal/mutation status updates reach the row and
  selected header together, and successful deletes remove their row before
  the background list refresh completes.
- `task-mode-actions.js` owns Task Mode's per-run/per-gate single-flight locks,
  destructive-action confirmation boundary and shared mutation/task request
  clients. The main controller now projects canonical results into the UI and
  uses one selection teardown path for close, run switch, authoritative 404
  and selected-run deletion, preventing a reopened panel from retaining an old
  graph, timeline, final result or gate.
- `task-mode-panel-layout.js` turns the narrow-screen Task Mode from a long
  three-pane vertical document into explicit Runs / Run / Inspector
  workspaces. It reuses `setOrchestrationPanelState()` for `aria-hidden`,
  `inert` and focus restoration, while desktop keeps all panes visible. Run
  selection enters the Run workspace and a newly unresolved human gate opens
  the Inspector without repeatedly stealing the operator's chosen view.
- `task-mode-run-controller.js` is the durable read/replay seam. It alone owns
  run identity, read generations, cursor-poller lifetime, bounded reconnects,
  terminal replay snapshots, compatibility final reads and authoritative-404
  reset. The Task Mode surface consumes a finite transition vocabulary instead
  of issuing GET/event requests or mutating session generations from DOM code.
- `task-mode-command-controller.js` is the normalized write-result seam above
  `task-mode-actions.js`. It interprets gate, abort, rerun and delete outcomes
  once, then invokes injected reconcile/toast/selection callbacks. The surface
  no longer branches on backend mutation reasons or task-create response fields.
- `task-mode-event-controller.js` is the durable event projection seam. It owns
  reducer state, selected-node state, gate removal and the one replay-page fan
  out to Graph, Timeline, Inspector and lifecycle callbacks. Human-gate actions
  and replay rendering therefore cannot mutate competing copies of gate state.
- `lib/task_replay.py` owns the matching framework-free wire contract,
  `tofu.task-replay/v1`, for both `TaskRuntime` memory pages and durable
  orchestration event pages. Its contract declares `run` as an optional
  canonical snapshot on `done=true` pages, so durable consumers avoid a second
  header read while generic replay producers remain compatible. The producer
  is authoritative over the cursor:
  negative/untrusted values are normalized, a cursor beyond the current log
  boundary is explicitly returned as `cursor.reset=true`, and missing pages
  retain the same event/cursor/terminal fields. Durable persistence computes
  page rows and its boundary in one SQL statement, so a concurrent append can
  land on this page or the next but cannot be acknowledged without being
  replayable. The browser accepts backward movement only for an explicit reset
  (plus the bounded rolling-server compatibility case), prevents malformed
  pages from rewinding behind delivered events, and commits terminal-page
  cursors before stopping. A corrupted browser cursor therefore cannot skip
  all future events permanently, and live/durable adapters no longer publish
  different not-found or replay shapes.
  Durable terminal pages additionally carry the canonical `run` snapshot that
  `OrchestrationRunService.replay()` already read to decide `done`. Task Mode
  renders final/error state from that snapshot without an immediate second GET;
  the GET remains only as a rolling-backend compatibility fallback. Active
  replay pages retain the generic task-replay shape.
- `orchestration-result.js` is the 104-line generic result core: it normalizes
  string, list, field-keyed and typed envelope failures plus `{ok,status,data}`
  without importing a domain vocabulary. Four independently loadable sibling
  projectors now mirror the backend module boundaries for inspection,
  terminal outcome, mutation and definition-write/CAS results. The inspection
  projector owns canonical-vs-injected normalizer selection for Document,
  Composer and Workspace, so those controllers cannot silently invent
  different diagnostic fields when a dependency is missing; Task Mode can
  load outcome behavior without inheriting mutation or authoring policy.
  `orchestration-definition-read.js` projects persisted Definition CRUD while
  `orchestration-authoring-read.js` owns Composer, Authoring Contract,
  builtin, layout and validation projections;
  `orchestration-runtime-read.js` owns Plan/Run/Task reads without growing the
  domain projector again. `orchestration-validation-request.js`,
  `orchestration-composer-request.js`, `orchestration-run-request.js`,
  `orchestration-workspace-request.js` and
  `orchestration-definition-request.js` are endpoint-specific clients over the
  same `orchestration-request-contract.js` adapter over the lower
  `orchestration-api-request.js` invoker. The adapter reads API method discovery,
  injected normalizer names and HTTP projector selection from the core
  `api/orchestration-endpoints.js` registry; leaf clients own only semantic
  endpoint ids and their domain arguments. Controllers no
  longer repeat normalized-method preference, direct-method rolling
  compatibility or thrown exception projection. Each Definition, Authoring
  and Runtime read module explicitly registers only its own projector names;
  the registry rejects duplicate ownership and a Task-only consumer no longer
  has to load unrelated Definition or Composer code. The validation client still
  gives an injected
  extension callback explicit priority; a transport
  failure does not advance `validatedRevision`, so the same graph can retry
  instead of being cached as invalid. The Run request client also preserves
  cleanup of a task ID returned after the user already cancelled the start.
  `orchestration-mutation-request.js` sits alongside those endpoint clients and
  projects ephemeral abort, human gates and durable abort/delete through the
  backend's single mutation contract for both Studio and Task Mode.
  `orchestration-mutation-command.js` is the next shared boundary: it catches
  transport exceptions, reports causes, resolves failure copy and exposes
  authoritative target absence once so surface controllers only project UI.
  `orchestration-task-request.js` owns durable list/get/create/event selection
  and response projection. Task Mode and Studio's **Run as Task** handoff now
  share that client, including normalized-method preference, direct-read
  compatibility, 404 classification and transport-cause diagnostics.
  On the backend, `spawn_runtime_flow()` is the matching live/durable start
  interface: TaskRuntime creation, abort wiring, worker construction, spawn
  and the durable/runtime ID invariant are implemented once. Subflow resolver
  lookup remains late-bound inside the worker, preserving configuration/test
  replacement semantics. Runtime and durable-read HTTP adapters now supply only
  request metadata and persistence dependencies; all state transitions are
  registered by the sibling mutation adapter.
  `orchestration-api-request.js` is the lower shared invoker for endpoint
  clients: normalized-method preference, direct-method compatibility, argument
  selection, exception projection and raw diagnostics are implemented once;
  Response-like adaptation delegates to `api/http-result.js`.
  `api/orchestration-endpoints.js` is the only mapping from semantic endpoint
  ids to routes, verbs, request shapes, Api method pairs and response
  projectors; `orchestration-request-contract.js` only adapts that frozen core
  registry to the deferred request invoker. Endpoint clients never call `.json()`, reconstruct
  `{ok,status,data}`, or declare `resultMethod`/`directMethod` themselves.
  `orchestration-http-read.js` publishes the single projector registry used by
  that contract layer.
  Explicitly injected normalizers still take priority for extensions and
  tests, while built-in clients contain no local semantic fallback; an unknown
  projector fails at the shared boundary instead of silently returning a
  reduced shape whose fields or failure reason can drift from the backend.
  Plan, ephemeral run, durable handoff, save and AI composition therefore
  share one error-copy seam and cannot mask a backend rejection with a local
  `.join()` type error during rolling deployments. Its matching success
  predicate checks both HTTP success and an inner logical `ok`, while retaining
  the old boolean result during rolling upgrades. All orchestration write
  operations now return the same normalized API result instead of mixing raw
  JSON, `Response` and booleans. The same module projects final text, partial
  state and terminal copy; Studio's ephemeral Run Drawer and durable Task Mode
  therefore label an unverified result identically instead of one calling it a
  generic result while the other calls it partial.
- `orchestration-feedback.js` is the shared safe toast/warning view used by
  Studio controllers and Task Mode. Validator issue text, severity styling
  and dwell cleanup now have one DOM implementation behind compatibility
  facades.
- `orchestration-run.js` owns start/poll/abort, durable handoff and gate
  mutation orchestration. `orchestration-run-drawer-view.js` owns drawer
  accessibility/focus, input seed fallback, log rows and the common action
  lock. `orchestration-human-gate-view.js` separately owns safe gate DOM,
  bounded multiline input, keyboard submission and per-request control locks;
  neither view knows API or mutation outcome shapes.
  `orchestration-panel-layout.js` coordinates the Studio work surfaces:
  independent desktop rails, canvas focus and mutually exclusive Composer/Run
  drawers. Hidden rails and drawers are inert,
  and at 769–1250px the Composer overlays instead of consuming a third fixed
  column, preserving usable graph width on tablets and small laptops.
  Panel-layout receives Composer and Run as one lifecycle-port shape and never
  inspects their DOM to infer whether a surface is open.
  All four Studio surface owners delegate their DOM accessibility projection
  to `orchestration-panel-state.js`, including focus-before-inert closure.
  Plan preview, ephemeral start and durable start share one controller-level
  pending state: all competing action controls and the run input lock together,
  while the live log exposes `aria-busy`. Human-gate cards retain independent
  per-request locks so an active run can still receive its required response.
  Poll failures release controls, and a task id that arrives after local
  cancellation is immediately aborted instead of orphaned.
  Ephemeral polling no longer calls `Api.runPoll` from the controller: the Run
  request client prefers `runPollResult`, projects the canonical replay page
  through the same HTTP-aware adapter as durable events, and retains the direct
  method only for rolling compatibility. On the backend,
  `task_replay_http_status()` is the single success/not-found/failure mapping
  used by both generic in-memory polling and durable replay routes.
  Abort/approve/input do not call `Api` or normalize mutations locally; their
  request facade delegates to the shared Studio/Task Mode mutation client.
- `orchestration-run-overlay.js` projects reducer state onto Studio node-card
  statuses and the selected-node trace. It consumes the reducer's canonical
  `{nodeId, nodeStatus, terminal}` change instead of switching on raw event
  types; every event reaches the overlay once, so a graph+trace transition can
  only refresh the Inspector once while transport remains DOM-agnostic.
- `run_status.py` owns the initial, valid and terminal durable-run vocabularies.
  The service rejects programmer typos before persistence, the store fences
  direct low-level writes, and the HTTP list filter returns a typed 400 with
  the same published status choices instead of silently creating drift.
- `OrchestrationRunService.create_new()` owns run-id allocation plus header
  creation, and every persistence exception (create/read/list/append/status/
  delete/recovery) crosses the same `RunServiceError` boundary. HTTP no longer
  assembles durable creation primitives or leaks a store-specific exception.
- `run_store_port.OrchestrationRunStorePort` is the complete structural
  persistence interface below that service. Composition validates all required
  operations once, and replay always consumes the atomic `get_event_page()`
  contract; application logic no longer probes optional store methods or
  carries a second legacy cursor algorithm.
- `routes/api_v1/orchestrations.py` is now a thin composition root. Focused
  definition, ephemeral-runtime and durable Task Mode adapters register onto
  its shared Blueprint and receive late-bound definition/run services plus the
  shared resolver/runtime. No adapter constructs a second repository, while
  `orchestration_route_ports.py` defines structural authoring/resolver/
  definition/run provider protocols without importing implementations. Its
  write, delete, replay and mutation result ports also make return values part
  of that boundary instead of weakening replaceable services with `Any`, and
  `orchestration_service_http.py` maps every expected application-service
  failure consistently while its generic result channel preserves those
  structural types through the HTTP error projector. `orchestration_run_http.py` likewise owns
  the one ephemeral/durable run-start preparation path, so validation,
  canonical FieldSpec values, input limits and source IDs cannot drift between
  Studio Run and Task Mode. Save and run rejection paths all call
  `orchestration_definition_http.invalid_definition_response()`, preserving
  one canonical inspection plus the same rolling errors/warnings fields.
  Authoring role lookup and durable-run filters both use the shared
  `request_parser.query_str()` mapping contract, so query type/default/trim
  behavior is no longer reimplemented by individual adapters.
  The resolver port now carries the full `ResolvedDefinition` domain result
  instead of flattening it to a dict; plan/layout/run responses and run logs
  therefore retain whether the executed snapshot was inline or stored.
- `lib/orchestration/_control_specs.py` is the physical backend owner of the
  control-kind catalogue, new-node defaults, control `FieldSpec`s, derived
  public value sets and field validation. `_defaults.py` only returns detached
  copies from that catalogue; the authoring contract, JSON wire schema and
  whole-graph validator also import it directly, so Inspector controls,
  defaults and accepted backend values cannot fork across parallel key tables
  or inside the former monolithic `_validate.py`.
- `lib/orchestration/_definition_contract.py` owns the schema identifier,
  ordered node types, definition-name cap and node-count cap. Builders,
  Composer, inspection, authoring responses, JSON wire/OpenAPI schemas and the
  validator import it directly, so backend acceptance and Studio limits do not
  depend on importing a validation implementation module.
- `lib/orchestration/_execution_projection.py` owns role brief rendering,
  first-reachable-role traversal and opening chat-phase classification. The
  engine, agent runner, definition inspection, chat task start and endpoint
  adapter consume this pure module directly; the adapter also imports its
  planner vocabulary, removing the second phase-classification table that
  previously lived beside event projection.
- `lib/orchestration/_topology_diagnostics.py` owns non-blocking execution
  hazard analysis, beginning with order-dependent verdict-channel use inside
  parallel regions. It shares `VERIFIER_ROLES` with default `emits`, the
  engine and endpoint event projection, replacing four independently
  maintained critic/reviewer/virtual-user sets with one role-axis vocabulary.
- The former `_roles.py` is now a compatibility facade over three physical
  owners: `_role_axes.py` defines known roles, execution options and fallback
  emits/scope semantics; `_role_specs.py` defines role FieldSpecs, bounds and
  validation; `_role_personas.py` projects the read-only swarm persona
  registry. Authoring, validation, graph execution and built-in templates
  import only the contract they consume, so prompt/catalogue changes no longer
  couple execution routing to Inspector field validation.
- `lib/orchestration/human_gate_runtime.py` owns the execution-side gate
  request lifecycle through injected approval/guidance ports, including stable
  events, timeout normalization and abort-aware guidance adaptation. This is
  the symmetric counterpart to mutation-side `human_gate_service.py`:
  `FlowExecutor` no longer imports task registries or implements a second wait
  protocol, and isolated subflows inherit the same ports explicitly.
- `lib/orchestration_budget.py` owns the atomic agent-start ceiling for an
  entire run tree. Parent graphs, concurrent branches and isolated nested
  executors share one budget object, while each executor retains its local
  transcript/count projection. A nested flow therefore cannot reset
  `max_agents` and oversubscribe the root run before the parent folds counts.
- `lib/orchestration_transcript.py` owns thread-safe completed-turn recording,
  verifier lookup and the role/subflow context projections. Engine results,
  loop verdict handling and replan summaries now read snapshots from this one
  ledger instead of traversing a mutable executor list independently.
- `lib/orchestration/_subflow_contract.py` owns the five-level nesting cap and
  embedded/reference node rules. Validator recursion is injected as a pure
  callback, while builder expansion, runtime isolated-subflow execution and
  the authoring wire limit import the same cap directly. `_validate.py` keeps a
  thin compatibility wrapper instead of owning a second recursion policy.
- The former `_build.py` is now a compatibility facade. Canonical Studio/chat
  templates live in `_builtin_definitions.py`, nested-role presentation mode
  lives in `_chat_projection.py`, and inline Group macro expansion lives in
  `_subflow_expansion.py`. Authoring, inspection, plan compilation and chat
  adapters import the focused owners directly, so changing one concern does
  not reload or couple the other graph transforms.
- `orchestration-inspector.js` converts backend `FieldSpec` contracts into
  escaped, data-marked controls. Backend field keys are never interpolated into
  executable attributes. Default role/control text and list caps are now
  materialized into each backend FieldSpec instead of remaining hidden
  validator arguments; the Inspector displays those limits, applies native
  `maxlength`, and retains exact list count/item bounds as data. The authoring
  controller exposes one detached `fieldSpec(ownerType, ownerName, key)` lookup;
  `orchestration-inspector-view.js` binds the controls locally and sends
  `{nodeId,key,value,isNumber,kind}` through one update seam.
  The Authoring Contract read now treats request limits as required wire data
  (including each limit used by Studio/Task Mode), so a partial response
  cannot silently release the palette with client-side bounds. Role/control
  FieldSpec and persona accessors return detached values, preserving the
  controller's immutable-snapshot boundary for every consumer.
  On phones, Palette/Inspector bottom sheets now share an explicit scrim
  close command; the same panel-state projector makes the obscured canvas
  inert and aria-hidden until the sheet closes, preventing accidental graph
  edits and background focus/reader traversal. Composer and Run Drawer expose
  visibility-only ports to Studio, which applies that same canvas isolation
  while either full-width mobile work surface is open. Those surfaces retain
  their own close/focus controls and do not acquire a second scrim or write
  background accessibility state themselves.
  Parallel width and branch count now come from graph edges; the misleading
  `max_concurrent`, `per_item` and `branches` controls were removed because the
  interpreter never consumed those params.
- `orchestration-inspector-content.js` owns headers, collapsible sections,
  run traces, persona presentation and control-flow summaries. Persona prompts
  have no browser fallback catalogue: the authoring contract is the only
  behavioral source. Trace output likewise consumes the detached backend
  `traceContract`, including status projection, the backend truncation flag and
  a shared visible truncation label. `orchestration-inspector-view.js` preserves expansion per
  workspace + node across parameter rerenders and delegates bounded scroll
  restoration to the shared scroll-state primitive; implicit text-only I/O stays
  collapsed while explicitly declared ports open for author attention.
- `orchestration-node-editor.js` is the single Inspector-to-graph mutation
  seam for node names, subflow roles and typed params. List normalization,
  FieldSpec text/list bound checks, optional-key omission, numeric coercion,
  history coalescing and refresh policy therefore cannot drift across controls,
  extension calls or DOM surfaces.
- FieldSpec values now have a matching end-to-end
  `tofu.orchestration.field-value/v1` contract. The backend's
  `prepare_definition()` performs inspection once and canonicalizes only a
  successful definition; stored CRUD, AI composition, ephemeral Studio runs
  and durable Task Mode snapshots therefore share the same accepted executable
  value shape. Known list values persist only as trimmed `array<string>` even
  when a REST client submitted a newline string or tuple, empty optional values
  are omitted, unknown/infra values remain forward-compatible, and embedded
  Group definitions are normalized recursively. The browser half lives in
  `orchestration-field-value.js`: renderers emit one FieldSpec `kind` plus its
  backend-authored bounds, the node editor delegates all draft conversion to
  the codec, and the codec dynamically consumes the detached
  `fieldValueContract` for wire-kind, newline-list, trim/drop and optional-empty
  policy. Unknown contract versions or unpublished kinds fail before graph
  mutation instead of being guessed into the current format. Non-JSON numeric
  values cannot silently collapse to `null`, rejected values are exposed through
  `aria-invalid`, and semantic no-op edits no longer create dirty/history or
  rerender churn.
- `orchestration-contract.js` owns the browser's strict, detached authoring
  snapshot and accessors, not durable lifecycle semantics. Run headers carry
  the backend-projected `terminal` boolean and mutation envelopes carry
  `resource_terminal`; Task Mode consumes those facts first. Legacy run
  snapshots missing the boolean use `orchestration-run-status.js` and the
  detached `runContract` accessor, while a rolling mutation response missing
  `resource_terminal` still causes a fenced authoritative reread. Opening Task
  Mode refreshes the authoring contract in parallel with its run list so the
  lifecycle vocabulary and role/control presentation stay current.
  `authoring_object_sections()` is the backend constructor for all 20
  object-policy documents, including Typed I/O; its ordered name registry is
  pinned exactly to `orchestration-contract-sections.js`, the browser's
  data-driven immutable store. One declared key list per side now drives
  adoption, detachment, snapshots and readiness, including `eventContract`
  and `runContract`; adding another section no longer requires parallel state,
  apply, getter and ready branches in the controller.
- The same boundary now executes `definitionWriteContract` instead of merely
  storing it. Workspace injects one detached policy into the shared definition
  request client; the endpoint registry derives the optimistic precondition
  header/token and allowed replace/delete operations from it, while the shared
  write projector derives conflict HTTP status, reason, format and operation
  from the same policy. Unsupported token syntax and required-but-missing
  versions fail before a request is sent. Rolling clients without the contract
  retain the published v1 fallback, but there is no second Studio-specific
  `If-Match` or 409 classifier.
- The remaining authoring contracts are executable browser policy, not
  metadata. `outcomeContract` supplies accepted terminal categories and shared
  final/error display bounds to Run Drawer, Task Mode and event formatting;
  `traceContract` supplies status projection, text bounds and truncation flags
  to the reducer and both Inspectors;
  `mutationContract` supplies retry reasons plus reconcile/existence/terminal
  field names to the shared write projector; `replayContract` supplies event,
  terminal and snapshot fields to both live and durable poll reads; and
  `inspectionContract` supplies diagnostic severities to validation, save and
  Composer projections. `definitionListContract` supplies metadata projection
  and canonical ordering, `definitionEntryContract` supplies the CAS version
  field for read/save/delete, and `runtimeStartContract` supplies identity and
  legacy-ID field mappings for ephemeral and durable starts. The strict
  authoring reader requires, shape-checks and version-checks every published
  section;
  detached accessors cross one controller, missing response formats retain
  rolling compatibility, and explicit future formats fail closed at the
  shared wire registry.
- `orchestration-shell-commands.js` is the one validated toolbar command
  adapter. The Shell speaks stable action names while Studio, workspace,
  history, viewport, Composer and Run controllers remain separate ports;
  missing controller capabilities fail during composition instead of leaving
  a silently dead panel button.
- `wire_contracts.request_limits_contract()` is the single owner of definition
  name/node-count, Composer, run-input and human-input size limits. The definition schema,
  corresponding HTTP schemas and parsers import those constants, while
  `orchestration-request-limits.js` projects the published limits onto Studio
  and Task Mode controls. The Studio title field therefore stops at the same
  120-character boundary that validation and OpenAPI advertise, instead of
  accepting a value that can only fail later on save. Rolling
  clients safely omit `maxlength` when an older server has not published the
  additive field instead of inventing a competing browser default.
  Graph Actions consumes the matching backend-published 200-node `maxItems`
  boundary at its single add-node seam, so palette clicks, taps and drops all
  reject consistently before allocating an ID or dirtying the document.
  The same seam combines the published five-level subflow `maxDepth` with the
  navigation stack. A Group can still be added at every valid level, but the
  first definition that would exceed the executor/validator recursion bound is
  rejected before it enters history; imported or rolling-client definitions
  remain protected by backend validation.
  Human input also has one required-value rule: OpenAPI requires a non-empty
  `response`, HTTP ingress rejects blank text, and both surfaces prevent blank
  submission before making a request.
  Composer history uses the same contract: the backend publishes and retains
  the newest eight entries, new clients upload only that window, and rolling
  clients may still send longer histories that ingress trims compatibly.
  `current` and every `history` item are type-checked instead of silently
  degrading malformed request data to an empty edit context.
  Studio Run and Task Mode human-response controls are bounded multiline
  editors: Enter submits, Shift+Enter inserts a newline, and pending mutations
  lock the textarea, matching the long-form input the backend contract permits.
  On compact Task Mode layouts, selecting a graph node is also the navigation
  command that reveals its Inspector; the newly projected trace is never left
  hidden behind a second, implicit tab switch.
  Studio Composer and Run inputs now match those long-form backend limits in
  presentation as well: both are vertically resizable within bounded panel
  space, and their input sections become independently scrollable in short
  viewports instead of clipping the action controls. The Run textarea is
  explicitly described by its mode guidance and the action row is a named
  control group.
- `orchestration-io-tools.js` owns backend `ioContract` adoption, defaults,
  caps, presets and immutable Typed-I/O edits. The same contract now publishes
  required and same-side-unique port-name rules, so Studio rejects an invalid
  draft before save while rolling clients and servers retain additive fallback.
- `orchestration-io.js` owns only Inspector rendering and local bindings. Port
  controls carry fixed data actions, while the node ID stays in a closure;
  add/remove/set/preset and edge bindings all converge on the pure I/O tools.
- `orchestration-graph.js` owns pure connection constraints, cascading deletes,
  definition serialization and non-destructive nested root snapshots.
- `orchestration-definition-snapshot.js` is the single live definition read
  port for Studio. Save, validation, export, Composer and both run modes all
  consume the same current-level/root projection, so an active nested Group is
  folded back into its parent exactly once instead of being reconstructed by
  each feature.
- `orchestration-graph-actions.js` is the sole structural-mutation seam for
  node/edge creation, reversal, cascading deletion and mutually exclusive
  node/edge selection. It composes the pure graph result with document
  dirtiness, localized rejection feedback and the minimum view refresh; Canvas,
  keyboard and Inspector commands therefore cannot drift into separate edit
  policies.
- `orchestration-navigation.js` owns Group enter/exit/root-collapse transitions
  and breadcrumb DOM. Depths are clamped before traversal, and breadcrumb
  actions use local listeners instead of executable attributes.
- `orchestration-canvas.js` owns viewport coordinate conversion, node clamps,
  port fan-out and SVG curve routing. Palette drops, touch/click-to-add,
  dragging and edge rendering now share the same geometry.
- `orchestration-viewport.js` owns zoom, fit-to-all, scroll extent and the
  presentation transform. Canvas controls live in a compact floating group
  rather than the already dense top toolbar. `Ctrl/Cmd+wheel` zooms around the
  pointer; ordinary wheel scrolling remains native. Scene scale/offset are
  injected into Canvas Geometry, so ports, temporary connections, drops and
  node drags remain aligned without rewriting persisted node coordinates.
- `orchestration-canvas-interaction.js` owns transient drag/connect state and
  one-time Canvas DOM wiring. Pointer and keyboard connections share one state
  machine; starting a node drag atomically selects that node and clears a
  previously selected edge.
- `orchestration-contract-loader.js` owns single-flight Authoring Contract
  transport, retry state and the only legacy-route decision: an explicit 404
  may use `/role-schema`, while 5xx, network loss and malformed 200 responses
  keep the palette locked and retain a typed diagnostic for retry. The older
  direct-body API remains a rolling-extension compatibility path. Method
  discovery, invocation and exception projection now use the same request
  invoker as every other Studio endpoint client; only the route-fallback policy
  remains loader-specific.
  `orchestration-contract.js` owns catalogue merge and typed access above the
  immutable `orchestration-contract-sections.js` registry; backend roles,
  personas, FieldSpecs, execution options, Typed I/O and node defaults enter
  through that one boundary. The role catalogue
  is presentation-only and carries no model tier; the palette remains in an
  explicit `aria-busy` loading state until the controller has settled on either
  the backend contract or its one rolling-deploy fallback. A fast click during
  startup therefore cannot persist a stale frontend-owned execution default.
- `orchestration-composer.js` owns AI chat state, single-flight requests and
  result adoption, `orchestration-composer-request.js` owns its API boundary,
  and `orchestration-composer-view.js` is the only owner of its DOM, ARIA,
  controls and focus timer. Closing the panel now cancels a
  pending focus, localized empty-state markup is restricted to emphasis, and
  all conversation content is emitted as text. Clearing invalidates the old
  request epoch, while document revisions independently prevent a valid but
  stale graph from overwriting concurrent edits. `Api.orchestrations.composeResult`
  preserves `{ok,status,data}` at the shared HTTP seam; the result normalizer
  keeps network failure, 5xx unavailability, 4xx rejection, malformed 200 and
  a valid `200 {ok:false}` graph response distinct. The legacy direct-body
  `compose()` remains available for extensions. The HTTP route calls the
  injected `OrchestrationAuthoringService.compose()` interface rather than
  importing either the compatibility facade or LLM implementation directly.
- `orchestration-store-browser.js` owns the saved-flow menu DOM, localized
  metadata timestamps, current-row semantics and request
  generation fence. Raw IDs remain in listener closures, and the controller
  emits only `onLoad(id)` / `onDelete(id,event,updatedAt)` commands; it cannot
  read or mutate the active graph. Row commands enter a real disabled,
  `aria-busy` state until their promise settles, preventing double-clicked
  load/delete requests while preserving retry after a rejected command.
- `orchestration-workspace-request.js` is the builtin/layout transport seam.
  It prefers `builtinResult()` / `layoutResult()` and their stable
  `{ok,status,data}` envelope, while retaining direct-body methods for rolling
  extensions. Its semantic projection keeps 404, 4xx validation rejection,
  logical rejection, 5xx, transport loss and malformed 200 responses distinct;
  thrown extension requests enter the same result shape instead of leaking a
  second exception-only path into Workspace.
- `orchestration-definition-request.js` is the single persisted-definition
  client shared by Store Browser and Workspace. Its `list/get/save/remove`
  methods prefer normalized APIs; the common invoker adapts legacy POST/PUT
  `Response` objects and returns the same semantic reason/conflict/error shape.
  Neither controller nor endpoint client now
  probes `Api` capabilities, parses JSON responses or interprets CAS payloads.
- `orchestration-workspace.js` owns builtin loading, backend layout, save/update
  and stored-definition load/delete behavior. It accepts canvas snapshots and
  mutation callbacks instead of reaching into graph globals. Stored record IDs
  stay in event-listener closures, create and update capabilities are checked
  independently, and late saves acknowledge only their submitted revision.
  Layout responses are guarded by both document revision and active Group path,
  so a root-layout response cannot move same-named nodes after the author has
  navigated into a child canvas. Initial auto-layout synchronizes the current
  history entry without manufacturing an unsaved edit.
  Builtin/layout and persisted CRUD failures retain backend diagnostics and
  otherwise map the semantic failure reason to shared localized copy. The
  definition request client uses
  `Api.orchestrations.save(id, definition, expectedUpdatedAt)`, whose normalized
  `{ok,status,data}` result hides POST/PUT and raw `Response` differences and
  applies the shared `If-Match` version precondition. Loading and successful
  saving update one root-document version; opening a builtin or deleting its
  stored source clears it, while Composer edits of the same root preserve it.
  Stored-list delete actions pass the listed entry version through the same API
  helper; stale delete conflicts preserve the draft and refresh the list.
  Each stored row now shows node count plus localized relative update time,
  with the absolute local timestamp on its semantic `<time>` tooltip, so an
  author can distinguish similarly named versions before loading or deleting.
  The store browser highlights the current definition with `aria-current`, and
  node counts consume the metadata summary while retaining a full-entry fallback
  during rolling upgrades. Definition loads have their own latest-selection
  generation plus document-revision/identity fence: an out-of-order GET or a
  response arriving after a new canvas edit cannot replace newer local state.
  Save is likewise a single-flight command, so keyboard/programmatic re-entry
  shares one mutation instead of racing duplicate PUTs against the same
  `updatedAt` token and producing a false conflict badge.
  `Api.orchestrations.listResult()` preserves HTTP status and error bodies at
  the shared request boundary, so an unavailable store renders a failure state instead of
  being collapsed into “no saved flows”; the older array-returning `list()` is
  retained as a compatibility facade for lightweight toolbar pickers.
  Item reads, saves and deletes follow the same rule: shared projectors
  distinguish a real 404 or CAS conflict from 4xx rejection, 5xx, transport
  loss, logical rejection and malformed success before either UI surface
  chooses copy or changes document state. Older direct-body list/get and raw
  create/update responses remain rolling-extension compatibility facades.
- `orchestration-session.js` owns the active root definition's persisted ID,
  `updatedAt` CAS token and definition-adoption policy. Workspace saves/deletes,
  stored loads, builtins, Composer edits, Run and conflict recovery all read or
  update that one session interface. Same-document Composer adoption preserves
  its version; switching documents or opening a builtin clears it; stored loads
  adopt the backend token while resetting navigation/history/view through one
  injected transition with no transport dependency.
- `orchestration-popup-menu.js` owns popup visibility and `aria-expanded`
  synchronization, live menu-item discovery, arrow/Home/End navigation and
  Escape/Tab focus restoration. Template and stored-flow menus share the same
  controller, outside toolbar/canvas actions close both without stealing
  focus, and Studio Escape invokes one `closeAll()` port instead of knowing
  menu IDs. The async stored-flow command returns its promise through the shell
  so keyboard focus moves only after backend rows have rendered. The workspace
  marks that request `aria-busy`, announces its state, and fences response
  generations; a list that settles after the menu closed cannot repaint or
  move focus into the hidden surface.
- `orchestration-palette.js`, `orchestration-shell.js` and
  `orchestration-node-view.js` own their DOM surfaces and local event binding.
  Palette now keeps a local, rerender-stable search query across backend role,
  control and Group catalogues; unmatched categories leave both the visual and
  accessibility tree, and a live empty result replaces a long blank rail.
  Filtering never mutates the backend catalogue or the shared add payload.
  The shell receives one explicit `commands` interface from the editor and has
  no executable HTML attributes or global-handler coupling. Loaded node IDs no
  longer enter inline event attributes; node ports are real focusable buttons
  and support Enter/Space wiring plus Escape cancellation. Palette avatar
  failures are also local one-shot listeners; the Studio/Task Mode authoring
  surface contains no executable event attributes.
- `orchestration-rich-copy.js` is the one formatting policy for intentional
  emphasis in localized Studio/Task Mode guidance. It permits only bare
  `b`, `i` and `code` tokens (normalizing `i` to semantic `em`) and escapes
  every other tag, attribute and metacharacter. Shell, Inspector hints,
  Canvas/Composer empty states and the Task Mode empty state consume this
  injected interface, so translations neither leak literal tags nor become
  unrestricted HTML.
- `orchestration-studio.js` owns lazy modal mounting, guarded open/close,
  mobile Palette/Inspector sheet exclusivity and document-level Escape/Delete
  policy. Selection, graph mutations, popups and discard checks are callback
  ports, so the controller has no authoring state. Opening moves focus into the
  modal dialog and closing restores the trigger focus. It also owns the
  cross-platform `Ctrl/Cmd+Z`, `Ctrl/Cmd+Shift+Z` and `Ctrl+Y` policy while
  leaving native text-field undo untouched. Tab/Shift+Tab are trapped within
  visible dialog controls; hidden menus, Composer and Run Drawer content are
  excluded through synchronized `aria-hidden` state. Canvas zoom shortcuts
  (`Ctrl/Cmd` + `+`, `-`, `0`) call the same viewport commands as the buttons.
  Escape consumes one layer at a time—connection, popup, Run/Composer, then
  mobile sheet—through controller ports rather than surface-specific global
  handlers. Composer also has its own accessible close action, which remains
  reachable when the panel covers the mobile work area. The same controller is
  the sole mobile background-policy owner: sheet classes plus Composer/Run
  visibility notifications resolve to one canvas `inert`/`aria-hidden`
  projection, while the sheet scrim remains independent.
- `orchestration-dialog.js` is the shared focus lifecycle used by Studio and
  Task Mode: trigger capture/restore, visible-control discovery and Tab/Shift+Tab
  containment now have one implementation. `task-mode-shell.js` owns the
  operating room's lazy DOM, delegated stable actions and Escape/backdrop close.
- The Studio Run Drawer now lives inside the flexing `.orch-body`, so its
  absolute bounds follow the real body height instead of a hard-coded 57px
  toolbar guess. The toolbar is a named accessibility group; opening the
  drawer focuses its input and closing it restores the invoking control.
  Toolbar/document/drawer/viewport controls share an explicit focus ring, and
  expanded menu/Composer triggers expose the same accent state visually as
  through `aria-expanded`.
  Studio drawers, menus, node state pulses and canvas transitions also honor
  `prefers-reduced-motion`, matching the Task Mode accessibility policy.
- `orchestration-history.js` owns a bounded, detached workspace+Group-stack
  history. Graph fingerprints are separate from transient selection/navigation,
  so undoing to the submitted save snapshot clears the dirty badge even when
  focus changed. Field text and Typed-I/O name edits coalesce by semantic key;
  add/delete/connect/reverse/drag/layout/AI adoption remain discrete operations.
  Its `captureCurrent`/`recordCurrent`/`syncCurrent`/`resetCurrent` and
  `undoAndApply`/`redoAndApply` interface also owns the workspace capture and
  restore choreography, so navigation, layout, loading and keyboard shortcuts
  cannot assemble subtly different history transitions in the editor.
  A save marks its request-time checkpoint, not whichever graph happens to be
  current when the response arrives, and deleting the stored source detaches
  that baseline explicitly.
- `orchestration-export.js` is the one definition-download interface used by
  both the toolbar and save-conflict recovery. It owns path-safe filenames,
  pretty JSON serialization, temporary anchors, delayed object-URL revocation
  and failure feedback, so recovery never reloads after a failed backup.
- `orchestration-edge-view.js` renders geometry routes as focusable SVG
  controls, and `orchestration-inspector-view.js` composes selected node/edge
  panels from the FieldSpec/I/O providers. Imported node and edge IDs remain in
  event-listener closures rather than executable attributes.
- `orchestration-canvas-view.js` is the single ordered refresh seam for the
  flow name, node cards, viewport, SVG edges, Inspector, empty state and Group
  breadcrumb. Empty-state translations are inserted as text, and switching to
  a non-empty graph removes stale guidance DOM instead of merely hiding it.
- `orchestration-editor-state.js` is the single owner for the active graph,
  selection, ID sequence, flow name and nested workspace stack. Controllers
  consume its explicit accessors and atomic graph/workspace operations. The
  old `_orch*` globals are accessor-backed compatibility aliases to that same
  state, so extensions and diagnostics cannot mutate a disconnected copy.
- `orchestration-studio-api.js` is the explicit cross-feature capability port
  for opening Studio, refreshing its backend contract, loading a persisted
  definition and showing shared feedback. Task Mode consumes that one object
  rather than probing independent globals. Its `openDefinition()` command
  suppresses the empty-canvas default bootstrap before loading the requested
  record, so a late builtin response cannot overwrite Edit-in-Studio.
- The backend authoring-contract controller is constructed immediately after
  its I/O contract dependency and before Palette, NodeView, Inspector or Studio
  consumers. Those surfaces never observe a temporary `null` contract owner;
  its change callback is also safe while Studio assembly is still in progress.
- Studio and Task Mode human-gate cards likewise keep backend request IDs in
  controller closures. Approve/reject/input buttons carry only fixed action
  markers, so opaque IDs cannot become inline JavaScript.
- `task-mode-list.js`, `task-mode-run-view.js`, `task-mode-graph.js` and
  `task-mode-inspector.js` own Task Mode's list, active-run header/result, graph
  and Inspector presentation. `task-mode-node-presentation.js` is their shared
  catalogue projection for node labels, subtitles, accents and icons, so graph
  and Inspector cannot drift into different node-kind rules. Inspector reading
  position survives trace refreshes within one run/node/gate scope, while a new
  run, selection mode or pending gate set begins at the top.
  `task-mode-timeline.js` owns formatted event rows, busy state and scroll
  anchoring while the controller retains reducer-driven refresh decisions.
  The controller retains request generations, polling and DOM effects only;
  durable reads and creation go through `orchestration-task-request.js`, while
  state changes go through `orchestration-mutation-request.js`. The run list exposes live
  loading state through `aria-busy`, announces the selected row with
  `aria-current`, and sanitizes backend status names before using them as CSS
  tokens; graph topology and Inspector traces use the same shared graph/result
  contracts as Studio.
- Task Mode run rows, title actions and graph cards also keep run/node IDs in
  local indexed closures. Its stable and dynamically rendered shell actions use
  fixed delegated markers, leaving the entire Task Mode surface free of inline
  handlers. Graph cards expose button semantics and support both pointer and
  Enter/Space inspection. Durable list/get/create/event calls share the same
  semantic request client with Studio, and all state changes share
  `orchestration-mutation-request.js` with Studio.
  `api/orchestrations.js` exposes normalized
  `taskListResult`/`taskEventsResult` reads while retaining direct-body methods
  for rolling compatibility;
  a human gate is removed only after the backend confirms resolution, so a
  transient failure remains actionable instead of disappearing optimistically.
  Gate submissions and run abort/delete mutations are single-flight per opaque
  run or request ID; gate controls expose their pending state and disable as a
  group until the shared mutation promise settles. The backend also rejects
  deletion of non-terminal runs, preventing a live worker from writing into a
  run header removed mid-flight.
  Open, terminal-final and mutation-resync header reads share one request
  generation, including repeated reads of the same run ID, so an older GET
  cannot repaint a newer header. Mutation reconciliation is non-destructive:
  transport/5xx failures preserve the visible timeline and pinned definition;
  only an authoritative 404 tears the stale view down.
  Relative time, unnamed runs, gate outcomes and completion summaries also use
  i18n keys rather than leaking English into the Chinese operating surface.
  Task Mode now shares the same modal focus restore contract and supports
  Escape-to-close with a localized accessible close label.

At the backend seam,
`authoring_service.OrchestrationAuthoringService.layout()` owns the
copy-before-layout rule, so the HTTP route cannot mutate caller-owned state.
`service.definition_request_schema()` also publishes the OpenAPI request shape
from the canonical schema ID, node types, control kinds and size limits; route
metadata no longer carries a second, already-divergent node schema.
`runtime_service.execute_runtime_flow()` is the single worker pipeline for transient
Studio runs and durable Task Mode runs: both now share event projection,
subflow resolution, execution normalization and runtime completion, with the
durable service supplied only as an optional persistence port.
`runtime_ports.py` publishes the minimal TaskRuntime, definition lookup,
durable-run and transition capabilities used by both the start facade and that
worker pipeline. Their dependency injection no longer relies on `Any`-typed
knowledge of concrete services, while each consumer remains coupled only to
the methods it actually invokes.
The generic task HTTP edge now follows the same rule:
`lib/task_runtime_ports.py` owns the structural replay, abort and combined
route-runtime capabilities. `routes/_task_routes.py`, both orchestration
runtime adapters and the abort-race classifier consume those ports; only the
composition root constructs the concrete `TaskRuntime`. This keeps polling and
abort semantics reusable without making route modules or domain classification
logic depend on the registry implementation.
The worker also reads conflict semantics from the shared mutation protocol,
not from the concrete run-service implementation; the runtime port therefore
remains a real substitution boundary rather than a type-only abstraction.
Durable creation is fail-closed: persistence returns an explicit success bit,
and the HTTP adapter does not create or spawn a runtime task unless its pinned
run header was actually committed.
Durable read failures likewise raise one `RunServiceError` instead of being
collapsed into an empty list or a false 404; the global API error boundary can
therefore report a traceable 500 while genuine empty/missing states stay intact.
Abort writes use the same distinction after their terminal fence: a worker
that won the terminal race returns 409, while a still-active row whose abort
could not be persisted returns 500 rather than a misleading conflict.
Durable event appends are now idempotent boolean writes. A real append failure
raises `DurableProjectionError` inside the shared worker pipeline and is
normalized into a `persistence` flow failure; token-level transient events
remain intentionally excluded from the durable log.
Lifecycle writes go through `OrchestrationRunService.transition_status()`,
which classifies a committed transition, an idempotent retry, a terminal race,
and a storage failure behind one typed interface. The worker confirms the
durable terminal state before completing its in-memory runtime; a rejected
`done` write therefore cannot be surfaced to Task Mode as a successful run.
If an accepted user abort wins that terminal fence, the shared pipeline aligns
the in-memory outcome to `aborted` instead of misreporting the race as a
database failure.
At process startup the same service retires any stale non-terminal header to a
typed `worker_lost` error. Events and the pinned graph remain replayable, while
the client receives a terminal fact instead of polling a vanished worker.
Terminal Task Mode headers expose **Run again**, which creates a new durable
instance from the preserved definition snapshot and input; the original run
remains immutable for comparison and debugging.
Failed runs render a partial result and their terminal error as separate
sections, so useful output no longer masks the reason the run failed.
Chat Endpoint, Autopilot and fallback graphs also resolve through the focused
`authoring_contract.build_builtin_definition()` registry.
HTTP CRUD, ephemeral/durable runs and the chat runner now obtain stored graphs
through `definition_service.OrchestrationDefinitionService`; only that service
constructs `OrchestrationStore`, wraps repository failures as
`DefinitionServiceError`, and returns one typed `DefinitionWriteResult` after
validation rather than letting each route reimplement the boundary.
All three execution adapters call `runtime_service.execute_flow()`. Its
`FlowRunOutcome` retains the executor, original exception and normalized
`failure_kind`, so chat can preserve partial traces and structural/crash
wording while sharing construction and exception normalization with REST and
durable runs. `lib/orchestration/run_status.py` is the single durable lifecycle
vocabulary consumed by persistence and application services. HTTP polling and
mutation contracts project explicit terminal booleans for the browser rather
than publishing a second client-side classifier. Durable-header outcome
projection and the public outcome contract also call this same lifecycle
boundary instead of embedding another terminal-status set. Terminal
persistence is fenced:
the first terminal timestamp is stable, a late worker/abort write cannot
resurrect or relabel a terminal run, and aborting an already terminal run
returns a typed 409 conflict. `OrchestrationRunService` is the framework-free
application boundary above that persistence: create/update/event append,
list/get/replay and abort/delete semantics no longer live in individual HTTP
handlers, and persistence failures remain distinguishable from not-found and
terminal conflicts.

The same pass repaired the stylesheet's unresolved `${_ORCH_CARD_W}` template
token (static CSS now declares the real 188px card width), localized Canvas
empty/subtitle states, and made palette chips plus Canvas nodes keyboard
focusable. Clicking a palette item now adds it on desktop as well as mobile.

Regression coverage: `test_orchestration_service.py` owns repository,
authoring/inspection module ownership and facade identity, resolution,
inspection, event-sink and terminal contracts;
`test_orchestration_wire_contracts.py` owns wire-projector detachment,
HTTP-import boundaries and exact browser/backend protocol identifier parity;
`test_frontend_orchestration_document_state.py` owns dirty/validation guards,
backend-generated task fields and non-destructive nested snapshots;
`test_frontend_orchestration_{studio,history,session,graph_tools,graph_actions,navigation,viewport,canvas_geometry,canvas_interaction,edge_view,node_view,composer,workspace,write_recovery,inspector_renderer,inspector_content,inspector_view,io_tools,panel_ux}.py`
own the pure Canvas boundaries and interaction/accessibility regressions.

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

- **`orchestration_agent_runner.py`** is the single composition edge. Its
  `OrchestrationSubAgentRunner` lazily imports `SubAgent`/`SubTaskSpec`, builds
  one leaf agent and maps its live stream to `step_delta`/`step_phase` events.
  `FlowExecutor` imports only this callable adapter and retains a two-line
  `_default_runner` compatibility patch point. `orchestration_runner_result.py`
  owns the full output/status/error/thinking result seam, then
  `orchestration_tool_usage.py` projects either supported telemetry shape into
  one immutable accounting value. The graph interpreter contains no swarm
  construction or raw Runner-dictionary parsing.
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
     (durable run instances) — all reachable *today* via the shared blueprint
     composed in `routes/api_v1/orchestrations.py`; authoring and ephemeral
     execution routes are registered by `orchestration_definition_routes.py`,
     `orchestration_authoring_routes.py` and
     `orchestration_runtime_routes.py`. A user can author, validate, compose,
     and run a graph now.
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

## 2. Module inventory — top-level `orchestration*.py` execution adapters

Current 2026-08-12 execution inventory is listed below. The schema/service
package under `lib/orchestration/` is inventoried separately by concern above.

Verdict: **OK** / **BIG** / **MISCUT**. Status: **HOT** / **live** / **flag-gated**.

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `orchestration_engine.py` | 729 | **BIG** | live (flag-gated for chat) | `test_orchestration_engine`, `test_orchestration_io`, `test_orchestration_emits_subflow`, `test_orchestration_nested_canvas`, `test_orchestration_phase_and_output` |
| `orchestration_budget.py` | 39 | OK | live (shared nested agent cap) | `test_orchestration_budget`, `test_orchestration_engine` |
| `orchestration_dataflow.py` | 156 | OK | live (runtime Typed I/O) | `test_orchestration_dataflow`, `test_orchestration_io`, `test_orchestration_engine` |
| `orchestration_feedback.py` | 199 | OK | live (shared feedback + convergence state) | `test_orchestration_feedback`, `test_orchestration_engine`, `test_flow_vu_progress_guard` |
| `orchestration_graph.py` | 160 | OK | live (pure topology) | `test_orchestration_graph`, `test_orchestration_engine` |
| `orchestration_plan.py` | 86 | OK | live (Studio preview) | `test_orchestration_plan`, `test_orchestration_engine`, `test_orchestration_service` |
| `orchestration_progress.py` | 146 | OK | live (producer ledger + replan summary) | `test_orchestration_progress`, `test_orchestration_engine`, `test_flow_vu_progress_guard` |
| `orchestration_outcome.py` | 358 | OK | live (terminal ledger + cross-surface projection) | `test_orchestration_outcome`, `test_flow_terminal_honesty`, `test_orchestration_endpoint_outcome`, `test_orchestration_run_service` |
| `orchestration_mutation.py` | 278 | OK | live (run/gate mutation contract) | `test_orchestration_mutation`, `test_orchestration_run_service`, `test_orchestrations`, `test_frontend_orchestration_result` |
| `orchestration_trace.py` | 153 | OK | live (durable node trace + trace contract) | `test_orchestration_trace`, `test_orchestration_phase_and_output`, `test_orchestrations` |
| `orchestration_transcript.py` | 92 | OK | live (transcript state + subflow membrane) | `test_orchestration_transcript`, `test_orchestration_engine` |
| `orchestration_runner_result.py` | 85 | OK | live (typed leaf-result port) | `test_orchestration_runner_result`, `test_orchestration_agent_runner`, `test_orchestration_engine` |
| `orchestration_tool_usage.py` | 107 | OK | live (Runner telemetry projection) | `test_orchestration_tool_usage`, `test_orchestration_engine`, `test_orchestration_progress` |
| `orchestration_agent_runner.py` | 146 | OK | live (default leaf adapter) | `test_orchestration_agent_runner`, `test_orchestration_role_params`, `test_orchestration_endpoint_adapter` |
| `orchestration_role_runtime.py` | 196 | OK | live (leaf-role lifecycle coordinator + raw-output membrane) | `test_orchestration_role_runtime`, `test_orchestration_engine`, `test_orchestration_runner_result` |
| `orchestration_subflow_runtime.py` | 232 | OK | live (isolated child-executor membrane) | `test_orchestration_subflow_runtime`, `test_orchestration_engine`, `test_orchestration_budget` |
| `orchestration_loop_runtime.py` | 273 | OK | live (verifier-loop policy coordinator) | `test_orchestration_loop_runtime`, `test_flow_terminal_honesty`, `test_flow_vu_progress_guard` |
| `orchestration_parallel_runtime.py` | 153 | OK | live (fan-out scheduler + join/error membrane) | `test_orchestration_parallel_runtime`, `test_orchestration_engine`, `test_flow_terminal_honesty` |
| `orchestration_branch_runtime.py` | 104 | OK | live (one-of-many classifier routing) | `test_orchestration_branch_runtime`, `test_orchestration_engine` |
| `orchestration_replan_runtime.py` | 99 | OK | live (bounded structural Planner delta) | `test_orchestration_replan_runtime`, `test_orchestration_loop_runtime`, `test_orchestration_engine` |
| `orchestration_execution_runtime.py` | 157 | OK | live (top-level seed/failure/outcome/result lifecycle) | `test_orchestration_execution_runtime`, `test_orchestration_engine`, `test_flow_terminal_honesty` |
| `orchestration/human_gate_runtime.py` | 174 | OK | live (execution-side gate port) | `test_orchestration_human_gate_runtime`, `test_orchestration_engine` |
| `orchestration_chat_event_sink.py` | 94 | OK | flag-gated (chat task projection) | `test_orchestration_chat_event_sink`, `test_orchestration_endpoint_runner` |
| `orchestration_chat_turn_persistence.py` | 93 | OK | flag-gated (turn DB/translation port) | `test_orchestration_chat_turn_persistence`, `test_orchestration_endpoint_runner` |
| `orchestration_chat_completion.py` | 136 | OK | flag-gated (canonical chat terminal projection) | `test_orchestration_chat_completion`, `test_orchestration_endpoint_outcome`, `test_flow_terminal_honesty` |
| `orchestration_chat_autopilot.py` | 110 | OK | flag-gated (Autopilot run boundary/cleanup) | `test_orchestration_chat_autopilot`, `test_orchestration_vu_mislabel` |
| `orchestration_chat_launch.py` | 193 | OK | flag-gated (immutable Chat Flow launch spec) | `test_orchestration_chat_launch`, `test_chat_flow_dispatch` |
| `orchestration_endpoint_runner.py` | 362 | OK | flag-gated | `test_orchestration_endpoint_runner`, `test_orchestration_endpoint_outcome` |
| `orchestration_endpoint_adapter.py` | 441 | OK | flag-gated | `test_orchestration_endpoint_adapter` |
| `orchestration_runs.py` | 408 | OK | live (durable runs) | `test_orchestrations` |
| `orchestration_composer.py` | 279 | OK | live (Studio) | `test_frontend_composer_*` |

`orchestration_engine.py` remains **BIG but now scheduling-focused**:
`FlowExecutor` walks start/role/parallel/barrier/loop/branch/stop and coordinates
focused control runtimes. Pure topology
queries are isolated in `orchestration_graph.py`; runtime Typed I/O in
`orchestration_dataflow.py`; trace sequencing/truncation in
`orchestration_trace.py`; shared feedback/convergence state in
`orchestration_feedback.py`; producer aggregation and replan summaries in
`orchestration_progress.py`; terminal fact ownership and surface projection in
`orchestration_outcome.py`; state-change classification and wire projection in
`orchestration_mutation.py`;
the production leaf implementation in `orchestration_agent_runner.py`; the
budget/input/Runner/accounting/Trace/output lifecycle of a single role node in
`orchestration_role_runtime.py`; the definition/depth/child-executor/result
membrane of an isolated nested graph in `orchestration_subflow_runtime.py`; and
bounded iteration, zero-deliverable/stuck/no-progress guards, replan routing and
honest exit projection in `orchestration_loop_runtime.py`; bounded thread-pool
fan-out, structural branch-failure projection, output merge and barrier resume
in `orchestration_parallel_runtime.py`; candidate projection, classifier-only
output matching, deterministic fallback and `branch_pick` publication in
`orchestration_branch_runtime.py`; bounded progress context and immutable
Planner delta-brief rewriting in `orchestration_replan_runtime.py`; and
Start-seed selection, timing, failure classification, terminal event and
detached result projection in `orchestration_execution_runtime.py`; and
dry-run preview compilation in `orchestration_plan.py`. Injected test/custom
runners and isolated child executors still use the same callable protocol and
configuration replay interface.

The former monolithic `orchestration.py` schema module is already the
`lib/orchestration/` package: validation, builders, roles, defaults, FieldSpecs,
Typed I/O, layout, definition service/runtime service/run service and lifecycle
contracts have separate physical owners.

The other four are correctly bounded: `composer` (NL→graph, mirrors the optimizer
proposer pattern), `runs` (durable DB-backed run instances, mirrors
`swarm/persistence.py`, best-effort never-raises), `endpoint_runner` (the chat-mode
convergence entry) + `endpoint_adapter` (FlowExecutor-event → endpoint-UI-schema
translator). The adapter is a clean stateful translator. Its legacy endpoint
wire envelope remains for compatibility, but the semantic projection is now
explicit and consumed by the frontend; transport names are no longer allowed to
define whether a user-side turn is a Critic or a virtual user.

---

## 3. Historical swarm inventory — 2026-07 baseline

The table below preserves the pre-split 7370 LOC / 17-file baseline used by the
original analysis. The current tree has already replaced `integration.py` with
the `_state`/`_tools`/`_rehydrate`/`_autocontinue`/`_logs`/`_config` package
boundaries recorded in §7; its old row is evidence of the resolved miscut, not a
description of today's filesystem.

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

**orchestration inbound:** `routes/api_v1/orchestrations.py` (composition root),
`routes/api_v1/orchestration_definition_routes.py` (definition persistence),
`routes/api_v1/orchestration_authoring_routes.py` (pure Studio authoring),
`routes/api_v1/orchestration_runtime_routes.py` (ephemeral plan/start/poll),
`routes/api_v1/orchestration_task_routes.py` (durable create/read/replay),
`routes/api_v1/orchestration_mutation_routes.py` (all run-state mutations),
`routes/api_v1/orchestration_run_http.py` (shared run-start request contract),
`routes/api_v1/orchestration_task_http.py` (durable read projection), and
`routes/api_v1/orchestration_service_http.py` (shared service-failure
projection),
`routes/chat.py` (via `resolve_chat_flow_entry` — the flag-gated chat
convergence). Internal: `endpoint_runner` → `engine` + `adapter` +
`orchestration_chat_event_sink` + `orchestration_chat_turn_persistence` +
`orchestration_chat_completion` + `orchestration_chat_autopilot` +
`orchestration_chat_launch`; Launch owns projection, initial phase/context,
model/tools/policy and Executor options, persistence receives the LIVE
`tasks_pkg.endpoint` DB-sync/translation functions, Completion owns common
terminal frames, and Autopilot isolates its run boundary/control cleanup.

**swarm inbound:** the `spawn_agents` tool handler (`tasks_pkg/handlers/misc.py` →
`swarm/integration.execute_swarm_tool`), `orchestrator.py` (between-round inbox
drain), startup rehydrate. `routes/api_v1/swarm.py` + `agents.py`.

**The composition edge:** `orchestration_engine._default_runner` →
`OrchestrationSubAgentRunner` → `swarm.SubAgent` + `swarm.SubTaskSpec` (lazy
imports in the adapter). `orchestration.py` → `swarm.registry.AGENT_ROLES`
(shared role catalogue).

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
   `agent_runner(node, context, iteration)`; the default delegates to
   `OrchestrationSubAgentRunner`, while tests inject a mock. The interpreter's
   control-flow logic is fully covered in CI with NO LLM call *because* of this
   seam — do not inline the swarm runner.
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
6. **The FlowExecutor→chat adapter must preserve transport compatibility and
   explicit semantic projection.** Endpoint frames keep
   `_isEndpointPlanner`/`_epIteration`/`endpoint_iteration`/
   `endpoint_critic_msg`, while flow frames additionally carry
   `flowProjection` + `turnRole` + `emits` and stable VU ids. The `emits` axis
   (user|assistant) is orthogonal to role; `virtual_user` must never acquire an
   `_isEndpointReview` marker.
   `OrchestrationChatTaskEventSink` is the sole owner of applying those live
   endpoint/delta/finalizer frames to task content, thinking, phase and current
   turn state before forwarding the unchanged event for replay.
7. **Flag defaults are OFF and symmetric** (`TOFU_ENDPOINT_VIA_FLOW`/
   `TOFU_AUTOPILOT_VIA_FLOW`). The live `tasks_pkg` paths stay authoritative until
   validated; single-box behaviour is byte-identical with flags off.
8. **`orchestration_runs`/`swarm persistence` are best-effort, never raise into a
   running flow** — durability is a safety net, not a critical path.

---

## 6. Known debt (grounded)

- **The transient endpoint/autopilot duplication remains the unit's real debt** (§1c):
  two live implementations (Unit-1 hand-built `endpoint.py`/`autopilot.py` AND the
  `FlowExecutor` graph path), gated by `TOFU_*_VIA_FLOW`. This is a deliberate
  strangler-fig migration. The 2026-08-08 tranche closes the known chat-boundary
  parity gaps (projection, context/policy inheritance, VU lifecycle and
  recovery), but it does not claim enough production evidence to flip the
  toggle defaults or delete the standalone driver. The finish line remains a
  measured cutover followed by retirement of one loop implementation.
- **`agent.py` (1252) is a second ReAct loop** parallel to `orchestrator.run_task`;
  `lib/agent_loop.py` exists to eventually unify them but the migration is partial
  (only the paper engines adopted it — CLAUDE.md §1).
- `orchestration_engine.py` is still large, but its former topology queries,
  Typed-I/O data plane, leaf-role lifecycle, isolated-subflow membrane,
  verifier-loop policy, fan-out scheduler, trace store, producer progress ledger, SubAgent
  stream/result adapter, terminal outcome ledger and dry-run compiler are
  physically separated behind explicit navigator, dataflow, role-runtime,
  child-executor, loop-runtime, parallel-runtime, trace, progress, runner, outcome and preview
  ports.
- `master.py` and `agent.py` remain large and warrant independent audits.

---

## 7. Segmentation verdict (this unit)

**Correctly bounded — leave as-is:**
`orchestration_composer`, `orchestration_runs`, `orchestration_endpoint_adapter`,
`orchestration_endpoint_runner`, `orchestration_chat_event_sink`,
`orchestration_chat_turn_persistence`,
`orchestration_chat_completion`,
`orchestration_chat_autopilot`,
`orchestration_chat_launch`,
`orchestration_agent_runner`, `orchestration_runner_result`,
`orchestration_tool_usage`, `orchestration_role_runtime`,
`orchestration_subflow_runtime`, `orchestration_loop_runtime`,
`orchestration_parallel_runtime`, `orchestration_branch_runtime`,
`orchestration_replan_runtime`,
`orchestration_execution_runtime`,
`orchestration_graph`, `orchestration_plan`, `orchestration_dataflow`,
`orchestration_trace`, `orchestration_progress`, `orchestration_feedback`,
`orchestration_outcome`; and in swarm:
`scheduler` (a reference extraction),
`registry`, `tools`, `persistence`, `artifact_store`, `snapshot`, `events`,
`result_format`, `rate_limiter`, `types`, `protocol`, `planner`, `messages`. The
whole `swarm/` package is *better* decomposed than `tasks_pkg`.

**Completed physical splits:**

1. `orchestration_engine.py` → `orchestration_agent_runner.py`: SubAgent
   construction, streaming and result normalization now live behind the
   pre-existing runner port.
2. `orchestration_engine.py` → `orchestration_graph.py`: entry selection,
   reachability, loop partitioning and parallel convergence now live in a pure,
   directly tested topology module. `FlowExecutionError` remains re-exported
   by the engine for import compatibility.
3. `orchestration_engine.py` → `orchestration_plan.py`: dry-run preview no
   longer constructs an executor or duplicates forward/reverse adjacency.
   `compile_plan` remains re-exported by the engine for rolling compatibility,
   while the definition service imports its physical owner directly.
4. `orchestration_engine.py` → `orchestration_dataflow.py`: seed addressing,
   implicit/named output publication, strict input resolution and artifact
   manifests now use one thread-safe runtime data-plane interface. The engine
   no longer keeps a shadow I/O store or reimplements port parsing.
5. `orchestration_engine.py` → `orchestration_trace.py`: trace bounds,
   sequencing, timestamps, thread safety and durable `step_trace` projection
   have one owner; its versioned contract also drives both frontend Inspectors.
   The executor retains only its public `trace` facade and compatibility limit
   constants.
6. `orchestration_engine.py` → `orchestration_progress.py`: concurrent
   producer snapshots, latest-turn fallback, deterministic iteration folding,
   verifier deliverables text and bounded replan summaries now share one
   ledger. Thin private proxies preserve rolling test/extension compatibility.
7. `orchestration_engine.py` → `orchestration_feedback.py`: node-attempt
   memory, pending reviewer feedback/directives, feedback history, verifier-
   specific stuck detection and VU diminishing-return accounting now advance
   through one thread-safe state channel. Engine private fields remain thin
   rolling-compatibility views.
8. `orchestration_engine.py` → `orchestration_outcome.py`: loop exits, node
   failures, artifact declarations and terminal classification now have one
   thread-safe owner. The same versioned result projects durable lifecycle,
   chat task status and finish reason; private ledger properties remain only as
   rolling compatibility views.
9. `orchestration_engine.py` → `orchestration_runner_result.py` +
   `orchestration_tool_usage.py`: the production runner now returns one typed,
   immutable result and legacy mappings normalize at one compatibility seam.
   Output/status/error/thinking and both tool telemetry shapes are no longer
   parsed inside graph execution.
10. `orchestration_engine.py` → `orchestration_role_runtime.py`: agent-budget
    claim, effective Typed-I/O/shared/verifier context, Runner invocation and
    normalization, transcript/outcome/Trace/progress publication, and final
    context projection now form one directly tested leaf lifecycle. The graph
    interpreter retains only a thin `_run_role` compatibility facade plus its
    scheduling counter callback; topology and loop policy do not enter the new
    module.
11. `orchestration_engine.py` → `orchestration_subflow_runtime.py`: reference
    resolution, recursion/budget gates, child result normalization, producer-
    only output membrane, parent transcript/Trace/dataflow publication and
    incomplete/failure projection now share one directly tested runtime. A
    minimal Child Executor Port and injected factory retain recursive reuse
    without importing `FlowExecutor` or creating a module cycle.
12. `orchestration_engine.py` → `orchestration_loop_runtime.py`: bounded
    iteration state, zero-deliverable/stuck/no-progress convergence guards,
    verifier progress, replan routing and canonical loop-exit facts now share
    one directly tested coordinator. Body walking, planner re-entry, verdict
    classification and progress parsing remain injected compatibility ports,
    preserving Engine monkeypatch seams and avoiding leaf-execution coupling.
13. `orchestration_engine.py` → `orchestration_parallel_runtime.py`: branch
    discovery, bounded thread-pool scheduling, loop-context concurrency
    diagnostics, abort translation, structural branch-failure recording,
    deterministic context merge and post-barrier resume now form one directly
    tested fan-out coordinator. The graph interpreter retains only a thin
    `_run_parallel` compatibility facade.
14. `orchestration_engine.py` → `orchestration_branch_runtime.py`: candidate
    labels, synthetic classifier input, classifier-only answer matching,
    deterministic first-edge fallback and `branch_pick` events now form one
    directly tested router. The raw-output membrane on the shared Role Runtime
    prevents labels already present upstream from overriding the classifier's
    actual answer; Engine retains only `_run_branch` delegation.
15. `orchestration_engine.py` → `orchestration_replan_runtime.py`: bounded
    producer-summary projection, structural-defect context and immutable
    Planner DELTA brief rewriting now share one directly tested runtime.
    Loop policy still decides when to re-plan; Role Runtime still owns leaf
    execution; Engine retains only `_run_replan` and summary facades.
16. `orchestration_engine.py` → `orchestration_execution_runtime.py`: Start
    seed resolution, clock ownership, abort/structural/unknown failure
    classification, canonical terminal events and detached result assembly now
    form one directly tested top-level lifecycle. Outcome Ledger remains the
    sole terminal semantic owner; Engine retains only a thin public `run()`
    facade and graph/control scheduling.
17. `orchestration_endpoint_runner.py` →
   `orchestration_chat_event_sink.py`: live endpoint iteration, delta and turn
   finalization frames now update task content/thinking/phase through one
   callable port; the runner only assembles that port with execution and
   persistence concerns.
18. `orchestration_endpoint_runner.py` →
   `orchestration_chat_turn_persistence.py`: the Adapter's live turn list is
   explicitly bound once, replacing the `_adapter_ref` closure. Incremental DB
   snapshots, per-turn translation and the final translation safety net now
   share one non-fatal persistence port.
19. `orchestration_endpoint_runner.py` →
   `orchestration_chat_completion.py`: final producer selection, partial trace,
   turn snapshot, canonical outcome/task fields, `endpoint_complete`/`done`
   frames and task persistence now advance through an idempotent two-phase
   Completion port. The Runner retains only Autopilot-specific lifecycle and
   marker cleanup between `prepare()` and `finish()`.
20. `orchestration_endpoint_runner.py` →
   `orchestration_chat_autopilot.py`: run-concluded projection, arm-marker
   cleanup and run-pin cleanup are independent non-fatal side effects behind
   injectable ports. A failure in one cleanup no longer prevents the other.
21. `orchestration_endpoint_runner.py` →
   `orchestration_chat_launch.py`: chat projection/phase, bounded history,
   system-policy channel, canonical model/tool assembly, thinking preference,
   abort hook and detached Executor options now form one immutable launch
   specification. Thin helper facades preserve extension/test compatibility.
22. The former monolithic `swarm/integration.py` is already a package split into
   `_state`, `_tools`, `_rehydrate`, `_autocontinue`, `_logs` and `_config`.

**Big but optional (defer unless touched):**
`orchestration_engine.py` (729 — graph walking and control-runtime coordination),
`orchestration.py` (1323 — the graph builders could split), `swarm/master.py`
(1479), `swarm/agent.py` (1743 — the deeper `agent_loop` unification is a separate
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
