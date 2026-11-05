# Infrastructure and process runtime

## Entry points and owners

| Concern | Sole owner | Public entry point |
|---|---|---|
| Quart shell and base lifespan | `lib/app/app_factory.py`, `lib/app/app_lifecycle.py` | `create_base_app()` |
| Request bytes and temporary-file overflow | `lib/http_transport/request_body.py` | `DemandRequest`, `DemandRequestBody`; owned ASGI receiver joins I/O before close |
| HTTP assembly | `lib/app/app_assembly.py` | `create_application()`, `configure_application()` |
| Process startup and shutdown | `lib/production_lifecycle.py` | `register_production_lifecycle()` |
| Role-scoped background services | `lib/server/server_background_services.py` | `start_registered_background_services()`, `stop_registered_background_services()` |
| Plugin contributions | `lib/app/plugin_registry.py` | Blueprint / task-runtime discovery and paired startup / shutdown hooks |
| Process-role ownership | `lib/process_roles.py` | `capabilities_for_role()` |
| Native release ownership | `lib/tasks_pkg/native_host.py` | `register_native_host()`, `NativeHost.client()`; explicit image, lazy acquisition, joined shutdown before dependencies |
| Event-loop services | `lib/serving_loop_lifecycle.py` | `register_serving_loop_lifecycle()` |
| Server composition | `server.py` | `create_app()`, `create_production_app()` |
| Storage-free developer composition | `tofu_agent/runtime.py` | `AgentRuntime`, `AgentExecution` |
| Lightweight sidecar and CLI | `tofu_agent/server.py`, `tofu_agent/cli.py` | `create_app()`, `tofu-agent serve` |
| Agent public surface | `lib/agent_core/`, `lib/agent_loop/agent_core_manifest.py` | `lib.agent_core` |
| Push delivery and residency policy | `lib/agent_core/push.py`, `lib/agent_core/push_bus.py`, `lib/agent_core/push_policy.py` | `push_event()`, `PushHub`, `resolve_push_budget()` |
| Background task lifecycle and SSE iteration | `lib/agent_core/task_runtime.py`, `lib/observability.py`, `routes/api_v1/tasks.py` | `TaskRuntime`, `instrument_sse()`, task stream |
| Cross-process leases and counters | `lib/runtime/runtime_state_store.py` | `get_store()` |
| CPython buffer hashing without a per-update GIL handoff | `lib/utils.py` | `sha256_holding_gil()`, `update_holding_gil()`; byte-equivalence and chunk-bound specs in `tests/utils/test_gil_held_digest.py` |
| Helper scripts spawned from the source their caller imported | `lib/runtime/pinned_helper_source.py` | `PinnedHelperSource.path()`; rule in [DEFENSIVE_PATTERNS.md](../DEFENSIVE_PATTERNS.md#long-running-processes-spawn-helpers-from-their-loaded-source) |
| One-job helper children: scrubbed environment, cancellation, joined exit | `lib/runtime/owned_helper_process.py` | `run_owned_helper()`, `join_owned_process()`; adopters: VLM PDF renderer, knowledge OCR |
| Adaptive direct/proxy egress | `lib/netpath.py` | `note_url()`, `decide()`, `report_outcome()` |
| Production teardown; automation effects join before storage closes | `lib/server/server_shutdown.py`, `lib/automations/runtime.py` | `shutdown_production_runtime()`, `stop_workers()` |
| Restart handoff and managed lifecycle generation | `serverctl_pkg/application_restart.py`, `lib/server/server_reexec.py`, `supervisor_protocol.py`, `supervisor.py` | `begin_server_reexec()`, `execute_pending_server_reexec()`, `refresh_supervisor()`, `/reload`, `/restart-deferred` |
| Native storage lifetime across Web restarts | `serverctl_pkg/native_storage_service.py`, `serverctl_pkg/native_storage_host.py`, `lib/storage/supervisor.py` | [Native lifecycle contract](../NATIVE_STORAGE_LIFECYCLE.md); managed daemon reuse, v2 attachment, drained upgrades and full-stop ownership |

`lib.agent_core.push` and `lib.agent_core.task_runtime` are the only module
paths for their capabilities. `scripts/check_architecture.py` rejects retired
facades and imports.

## Assembly and lifecycle

Blueprint registration has no worker-start option or lifecycle side effects.
Embedders that need workers explicitly call the process service owner after
storage initialization. Per-app role latches, reentrant startup protection,
plugin-first shutdown and retained latches after incomplete joins belong to
that owner. Catalog and plugin startup share the same readiness scheduling
boundary; inline embedder behavior and shutdown-before-ready suppression are
preserved. Plugin entry-point groups remain unchanged.

HTTP-approved restarts and the idle auto-restart watcher call the same
`server_reexec.perform_server_reexec()` service. HTTP authorization, cooldown
and response flushing remain in the request boundary; frontend preparation,
Supervisor delegation, acceptance rollback and in-place preparation are owned
by the process lifecycle. Neither headless startup nor restart imports routes.

Optional Codex catalogue discovery runs on its existing owned refresher thread.
Problem: the startup caller repeated a local shared-login command probe before
the worker's own authentication check, adding 3–4 seconds to route-service boot.
Decision: arm the existing worker without that synchronous probe; its first
refresh still checks credentials, and an unauthenticated worker exits without
polling or upstream catalogue access. Stop and start serialize their event
updates with the same ownership lock; a blocked probe remains owned until it
joins. Alternatives rejected: treating login as a readiness prerequisite,
caching an assumed authenticated result, or adding another background pool.
Verify `tests/oauth/test_codex_catalog.py`, process-role and import-isolation tests.

`create_app()` builds an independent application suitable for tests and schema tools. It owns only application-local resources: middleware, blueprints, auth, static delivery, error mapping and logging hooks. Importing it must not start a storage process, worker or network integration.

`create_production_app()` adds the two process lifecycles. Quart's native lifespan is the authority for startup rollback and shutdown. Startup handlers run in registration order; shutdown handlers run in reverse order and all get a cleanup attempt even if one fails.

Both server factories forward `native_image`, `native_root`, `native_token_count` and `native_html_factory` to common assembly. Token counting defaults to `False`, requires a strict boolean plus image, and loads under the joined native host. HTML defaults to `None`; its trusted factory returns a fresh owned pinned binding on first HTML use. `knowledge/html_application.py` joins close off the serving loop. Problem: outer factories must preserve explicit native selections. Decision: freeze application-owned selectors and retain lazy acquisition and joined cleanup. Alternative rejected: global activation bypasses workload admission and ownership.

**Early-serving boot (2026-09-20).** Hypercorn creates its socket only after `before_serving` returns, so holding all nine phases in the lifespan kept the browser from even loading the shell for the full ~24s boot. The lifespan now runs only frontend artifact validation inline (`api`/`all`), then the `BootRunner` (`lib/production_lifecycle.py`) executes phases 2–9 in one serving-loop task, then the readiness gate opens. Rejected alternatives: binding before the lifespan (impossible in Hypercorn 0.18); a second status port (a second authority for the same truth). Truthfulness rules:

- An inline frontend phase (1) failure keeps the fail-fast native rollback contract.
- A background phase failure latches `failed` (phase, error, retry count) and
  the process STAYS UP (owner ruling: show the failure, do not kill the
  listener); `POST /api/boot/retry` re-runs from the failed phase only, and
  phases are revision-idempotent, so retry never double-applies.
- Before latching, a background phase whose typed failure classifies itself
  retryable (`exc.retryable is True`, including storage starvation) is retried
  with capped backoff (`_BOOT_PHASE_RETRY_BACKOFF_S`) inside a fixed 20-minute
  window (`_BOOT_PHASE_AUTO_RETRY_WINDOW_S`). This survives multi-minute
  pressure without an unbounded repair loop; deterministic bugs latch at once.
- Phase 7 owns only readiness-critical workers (route schedulers, proxy state, first-boot key, integration worker). Best-effort sweeps and probes (production resumes, podcast interrupted-marking, netpath prober, LAN responder) and `tofu.startup` plugin hooks run after the gate (`start_deferred_background_services`, post-gate scheduled hooks): they are idempotent and invisible within seconds of ready, and under authority starvation each one re-pays the full storage round trip — serializing them ahead of the gate cost 147-190s on 2026-09-22 versus ~4s healthy.
- `GET /api/boot` exposes the per-phase projection; `/api/health` carries a
  compact `boot` field; `enforce_boot_gate` (`lib/http_transport/request_lifecycle.py`)
  fences non-probe `/api/*` with a truthful 503 (`server_booting` with the
  live phase, `server_boot_failed` with the latched error) until ready.
  Probes, `/api/boot*`, `/api/client-error` and non-API paths stay
  answerable; runner-less assemblies (test factories, embedders) are unfenced.
- The browser projects `/api/v4/boot` into a compact, non-modal dock (`frontend/src/boot-splash.ts`), with expandable phase diagnostics, durations and retry errors. Reading and drafting stay available; shared composer send admission remains closed until ready, including Enter/direct image sends. Backend admission stays default-deny. Failure retains phase-local retry; post-ready storage blips stay with the storage banner. `ProductionStartupSteps.post_ready` owns idempotent reconstructible recovery and the OAuth / world-model catalogue projections (`lib.server.server_background_services.start_post_ready_catalog_sync`) after the gate, never delaying readiness; turn-settlement stays with boot recovery and the backstop. Workspace intent recovery uses configured roots and explicit owner/task registrations; the round commit owner captures file/cwd diagnostics separately. Problem: rescanning modification journals per orphaned task blocks readiness behind project writers, and legacy index duplicates replay identical work. Decision: keep required intent acknowledgment synchronous without journal reads and deduplicate acknowledged owner/project/work identities within each bounded recovery page; failed intents remain retryable. Alternative rejected: moving ownership settlement past readiness would expose unsettled work as claimable.
- Background-command recovery splits the same way: the record scan (which gates wakes and workspace capture) stays in the readiness phase, while the collective background-batch wake, which starts model turns, runs from `_run_post_ready_deferred` for the task-recovery role and is retried by the maintenance tick. Measured 2026-10-23: the in-phase wake cost ~17 s of a 95 s recovery phase, 5 s of it a reaper self-wait on the dispatching thread's own non-reentrant `_dispatch_lock`; `dispatch_next_queued` now skips a nested entry on the owning thread immediately. The dominant remaining cost on large stores is the native Project Brain recovery snapshot (9 pages, 70.6 s holding the authority on that boot), which scales with stored history rather than active work.

**Workspace recovery handoff.** The readiness-critical work-item pass acknowledges durable workspace terminal intent only; the lifecycle-owned integration worker then captures checkpoints and delivers or preserves them. A slow Git scan of one old checkout cannot hold HTTP readiness. Failed intent writes retain active retry eligibility, and task completion never implies delivery. See [restart handoff](../CONTINUOUS_DELIVERY.md#decision-and-execution) and `tests/project_mod/test_startup_workspace_intent.py`.

**Shell-first restart.** Problem: the full-screen splash hid the usable shell, and storage-boundary scanning/spool cleanup held the listener. Decision: keep only artifact publication before serving; prove storage exclusivity as phase 2 before acquiring authority, and clean abandoned tool spools after readiness. A phase-2 failure leaves the shell/diagnostics up and never acquires storage. The existing restart observer docks outside Settings and keeps its identity and approval checks. Completion offers explicit reload to preserve drafts. Diagnostic records come from boot/preparation snapshots, not unrestricted application logs. Alternatives rejected: opening write admission early risks incomplete recovery; a second server/status port duplicates lifecycle and auth ownership; automatic reload discards work composed during recovery. Verification: production-lifecycle slow-boundary gate, boot/composer contracts, shipped-app and responsive dock tests.

An approved or idle-HEAD restart is offered to the host-local Supervisor FIRST for every launch mode (not only `TOFU_MANAGED_BY=supervisor`): the manager kills and respawns the worker from outside, so a restart never depends on the sick process replacing its own image — the root cause of the stuck-retry incident. Only when the manager cannot be reached or refuses does the worker fall back to the in-place re-exec below (that path arms the deadline and releases producers and transports; the exec gate requires a storage-release certificate or exits for new-PID recovery). When EVERY bridge refuses before the shutdown point of no return, the acceptance stamp is rolled back (`rollback_restart_stamp`, PID-guarded) so the next human-approved retry is a real retry instead of a cooldown/`restartPending` refusal against a process that will never replace itself.
One existing Sidecar watcher uses close-on-exec pipe EOF to bind its lease to one process image; no child, live FD, or thread crosses exec. A Supervisor-owned replacement first verifies a SHA-256 generation over the standard-library manager dependency closure; a stale Supervisor re-execs itself without stopping the independent worker, then acknowledges a deferred worker restart before signalling that worker. Human-approved `application-update` and `application-restart` requests proceed after the response-flush window, using the same manager restart as the CLI. An explicit quiescence-wait override still applies; unattended deferred restarts keep the 3600s idle wait and require confirmed quiet. Cooldown 429s report `restartPending` while the accepting PID is still alive, so the UI cannot treat a still-running process as a completed bounce. This prevents a long-lived parent from injecting pre-update concurrency/RSS defaults into every new child; the in-place exec path is now only the no-manager fallback.

**Approved restart latency.** Problem: the UI repeated a 15-second idle wait after human interruption approval, while the CLI immediately entered manager preparation. Independent background-loop joins also accumulated serially before storage release. Decision: remove the redundant approved idle wait and join independent producers concurrently, retaining their individual deadlines and failure policies. Task settlement, translation/browser joins, automation settlement, transport drain, counter flushes and final storage release retain their dependency order. Cancellation joins the outstanding producer cleanup before propagating. Alternatives rejected: shortening the hard deadline risks interrupted durable cleanup; increasing readiness timeouts does not make recovery faster; treating arbitrary `application-*` sources as approved expands interruption authority. Specs: supervisor deferred-restart and production-shutdown lifecycle tests.

The CLI distinguishes an expired foreground wait from a failed boot. A live, identity-checked HTTP readiness response reporting `starting` is not declared stuck merely because its process is old. The command still exits nonzero until readiness is proved and directs the user to idempotent `start` to observe the existing boot. Explicit boot failure and a genuinely unresponsive old worker retain their failure diagnostics. Specs: `tests/server/test_serverctl.py`.

**Scheduler bootstrap and readiness.** Problem: six serial built-in definition commands held HTTP readiness behind storage writes; a full-application profile measured 13.3s in this background-worker phase. Decision: the existing scheduler thread runs those same owner-scoped, atomic reconciliation operations before its first due-task claim. HTTP startup still resumes active timer watchers synchronously. Stop joins the same scheduler owner, skips unstarted bootstrap operations, and cannot certify route-service quiescence while an owner remains live. Production teardown preserves storage dependencies on a failed route-service join. Alternatives rejected: dropping reconciliation loses legacy adoption and configured enablement; claiming before reconciliation can execute stale built-ins; adding another startup thread creates another lifetime to settle. Specs: scheduler built-in/backup ownership, timer resume, process-role and shutdown lifecycle tests. Scheduling semantics remain in [scheduling operations](scheduling_ops.md).

On 2026-10-27, full-application disposable native-daemon trials after these changes measured 3.7s and 7.2s from worker spawn to ready, with 6.9s and 15.5s of observed readiness unavailability. Request-to-ready remained 15.2s and 26.9s, including preparation while the old worker served. The scheduler phase fell to 0.1–0.4s; native PID and service identity stayed unchanged across both replacements. These measurements do **not** establish a few-second end-to-end production guarantee. Source preparation, shutdown and shared-host I/O variation remain in the complete budget; first activation and engine upgrades still need separate cold-recovery evidence.
`lib/hypercorn_runtime.py` owns the programmatic Hypercorn transport boundary. Problem: joining a closed listener before draining active HTTP/WebSocket tasks can block the shutdown lifespan until the process hard deadline. Decision: close admission, drain/cancel connection tasks within the configured grace, then join listeners and run application cleanup. Alternatives rejected: reducing the hard deadline skips durable storage release; globally patching asyncio or installed Hypercorn hides ownership. Its live-socket tests cover unfinished streams, idle peers, short requests, and push. Hypercorn's connection drain defaults to 1s (`TOFU_GRACEFUL_TIMEOUT`): it waits for every open connection task, and a connected browser always holds a long-lived push channel, so any larger fixed window is burned in full on every shutdown before those connections are hard-closed anyway. In-flight agent work drains one layer up (task quiesce plus `TOFU_SHUTDOWN_DRAIN_SECS`), not at the transport. The shutdown bridge polls the signal event at 50ms so a requested restart enters the drain promptly, and the production shutdown stack plus the storage-boundary stop log their durations for restart-budget evidence.

**Restart observation.** Problem: a native database/search recovery took 266s, including 245s in storage, while the browser abandoned verification at 180s and enabled another restart. Decision: 180s warns once; the attempt continues observing with one in-flight health request and retains exclusive ownership until readiness or a latched boot failure. Server phase ordinals drive progress; elapsed time never advances the displayed phase. Clicking restart while a boot is already running adopts that boot without an executable restart POST. A failed boot retries `/api/v4/boot/retry` from its failed phase. Alternative rejected: raising a fixed ceiling still strands slower valid boots and hides real failures. Native stop now interrupts search rebuilding between complete source pages and projection batches, retaining unpublished work and exact dirty tokens for retry. Problem: observed shutdown exhausted its 30s child budget at `maintenance`, before either WAL checkpoint, then the next boot replayed the unretired window. Alternative rejected: increasing the kill budget does not bound a multi-page maintenance round; bypassing source/ACK integrity risks missing search content. Regression: restart-observation unit, retained-runtime and visual tests, plus native cancellation/retry and worker/maintenance neighbors.

**HTTP peer reset ownership.** Problem: a disconnected response send escaped Quart's joined HTTP children and polluted framework/asyncio error logs. Decision: the owned HTTP adapter marks resets and broken pipes at its ASGI send port, joins both children, and contains only those transport failures at DEBUG. The TCP owner also contains its send-port peer failures, including nested task groups; mixed groups preserve unrelated protocol and cleanup errors. Application errors, including application-raised resets and cleanup failures, still propagate; parent cancellation still joins every owned child. That cancelled-parent join applies the same send-port classification: server stop aborts transports before cancelling request tasks, so a streaming response's final frame can find its peer gone, and that disconnect logs at DEBUG without a traceback. Every other cleanup failure, including an application-raised reset, logs `HTTP child cleanup failed` at ERROR with its traceback, because the cancellation, not the child failure, propagates. Alternative rejected: catching all connection errors hides application failures. Regression: `tests/server/test_http_request_lifecycle.py`.

**Storage shutdown budget.** Problem: a fixed 5s child-stop timeout can interrupt native authority/search checkpoints, leaving WAL replay on every subsequent boot (observed: 58.6 MB search WAL and 12.2s storage startup). Decision: storage, the last runtime owner released, receives half of `TOFU_SHUTDOWN_HARD_SECS` (15s at the default 30s process deadline). The existing process hard deadline still bounds the entire drain, and storage-release certification still follows the completed stop. Alternative rejected: shortening the process deadline or skipping checkpoints hides shutdown cost by moving it into startup recovery.

**In-memory counter flushes.** Problem: per-key API usage counters (`lib/usage_tracker.py`) reach disk only when a later `record()` arrives at least 30 s after the previous write, or through `flush()`, and nothing called `flush()` (its docstring also credited `/metrics`, which only reads). Every graceful shutdown therefore dropped the counters recorded since the last write, including the final requests before an idle period. Decision: production shutdown flushes them through `_shutdown_loaded_module('lib.usage_tracker', 'flush')` right after the key-health flush, once dispatch producers and client pools have stopped. Each flush has its own `try`/warning, so one failed store skips neither the other flush nor the event-storage teardown that follows; an unused owner stays unloaded. An abrupt kill still loses the unwritten interval: usage is reconstructible telemetry, not billing authority. Alternatives rejected: an `atexit` hook runs after logging teardown and is skipped when the hard deadline ends the process with `os._exit`; writing on every `record()` puts a JSON rewrite on the request path; a timer thread adds a lifecycle owner for telemetry that the shutdown flush already bounds. Spec: `tests/server/test_server_shutdown_lifecycle.py` (order, off-loop execution, either flush failing).

**Slow-peer shutdown.** A cancelled connection can still await its buffered
writer close indefinitely when the peer stops reading. After the transport
grace, the transport owner aborts its remaining writers before cancelling and
joining connection tasks. Short requests retain their grace to finish. The
alternative of cancelling tasks alone reproduces a blocked shutdown with both
plain HTTP and TLS slow readers; [proxy runtime](../PROXY_RUNTIME.md) owns stall deadlines and port recovery.

**Closed-peer timer ownership.** Problem: after TLS/H2 EOF, an idle timer inside the connection task group can delay producer/connection reclamation until keepalive expiry. Decision: the read owner stops and joins that timer after latching close authority and notifying protocol disconnect, even on faults, before the task group joins. Late idle notifications cannot restart it; the transport still flushes buffered responses under its existing close policy. A real TLS/H2 regression fails with the original read method and passes with this ordering. Reject relying on GC or shortening keepalive: neither establishes quiescence. Repeated Headless H2 diagnostics expose transport residency beyond producer counts; measurement scope and remaining gates.

**HTTP request child ownership.** Problem: cancelling the ASGI request parent can leave its receiver/response tasks alive, including an H2 send blocked on flow control. Decision: both application shells use the lazy shared Quart HTTP class factory in `lib/app/app_lifecycle.py`; every exit cancels/joins only that request's unfinished children, with creation rollback and existing normal error projection. SSE views explicitly close their nested event iterators. Reject GC finalization or global task cancellation: neither preserves scoped quiescence. Repeated diagnostics and scope.

**Startup memory diagnostics.** The runtime self-check uses the same effective
pressure as request admission and lifecycle doctor, retaining raw usage in logs
and falling back conservatively when reclaim evidence is missing. Problem:
post-verification file cache produced a 96.7% raw-use critical alarm with only
62.4% effective pressure. Decision: subtract only observed immediately reclaimable
file cache for this alarm. Alternative rejected: suppressing all memory warnings
would hide real working-set pressure and missing measurement evidence.

**Push transport drain.** Problem: Hypercorn waits for connection tasks before Quart runs its shutdown handlers; an open push WebSocket can therefore keep a managed restart at the worker's hard deadline without entering the production shutdown stack. Decision: the serving-loop shutdown trigger fences new push registrations and wakes existing clients before handing control to Hypercorn. The socket handler then unregisters each client and releases its subscriptions; the ordinary lifespan remains responsible for the push bus and storage. The alternative of closing sockets in the production shutdown handler was rejected because that handler runs after the connection drain it needs to unblock. An exhausted hard deadline writes to the manager console and exits nonzero. Every materialized resource budget records a policy version and the exact automatically generated knob names. A later policy generation may replace only those attributed values; operator/.env values are never inferred from their numbers or overwritten. Runtime resolution validates and clamps a present positive value before consulting the adaptive default, so launch-materialized/operator budgets never trigger an unused filesystem/cgroup probe; missing, malformed, zero, or negative values still derive from the one cached launch observation and remain bounded. Manager status exposes a credential-free live subset, and `serverctl.py doctor` compares the loaded Supervisor generation and worker budget with the next launch before recommending a restart. Doctor memory diagnosis uses the shared `cgroup_guard.reclaimable_file_bytes` rule: subtract immediately reclaimable inactive file cache (hierarchical v1 total or v2 `inactive_file`) before the 90% critical finding, retain raw usage and OOM counters, and fall back to raw pressure when cache evidence is unavailable. Problem: a cache-heavy shared cgroup can appear full while its working set has ample headroom. Decision: report both raw usage and effective pressure. Alternative rejected: raw usage alone incorrectly reports critical pressure during large storage reads; ignoring raw usage entirely would hide uncertainty when `memory.stat` is unreadable. CLI lifecycle POSTs wait up to 1660s for the manager's bounded shutdown, lease release, and frontend preflight before interpreting the response. Problem: a 35s socket deadline reported a failed restart while the manager was still replacing the worker. Decision: cover the manager's 300s shutdown ceiling plus 1320s artifact preflight and bounded release overhead; readiness retains its separate `--wait` limit. Alternative considered: return immediately from the manager POST, but that would require a new durable operation receipt to distinguish an accepted request from a failed replacement. For a non-test checkout the lifecycle manager rejects pytest markers/data at admission, persisted-load, worker adoption, and spawn; a contaminated launch envelope is quarantined and replaced only by the clean declared environment, and an unsafe `.env` stops desired state. A test copy declares a private root under `TOFU_PYTEST_RUN_ROOT`, keeps script/project/data below it, uses a non-15000 port, and revalidates the sole listener PID before either signal. Durable Supervisors outlive their launcher; ephemeral test/dev launches may set `TOFU_SUPERVISOR_OWNER_PID`, whose disappearance stops the owned worker, releases its port, retires the Supervisor, and blocks watchdog restart.

The ASGI lifespan and request path consume prebuilt frontend artifacts and
never invoke Node. Both validate the atomic published graph (entries, recursive
references, safe paths, files, and manifest digest); the manifest is the commit
point, so later source edits cannot withdraw it during recovery or hard refresh.

**Page tags follow manifest publications.** Problem: `GET /` revalidated the
whole graph whenever its 5-second tag cache expired, so nearly every reload paid
for it. Each reference ran `realpath` three times plus `isfile`: about 28,000
network-filesystem metadata calls for 713 references (177 distinct files). Each
call re-acquires the GIL behind busy serving threads. On a loaded worker,
hard-refresh HTML took 3-76 s; one validation took 91 ms in an idle process and
164 s beside two CPU-bound threads. Decision: `lib/vite_assets.py` keys page
tags by declared entry and manifest publication (inode, mtime, size). The
renamed manifest changes inode even when a coarse-mtime mount repeats mtime and
size, so the next page serves a new publication and elapsed time invalidates
nothing. One validation resolves the output root once and accepts each distinct
direct child of the real `assets` directory with a single `lstat`, shared
between graph validation and tag assembly; symlinks, nested names and absent
files keep the exact `realpath`/`isfile` check. A miss now makes 196 metadata
calls (3 ms idle, about 3 s beside two CPU-bound threads) once per publication;
a hit makes one `stat`. Alternatives rejected: a longer TTL is a tuning knob
that still revalidates under load; one `scandir` snapshot releases the GIL per
directory entry (737 with retained generations, 1,474 for validation plus
tags); dropping file checks on the request path would serve a broken graph
instead of failing closed.

Publication retains the pinned current and previous graphs plus dropped generations inside a bounded age/byte window (`scripts/vite_asset_retention.mjs`), so a long-lived tab's content-hashed chunk URLs keep resolving across rebuilds. Before a source-checkout `start`, approved `restart`, or cold manager launch, native engine preparation verifies compiled source identity (see [STORAGE_TOFUDB.md](../STORAGE_TOFUDB.md)); `serverctl.py` validates the manifest's SHA-256 over every authoring input and may rebuild once only for the owning frontend role with local Vite. Content rather than timestamps distinguishes drift, so a valid prebuilt release needs no Node; a mismatched or corrupt graph fails before a live worker is stopped. The stdlib manager invokes the same isolated `prepare-frontend` command for cold starts and automatic crash recovery. Approved restarts prepare once before shutdown for every backend, then pass that exact launch environment (including managed engine pins) through the replacement spawn. Problem: rebuilding changing source after stopping the old worker can refuse the replacement and leave no listener. Decision: consume the successful preparation within that one restart; concurrent lifecycle changes invalidate it before shutdown. The receipt is never persisted or reused by later recovery. Runtime artifact/pin checks remain active. Alternative rejected: repeating a repair-capable preflight after shutdown makes source edits a service outage. Real-process tests prove preparation failure preserves the old listener and successful restart needs no second build. The browser labels missing replacement responses as waiting for the server, reserving readiness wording for an actual boot phase reported by the replacement.

**Settings restart uses fresh control code.** Problem: a serving worker can retain preparation and Supervisor protocol modules from before a source update. Decision: `serverctl_pkg/application_restart.py` runs preparation and deferred manager handoff in fresh interpreters rooted at the target checkout, using the same `serverctl.py` artifact owners and Supervisor generation protocol as CLI restart. The worker retains approval, task protection and HTTP presentation; preparation failure leaves it live and does not consume approval. Only changed managed engine pins return to its environment, guarded against concurrent overrides. Alternatives rejected: reloading live modules mutates serving dependencies; running blocking CLI restart inside the HTTP request can stop the caller before its response flushes. Existing bounded process supervision relays preparation phases, limits logs and joins cancelled descendants; the outer preparation budget is 2010s (native 1200s + frontend 660s + overhead), with no resident process or persistent log added. Three no-work controller round trips measured 0.90–2.30s on this loaded host, excluding builds. Manager-unavailable fallback retains the existing bounded re-exec and stamp rollback contract.

**Managed workers take the current project `.env` (2026-10-23).** Problem: the long-lived Supervisor copied its own environment, a snapshot of whichever shell launched it, into every worker generation and overlaid only the lifecycle allowlist (`SERVER_ENV_KEYS` through `serverEnv`), while the worker's boot `.env` load never overrides an inherited name. Every other `.env` key stayed pinned to the daemon's launch shell: a corrected `CODEX_HOME` never reached restarted workers, which kept reporting `codex_home_unavailable`. Decision: `server_manager.managed_worker_environment()` is the one composition for `LifecycleManager._worker_environment` (restart preparation and every spawn) and `supervisor.do_start`: ambient copy, then every key the project's current `.env` declares (`tofu_dotenv.read_dotenv_values`, re-read per generation), then manager-owned keys (`TOFU_SERVER_WORKER`, `TOFU_MANAGED_BY`, console-log keys, `TOFU_PROJECT_PATH`, `PORT`), then validated `serverEnv`, then probed resource defaults for still-empty knobs. Keys the `.env` does not declare keep their ambient value, so deleting a line does not unset the name: the worker falls back to the Supervisor's ambient value (the launch-shell value, if it had one) until the Supervisor itself is relaunched from a clean environment; an in-place `/reload` keeps the same ambient environment. To clear a name, assign it explicitly empty (`NAME=`): the worker then receives an empty string, which the probed resource defaults and Tofu's `CODEX_HOME` readers (`lib/oauth/codex_*_login.py`) treat as unset; programs the worker launches still inherit the name, empty. Alternative rejected: remembering previously declared keys to unset them later adds hidden persistent state and cannot distinguish a deletion from an intended fallback to the launch environment. Every lifecycle read of the file (this overlay, the `SERVER_ENV_KEYS` view `project_server_env`, and the managed-pin refresh around preparation) goes through the same canonical reader (`_read_project_dotenv` over `read_dotenv_values`), so an unreadable, oversized or non-UTF-8 `.env` reads as empty everywhere: manager construction, restart preparation and `do_start` continue on the ambient composition, and the warning is logged once per distinct (path, failure) per process (at most 32 remembered, oldest evicted; a successful read clears it) rather than on every composition. The worker's own boot loader still rejects such a file (`server.py` exits 2 with `Invalid project .env`). Interpreter/loader bootstrap names (`PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `DYLD_*`; `_DOTENV_EXEC_EXCLUDED_*`) are not overlaid: as before this decision, only the worker's post-start boot loader applies them from `.env`, and only when not inherited, so `.env` can steer the worker's child processes but never which code or native libraries the worker interpreter itself loads; set them in the Supervisor's launch environment to affect the worker. Alternative rejected: overlaying them would silently grant `.env` a capability the boot-loader contract never had. Native-engine and frontend preparation inherit the composed environment. `serverEnv` validation and persistence are unchanged, so an allowlisted key persisted by the last forwarding start/restart still outranks a later `.env` edit until `serverctl.py start`/`restart` forwards it again. Direct `python server.py` launches keep the shell-wins rule of `load_dotenv_file`. Alternatives rejected: making `load_dotenv_file` override globally breaks explicit shell overrides for direct launches; adding `CODEX_HOME` to `SERVER_ENV_KEYS` fixes one key and leaves the class; requiring operators to relaunch the daemon from a clean shell is an unenforced ritual that fails again at the next edit. Activation: `server_manager.py` and `supervisor.py` are in `SUPERVISOR_SOURCE_FILES`, so the next Settings restart (or `serverctl.py restart`) sees a stale generation, `/reload` re-execs the Supervisor in place (same PID and ambient environment) and the replacement worker receives the `.env` values. Verification: the `.env` composition specs in `tests/server/test_server_manager.py` and `tests/server/test_supervisor.py`.

**Exited workers are reaped; zombies are dead (2026-10-25).** Problem: `_spawn` keeps its `Popen` only locally, so `subprocess._cleanup` reaps a dead worker at the manager's next `Popen` only within the image that spawned it. A `/reload` exec discards those objects, so a worker started before the exec and stopped after it stayed a zombie for the Supervisor's lifetime. Signal 0 still reaches a zombie and its cmdline reads empty, so `lib/server_boot/lock.py` counted a zombie lock holder as a live server and refused every replacement boot, and fault-dump pruning kept zombies' dumps. Decision: `runtime_guards.pid_is_alive` (re-exported by `server_manager`) is the one liveness rule for the manager, the instance lock and fault-dump pruning. A zombie (or dead) PID whose last thread exited is dead; a zombie leader with running threads, a PID that refuses signal 0 with `EPERM`, and an unreadable `/proc` stay alive (fail closed), and PIDs `<= 0` are never alive. `_record_planned_exit` and `_record_failure` record `proc_exit_observation` and save it, then `reap_exited_child` waits with `WNOHANG` for exactly that PID, and only when `/proc` shows a zombie whose parent is this process with the recorded start ticks. The module captures its children at import, which `supervisor.py` performs before starting any child, so after a `/reload` exec these are exactly the previous image's children. Each reconcile tick reaps those that have exited, including ones that exit later; every manager's recorded worker stays pending until that manager has observed and reaped it. Children this image starts are never candidates, so every `Popen` keeps its own exit status. Alternatives rejected: `waitpid(-1)` or a `SIGCHLD` reaper steals statuses from other `Popen` objects (preflight, `ss`); comparing child start ticks with a clock is unreliable because this container reports `CLOCK_BOOTTIME` and `/proc` start times on different bases; state-only zombie exclusion would release a lock that a still-running thread holds (see [defensive patterns](../DEFENSIVE_PATTERNS.md)); putting the sweep in `supervisor.py` was rejected because `git merge` rewrites a changed file by unlink and create, and the live Supervisor's ownership sentinel treats a missing `supervisor.py` as ownership loss and stops its worker. Activation: the next `/reload` re-exec reaps leftover zombies within one monitor tick and leaves the adopted live worker alone. Verification: `tests/server/test_supervisor_reexec_children.py` (real `/reload`-style exec), `tests/server/test_server_native_exit.py`, the zombie-holder specs in `tests/server_boot/test_instance_lock_reclaim.py` and `tests/cgroup_guard/test_loop_stall_watchdog.py`.

**Supervisor ownership survives file replacement (2026-10-26).** Problem: `git merge`/`checkout` replace a changed file by `unlink()` then `open(O_CREAT|O_EXCL)` (strace, git 2.34), while the Supervisor polled `os.path.isfile(supervisor.py)` every ≤0.5 s and treated one miss as ownership loss: it persisted desired=stopped and stopped its worker, so any merge touching `supervisor.py` into the live checkout could stop the server. The declared owner (`TOFU_SUPERVISOR_OWNER_PID`) was probed with signal 0, so an exited-but-unreaped owner or a reused PID kept an ephemeral Supervisor alive. Decision: `server_manager.SupervisorOwnership` owns the rule and `ManagerHTTPServer.service_actions` only asks it. Ownership is lost when (1) `supervisor.py` stays missing for `SUPERVISOR_SOURCE_MISSING_GRACE_SECONDS` (10 s) in an intact checkout; (2) it is missing and the checkout directory is gone, or the path names a different directory than at start (`st_dev`/`st_ino`), confirmed by one more poll (`SUPERVISOR_CHECKOUT_MISSING_GRACE_SECONDS`, 0.5 s), because no git operation removes or replaces the checkout root; (3) the declared owner exited (zombie included) or its PID now has other start ticks. Only `ENOENT`/`ENOTDIR` count as missing; any other `stat` error neither confirms nor clears a miss. A miss and its end are logged (`ownership sentinel missing` / `reappeared`). Directory identity is consulted only while the file is missing, so a filesystem that renumbers inodes cannot stop a worker by itself; on this BeeGFS FUSE mount `st_ino` is stable (the git index's inode numbers equal the low 32 bits of current `st_ino` for files untouched since August). A checkout atomically replaced by another that contains `supervisor.py` is not ownership loss, as before. The watchdog's `_owner_alive` (`supervisor.sh`) applies the same zombie rule, so it does not restart a Supervisor that retired for its owner. Alternatives rejected: checking only that the checkout directory exists, because after a move the manager's next state write (`mkdir(parents=True)`) recreates the old path and a moved checkout then kept its Supervisor for the whole file grace; N consecutive misses, which depends on poll cadence when a slow FUSE rewrite spans several polls; watching the file's inode, which every merge changes. Verification: `tests/server/test_supervisor_ownership.py` (real Supervisor across a git-style rewrite, deleted checkout, moved-and-recreated checkout, zombie owner), the ownership specs in `tests/server/test_supervisor.py`, including the zombie-owner watchdog.

**Worker exit status is held until retirement (2026-10-26).** Problem: `_spawn` dropped its `Popen`; CPython parks a dropped, still-running `Popen` in `subprocess._active`, and the manager's next `Popen` (the `ss` port probe in every reconcile, a preflight) reaped the exited worker before `_record_failure` read `/proc`, so 809 recorded failures carry no exit code or signal and no `exit observation` line. Decision: the manager keeps the generation's `Popen` (`_worker_process`) until it retires that generation. On Linux the status is still read from the unreaped zombie's `/proc/<pid>/stat` first, saved, and only then reaped through that `Popen`; without `/proc` the `Popen` return code supplies exit code or signal (`source: popen_returncode`; raw status and core flag unknown). Every retirement logs exactly one `worker process exit observation` line (planned exits at INFO, failures at WARNING), or `... unavailable` with the reason; a generation inherited across `/reload` keeps the inherited-child reaper above. Alternatives rejected: `waitpid(-1)` or a `SIGCHLD` handler (steals the statuses of preflight and `ss` children); recording the failure from a waiter thread (a second writer of lifecycle state racing reconcile). Verification: `test_real_worker_exit_status_survives_the_managers_own_subprocesses` (SIGKILL and stop of a real worker) in `tests/server/test_server_manager.py`.

**A manager judges only workers it can observe (2026-10-26).** Problem: Supervisors on two hosts can serve one network-mounted checkout and share its `data/server-manager-state.json` and `logs/server-manager.log` (observed: the log's `worker failure 1/5 ... cause=unexpected_exit` lines, each about 6 s after a reload and followed by an `automatic-recovery` spawn that only a failing frontend preflight refused, came from a peer whose reload reaped PIDs above this kernel's `pid_max`, while the serving host's Supervisor never reloaded). The peer loads the shared state, whose worker and instance lock name the serving host, so `read_lock_status` says `running: False` (foreign lock), `_launcher_is_alive` looks the PID up in the peer's own PID namespace, and reconcile counted the absence as an exit, rewrote the shared state and tried to start a second worker against the same data directory. A peer `stop()` (for example on its own ownership loss) would also have persisted desired=stopped, which the serving host's manager loads at its next reload and enforces by stopping its worker. Decision: a worker recorded on another host, or an instance lock held by another host, puts reconcile and `status()` in `conflict` with the owning host in `lastError`; reconcile then records no failure, spawns nothing and does not write the state file, `stop()` refuses without persisting desired, and `_launcher_is_alive`/`_remembered_worker_status` never look up a foreign PID locally. Separately, a local worker that is still alive but holds no instance lock after the boot grace is `degraded`, never a failure, for the same reason a stalled loop is retained: counting it would start a second generation beside it. An explicit `start` on another host keeps its behavior, so an operator can move the service; after a host is recreated under a new hostname (this deployment's pod names have changed before), the new host's manager reports `conflict` until that explicit start, because it cannot tell a dead previous host from a live peer. Alternatives rejected: taking over once the shared state's `updatedAt` goes stale (a peer would start a second worker whenever the owner's Supervisor is down past the threshold while its worker still serves); refusing to load a state file written on another host (breaks an operator's host move and still lets a stale peer write); relying on `flock(data/.server.lock)` to stop the second worker (cross-host exclusion on the FUSE mount is unverified, and the spawn already runs preparation and storage startup first). Retiring the peer itself needs an operator on its host. Verification: `test_manager_never_judges_or_replaces_a_worker_recorded_on_another_host`, `test_foreign_instance_lock_blocks_automatic_spawn`, `test_live_generation_without_the_instance_lock_is_never_replaced` in `tests/server/test_server_manager.py`.

**Liveness consumers share the zombie rule (2026-10-26).** The rule moved from `server_manager.py` to the stdlib-only `runtime_guards.py` so `lib`, the boot lock, scripts and the Supervisor import one implementation. Migrated where an exited, unreaped child or a zombie lock holder could read as alive: local model engines (`lib/local_serve/_process.py`; start drops the engine's `Popen` after readiness), fault-dump pruning (`lib/logging_pkg/log_retention.py`; run by the Supervisor, whose dead worker is its child), the storage cutover's Web lock holder (`lib/storage/storage_boundary.py`), the Supervisor's declared owner (`SupervisorOwnership`, replacing `supervisor.pid_is_alive`), the watchdog owner check (`supervisor.sh`) and the sidecar crash benchmark (`scripts/tofudb_perf_compare.py`; the killed sidecar is its own child until the runtime monitor polls it). Kept, because no zombie can be misread there: `lib/compat/_platform.py` `is_process_alive` and `supervisor._pid_is_server` (no production caller; the latter serves only the legacy `read_status`/`do_start`/`do_stop`, which the HTTP handler never calls); `lib/storage_authority_sql/offline.py` (the following flock probe already reports a lockless zombie dead); `lib/storage_authority_sql/preflight.py` (Windows-only branch; POSIX uses flock); `backup_policy.py` and `shipper.py` (temporaries' authors are sidecars, reaped by their StorageSupervisor before the reclaiming successor starts; offline CLIs are waited by their caller; orphans go to PID 1); `lib/tasks_pkg/background_commands.py` (the launching thread waits its `Popen`, recovered records are orphans PID 1 reaps, and the start time is also checked). Verification: `tests/server/test_zombie_liveness_consumers.py` and the zombie-engine spec in `tests/local_serve/test_local_serve_process.py`.

Activation of the four records above: `server_manager.py`, `supervisor.py` and `runtime_guards.py` are Supervisor source files, so the next Settings restart or `serverctl.py restart` re-execs the Supervisor in place; that `/reload` also reaps the zombies the pre-2026-10-25 image left. A running watchdog keeps executing its already-open `supervisor.sh` until it is relaunched.

Frontend validation always runs the target checkout's verifier in a fresh interpreter, including in-app restart of the current checkout. Problem: the serving worker caches validator code and the authoring-input inventory at import time; after an update, Node can build with new rules while that worker rejects the valid graph using old rules. Decision: check the complete graph and source freshness with the target generation before and after repair. Alternatives rejected: repeated rebuilding cannot reconcile different rules; reloading modules inside the serving worker mutates its live dependencies.

**Restart preparation cache.** Problem: preparing a source checkout compiled from a new random directory on every attempt, invalidating Cargo's source-path and mtime evidence; already compiled matching local engines were ignored. Decision: under the existing preparation lock, reuse a matching local release or update-build artifact before downloading or compiling, retaining source, transport, disposable semantic-probe and deployment-pin checks. Builds use one owned `target/update-source-root/packages/tofu-db` snapshot, preserve unchanged file bytes/mtimes, and remove deleted inputs and empty directories. Transitive path dependencies (e.g. `tofu-runtime` and its vendored `tofu-rayon-core`) are build inputs too: they are mirrored as sibling crates beside the snapshot so their Cargo-relative locations still resolve, and the compiled source identity hashes every mirrored crate keyed by package name, so a sibling edit both invalidates the engine and stays compilable from the snapshot. This is reconstructible cache, bounded to one source tree and the existing Cargo target, never user state. Problem: staging the crate directly below `target/` made external root inputs resolve into retained source and fail the unowned-file guard. Decision: mirror the repository layout inside the owned build tree, including external inputs and ownership ledgers. Reusing source files or weakening the guard is rejected; `test_native_engine_root_inputs.py` verifies actual build composition and preservation of existing foreign files, including redirected snapshot ancestors. See [storage operations](../STORAGE_OPERATIONS.md) for optimized restart builds and actual preparation progress. Alternatives rejected: compiling the changing live checkout loses snapshot isolation; trusting a cached path without identity/semantic checks can activate an incompatible engine. Frontend build-lock acquisition waits at most 60s, reports contention and revalidates after acquisition so waiters reuse a new publication. Failure retains the serving process and approval. **Source-change recovery:** source changes during engine preparation are recovered inside that same lock, with at most three attempts and a two-second settling pause, all charged to the original total deadline. A typed source-drift failure discards the stale candidate before publication and refreshes the build snapshot; incompatible artifacts without source drift, storage-configuration changes and cancellation are not replayed. The existing preparation projection reports `retrying` in English and Chinese without starting shutdown or readiness probes. Continued source churn returns an actionable refusal: the server is still running, finish the update/source edits and choose Restart again; no manual build or cache cleanup is required. Once publication has started, a source change refuses activation rather than replaying with stale pins. Problem: a successful compile could become incompatible before publication and force users to guess whether another restart would help. Alternatives rejected: weakening identity checks, endless retries, retrying after shutdown, or silently accepting changed storage selection. The conversation-search generator emits the same declared volume policy for Rust and Python, keeping fresh validation imports consistent with native bounds. Embedded candidate refusals preserve the bounded loader/subprocess error and distinguish malformed metadata, source mismatch and allocator mismatch. Problem: collapsing every attestation failure into an incompatibility message hid load errors and timeouts. Alternative rejected: accepting a library based only on file presence or binary metadata weakens pair admission. Source builders probe Node before use, skip unusable interpreter siblings and PATH candidates, and honor an explicit `TOFU_NODE` executable without fallback. In-app restart preparation captures the bounded build-output tail and includes it in the refusal message for both legacy and v4 problem responses; the browser displays the reason as text and the v4 request ID for log correlation. Problem: stderr-only build failures and discarded v4 detail left operators with an exit code and no cause. Alternative rejected: asking operators to rebuild manually before obtaining evidence. The browser describes application preparation, including the storage engine, rather than mislabeling compilation as frontend work. Live evidence on 2026-09-30: engine compilation consumed 289s, frontend rebuild/validation 48.9s; this optimization targets repeated preparation and does not claim a cold compile is instantaneous. For network/userspace checkouts, the stdlib manager routes CPython bytecode to a verified host-local private namespace keyed by project and interpreter. Its adaptive 16..64 MiB personal budget, 100,000 entries, 64 namespaces, seven-day TTL, 256 MiB reserve and manager-held lease make it disposable and race-safe; explicit Python policy wins and setup failure falls back to normal imports.

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

Learned routing state is only invalidated by a real topology change.
`set_proxy_pool` and `set_bypass_domains` compare the applied value first and
return without clearing health/choice caches, bumping the topology epoch, or
resetting egress/netpath measurements when it is unchanged. Problem: Settings
Save re-POSTs the complete pool and bypass editor state on every save, so an
unchanged save used to invalidate every learned verdict and clear each host's
chosen path — hosts fell back to unmeasured routing, the prober re-probed them
through ~1s proxy round-trips, and user requests took the slow evaluation path
right after a save. Decision: idempotency at the runtime applier, not at the
route, because pool/bypass are also applied at startup and from adapters.
Alternatives rejected: comparing in the HTTP route would leave every other
caller pair invalidating; keeping a "was this a no-change save" flag at the
route cannot survive the concurrent-writer read-modify-write.

The public orchestrator probes are `/health/live`, `/health/ready`, and `/health/startup`. Liveness performs no dependency I/O; readiness/startup project only lifecycle state, process role, and a storage-ready boolean (diagnostics stay authenticated). `/api/health` is the identity/liveness projection, `/api/ready` the dependency-readiness projection used by the personal UI. Readiness carries the serving PID so the manager proves locked-worker identity and dependency readiness in one request; older-generation workers fall back to `/api/health`. A live-but-unready worker is degraded. Probe failure and stale heartbeats preserve the live owner for in-place recovery; see [the reliability contract](../runbooks/RELIABILITY_RUNBOOK.md).

**Pending source changes.** Problem: `/update/check` generated a complete text
patch even after HEAD already proved that the source differed from the frozen
boot fingerprint. Decision: a changed short HEAD establishes `pending=true`;
`git diff --quiet HEAD` supplies the fresh tracked-edit boolean without rendering
the patch. A failed dirty probe reports `dirty=null`. A matching short HEAD
still compares the complete fingerprint, including uncommitted edits and the
full commit identity. The boot fingerprint format and health path are unchanged.
Alternatives rejected: a TTL could hide a new edit; dropping the diff for matching
HEAD would miss the dominant uncommitted-edit workflow. On the live Codelab
checkout, ABBA (eight calls per leg, identical results and stable HEAD) reduced
this changed-HEAD computation from 1744/1689 ms to 561/375 ms wall median,
and 451/462 ms to 108/52 ms process-plus-child CPU median. These are measurements
of the Git computation, not the whole update endpoint or the unchanged-HEAD path.
Verify `tests/boot_identity/`, updater and health-liveness contracts.

**One repository observation per update check.** Problem: each `/update/check`
spawned `git rev-parse --git-dir` for the checkout probe and `git rev-parse HEAD`
twice for the pending-code fingerprint; on a loaded server every child costs
several GIL hand-offs. Decision: `observe_repository()` runs
`git rev-parse --git-dir HEAD` once (rev-parse sets up the repository before
parsing arguments, so the git-dir line proves discovery and HEAD resolves as
`rev-parse HEAD` would); the update check and `pending_code_changes` both take
that observation, and a resolved HEAD is never read again for the fingerprint.
An unresolved HEAD is still re-read. Alternatives rejected: caching checkout
availability or HEAD by inode/mtime is not exact for a repaired or rewritten
repository; deriving the tracked-edit verdict from the TTL-cached `git status`
would serve a stale or differently defined `dirty`. Cold checks remain dominated
by `git status --porcelain`'s untracked-file walk, which the payload's
`blocking` list requires. Against the shared checkout (8 requests per leg),
child processes fell from 5 to 3 (cold) and 4 to 2 (warm status), and warm
checks from 61 to 48 ms idle and 709 to 343 ms with three CPU-bound threads;
payloads were identical. Verify `tests/self_update/test_repository_observation.py`.

**Update source follows the checkout's upstream.** Problem: the updater pulled
a hardcoded `origin`/`main` and compared against GitHub release tags, so an
internal clone whose only branch is `master` (each push one release snapshot
that bumps `VERSION`) could not apply (no remote ref `main`) and its check
reported an unrelated GitHub version. Decision: `resolve_update_upstream()`
gives check and apply one remote/branch: explicit `TOFU_UPDATE_REMOTE` /
`TOFU_UPDATE_BRANCH`, else the current branch's `branch.<name>.remote` /
`branch.<name>.merge`, else `origin`/`main` for a detached or untracked HEAD.
When that remote's URL (`git ls-remote --get-url`) is `github.com/<UPDATE_REPO>`
(https, ssh or scp-like, optional `.git`) or `TOFU_UPDATE_REPO` is set, the
check keeps the release-tags API; otherwise it fetches the upstream and compares
the fetched commit's `VERSION`, with the same payload (`tag` is the fetched
commit), failure kinds, result cache and failure backoff. Non-git installs keep
the tags check and tarball overlay. A cold git-upstream check spawns at most
seven git children per cache period; warm checks spawn none of them.
Alternatives rejected: seeding `TOFU_UPDATE_*` into the internal export fixes
one channel with configuration that drifts from the clone's real upstream;
`git ls-remote` yields a commit, not a release version; reading `VERSION`
through one Git host's REST API ties the check to that host. Verify
`tests/self_update/test_self_update_upstream.py`.

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

`lib/agent_loop/agent_core_manifest.py` is executable documentation for the reusable
agent base. `tests/agent_core/test_agent_core_boundary.py` walks its Python AST and
enforces two dependency rules:

- core reaches concrete tools/providers only through
  `lib.tools.registry` and `lib.llm_dispatch.provider_registry`;
- core reaches persistence only through the `ConversationStore` protocol and
  `lib.agent_core.store`, never by importing `lib.storage`,
  `lib.storage_authority_sql`, or `lib.conversations`.

The lazy `lib.agent_core` facade avoids orchestration imports at package import. `worker_executor.py` owns demand delivery: launch values size idle reuse, its handoff deque carries cancellation until physical entry, and proven-wedged calls retain ownership until they return without blocking new work. `InstrumentedThreadPoolExecutor` keeps asyncio's required stdlib type while delegating scheduling to that shared owner; synchronous routes have no active-count ceiling. Standard per-carrier initialization, broken-pool errors, cancellation, non-daemon interpreter-exit draining and serving-loop idle replacement remain; the facade hands its own `initializer`/`initargs` to that owner because CPython 3.14 removed the stdlib executor's private copies. Metrics expose real ownership without task payloads. Problem: a fixed default pool queued unrelated routes behind busy calls. Decision: one demand owner behind the compatibility facade. Alternatives: larger pools still queue; two schedulers can replay rejected work; daemon-only cleanup can lose a late accepted write. The 64-call asyncio rendezvous and subprocess-exit tests verify startup and teardown.

## Push and shared runtime state

`PushHub` owns subscriptions/local delivery; `push_bus.py` feeds its one delivery function from the in-process or Redis transport, with per-socket bounded lossy events plus bounded control/RPC lanes. Each socket's reconstructible event backlog is bounded by `TOFU_PUSH_EVENT_QUEUE_CAPACITY` (default 16,384, the envelope maximum, independent of host size and worker hints; explicit values 64..16,384); a stalled socket that fills its backlog is disconnected so it reconciles from durable state rather than growing without limit. Local fan-out serializes once per target. The browser's liveness pings may set literal `buildProbe: true`; the priority pong then carries the optional `buildId` from the startup-validated `lib.vite_assets` snapshot via one single-flight manifest refresh; filesystem validation never delays the pong, and an old peer omitting either field stays fail-quiet. Transient saturation drops oldest; after 15 seconds and 256 drops it disconnects for durable reconciliation, while half-drain or five quiet seconds resets grace. One oversized/unencodable frame disconnects immediately. Episode logs expose channel/type/task/socket and byte counters, never payload. Push receive silence expires after 180 seconds; every inbound frame renews the deadline. The browser normally sends proof-of-life every 4 seconds, or 20 in a hidden tab, leaving room for background timer clamping. Expiry joins both socket halves, closes connection-owned RPC work, and unregisters subscriptions before a replacement claims capacity. Problem: a proxy can retain a dead browser's TCP connection for hours; observed evidence showed 23 unreleased generations from one browser exhausting its 24-slot owner budget. Decision: bound receive silence at the route lifetime. Alternative rejected: raising capacity only delays the leak and keeps obsolete event queues resident. A peer's TCP `ETIMEDOUT` during writer cleanup is an ordinary disconnect; unrelated cleanup errors still surface. `runtime_state_store.py` is the sole lease/counter/heartbeat substrate for admission accounting and cross-replica subscription state. Callers never depend on Redis directly; degradation preserves process availability while authorization and owner filtering remain fail-closed. SSE routes have no per-principal connection quota; route close/teardown owns release. Conversation Sync keeps one page-generation owner per active subscription: replacement closes the old generator and releases its lease synchronously, while a 204 stop response ends native EventSource retry for a superseded stream. The broker's inactive generation history is bounded by `TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY` with a 128-entry floor; it never retains events or conversation projections. A 10-second response-body start deadline also reclaims admission when a downstream disconnects before ASGI enters the generator.

On a network/FUSE data root, `fs_keepalive.py` owns one interruptible 15-second coordinator (an Event deadline rather than half-second sleep fragments) and one serialized metadata-probe daemon. At most one stat batch may be in flight: if the mount enters uninterruptible sleep, later deadlines report that exact probe instead of accumulating abandoned threads. Shutdown retains a stuck composite owner and refuses to start a duplicate until the kernel call returns. The probe is also liveness evidence: `filesystem_unresponsive()` is true while a stat stays unanswered past `STAT_TIMEOUT_S`, and `filesystem_resumed_at()` records when such a stat returned. `lib/tasks_pkg/liveness.py` treats the first as an explained wait for every agent and never counts silence before the second.

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

Recovery diagnostics record start and successful completion for each named readiness-critical/best-effort substep and attempt, including elapsed seconds. Retry and fail-closed behavior stay with the existing recovery owners. Native startup/stop logs emit bounded one-shot `startup_timing` / `shutdown_timing` records for engine open, overlay replay, search open, driver joins, overlay merge and authority/search checkpoints. Problem: nine broad boot phases hid multi-second substeps and a 31-second storage stop. Decision: measure existing ownership boundaries. Alternative rejected: shrinking durability or stop budgets would move recovery cost into the next boot. The client-side stop owner also records pooled-client close, owner-pipe EOF, child exit/return code, and a warning before a timeout kill. A clean parent exit therefore cannot conceal a child that exhausted its checkpoint budget.

**Storage recovery without watchdog restart loops.** Problem: the wire catalog
classifies `project_brain.recovery.snapshot` as maintenance even though its IR
has no writes or receipts. It therefore held the exclusive authority while
hydrating historical projects. On 2026-10-26, active-work recovery repeatedly
crossed the 10-second authority watchdog threshold; the supervisor recycled
storage at least 15 times and readiness stayed in phase 4 for over 54 minutes.
Decision: run this certified read through the existing published-snapshot
dispatcher shared by daemon and embedded transports. Authentication, deadlines,
owner-bound cursors, and the maintenance wire contract stay at their existing
boundaries. Actual work settlement remains transactional and readiness-critical.
Recovery pages yield between projects after one second, retain the 1000-row
ceiling, and resume after the last consumed project document even when the
active result is empty (one document per project; see
[project-document recovery walk](../STORAGE.md#restart-readiness-and-recovery)). One project's hydration is indivisible, so this is a cooperative
page target, not a one-second hard I/O deadline. Log each page's duration, active
project count and continuation state without recording user data.
Alternatives rejected: raising/disabling the watchdog hides real wedged writers;
declaring readiness before settlement exposes orphaned work; increasing RPC
timeouts retains the exclusive-lock bottleneck. Verify published recovery while
both the writer FIFO and authority mutex are held, unchanged WAL/outbox state,
owner denial, yielded/deleted cursors, and the Python recovery/cutover neighbors.

Deployment evidence (2026-10-26): the previous process reached 26 storage
restarts and terminal fencing. With the fix, the same configured native store
completed authority startup in 111.267s and recovery in 135.326s with zero
storage restarts. These are cold/recovery measurements after the crash loop,
not a warm-restart promise. The remaining history reads are observable page by
page. Verification: 2,088 native unit tests passed (15 explicitly ignored),
52 SQLite/native application cases and five Project Brain differential cases
passed against the prepared binary. The subsequent optional-import failure
was recovered by retrying phase 6, without repeating storage recovery.

Project Brain hydration reuses the versioned-document decoded-value reader.
Problem: the byte-returning port parsed an entire history tree for validation,
discarded it, and the projection reader parsed it again. Decision: validate the
stored namespace/key before blob I/O, reuse the existing owner-bound verified
materializer, and transfer its single strict JSON value to projection identity
validation. Alternatives rejected: an unchecked byte port loses JSON corruption
checks; a separate parser duplicates the format authority. Namespace/key,
blob-owner, byte-bound, malformed Unicode, numeric and recursion checks remain
executable specifications. This removes redundant parsing and copying; it does
not by itself establish a minute-scale cold-start improvement.

**Optional imports and readiness.** Problem: critical-import validation loaded
`search_bridge`, whose package facade eagerly loaded optional search providers;
a partially initialized vertical registry then prevented boot after storage
recovery had completed. Decision: keep the bridge behind the existing
`ensure_search_runtime()` first-use boundary, including in actual readiness
validation. A failed required import retains its traceback. Alternative rejected:
catching and hiding search failures would break the first-use error contract.
Verify cold readiness with search imports refused and normal/concurrent bridge
activation independently; registering routes alone does not exercise this phase.

**Concurrent tofu-search first imports.** Problem: first uses race. A task's
tool-schema lookup may import `tofu_search.search.vertical` while another thread
is inside the `tofu_search` facade. Vertical handlers imported siblings through
their package, which waits for the package and, through `__import__`, for the
facade; both eagerly import those handlers, so CPython's deadlock avoidance
handed each thread a partially initialized module (the phase-6 failure above).
Decision: tofu-search keeps its module-level import graph acyclic, counting those
hidden waits; package modules bind siblings with `importlib.import_module` and
import names only from concrete modules. CPython also holds a module's lock while
loading its missing parent package, so each subpackage loads every child the
facade needs and the facade enters `tofu_search.search` through the package
(package `tests/test_import_graph.py`, `tests/test_concurrent_import.py`).
Alternatives rejected: serializing host imports cannot cover the direct
submodule users (paper, research, browser, citation audit, summaries); a lazy
facade would let the `tofu_search.search` subpackage overwrite the public
`search()` function; function-local imports would scatter the patchable HTTP
seam. Residual: CPython can still report a deadlock when two threads start the
same absent package within its spec lookup; no module-level change removes that.

**Joined artifact preparation.** Problem: the manager's 1320-second timeout
could kill a valid native-plus-frontend preparation whose inner budgets total
1890 seconds; frontend `subprocess.run` also left compiler descendants behind
on timeout and captured unbounded output. Decision: `serverctl_pkg/restart_budget.py`
composes native queue/build, frontend queue/build and cleanup into the same
2010-second preparation budget for the application and manager. The CLI's
2370-second lifecycle request covers preparation, configured shutdown and lease
release. Native compilation still uses the remaining shared 1200-second clock,
reserving 30 seconds for validation/publication. Unused queue time remains
available; no inner operation renews the clock.

Frontend, native and manager preparation use the existing bounded subprocess
owner, with live progress or captured diagnostics, process-group teardown and
joined children. SIGTERM unwinds the preparation CLI so nested native sessions
also close before its parent exits. Failure leaves the old service running and
releases the build lock. Supervisor generation identity includes this runner,
the deadline owner and the actual stdlib worker-resource dependency closure;
otherwise a manager can retain old control code after an update. Alternatives
rejected: extending only one timeout repeats the mismatch at another layer;
killing just the leader leaks compilers; skipping candidate verification risks
activating a mismatched database engine. Verify the real descendant-timeout,
output-budget and budget-composition cases in
`tests/server/test_restart_preparation_safety.py`, manager/CLI process roundtrips,
and the Supervisor re-exec test.

**Reusable native builds.** Problem: deriving release `codegen-units` from
available memory changed Cargo's compilation profile between restarts. Missing
optional `.cargo` directories in the private snapshot also made every build
script run stale ([Cargo's documented missing-input behavior](https://doc.rust-lang.org/cargo/faq.html#why-is-cargo-rebuilding-my-code)).
Decision: keep `opt-level=3`, Thin LTO and incremental compilation, use a fixed
16 codegen units, and leave Cargo's jobserver and explicit operator overrides
in control. Host observations run only when `CARGO_LOG` requests diagnostics;
a failed observation cannot refuse compilation. Ordinary restarts do not run
that probe. Snapshot synchronization preserves empty watched `src`/`.cargo`
roots, directory mtimes and unchanged file bytes for every path dependency.
New/deleted files and configuration still invalidate the same compiled source
identity; empty directories add no identity bytes. One manifest read scope
serves snapshot discovery, and obsolete cache candidates do not each trigger
another full source-tree hash. Live source/configuration checks remain before
compilation, validation and publication. Alternatives rejected: dropping
optional-directory watches misses new inputs; skipping identity checks can
activate stale code; reducing release optimization trades serving performance
for restart speed. The real Cargo freshness/identity tests prove an unchanged
snapshot reports a fresh artifact and transitive edits still rebuild.

Native compiler state uses `local_placement`'s `native-build` purpose under
`native-builds/<canonical-project-sha256>`, sharing the existing runtime-cache
mode (`TOFU_RUNTIME_REPLICA`) and `TOFU_LOCAL_DIR`/XDG placement policy. Auto mode
requires a private executable local volume with measured benefit. Local storage
failure or a declined placement uses the checkout cache; it never refuses
work. Isolated certification retains its explicitly owned build root. The
checkout's existing engine preparation lock serializes both cache locations;
old shared candidates remain eligible for identity/semantic verification.

This is reconstructible compiler state, separate from deployed engines and
user databases. There is one source snapshot per checkout and Cargo caches
per host ABI/build environment, not another directory per restart or source
revision. Cargo owns incremental-history retention; no new background worker
or automatic deletion is introduced. An unused project cache may be removed
while holding that checkout's preparation lock; losing it only requires a
rebuild. On the measured Linux host the private snapshot plus one release
cache occupied 2,590,270,536 bytes (2.41 GiB), a measurement rather than a cap.
Two real builds of unchanged source measured 41.876 seconds before watched-root
normalization and 2.647 seconds after (93.7% less, including snapshot refresh).
A new local cache took 159.035 seconds to build; disposable semantic,
owner-scoped write/read and reclaim-boundary probes passed. These figures
measure artifact preparation, not production shutdown/recovery latency.
Shared Cargo registry-lock contention can still add queue time: a later
unchanged-source sample took 25.6 seconds while waiting for that external lock,
without recompiling the engine. The private target cache does not change the
operator's Cargo registry/toolchain configuration.

**Source-build compiler selection (2026-10-20).** Problem: a restart refused
with `cc1: error: -Werror=date-time: no option -Wdate-time` after compiling
~190 crates. Cargo applies `.cargo/config.toml` `[env]` only to variables
absent from its environment, and the preparation child synthesized `CC` from
the first `cc`/`gcc`/`clang` on PATH, so it silently outranked the crate's own
devtoolset pin and the image's gcc 4.8 failed inside `libmimalloc-sys` instead
of at preflight. Decision: the crate's declared `[env] CC` is the first
candidate, PATH candidates follow, and a candidate is accepted only after
compiling a one-line program with `-Wdate-time` — the flag cc-rs adds for
libmimalloc-sys. The check runs in the build child only, on the deployment
PATH, and never mutates the caller's environment or the tree. Alternatives
rejected: trusting file existence and executability (what the failing image
provided), hardcoding a devtoolset path (breaks portable launches), letting
cargo's own use of the pin suffice (it does not, because `CC` was already
set), and removing the `-Wno-error=date-time` workaround (upstream owns it).
Refusal is actionable: install gcc 4.9 or newer, or provide a matching
artifact. Verification: `tests/server/test_native_engine_update.py`
pin-priority, unusable-pin fallback and refusal cases, plus a real isolated
`--prepare-only` build on the affected host.

**No tracked crate compiler pin (2026-10-24).** Problem: `packages/tofu-db/
.cargo/config.toml` pinned `/opt/rh/devtoolset-9/root/usr/bin/gcc`; the build
host no longer has `/opt/rh` (its system compiler is gcc 12.3), so every plain
`cargo build/test` in the crate failed inside cc-rs before compiling, while the
source-build preparation above already skipped the dead pin. Decision: the
crate declares no compiler; plain cargo uses the PATH `cc`, the preparation
child keeps its `-Wdate-time` capability probe, and a host whose default
compiler is too old exports `CC` for plain cargo. Alternatives rejected:
re-pinning another absolute path (one image change repeats the breakage, and a
tracked path leaks one host's layout into every checkout) and a wrapper that
probes before cargo (plain cargo bypasses it). Verification: plain
`cargo test` in the crate with no `CC` exported on the affected host.

**Updater Git executable admission.** Problem: the first Git on PATH may exist but fail to load on the host ABI, hiding the boot source fingerprint. Decision: the updater probes PATH candidates in order with `git --version` (3s timeout each), then Windows install locations, and caches the first runnable Git for the process lifetime. No repository operation is retried on another binary. Alternative rejected: trusting file existence preserves the ABI failure; hardcoding a host-specific Git path breaks portable launches. If no candidate runs, the ordinary Git command failure remains visible to its caller.

**Late HTTP/2 body and TLS-close containment.** Problem: an early response may remove an ASGI stream before its trailing DATA arrives; upstream dispatch then raises `KeyError`, breaking other requests on that connection. Decision: an instance-owned lazy event adapter drops only dispatch to removed ASGI streams and acknowledges their bytes once. Live stream events and protocol parsing remain upstream-owned; HTTP/2 upgrades install the same guard on their next read. TLS shutdown additionally contains only `APPLICATION_DATA_AFTER_CLOSE_NOTIFY` when the writer is already closing, after upstream timer teardown. Other TLS errors propagate. Alternatives rejected: patching installed/global protocol classes loses ownership; swallowing every protocol/TLS error hides real faults; raising on late bytes breaks an otherwise valid early-response connection. Repeated real TLS cancellation and flow-credit/other-stream tests own this seam.


**Deferred control protects active work (2026-10-09).** Problem: a request labelled `application-test` matched the broad `application-*` short-wait policy and restarted a live worker with six running tasks. Decision: only the declared `application-update` and `application-restart` actions retain the approved interruption policy. Ordinary deferred requests cancel on timeout, unknown quiescence or probe failure; only a boolean quiet observation permits shutdown. Test-labelled mutations are refused by the Supervisor outside disposable pytest projects, and the deferred-control client rejects inherited test context before refreshing a production manager, even with a supplied clean environment. Alternatives rejected: extending the timeout only postpones interruption; automatically replaying stopped tools can duplicate side effects; hiding the interrupted status would misrepresent durable execution. These controls add no resident worker or persistent state. Verification: `tests/server/test_supervisor.py`.

## Worker runtime replicas on slow volumes

**Problem (2026-10-25).** On network/FUSE hosts the managed worker imports
its interpreter, standard library and site-packages through the same slow
volume as the checkout: every import probe, stat and failed open pays a
network round trip (median uncached lookup 327 µs on DolphinFS versus 5.6 µs
on the local overlay). **Decision.** `serverctl_pkg/runtime_replica.py`
keeps a verified, immutable local copy of the worker's Python environment
(a standalone prefix such as a conda env, or a virtualenv rebased onto a
replica of its base) under the root chosen by
`serverctl_pkg/local_placement.py`, and the manager spawns the worker from
it. Placement is automatic (`TOFU_RUNTIME_REPLICA=auto`): only a source the
mount table classifies as network/userspace (or a measured lookup above
50 µs where no mount table exists) moves, only to a private, executable,
non-temporary local root at least 3x faster with the shared byte/inode reserve
([index and cache capacity policy](../INDEX_FASTPATH.md)); the root is
`$XDG_CACHE_HOME/tofu/runtime-replicas`, then
`~/.cache/tofu/runtime-replicas` (tools commonly redirect XDG directories
onto shared volumes), or `$TOFU_LOCAL_DIR/runtime-replicas` alone. `on`
skips the speed comparison; `off` disables the mechanism.

Lifecycle and invariants:

- Selection at spawn never copies. It describes the interpreter once,
  compares a cheap fingerprint (directory listings of `bin`, `lib`, the
  stdlib, site-packages and `conda-meta`, plus distribution metadata and
  `.pth` stats; bytecode writes do not count) with the published manifest,
  and leases the generation (shared `flock`). Measured cost here: 0.2-0.3 s.
- A missing or stale replica runs the source interpreter; one detached,
  `nice` builder starts only after that worker reports ready, so copying
  never competes with boot. It copies with 16 fixed threads, hard-links
  files unchanged since the previous generation, rewrites internal absolute
  symlinks and prefix `.pth` entries, removes write permission, re-checks the
  fingerprint, and verifies the copy by running it (prefix, base prefix,
  standard modules, no mapped source files, identical package listing)
  before an atomic `live` symlink swap. Failures back off from 30 min to
  24 h per source fingerprint; a crashed builder counts as a failure;
  layouts verification cannot reproduce stay unsupported until the source
  changes; a space shortage retries once the space returns.
- Generations are immutable under a running interpreter. Collection removes
  only non-live generations that no lease (manager, and the worker's own
  lifetime lease from `server_boot_env`) holds and no live process maps
  (`/proc/*/maps`, which covers children that outlive their worker). Whole
  registries retire after 30 idle days, when the source interpreter is gone
  while its parent directory is visible, or to make room for a replica in
  demand. Replicas are reconstructible caches, never install targets.
- Installs, restarts and launchers resolve the source interpreter through
  `runtime_replica.authoritative_interpreter`: self-update requirements,
  the ffmpeg bootstrap, in-place re-exec, restart control children and the
  Supervisor's `TOFU_SUPERVISOR_PYTHON`. Environment re-exec guards accept a
  replica of the marked environment (`runs_marked_environment`).
- A worker that fails before readiness on a replica makes that generation
  suspect: the next spawn uses the source interpreter. Readiness there
  rejects the generation (three rejections of one source stop rebuilding
  it); a second failure there clears the suspicion, so an unrelated boot
  failure never discards a good replica.

Measurements on this host (conda env, 10.4 GB, 149,896 files): first build
152 s wall, 368 MB RSS, in the background; median of five runs, source
versus replica: interpreter startup 0.116 s vs 0.041 s, a stdlib plus
dependency import set 1.37 s vs 0.65 s, `import lib.app.app_assembly`
1.42 s vs 0.48 s. Disk growth is one environment copy per source plus a
transient second generation during a rebuild, sharing unchanged files. The
manager's bytecode cache is keyed by the source interpreter, so project
bytecode survives a new generation; the generation's own standard library
and packages compile into it once, and the sweep below removes the copies
of removed generations.

**Alternatives considered.** Copying the environment synchronously at
start delayed launch by minutes. Running from the mutable `live` alias
would mix generations inside one process during a rebuild. Copying into
`/tmp` exposes files to age-based cleaners and slower, erratic fsync.
Replicating whole checkouts or switching the data root weakens durability;
the checkout stays on the data volume and task checkouts move only by
operator choice (`TOFU_LOCAL_WORKSPACES`, see
[project_integration.md](project_integration.md)). Importing the storage
authority's `describe_mount` into the manager would load the application
package before its dependencies are known to work; `local_placement`
mirrors it, and `tests/server/test_local_placement.py` keeps the two equal.
Verification: `tests/server/test_local_placement.py`,
`tests/server/test_runtime_replica.py` (including a real virtualenv
replica) and `tests/server/test_runtime_replica_manager.py`. Inspect with
`python -m serverctl_pkg.runtime_replica status` and
`python -m serverctl_pkg.local_placement`; reclaim with
`python -m serverctl_pkg.runtime_replica prune [--all]`.

## Host-local bytecode caches

**Problem (2026-10-25).** The managed worker's `PYTHONPYCACHEPREFIX`
namespace (`serverctl_pkg/python_bytecode_cache.py`) had a 64 MiB size quota
enforced at launch by deleting whole inactive namespaces. The commands the
worker runs inherit the prefix, so pytest in every worktree and scratch tree
wrote into it: measured 1.8 GiB, 61,739 live and 45,962 dead `.pyc` files.
At every restart the quota deleted the server's own namespace, and each
boot recompiled the whole server from the network volume. A quota cannot
recognise dead bytecode: 722 MiB (39%) belonged to source directories that
no longer existed.

**Decision (owner, 2026-10-25): bytecode caches have no size quota.**
Retention removes exactly what nothing can use, shared by the server cache
and the run-command cache (`lib/project_mod/python_bytecode_cache.py`):

- Bytecode whose source directory was provably removed. The prefix mirrors
  each source directory's absolute path, so a top-down sweep removes a dead
  subtree with one lookup and never descends into it. It runs in a detached,
  niced process (`python -m serverctl_pkg.python_bytecode_cache sweep`),
  requested on healthy manager passes and after run-command releases, at
  most once per six hours per namespace. Measured on that namespace: 6,021
  directories checked in 5.2 s cold and 1.4 s warm.
- Namespaces unused for seven days, or whose recorded project, workspace or
  interpreter (`.identity.json`) was provably removed.
- While the cache volume is below the shared placement reserve (max(2 GiB,
  5%)), other unused namespaces, oldest first; if space is still short the
  cache is skipped for that launch or command, which never fails.

"Provably" means "does not exist" seen from a reachable, non-empty parent;
an I/O error or an unmounted volume's empty mount point never counts. The
namespace being activated and every namespace with a live lease are never
removed. Tool commands keep sharing the server namespace, so `cd wt &&
pytest` never writes `__pycache__` onto the network volume, a shape the
run-command cache cannot serve.

**Alternatives considered.** A larger quota postpones the same wipe and
still cannot tell live bytecode from dead. Pruning whole namespaces by size
deletes the bytecode in use. Ending the inheritance would send the most
common test shapes back to in-tree `__pycache__` on the network volume.
Checking every `.pyc` against its source costs one network lookup per file
(about ten times the directory sweep) for little extra reclamation. A sweep
on the launch path delays boot by seconds. The `TOFU_RUN_PYTHON_CACHE_MAX_MIB`
and `TOFU_SERVER_PYTHON_CACHE_MAX_MIB` launch settings and the run cache's
file, namespace and reserve knobs were removed. Verification:
`tests/server/test_server_python_bytecode_cache.py` and
`tests/tasks_pkg/test_run_command_python_cache.py`.

## File-backed request demand

**Problem:** a raw Quart body could accumulate large multipart data in RAM; video input was refused independently by the picker, HTTP layer and scratch receiver at 512 MiB. **Decision:** the existing ASGI receiver/handler owns one request buffer that spills to an anonymous local file. Its RAM preference derives from one application startup capacity/headroom probe, scales upward and downward, and permits zero. Multipart consumers read 64 KiB transport chunks; consumed file prefixes are reclaimed when the buffer drains. RAM allocation failure follows the same disk path without dropping the prefix. Blocking file work uses the serving loop's existing executor; cancellation joins accepted I/O before closing the file. Multipart scratch streams register with their request before parsing completes, so incomplete/error paths close them too. No new queue, authority or idle worker is added. Explicit body caps still apply to complete received bytes, including consumed chunks. The default app/video source path has no private byte quota; other route and decoder policies remain separate boundaries under review. **Alternatives considered:** merely raising caps postpones refusal; removing caps without spooling risks accumulated RAM; creating another ASGI task owner would duplicate the existing receiver/handler lifecycle. A synthetic 513 MiB transport at zero RAM preference completes with exact SHA-256 and closed scratch ownership; tests cover pressure/fallback, allocation failure, consumed-byte accounting and cancellation during a blocked write. The initial observed run processed 537,919,488 bytes in 1.285 seconds on this host; this is a synthetic transport measurement, not video analysis throughput or a cross-machine claim.

## HTTP source ownership

Problem: inbound and outbound HTTP policy accumulated as unrelated top-level modules. Decision: `lib/http_transport/` owns these modules, with explicit assembly entry points and no eager imports. Alternatives rejected: increasing the top-level module allowance hides ownership drift; compatibility facades preserve two import paths. Existing transport, cancellation and shutdown tests retain behavior. <!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
