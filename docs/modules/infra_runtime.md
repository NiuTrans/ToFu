# Infrastructure and process runtime

This map describes the production process that runs now. It intentionally
contains no migration timeline, incident transcript, line-count snapshot or
alternate import path.

## Entry points and owners

| Concern | Sole owner | Public entry point |
|---|---|---|
| Quart shell and base lifespan | `lib/app_factory.py`, `lib/app_lifecycle.py` | `create_base_app()` |
| HTTP assembly | `lib/app_assembly.py` | `create_application()`, `configure_application()` |
| Process startup and shutdown | `lib/production_lifecycle.py` | `register_production_lifecycle()` |
| Process-role ownership | `lib/process_roles.py` | `capabilities_for_role()` |
| Event-loop services | `lib/serving_loop_lifecycle.py` | `register_serving_loop_lifecycle()` |
| Server composition | `server.py` | `create_app()`, `create_production_app()` |
| Storage-free developer composition | `tofu_agent/runtime.py` | `AgentRuntime`, `AgentExecution` |
| Lightweight sidecar and CLI | `tofu_agent/server.py`, `tofu_agent/cli.py` | `create_app()`, `tofu-agent serve` |
| Agent public surface | `lib/agent_core/`, `lib/agent_core_manifest.py` | `lib.agent_core` |
| Push delivery | `lib/agent_core/push.py`, `lib/agent_core/push_bus.py` | `push_event()`, `PushHub` |
| Background task lifecycle | `lib/agent_core/task_runtime.py` | `TaskRuntime` |
| Cross-process leases and counters | `lib/runtime_state_store.py` | `get_store()` |
| Adaptive direct/proxy egress | `lib/netpath.py` | `note_url()`, `decide()`, `report_outcome()` |
| Production teardown | `lib/server_shutdown.py` | `shutdown_production_runtime()` |
| In-place restart handoff | `lib/server_reexec.py` | `begin_server_reexec()`, `execute_pending_server_reexec()` |

`lib.agent_core.push` and `lib.agent_core.task_runtime` are the only module
paths for their capabilities. `scripts/check_architecture.py` rejects retired
facades and imports.

## Assembly and lifecycle

`create_app()` builds an independent application suitable for tests and schema
tools. It owns only application-local resources: middleware, blueprints, auth,
static delivery, error mapping and logging hooks. Importing it must not start a
storage process, worker or network integration.

`create_production_app()` adds the two process lifecycles. Quart's native
lifespan is the authority for startup rollback and shutdown. Startup handlers
run in registration order; shutdown handlers run in reverse order and all get
a cleanup attempt even if one fails.

The required production sequence is:

1. start loop diagnostics and loop-owned executors;
2. validate frontend artifacts for `api`/`all` (a validation exception is a
   required-phase failure; `worker`/`scheduler` skip the phase);
3. prove the exclusive Sidecar storage boundary;
4. start and health-check the Sidecar;
5. run durable recovery through Sidecar repositories for `worker`/`all`;
6. validate critical imports;
7. start only the background owners assigned to the declared process role;
8. start role-scoped optional integrations;
9. open the readiness gate.

A required phase fails startup. Optional integrations report degradation but
do not create a second readiness or storage authority. A startup exception
immediately runs the registered shutdown stack because Quart does not call
`after_serving` when `before_serving` fails.

An approved or idle-HEAD restart arms the deadline and releases producers and
transports. The exec gate requires a storage-release certificate or exits for
new-PID recovery. One existing Sidecar watcher uses close-on-exec pipe EOF to
bind its lease to one process image; no child, live FD, or thread crosses exec.

The ASGI lifespan and request path consume prebuilt frontend artifacts and
never invoke Node. Both validate the atomic published graph (entries, recursive
references, safe paths, files, and manifest digest); the manifest is the commit
point, so later source edits cannot withdraw it during recovery or hard refresh.
Before a source-checkout `start`, approved `restart`, or cold manager launch,
`serverctl.py` validates authoring freshness and may rebuild once only for the
owning frontend role with local Vite. Release installs remain fail-closed
without a valid graph and never acquire a production Node dependency.
For network/userspace checkouts, the stdlib manager routes CPython bytecode to a
verified host-local private namespace keyed by project and interpreter. Its
adaptive 16..64 MiB personal budget, 100,000 entries, 64 namespaces, seven-day
TTL, 256 MiB reserve and manager-held lease make it disposable and race-safe;
explicit Python policy wins and setup failure falls back to normal imports.

HTTP assembly compresses eligible whole responses outside the serving loop.
Static artifacts may use their content-addressed 48-entry/8 MiB cache ceiling;
dynamic user responses are never cached. For dynamic bodies below 1 MiB and
for every distributed response, Brotli quality 4 / gzip level 6 preserve the
bandwidth profile. Personal-mode dynamic bodies at or above 1 MiB use Brotli
quality 2 / gzip level 1 to bound CPU and executor occupancy on the reference
computer. This topology decision has no per-request inference or unbounded
queue; the application default executor remains the execution boundary.

`lib/process_roles.py` is the sole role-to-capability table. API replicas own
frontend/catalog/request services, workers own task recovery/execution, and
schedulers own timed jobs and event maintenance. Personal mode is always
`all`; distributed split roles must not infer ownership from which route or
module happened to be imported.

`lib/netpath.py` owns process-local direct/proxy scoring. Real request outcomes
are the freshest signal. Its lifecycle worker probes only a path whose bounded
deadline is due: a new or failed route wakes it immediately, repeated failures
back off to the configured ceiling, healthy traffic postpones redundant
synthetic requests, and a host unused for 24 hours is not rearmed by restart.
The tracked-host ceiling is 64 and persisted timestamps are clamped before they
can schedule work.

The public orchestrator probes are `/health/live`, `/health/ready`, and
`/health/startup`. Liveness performs no dependency I/O. Readiness and startup
project only lifecycle state, process role, and a storage-ready boolean;
detailed diagnostics remain authenticated. `/api/health` is the
application-facing identity/liveness projection and `/api/ready` is the
dependency-readiness projection used by the personal UI. The lifecycle manager
requires the locked worker PID from `/api/health` and a passing `/api/ready`
response before it reports startup complete. A live-but-unready worker is
degraded, but dependency failure alone never enters the manager's wedge-kill
policy.

The lightweight sidecar owns a separate, intentionally smaller ASGI assembly.
It imports `tofu_agent.runtime`, not `server.py`, and exposes only health,
capabilities, agent submission, process-memory task replay, abort, and custom
tool resolution. A non-loopback bind is default-deny without a bearer token.
It must never grow application storage, accounts, billing, conversation, or
frontend lifecycles; those requirements select the full composition instead.

## Storage and identity

The Sidecar is the only durable storage authority. There is no runtime storage
selector: personal mode uses its SQLite adapter and distributed mode uses its
PostgreSQL adapter. Application assembly never imports or owns a driver,
connection, transaction, schema, or database path.

User identity is resolved at the HTTP/auth boundary and is then an explicit
parameter of repositories, project coordination and push delivery. A frame
with durable or user-visible state must carry its owner; absence of an owner is
not permission to broadcast. `PERSONAL_USER_ID` may be selected only at a
composition boundary for the personal deployment.

## Agent-core boundary

`tofu_agent.AgentRuntime` composes this core with an explicit principal and
marks tasks transient before their first event. The manager's durable birth,
event, result, project-feed, latest/supersede, and affinity paths all skip such
tasks. This is the only supported storage-free task composition; adapters must
not implement another agent loop or monkeypatch persistence after submission.

`lib/agent_core_manifest.py` is executable documentation for the reusable
agent base. `tests/test_agent_core_boundary.py` walks its Python AST and
enforces two dependency rules:

- core reaches concrete tools/providers only through
  `lib.tools.registry` and `lib.llm_dispatch.provider_registry`;
- core reaches persistence only through the `ConversationStore` protocol and
  `lib.agent_core.store`, never by importing `lib.storage`,
  `lib.storage_sidecar`, or `lib.conversations`.

The lazy `lib.agent_core` facade maps discoverable public symbols to their
defining modules without importing the orchestration graph at package import
time.

## Push and shared runtime state

`PushHub` owns subscriptions and local delivery. `push_bus.py` transports a
frame between replicas; the in-process backend is the one-process transport
and the Redis backend is the shared transport. Both feed the same local
delivery function.

`runtime_state_store.py` is the single lease/counter/heartbeat substrate for
admission, SSE limits and cross-replica subscription state. Callers depend on
its interface, never Redis directly. Backend degradation is observable and
must preserve process availability; authorization and owner filtering remain
fail-closed independently of transport availability.

Every SSE route shares the launch-probed `TOFU_MAX_SSE_PER_PRINCIPAL` budget
(8..24 personal, 12 on the 8 GiB reference/fallback profile, 64 distributed,
hard ceiling 128). Conversation Sync additionally keeps one bounded
page-generation owner per active subscription: replacement closes the old
generator and releases its lease synchronously, while 204 capacity refusal
stops native EventSource retry. The broker's inactive generation history is
bounded by `TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY` with a 128-entry floor that
matches the absolute SSE cap; it never retains events or conversation
projections. A 10-second response-body start deadline also reclaims admission
when a downstream disconnects before ASGI enters the generator.

On a network/FUSE data root, `fs_keepalive.py` owns one interruptible 15-second
coordinator and one serialized metadata-probe daemon. The coordinator uses an
Event deadline rather than half-second sleep fragments. At most one stat batch
may be in flight: if the mount enters uninterruptible sleep, later deadlines
report that exact probe instead of accumulating abandoned threads. Shutdown
retains a stuck composite owner and refuses to start a duplicate until the
kernel call returns.

## Change rules

- Add process-wide work through a named lifecycle handler with an idempotent
  shutdown owner. Do not start work during module import.
- Add an HTTP concern in `app_assembly.py`; add a loop-bound concern in
  `serving_loop_lifecycle.py`; add a required boot phase in
  `ProductionStartupSteps`.
- Do not add a second app factory, readiness gate, task registry, push hub,
  storage mode or error taxonomy.
- Keep blocking filesystem, subprocess and network work off the Quart event
  loop. Path lookup/read-only imports do not create directories, and one
  launch snapshot serves every flag from the same small config file.
- Put ownership keys in durable/task data before work leaves the request
  context. Worker threads must not infer a user from globals.
- A recovery job must be idempotent and operate through the authoritative
  repository. Do not mask failed boot recovery with an unbounded periodic
  repair loop.

## Verification ladder

Run the smallest relevant checks first:

```bash
python3 scripts/check_architecture.py
pytest -q tests/test_agent_core_boundary.py
pytest -q tests/test_app_factory.py tests/test_app_assembly.py tests/test_app_lifecycle.py
pytest -q tests/test_production_lifecycle.py tests/test_serving_loop_lifecycle.py
pytest -q tests/test_restart_smoke.py
```

For a live startup failure, inspect lifecycle state under `app.extensions`
before adding recovery behavior. The state records the current handler,
completed startup phases, shutdown attempts and shutdown errors.
