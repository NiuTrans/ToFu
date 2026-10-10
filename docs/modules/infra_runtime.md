# Infrastructure and process runtime

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
| Push delivery and residency policy | `lib/agent_core/push.py`, `lib/agent_core/push_bus.py`, `lib/agent_core/push_policy.py` | `push_event()`, `PushHub`, `resolve_push_budget()` |
| Background task lifecycle and SSE iteration | `lib/agent_core/task_runtime.py`, `lib/observability.py`, `routes/api_v1/tasks.py` | `TaskRuntime`, `instrument_sse()`, task stream |
| Cross-process leases and counters | `lib/runtime_state_store.py` | `get_store()` |
| Adaptive direct/proxy egress | `lib/netpath.py` | `note_url()`, `decide()`, `report_outcome()` |
| Production teardown; automation effects join before storage closes | `lib/server_shutdown.py`, `lib/automations/runtime.py` | `shutdown_production_runtime()`, `stop_workers()` |
| Restart handoff and managed lifecycle generation | `serverctl_pkg/application_restart.py`, `lib/server_reexec.py`, `supervisor_protocol.py`, `supervisor.py` | `begin_server_reexec()`, `execute_pending_server_reexec()`, `refresh_supervisor()`, `/reload`, `/restart-deferred` |

`lib.agent_core.push` and `lib.agent_core.task_runtime` are the only module
paths for their capabilities. `scripts/check_architecture.py` rejects retired
facades and imports.

## Assembly and lifecycle

`create_app()` builds an independent application suitable for tests and schema tools. It owns only application-local resources: middleware, blueprints, auth, static delivery, error mapping and logging hooks. Importing it must not start a storage process, worker or network integration.

`create_production_app()` adds the two process lifecycles. Quart's native lifespan is the authority for startup rollback and shutdown. Startup handlers run in registration order; shutdown handlers run in reverse order and all get a cleanup attempt even if one fails.

**Early-serving boot (2026-09-20).** Hypercorn creates its socket only after
`before_serving` returns, so holding all nine phases in the lifespan kept the
browser from even loading the shell for the full ~24s boot. The lifespan now
runs only the two listener-critical phases inline — validate frontend
artifacts (`api`/`all`), prove the exclusive storage boundary — then the
`BootRunner` (`lib/production_lifecycle.py`) executes phases 3–9 in one
serving-loop task, then the readiness gate opens. Rejected alternatives:
binding before the lifespan (impossible in Hypercorn 0.18); a second status
port (a second authority for the same truth). Truthfulness rules:

- An inline phase (1–2) failure keeps the fail-fast native rollback contract.
- A background phase failure latches `failed` (phase, error, retry count) and
  the process STAYS UP (owner ruling: show the failure, do not kill the
  listener); `POST /api/boot/retry` re-runs from the failed phase only, and
  phases are revision-idempotent, so retry never double-applies.
- Before latching, a background phase whose typed failure classifies itself
  retryable (`exc.retryable is True`, including storage starvation) is retried
  with capped backoff (`_BOOT_PHASE_RETRY_BACKOFF_S`) inside a fixed 20-minute
  window (`_BOOT_PHASE_AUTO_RETRY_WINDOW_S`). This survives multi-minute
  pressure without an unbounded repair loop; deterministic bugs latch at once.
- Phase 7 owns only readiness-critical workers (route schedulers, proxy
  state, first-boot key, integration worker). Best-effort sweeps and probes
  (production resumes, podcast interrupted-marking, netpath prober, LAN
  responder) and `tofu.startup` plugin hooks run after the gate
  (`start_deferred_background_services`, post-gate scheduled hooks): they
  are idempotent and invisible within seconds of ready, and under authority
  starvation each one re-pays the full storage round trip — serializing them
  ahead of the gate cost 147-190s on 2026-09-22 versus ~4s healthy.
- `GET /api/boot` exposes the per-phase projection; `/api/health` carries a
  compact `boot` field; `enforce_boot_gate` (`lib/http_request_lifecycle.py`)
  fences non-probe `/api/*` with a truthful 503 (`server_booting` with the
  live phase, `server_boot_failed` with the latched error) until ready.
  Probes, `/api/boot*`, `/api/client-error` and non-API paths stay
  answerable; runner-less assemblies (test factories, embedders) are unfenced.
- The browser turns the same window into a cold-start splash
  (`frontend/src/boot-splash.ts`): per-phase progress from `/api/boot`
  (legacy fallback: `storage.ready`), self-dismissal on first ready, retry
  on failure; post-ready storage blips stay with the storage banner.
  `ProductionStartupSteps.post_ready` owns idempotent reconstructible
  recovery and the OAuth / world-model catalogue projections
  (`routes.start_post_ready_catalog_sync`) after the gate, never delaying
  readiness; turn-settlement stays with boot recovery and the backstop. Workspace intent recovery uses configured roots and explicit owner/task registrations; the round commit owner captures file/cwd diagnostics separately. Problem: rescanning modification journals per orphaned task blocks readiness behind project writers, and legacy index duplicates replay identical work. Decision: keep required intent acknowledgment synchronous without journal reads and deduplicate acknowledged owner/project/work identities within each bounded recovery page; failed intents remain retryable. Alternative rejected: moving ownership settlement past readiness would expose unsettled work as claimable.

An approved or idle-HEAD restart is offered to the host-local Supervisor FIRST for every launch mode (not only `TOFU_MANAGED_BY=supervisor`): the manager kills and respawns the worker from outside, so a restart never depends on the sick process replacing its own image — the root cause of the stuck-retry incident. Only when the manager cannot be reached or refuses does the worker fall back to the in-place re-exec below (that path arms the deadline and releases producers and transports; the exec gate requires a storage-release certificate or exits for new-PID recovery). When EVERY bridge refuses before the shutdown point of no return, the acceptance stamp is rolled back (`rollback_restart_stamp`, PID-guarded) so the next human-approved retry is a real retry instead of a cooldown/`restartPending` refusal against a process that will never replace itself.
One existing Sidecar watcher uses close-on-exec pipe EOF to bind its lease to one process image; no child, live FD, or thread crosses exec. A Supervisor-owned replacement first verifies a SHA-256 generation over the standard-library manager dependency closure; a stale Supervisor re-execs itself without stopping the independent worker, then acknowledges a deferred worker restart before signalling that worker. Application-initiated deferred restarts (`source=application-*`) wait at most 15s for `/health/quiescence` then replace the worker even if tasks remain; unattended deferred restarts keep the 3600s idle wait. Cooldown 429s report `restartPending` while the accepting PID is still alive, so the UI cannot treat a still-running process as a completed bounce. This prevents a long-lived parent from injecting pre-update concurrency/RSS defaults into every new child; the in-place exec path is now only the no-manager fallback.
`lib/hypercorn_runtime.py` owns the programmatic Hypercorn transport boundary. Problem: joining a closed listener before draining active HTTP/WebSocket tasks can block the shutdown lifespan until the process hard deadline. Decision: close admission, drain/cancel connection tasks within the configured grace, then join listeners and run application cleanup. Alternatives rejected: reducing the hard deadline skips durable storage release; globally patching asyncio or installed Hypercorn hides ownership. Its live-socket tests cover unfinished streams, idle peers, short requests, and push. Hypercorn's connection drain defaults to 1s (`TOFU_GRACEFUL_TIMEOUT`): it waits for every open connection task, and a connected browser always holds a long-lived push channel, so any larger fixed window is burned in full on every shutdown before those connections are hard-closed anyway. In-flight agent work drains one layer up (task quiesce plus `TOFU_SHUTDOWN_DRAIN_SECS`), not at the transport. The shutdown bridge polls the signal event at 50ms so a requested restart enters the drain promptly, and the production shutdown stack plus the storage-boundary stop log their durations for restart-budget evidence.

**HTTP peer reset ownership.** Problem: a disconnected response send escaped
Quart's joined HTTP children and polluted framework/asyncio error logs.
Decision: the owned HTTP adapter marks resets and broken pipes at its ASGI send
port, joins both children, and contains only those transport failures at DEBUG.
The TCP owner also contains its send-port peer failures, including nested task
groups; mixed groups preserve unrelated protocol and cleanup errors.
Application errors, including application-raised resets and cleanup failures,
still propagate; parent cancellation still joins every owned child. Alternative
rejected: catching all connection errors hides application failures. Regression:
`tests/server/test_http_request_lifecycle.py`.

**Storage shutdown budget.** Problem: a fixed 5s child-stop timeout can interrupt
native authority/search checkpoints, leaving WAL replay on every subsequent
boot (observed: 58.6 MB search WAL and 12.2s storage startup). Decision: storage,
the last runtime owner released, receives half of `TOFU_SHUTDOWN_HARD_SECS`
(15s at the default 30s process deadline). The existing process hard deadline
still bounds the entire drain, and storage-release certification still follows
the completed stop. Alternative rejected: shortening the process deadline or
skipping checkpoints hides shutdown cost by moving it into startup recovery.

**Slow-peer shutdown.** A cancelled connection can still await its buffered
writer close indefinitely when the peer stops reading. After the transport
grace, the transport owner aborts its remaining writers before cancelling and
joining connection tasks. Short requests retain their grace to finish. The
alternative of cancelling tasks alone reproduces a blocked shutdown with both
plain HTTP and TLS slow readers; [proxy runtime](../PROXY_RUNTIME.md) owns stall deadlines and port recovery.

**Closed-peer timer ownership.** Problem: after TLS/H2 EOF, an idle timer
inside the connection task group can delay producer/connection reclamation
until keepalive expiry. Decision: the read owner stops and joins that timer
after latching close authority and notifying protocol disconnect, even on faults,
before the task group joins. Late idle notifications cannot restart it; the transport still
flushes buffered responses under its existing close policy. A real TLS/H2
regression fails with the original read method and passes with this ordering.
Reject relying on GC or shortening keepalive: neither establishes quiescence.
Repeated Headless H2 diagnostics expose transport residency beyond producer
counts; [measurement scope and remaining gates](../evidence/system_http_runtime_20261007.json).

**HTTP request child ownership.** Problem: cancelling the ASGI request parent
can leave its receiver/response tasks alive, including an H2 send blocked on
flow control. Decision: both application shells use the lazy shared Quart HTTP
class factory in `lib/app_lifecycle.py`; every exit cancels/joins only that
request's unfinished children, with creation rollback and existing normal error
projection. SSE views explicitly close their nested event iterators. Reject GC
finalization or global task cancellation: neither preserves scoped quiescence.
[Repeated diagnostics and scope](../evidence/system_http_runtime_20261007.json).

**Startup memory diagnostics.** The runtime self-check uses the same effective
pressure as request admission and lifecycle doctor, retaining raw usage in logs
and falling back conservatively when reclaim evidence is missing. Problem:
post-verification file cache produced a 96.7% raw-use critical alarm with only
62.4% effective pressure. Decision: subtract only observed immediately reclaimable
file cache for this alarm. Alternative rejected: suppressing all memory warnings
would hide real working-set pressure and missing measurement evidence.

**Push transport drain.** Problem: Hypercorn waits for connection tasks before
Quart runs its shutdown handlers; an open push WebSocket can therefore keep a
managed restart at the worker's hard deadline without entering the production
shutdown stack. Decision: the serving-loop shutdown trigger fences new push
registrations and wakes existing clients before handing control to Hypercorn.
The socket handler then unregisters each client and releases its subscriptions;
the ordinary lifespan remains responsible for the push bus and storage. The
alternative of closing sockets in the production shutdown handler was rejected
because that handler runs after the connection drain it needs to unblock.
An exhausted hard deadline writes to the manager console and exits nonzero.
Every materialized resource budget records a policy version and the exact automatically generated knob names. A later policy generation may replace only those attributed values; operator/.env values are never inferred from their numbers or overwritten. Runtime resolution validates and clamps a present positive value before consulting the adaptive default, so launch-materialized/operator budgets never trigger an unused filesystem/cgroup probe; missing, malformed, zero, or negative values still derive from the one cached launch observation and remain bounded.
Manager status exposes a credential-free live subset, and `serverctl.py doctor` compares the loaded Supervisor generation and worker budget with the next launch before recommending a restart. Doctor memory diagnosis uses the shared `cgroup_guard.reclaimable_file_bytes` rule: subtract immediately reclaimable inactive file cache (hierarchical v1 total or v2 `inactive_file`) before the 90% critical finding, retain raw usage and OOM counters, and fall back to raw pressure when cache evidence is unavailable. Problem: a cache-heavy shared cgroup can appear full while its working set has ample headroom. Decision: report both raw usage and effective pressure. Alternative rejected: raw usage alone incorrectly reports critical pressure during large storage reads; ignoring raw usage entirely would hide uncertainty when `memory.stat` is unreadable.
CLI lifecycle POSTs wait up to 1660s for the manager's bounded shutdown, lease release, and frontend preflight before interpreting the response. Problem: a 35s socket deadline reported a failed restart while the manager was still replacing the worker. Decision: cover the manager's 300s shutdown ceiling plus 1320s artifact preflight and bounded release overhead; readiness retains its separate `--wait` limit. Alternative considered: return immediately from the manager POST, but that would require a new durable operation receipt to distinguish an accepted request from a failed replacement.
`TOFU_RESOURCE_PROFILE=throughput` dedicates the probed CPU capacity and 90% of
available memory to foreground Agent work, retaining per-task RSS admission,
finite queues, cancellation and teardown; it changes resource policy only, with
`balanced` the desktop default, the manifest recording the profile, and operator
overrides surviving a switch.
For a non-test checkout the lifecycle manager rejects pytest markers/data at
admission, persisted-load, worker adoption, and spawn; a contaminated launch
envelope is quarantined and replaced only by the clean declared environment, and
an unsafe `.env` stops desired state. A test copy declares a private root under
`TOFU_PYTEST_RUN_ROOT`, keeps script/project/data below it, uses a non-15000
port, and revalidates the sole listener PID before either signal.
Durable Supervisors outlive their launcher; ephemeral test/dev launches may set
`TOFU_SUPERVISOR_OWNER_PID`, whose disappearance stops the owned worker, releases
its port, retires the Supervisor, and blocks watchdog restart.

The ASGI lifespan and request path consume prebuilt frontend artifacts and
never invoke Node. Both validate the atomic published graph (entries, recursive
references, safe paths, files, and manifest digest); the manifest is the commit
point, so later source edits cannot withdraw it during recovery or hard refresh.

Publication retains the pinned current and previous graphs plus dropped
generations inside a bounded age/byte window
(`scripts/vite_asset_retention.mjs`), so a long-lived tab's content-hashed
chunk URLs keep resolving across rebuilds.
Before a source-checkout `start`, approved `restart`, or cold manager launch, native engine preparation verifies compiled source identity (see [STORAGE_TOFUDB.md](../STORAGE_TOFUDB.md));
`serverctl.py` validates the manifest's SHA-256 over every authoring input and
may rebuild once only for the owning frontend role with local Vite. Content
rather than timestamps distinguishes drift, so a valid prebuilt release needs
no Node; a mismatched or corrupt graph fails before a live worker is stopped.
The stdlib manager invokes the same isolated `prepare-frontend` command before
every worker spawn, including automatic crash recovery, so a long-lived manager
cannot bypass the gate after source files change. Native restarts also run it before shutdown; preparation failure keeps the old worker live.

**Settings restart uses fresh control code.** Problem: a serving worker can
retain preparation and Supervisor protocol modules from before a source update.
Decision: `serverctl_pkg/application_restart.py` runs preparation and deferred
manager handoff in fresh interpreters rooted at the target checkout, using the
same `serverctl.py` artifact owners and Supervisor generation protocol as CLI
restart. The worker retains approval, task protection and HTTP presentation;
preparation failure leaves it live and does not consume approval. Only changed
managed engine pins return to its environment, guarded against concurrent
overrides. Alternatives rejected: reloading live modules mutates serving
dependencies; running blocking CLI restart inside the HTTP request can stop the
caller before its response flushes. Existing bounded process supervision relays
preparation phases, limits logs and joins cancelled descendants; the outer
preparation budget is 2010s (native 1200s + frontend 660s + overhead), with no
resident process or persistent log added. Three no-work controller round trips
measured 0.90–2.30s on this loaded host, excluding builds. Manager-unavailable fallback retains
the existing bounded re-exec and stamp rollback contract.

Frontend validation always runs the target checkout's verifier in a fresh
interpreter, including in-app restart of the current checkout. Problem: the
serving worker caches validator code and the authoring-input inventory at
import time; after an update, Node can build with new rules while that worker
rejects the valid graph using old rules. Decision: check the complete graph and
source freshness with the target generation before and after repair.
Alternatives rejected: repeated rebuilding cannot reconcile different rules;
reloading modules inside the serving worker mutates its live dependencies.

**Restart preparation cache.** Problem: preparing a source checkout compiled
from a new random directory on every attempt, invalidating Cargo's source-path
and mtime evidence; already compiled matching local engines were ignored.
Decision: under the existing preparation lock, reuse a matching local release
or update-build artifact before downloading or compiling, retaining source,
transport, disposable semantic-probe and deployment-pin checks. Builds use one
owned `target/update-source` snapshot, preserve unchanged file bytes/mtimes,
and remove deleted inputs and empty directories. Transitive path dependencies
(e.g. `tofu-runtime` and its vendored `tofu-rayon-core`) are build inputs too:
they are mirrored as sibling crates beside the snapshot so their Cargo-relative
locations still resolve, and the compiled source identity hashes every mirrored
crate keyed by package name, so a sibling edit both invalidates the engine and
stays compilable from the snapshot. This is reconstructible cache,
bounded to one source tree and the existing Cargo target, never user state.
See [storage operations](../STORAGE_OPERATIONS.md) for optimized restart builds and actual preparation progress.
Alternatives rejected: compiling the changing live checkout loses snapshot
isolation; trusting a cached path without identity/semantic checks can activate
an incompatible engine. Frontend build-lock acquisition waits at most 60s,
reports contention and revalidates after acquisition so waiters reuse a new
publication. Failure retains the serving process and approval. Source builders probe Node before use, skip unusable interpreter siblings and PATH candidates, and honor an explicit `TOFU_NODE` executable without fallback. In-app restart preparation captures the bounded build-output tail and includes it in the refusal message for both legacy and v4 problem responses; the browser displays the reason as text and the v4 request ID for log correlation. Problem: stderr-only build failures and discarded v4 detail left operators with an exit code and no cause. Alternative rejected: asking operators to rebuild manually before obtaining evidence. The browser
describes application preparation, including the storage engine, rather than
mislabeling compilation as frontend work. Live evidence on 2026-09-30: engine
compilation consumed 289s, frontend rebuild/validation 48.9s; this optimization
targets repeated preparation and does not claim a cold compile is instantaneous.
For network/userspace checkouts, the stdlib manager routes CPython bytecode to a verified host-local private namespace keyed by project and interpreter.
Its adaptive 16..64 MiB personal budget, 100,000 entries, 64 namespaces, seven-day TTL, 256 MiB reserve and manager-held lease make it disposable and race-safe; explicit Python policy wins and setup failure falls back to normal imports.

HTTP assembly compresses eligible whole responses outside the serving loop. Static artifacts may use their content-addressed 48-entry/8 MiB cache ceiling; dynamic responses are never cached. Bodies below 1 MiB and every distributed response use Brotli q4/gzip 6; personal-mode bodies >=1 MiB use Brotli q2/gzip 1 to bound CPU. This topology has no per-request inference or unbounded queue; the application default executor remains the execution boundary.

`lib/process_roles.py` is the sole role-to-capability table. API replicas own
frontend/catalog/request services, workers own task recovery/execution, and
schedulers own timed jobs and event maintenance. Personal mode is always
`all`; distributed split roles must not infer ownership from which route or
module happened to be imported.

`lib/netpath.py` owns process-local direct/proxy scoring; real request outcomes
are the freshest signal. Its lifecycle worker probes only a path whose bounded
deadline is due (new/failed routes wake it immediately, failures back off to the
ceiling, healthy traffic postpones synthetic requests, a host idle 24h is not
rearmed); the tracked-host ceiling is 64 and persisted timestamps are clamped
before they can schedule work.

The public orchestrator probes are `/health/live`, `/health/ready`, and
`/health/startup`. Liveness performs no dependency I/O; readiness/startup project
only lifecycle state, process role, and a storage-ready boolean (diagnostics stay
authenticated). `/api/health` is the identity/liveness projection, `/api/ready`
the dependency-readiness projection used by the personal UI. Readiness carries the
serving PID so the manager proves locked-worker identity and dependency readiness
in one request; older-generation workers fall back to `/api/health`. A
live-but-unready worker is degraded. Probe failure and stale heartbeats preserve
the live owner for in-place recovery; see [the reliability contract](../runbooks/RELIABILITY_RUNBOOK.md).

`/api/health` additionally carries `bootProfile`: the previous boot's measured per-phase startup timings (`{phases: [{key,label,index,duration_s}], ...}`), persisted by `lib/boot_profile.py` at readiness and keyed by the same 9-phase ordinal/key catalog as `lib/startup_phases.py`. The in-app restart progress bar (`frontend/src/runtime/sections/update.js`) budgets progress from this host's own observed boot shape; the field is absent on a first-ever boot, where the client falls back to its built-in weight estimate. A boot never fails over this telemetry: persist/load are best-effort and schema-gated. The bar reports three truthful stages: request preparation, a bounded drain band labelled shutdown that eases while the old process still answers, then the measured boot timeline anchored at the first failed health probe (old process gone); the changed-bootId check makes early answers from the old process harmless.

Successful high-frequency `/api/live` and `/api/ready` probes are filtered from the durable access log; repeated failures retain their first-two, power-of-two, and five-minute checkpoints while request metrics keep the exact count. Probe cadence and fault detection are unchanged.

The lightweight sidecar owns a separate, deliberately smaller ASGI assembly: it
imports `tofu_agent.runtime`, not `server.py`, and exposes only health,
capabilities, agent submission, process-memory task replay, abort, and custom
tool resolution. A non-loopback bind is default-deny without a bearer token; it
must never grow storage, accounts, billing, conversation, or frontend lifecycles.

## Storage and identity

The supervised storage process is the only durable storage authority. Personal
mode defaults to SQLite and can explicitly select native Tofu-DB under the
selection and migration contract in `docs/STORAGE.md`; distributed mode uses
PostgreSQL. Application assembly never imports or owns a driver,
connection, transaction, schema, or database path.

User identity is resolved at the HTTP/auth boundary and is then an explicit
parameter of repositories, project coordination and push delivery. A frame
with durable or user-visible state must carry its owner; absence of an owner is
not permission to broadcast. `PERSONAL_USER_ID` may be selected only at a
composition boundary for the personal deployment.

## Agent-core boundary

`tofu_agent.AgentRuntime` composes this core with an explicit principal and marks
tasks transient before their first event; the manager's durable birth/event/result/
project-feed/latest/supersede/affinity paths all skip such tasks. This is the only
supported storage-free composition; adapters must not implement another agent loop
or monkeypatch persistence after submission.

`lib/agent_core_manifest.py` is executable documentation for the reusable
agent base. `tests/agent_core/test_agent_core_boundary.py` walks its Python AST and
enforces two dependency rules:

- core reaches concrete tools/providers only through
  `lib.tools.registry` and `lib.llm_dispatch.provider_registry`;
- core reaches persistence only through the `ConversationStore` protocol and
  `lib.agent_core.store`, never by importing `lib.storage`,
  `lib.storage_sidecar`, or `lib.conversations`.

The lazy `lib.agent_core` facade avoids orchestration imports at package import. `worker_executor.py` owns demand delivery: launch values size idle reuse, its handoff deque carries cancellation until physical entry, and proven-wedged calls retain ownership until they return without blocking new work. `InstrumentedThreadPoolExecutor` keeps asyncio's required stdlib type while delegating scheduling to that shared owner; synchronous routes have no active-count ceiling. Standard per-carrier initialization, broken-pool errors, cancellation, non-daemon interpreter-exit draining and serving-loop idle replacement remain. Metrics expose real ownership without task payloads. Problem: a fixed default pool queued unrelated routes behind busy calls. Decision: one demand owner behind the compatibility facade. Alternatives: larger pools still queue; two schedulers can replay rejected work; daemon-only cleanup can lose a late accepted write. The 64-call asyncio rendezvous and subprocess-exit tests verify startup and teardown.

## Push and shared runtime state

`PushHub` owns subscriptions/local delivery; `push_bus.py` feeds its one delivery function from the in-process or Redis transport, with per-socket bounded lossy events plus bounded control/RPC lanes: 8 GiB reference 64 sockets / 12 per owner / 1,000 events / 4 MiB / 2 MiB per frame; distributed 256 / 64 / 1,000 / 16 MiB / 8 MiB; local fan-out serializes once per target. The browser's liveness pings may set literal `buildProbe: true`; the priority pong then carries the optional `buildId` from the startup-validated `lib.vite_assets` snapshot via one single-flight manifest refresh; filesystem validation never delays the pong, and an old peer omitting either field stays fail-quiet.
Transient saturation drops oldest; after 15 seconds and 256 drops it disconnects for durable reconciliation, while half-drain or five quiet seconds resets grace. One oversized/unencodable frame disconnects immediately. Episode logs expose channel/type/task/socket and byte counters, never payload.
Push receive silence expires after 180 seconds; every inbound frame renews the
deadline. The browser normally sends proof-of-life every 4 seconds, or 20 in a
hidden tab, leaving room for background timer clamping. Expiry joins both socket
halves, closes connection-owned RPC work, and unregisters subscriptions before a
replacement claims capacity. Problem: a proxy can retain a dead browser's TCP
connection for hours; observed evidence showed 23 unreleased generations from
one browser exhausting its 24-slot owner budget. Decision: bound receive silence
at the route lifetime. Alternative rejected: raising capacity only delays the
leak and keeps obsolete event queues resident. A peer's TCP `ETIMEDOUT` during
writer cleanup is an ordinary disconnect; unrelated cleanup errors still surface.
`runtime_state_store.py` is the sole lease/counter/heartbeat substrate for admission, SSE limits and cross-replica subscription state. Callers never depend on Redis directly; degradation preserves process availability while authorization and owner filtering remain fail-closed.
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

On a network/FUSE data root, `fs_keepalive.py` owns one interruptible 15-second coordinator (an Event deadline rather than half-second sleep fragments) and one serialized metadata-probe daemon. At most one stat batch may be in flight: if the mount enters uninterruptible sleep, later deadlines report that exact probe instead of accumulating abandoned threads. Shutdown retains a stuck composite owner and refuses to start a duplicate until the kernel call returns.

## Change rules

- Add process-wide work through a named lifecycle handler with an idempotent shutdown owner; never start work during module import.
- Route HTTP concerns to `app_assembly.py`, loop-bound concerns to `serving_loop_lifecycle.py`, and required boot phases to `ProductionStartupSteps`.
- Do not add a second app factory, readiness gate, task registry, push hub, storage mode or error taxonomy.
- Keep blocking filesystem/subprocess/network work off the Quart event loop.
  Read-only imports create no directories; one launch snapshot serves every flag.
- Put ownership keys in durable/task data before work leaves the request
  context. Worker threads must not infer a user from globals.
- A recovery job must be idempotent and operate through the authoritative
  repository. Do not mask failed boot recovery with an unbounded periodic
  repair loop.

## Verification ladder

Run `python3 scripts/check_architecture.py`, then the focused suites in
`tests/agent_core/test_agent_core_boundary.py`,
`tests/app_assembly/`, `tests/app_lifecycle/`, `tests/app_factory/`,
`tests/serving_loop_lifecycle/`, and `tests/agent_core/test_restart_smoke.py`.
For live startup failures, inspect `app.extensions` lifecycle state: it records
the current handler, completed phases, shutdown attempts and errors.

## Writable path selection

`runtime_path_policy.py` owns the data/log base policy. Runtime data and logging share one locked startup snapshot, so concurrent imports and directory creation cannot split their roots. Frozen portable-install probes use an exclusive temporary file and never truncate a fixed filename. The explicit resolver accepts an environment mapping; source/read-only resolution performs no directory creation, config path lookup uses the same snapshot without creating the data directory, and mutation boundaries create their own parents. Conversation diagnostics merge project `.env` then process overrides and use that same environment for backend, directory and fastpath discovery; a frozen read-only diagnostic needs an explicit data root instead of performing a writable probe.

Recovery diagnostics record start and successful completion for each named
readiness-critical/best-effort substep and attempt, including elapsed seconds.
Retry and fail-closed behavior stay with the existing recovery owners. Native
startup/stop logs emit bounded one-shot `startup_timing` / `shutdown_timing`
records for engine open, overlay replay, search open, driver joins, overlay
merge and authority/search checkpoints. Problem: nine broad boot phases hid
multi-second substeps and a 31-second storage stop. Decision: measure existing
ownership boundaries. Alternative rejected: shrinking durability or stop
budgets would move recovery cost into the next boot.
The client-side stop owner also records pooled-client close, owner-pipe EOF,
child exit/return code, and a warning before a timeout kill. A clean parent
exit therefore cannot conceal a child that exhausted its checkpoint budget.

Engine precompilation shares the existing 600-second preparation deadline.
Problem: the fixed 420-second compile cap killed a healthy first release build
while the outer preparation clock still had time. Decision: use the actual
remaining outer budget and reserve 30 seconds for validation/publication; an
exhausted reserve refuses compiler spawn. Alternatives rejected: extending the
whole preparation deadline or bypassing identity/library verification. Compiler
concurrency remains derived from launch CPU and memory headroom.

**Updater Git executable admission.** Problem: the first Git on PATH may
exist but fail to load on the host ABI, hiding the boot source fingerprint.
Decision: the updater probes PATH candidates in order with `git --version`
(3s timeout each), then Windows install locations, and caches the first runnable
Git for the process lifetime. No repository operation is retried on another
binary. Alternative rejected: trusting file existence preserves the ABI failure;
hardcoding a host-specific Git path breaks portable launches. If no candidate
runs, the ordinary Git command failure remains visible to its caller.

**Late HTTP/2 body and TLS-close containment.** Problem: an early response may
remove an ASGI stream before its trailing DATA arrives; upstream dispatch then
raises `KeyError`, breaking other requests on that connection. Decision: an
instance-owned lazy event adapter drops only dispatch to removed ASGI streams
and acknowledges their bytes once. Live stream events and protocol parsing
remain upstream-owned; HTTP/2 upgrades install the same guard on their next read.
TLS shutdown additionally contains only `APPLICATION_DATA_AFTER_CLOSE_NOTIFY`
when the writer is already closing, after upstream timer teardown. Other TLS
errors propagate. Alternatives rejected: patching installed/global protocol
classes loses ownership; swallowing every protocol/TLS error hides real faults;
raising on late bytes breaks an otherwise valid early-response connection.
Repeated real TLS cancellation and flow-credit/other-stream tests own this seam.


**Deferred control protects active work (2026-10-09).** Problem: a request
labelled `application-test` matched the broad `application-*` short-wait policy
and restarted a live worker with six running tasks. Decision: only the declared
`application-update` and `application-restart` actions retain the approved
interruption policy. Ordinary deferred requests cancel on timeout, unknown
quiescence or probe failure; only a boolean quiet observation permits shutdown.
Test-labelled mutations are refused by the Supervisor outside disposable pytest
projects, and the deferred-control client rejects inherited test context before
refreshing a production manager, even with a supplied clean environment.
Alternatives rejected: extending the timeout only postpones interruption;
automatically replaying stopped tools can duplicate side effects; hiding the
interrupted status would misrepresent durable execution. These controls add no
resident worker or persistent state. Verification: `tests/server/test_supervisor.py`.
