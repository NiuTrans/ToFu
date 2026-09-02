# Changelog

All notable changes to tofu-open are documented in this file.

## [Unreleased]

### Changed
- Aligned every live LLM stream transport with native Codex's rolling idle
  contract. Sync SSE, async SSE, and Responses WebSocket attempts now stop only
  after 300 seconds with no transport activity; comments, keep-alives, metadata
  events, reasoning, text, and tool traffic all renew the window. The former
  semantic-progress deadline is no longer armed, its environment names migrate
  as deprecated aliases for the transport-idle duration, and true silence uses
  the existing bounded stream-interruption retry path. Historical
  `semantic_progress_timeout` timelines remain readable.
- Routed in-place update and idle-HEAD restarts through Hypercorn/Quart's
  bounded production shutdown before the main thread calls `execv`. The old
  thread-owned path skipped the lifespan, orphaned its Storage Sidecar under
  the unchanged worker PID, and made the fresh image wait on its own project
  lease; one observed restart failed storage startup twice and required a
  73-second manager recovery. Restart now stops admission, quiesces producers,
  closes transports, releases the sole storage authority, preserves the PID
  and endpoint only after cleanup, and falls back to manager recovery if the
  final exec cannot replace the stopped image.
- Removed repeated network-volume metadata work from the Python package import
  boundary. An isolated cold `import server` spent 78.9 seconds wall time but
  only 1.67 seconds on CPU: `lib` initialization alone held 32.0 seconds, with
  eager config-directory creation, repeated reads of one `features.json`, and
  a pricing import that pulled in the shared HTTP stack. Config paths are now
  filesystem-pure until a writer creates their parent, all flags resolve from
  one reloadable launch snapshot, credential-key creation owns its missing
  parent, and pricing loads HTTP only inside an explicit online refresh. A
  repeat import of the early instance-lock module fell from 1.784 to 0.930
  seconds (48%) while leaving pricing and HTTP modules unloaded.
- Routed managed server-worker bytecode for network/userspace checkouts into a
  verified host-local cache instead of repeatedly reading every `.pyc` body
  through the source mount. Project and interpreter fingerprints isolate the
  namespace; a manager-held shared lease protects live workers while
  launch-time symlink-safe LRU maintenance enforces a probed 16..64 MiB
  personal budget (128 MiB distributed, 512 MiB hard cap), 100,000 entries, 64
  namespaces, seven-day TTL, and a 256 MiB free-space reserve. Operator Python
  policy wins and any cache failure falls back to the unchanged launch. In a
  four-run full `import server` comparison, network-cache baselines were 6.934
  and 5.697 seconds, local first fill was 8.707 seconds, and local reuse was
  4.274 seconds (about 32% below the baseline mean) for 32.3 MB / 1,785 files.
- Removed tofu-search, trafilatura/lxml, and PDF extraction from ordinary server
  boot, handler registration, empty-input failures, unrelated config reloads,
  and the first conservative search-tool schema. One concurrency-safe activation
  boundary now installs the PyMuPDF classic policy, current config, browser, and
  auth providers at the first valid search/fetch use; independent research,
  paper, browser, PDF-export, and API entry points cross the same resource guard.
  Three unchanged-process imports averaged 3.477 seconds and 100,712 KiB peak RSS
  versus the measured 6.316-second / 155,696-KiB eager baseline (about 45% less
  startup wall time and 35% less peak memory); first search pays the deferred
  import once. Before activation, `vertical=auto|off` remains available without
  importing metadata; runtime-derived explicit verticals appear on later turns.
- Split the generated API v4 release latch from its dormant strict DTO/OpenAPI
  runtime. Ordinary v1 server boot now imports only dependency-free constants;
  the same generator still owns every artifact, while Pydantic adapters and the
  canonical document load on the first actual v4 response. All 22 v4 contract
  tests retain strict validation and generated-artifact drift checks. In three
  follow-up imports, Pydantic stayed absent and mean peak RSS fell from 100,712
  to 93,041 KiB (about 7.6%); mean wall time was 3.400 seconds versus 3.477 in
  the immediately preceding three-run sample.
- Removed dormant paper generation engines from HTTP route registration. The
  paper-deepen task authority now lives in a lightweight runtime module, while
  its agent loop loads only when a section is actually deepened. Podcast route
  seams remain monkeypatch-compatible but load the worker on first use, and a
  background interruption sweep no longer pulls in the LLM script or TTS/audio
  stages. Isolated boot checks changed all four optional modules from loaded to
  absent while retaining one thread; three-process peak RSS averaged 93,076 KiB
  versus the immediately preceding 93,456 KiB (380 KiB lower). Wall samples
  were noisy (3.403 versus 3.216 seconds), so no startup-time win is claimed.
  Grounded blocking/streaming recommendation later joined the same boundary;
  its route/ownership/grounding suites passed 43/43, while the next three-run
  wall sample remained noisy (2.959 versus 2.878 seconds) and RSS was flat.
- Deferred the remaining request-scoped execution graph behind real work. Plain
  global-model resolution now returns without importing the ephemeral BYO
  dispatcher; inline and registered BYO paths mint/dispose through one lazy
  lifecycle seam shared by native agent-run and both compatibility APIs. Paper
  QA and translation routes likewise keep only their task runtimes resident and
  load engines inside the spawned task. BYO/compatibility tests passed 88/88 and
  paper QA/translation tests passed 12/12. Three isolated imports averaged
  2.878 seconds versus the immediately preceding 3.403 seconds (about 15.4%
  lower); peak RSS was unchanged within noise (93,060 versus 93,076 KiB).
- Moved paper report execution behind its existing server-owned task boundary.
  Route registration, cache hits, polling, dedup, and abort now retain only the
  lightweight report runtime; the worker imports inside the spawned task. This
  also removes task handlers, the executor, and the complete tool registry from
  ordinary boot without weakening the registry's import-complete plugin
  contract. Report/review/rebuttal/abort/injection suites passed 104/104. Three
  isolated imports averaged 2.466 seconds versus the preceding 2.959 seconds
  (about 16.7% lower), while peak RSS fell from 93,008 to 89,431 KiB (about
  3.8%); all four deferred module families and optional search/v4/BYO stacks
  remained absent with one thread.
- Replaced the eager `lib.swarm` package facade with lazy compatibility exports.
  Importing the role registry for Orchestration Studio OpenAPI previously also
  initialized agents, schedulers, integration state, task handlers, LLM
  dispatch, and project tools. Child-module imports now remain focused, while
  historical package symbols and `from lib.swarm import persistence` still
  resolve on first use. Swarm suites passed 195/195 and orchestration authoring,
  service, HTTP, and application-port suites passed 121/121. Three isolated
  server imports averaged 2.411 seconds versus 2.466 seconds; peak RSS fell from
  89,431 to 87,105 KiB (about 2.6%), with role metadata present but swarm agent,
  integration, and project modules absent.
- Made `lib.llm` a lazy compatibility facade so pure stream-result/verdict
  consumers no longer initialize HTTP policy, sync/async chat transports, SSE
  clients, or shared network helpers during route registration. All 66 historic
  package exports still resolve from their focused owners, and child-module
  imports such as diagnostics remain compatible. Verdict/boundary tests passed
  38/38; body/cache, transport, timeout, Responses, Anthropic, dispatcher,
  health, error, and provider-pin neighbors passed 405/405. Three server imports
  averaged 2.375 seconds versus 2.411 seconds; peak RSS fell from 87,105 to
  81,127 KiB (about 6.9%), with typed stream evidence resident but all five LLM
  transport modules absent.
- Extended that request-loaded boundary through the 31-symbol
  `lib.llm_dispatch` facade, the task-stream call seam, and the shared LLM error
  taxonomy. Route registration no longer constructs provider discovery or
  dispatcher state; transport modules still resolve the identical concrete
  retry exception tuple on use. Skill catalog/install, webhook delivery and
  validation, and arXiv title/PDF routes likewise retain their historical
  monkeypatch seams while loading the shared HTTP/SafeFetch stack only for an
  explicit egress. Focused boundary, dispatch/retry/task-stream, webhook,
  paper, skill, approval, and registry suites passed 460/460. Three isolated
  server imports averaged 2.187 seconds versus the preceding 2.375 seconds
  (about 7.9% lower); peak RSS fell from 81,127 to 74,048 KiB (about 8.7%). The
  process retained one thread with `requests`, `urllib3`, HTTP/SafeFetch,
  dispatcher/discovery, and online skill modules all absent.
- Restored automatic My Context learning after completed interactive turns; a
  finalizer refactor had retained the learner and its UI but dropped the only
  production call site, leaving About me, Work rules, and Response preferences
  empty. The repaired pass is off the response hot path and single-slot bounded.
  It reviews short turns only when they contain a durable-context signal, accepts
  at most two concise items with verbatim real-user evidence, rejects ungrounded
  or conversational filler and one-off task details, de-duplicates writes, and
  preserves the existing per-item undo trail and owner scope.
- Bounded Conversation Sync reconnect amplification at both ownership and
  principal scope. One live process exposed 205 conversation SSE streams for
  only 5 running tasks; the local code-server proxy simultaneously held about
  5.94 GB in socket receive queues (roughly 4.73 GB ESTABLISHED and 1.21 GB
  CLOSE-WAIT) because downstream disconnects did not retire infinite upstream
  heartbeat streams. Generated EventSource URLs now carry a page ID and
  monotonic generation: equal/new reconnects synchronously supersede the old
  broker subscription, wake its wait, and release its exact shared lease;
  delayed generations and residual capacity failures receive HTTP 204 to stop
  native retry. A current identified page can evict the oldest local
  conversation zombie before retrying the distributed-safe principal slot,
  while chat/remote leases remain protected. The shared launch-probed ceiling
  is 8..24 in personal mode (12 on the 8 GiB reference/fallback), 64 in
  distributed mode, and hard-capped at 128. Owner tombstones reuse the bounded
  browser-client registry with a 128-entry active-stream floor, and bounded
  metrics distinguish admission,
  supersession, eviction, stale generation, and capacity refusal without IDs.
  A 10-second body-start deadline closes the narrow pre-generator disconnect
  gap, so even an unconsumed admitted response releases its broker entry and
  slot. Conversation command startup failures also preserve their validated,
  persisted `task_start_failed` envelope instead of passing it through the
  generic 500-detail redactor and losing the user-visible retry guidance.
- Replaced per-root project tree scan pools with one launch-probed process-wide
  pool and made retained paths a cross-root budget. Two concurrent roots could
  previously create 32 scan workers plus two builders, while four retained
  600,000-path indexes represented roughly 491 MiB in sampled build-row objects
  alone. On the retained 4,775-file FUSE tree, two-pass median rebuild time was
  1.369 s at 4 workers, 0.283 s at 8, and 0.412 s at 16; the personal profile
  now selects 2..8 shared workers (8 on the 8 GiB reference machine), 50,000..
  600,000 total retained paths (409,600 on the reference), and 2..4 secondary
  root slots. Both pools retire after the exact active batch, oversized older
  disk indexes are declined before loading their body, write-freshness updates
  discard their superseded disk snapshot before an LRU eviction can resurrect
  stale paths, concurrent walk futures drain before publishing, and every
  override retains an explicit hard ceiling.
- Reused exact owner-scoped Project Brain snapshots across request-local views.
  Project context preparation now queries sibling conversations once for both
  prompt digest and UI metadata, and an active board once for both claim gating
  and prompt rendering. This removes up to two duplicate Sidecar reads per
  project task and prevents those paired views from drifting between queries;
  the public string digest API and pull-based full board tool remain unchanged.
  The conversation repository now pushes an optional owner/project predicate
  into SQLite/PostgreSQL before ordering and limiting. Context preparation
  therefore reads at most 24 sibling headers with only `projectPath` and
  `projectSummary`, while the cross-conversation tool and stranded-work
  dispatcher no longer retrieve full settings or transcripts merely to filter
  by project. A projected path witness keeps mixed-version reads fail-closed.
  A read-only aggregate on the retained authority measured the old selection
  at 4,786 headers / 15,246,228 raw settings bytes; the new selection was 24
  headers / 21,656 raw settings bytes, with about 3,498 bytes in the two values
  that cross the projection. These are payload bounds, not an end-to-end
  latency claim.
- Let fixed-policy automatic context compaction use the long task's observed
  survival instead of assuming every task has only one request left. The
  original one-round cache-rewrite gate remains through round 3; completed
  rounds 4/8/16/32/64 earn bounded 2/3/4/5/6-round exact-ROI horizons, capped
  by the resolved remaining API-round budget. In the retained production log,
  the earliest decline for each of nine distinct long tasks projected
  1.18–3.89 rounds to repay and every task then ran past break-even, leaving a
  rough 70.3M-token prompt-exposure upper bound. Retry witnesses now include
  the evaluated horizon, so each newly earned step rechecks exact economics;
  hard-window, manual/reactive, adaptive, evidence and anchor gates are
  unchanged.
- Moved typed shared-project TPM contention control in front of network I/O.
  Once one `(provider_id, model)` request reports the shared limit, every later
  sync/async chat or stream task reserves a one-per-second family probe after
  its local cache gates instead of first spending another large rejected API
  request. Deep queues recheck in abortable three-second slices without a
  capped wake-up herd, slot parking, health penalties, or fallback steering;
  waits enter dispatcher queue accounting, and two consecutive successes
  after reserved probes drain are required to clear an intermittent limit.
- Bounded live `run_command` recovery state at the same 100,000-character
  limit as its settled result, retaining an explicit prefix/tail marker and
  total count instead of repeatedly copying an unbounded subprocess log. A
  measured grep accidentally emitted 10,328,512 characters: once its Turn
  patch crossed 4 MiB, 425 subsequent frames rebuilt the same oversized
  projection and the worker reached the 6 GiB RSS guard. Completed rounds now
  strip the redundant live buffer from Turn persistence, while a carried
  oversized structural frame retries atomically with a slim text projection
  and opens a 30-second probe circuit so the exact task event remains durable
  without a deterministic retry storm.
- Separated frontend deployment freshness from runtime artifact integrity.
  Explicit source-checkout start/restart and packaging gates still reject or
  rebuild locale chunks that predate their authoring catalogs, while the ASGI
  lifespan now validates only the complete atomically published Vite graph.
  An RSS recycle, OOM, or abnormal worker exit therefore reuses the same graph
  already trusted by hard refreshes instead of turning a harmless source edit
  into a five-attempt crash loop; missing or corrupt published assets remain a
  fatal startup error, and runtime recovery never acquires a Node dependency.
- Added an explicit single-checkout Git mode for model-only development where
  per-task worktrees and model-driven merges are too expensive. Tool execution
  is completely independent of Git checkpoint state: no dirty baseline, lock,
  configuration, or Git failure can reject or delay a project tool. Terminal
  settlement instead creates a best-effort workspace checkpoint through an
  alternate index and a short Git-only advisory lock, advances the development
  branch by CAS, and never checkout/reset/stash/merge/pushes. Concurrent
  conversations may be coalesced into the same snapshot; bytes arriving after
  capture remain dirty for the next settlement. Failed tasks are preserved as
  WIP commits, while `refs/tofu/stable` advances only when the exact
  stable-to-checkpoint delta passes the configured gate and the canonical
  checkout remains identical before and after verification. Export resolves
  stable only for repositories that explicitly opt in and have activated a
  task-end baseline; arbitrary attached projects retain HEAD/no-auto-commit
  behavior. Forbidden paths and semantic suffixes come from the same policy as
  isolated integration.
- Made large fastpath activation restart-safe and removed its shadow-copy write
  amplification. The 87.6 GiB live classic authority exceeded both the
  Sidecar's 30-second ready wait and Hypercorn's 60-second lifespan limit, so
  each manager retry truncated the private copy and reread up to 82+ GiB.
  Seed copies now durably checkpoint every 256 MiB, resume only against an
  unchanged source fingerprint, publish bounded byte progress through a
  renewable 30-second stall watchdog inside a 900-second hard limit, and give
  the outer lifespan the same budget plus 60 seconds. Completed copy ranges
  are explicitly released from page cache. A fingerprinted first activation
  hard-links the immutable classic database as shadow generation 1, consuming
  no second database-sized allocation or data copy. Later rebases use one
  stable sequential database-image copy plus the concurrently-written WAL
  prefix; this replaces SQLite backup's page-at-a-time network writes, which
  repeatedly restarted under live commits and remained stuck around 646 MiB.
  Startup also reclaims at most 64 exact private snapshot artifacts whose
  recorded creator PID is dead, so a killed rebase cannot accumulate one large
  orphan per restart; live-owner and unrecognized names are never touched.
  The rebase trigger now scales to one eighth of the authority under a
  launch-time disk-derived 64 MiB..8 GiB ceiling instead of copying an 87 GiB
  database after every fixed 64 MiB of WAL growth. Canonical fastpath backups
  now force and pin one checkpointed shadow generation (hard link on the same
  filesystem, sequential copy across devices) instead of running a second
  page-wise live backup; integrity, checksum, atomic publication, deadline,
  and single-file restore guarantees remain. `serverctl doctor` now reads this
  Sidecar manifest authority and separately reports the retired
  `db_snapshots/` published/interrupted footprint without deleting it.
- Bounded the BeeGFS/FUSE keepalive runtime. Its 15-second safety cadence now
  uses one interruptible Event deadline instead of thirty half-second sleeps,
  reducing quiet timer wakeups from 172,800 to 5,760 per day (96.7%). One
  persistent serialized probe replaces two newly-created stat threads per
  cycle (11,520 thread starts/day), and a kernel-stuck stat remains the sole
  in-flight generation instead of leaking more daemon threads after every
  timeout. Composite shutdown is bounded and refuses a duplicate until that
  exact probe has returned.
- Made desktop LAN discovery event-driven and lifecycle-owned. The responder
  now blocks on UDP-or-private-wake socket readiness instead of timing out every
  0.5 seconds (eliminating 172,800 idle application wakeups per day), starts at
  most one exact owner per request process, rolls back partial socket/thread
  startup, and wakes plus bounded-joins during normal server shutdown.
- Replaced the deterministic Git integration queue's fixed three-second idle
  Sidecar poll with generation-aware adaptive deadlines. Durable local
  submit/retry operations wake the worker immediately; a continuously empty
  queue backs off through 3, 6, 12, 24, 48, then 60 seconds, retaining a
  one-minute cross-process and abandoned-claim discovery bound while reducing
  empty reads from 28,800 to at most 1,440 per day (95%). Storage failures keep
  their independent 5..30-second circuit delay, and the worker now has a
  bounded exact-owner shutdown contract so timed-out generations cannot be
  duplicated. Only a lifecycle-armed task-worker process may start it, so a
  future API-only replica can persist work but cannot become a Git executor.
- Consolidated logging maintenance around actual work. Core and registered
  external retention now share one upgradeable 15-minute runtime (standalone
  launchers retain external-only coverage); the duplicate-tail worker exists
  only from the first suppressed delta through its exact quiet checkpoint and
  then releases retained record payloads; and an empty aggregate store sleeps
  to its hourly TTL boundary while new rows still flush on the original
  15-second batch window. On the sampled external-console server this reduces
  periodic logging workers from four to two and quiet wakeups from 968 to at
  most 5 per hour (99.4%+), without changing QueueListener isolation, exact
  occurrence deltas, failure backoff, final shutdown flush, or text-log
  authority.
- Added semantic, restart-safe backoff to remote provider model catalogues. One
  sampled provider had preserved its 44-row last-good catalogue through 80
  consecutive empty `/models` responses but still retried every six hours.
  Each provider now persists `next_attempt_at`: exact unchanged snapshots back
  off from 6 to 12 hours (50% fewer steady requests), failures back off through
  12, 24, then 48 hours (87.5% fewer at the ceiling), and the worker sleeps to
  the earliest provider deadline instead of waking at the base cadence. Model
  additions, metadata changes, and first-snapshot pending removals reset to the
  six-hour confirmation floor; Settings Save remains an immediate forced wake,
  last-good/lease/stale-result fences remain intact, and connection changes
  clear all old-account deadline state.
- Made the project-presence TTL sweeper batch-scoped. Presence is process-local
  and starts empty, yet boot recovery previously started a thread unconditionally;
  the sampled worker still retained it long after the final stale peer was
  reaped. Empty startup now owns zero presence threads, the first announce starts
  one bounded shared sweeper, and the worker releases its exact generation when
  the last peer disappears so a later announce can create a new batch safely.
  The 25-second active / 180-second idle TTLs, owner filtering, conflict advice,
  push failure semantics, and shutdown join remain unchanged; invalid intervals
  are bounded and a failed thread start rolls ownership back without failing the
  task's ephemeral announce path.
- Replaced netpath's fixed three-minute, dual-path sweep with per-path adaptive
  deadlines. The sampled live state held 12 active hosts, 11 proxy decisions,
  and 68,353 accumulated direct-path failures while the worker was still making
  fresh HTTPS probes. New paths and passive request failures still wake the
  worker immediately; repeated failures back off through 3, 6, 12, 24, 48, then
  60 minutes, healthy real traffic postpones redundant probes, and persisted
  last-use timestamps no longer reactivate every historical host for 24 hours
  after restart. A deterministic day-budget test cuts one stable host from 960
  active requests to at most 55 (at least 94.3%) while retaining hourly recovery
  detection; intervals have a 30-second floor and six-hour hard ceiling.
- Moved yesterday-report backfill onto the existing owner-scoped durable
  scheduler and removed the dedicated `daily-report-scheduler` thread that
  otherwise slept for six hours after its startup pass. The personal built-in
  runs at 00:00/06:00/12:00/18:00 local time, rebuilds only
  `reports:maintain` for the task row's owner, and executes off the 30-second
  scheduler tick. On boot, a bounded process-local hint is queued only when
  yesterday is actually missing; the Sidecar claim remains the duplicate-run
  authority. The historical backfill service, LLM analysis, atomic report
  save, failure visibility, and legacy start/stop imports remain intact, while
  distributed ownerless composition still installs no personal report task.
- Consolidated local-provider health and well-known-port discovery onto one
  lifecycle-owned monitor, removing the always-resident `local-autodiscover`
  thread. The sampled live process made 724 model-list requests across 362
  identical “Ollama answers but serves no models” cycles: known engines now
  start directly at `/v1/models`, and an open-but-empty endpoint backs full
  HTTP discovery through 2, 4, 8, then 15 minutes while cheap TCP topology
  checks remain every two minutes. A newly opened port or explicit Settings
  change still probes immediately, repeated empty state logs collapse to one
  transition, and the former start/stop API remains a thread-free compatibility
  facade. At steady empty state this cuts the default 1,440 requests/day to 96
  (93.3%) without delaying detection that a local service has started.
- Consolidated orphaned billing-reserve recovery onto one durably claimed
  scheduler task. The live open-mode personal server completed 379 scheduled
  Sidecar sweeps across the sampled logs and every result was `0/0`; a second
  silent `billing-janitor` thread queried the same operation on its own
  five-minute loop. Open/private and billing-disabled deployments now
  reconcile the built-in disabled and start no janitor thread. Active
  multi-user billing retains one five-minute crash-recovery cadence, the same
  idempotent release path and 30-minute TTL, now with the running-task guard
  moved into the canonical sweep. The legacy API and TTL environment name
  remain compatible without owning a second lifecycle.
- Scoped the swarm registry's five-minute cleanup timer to live sessions. The
  optional integration previously launched a recursive `threading.Timer` at
  import and kept one rotating thread resident even when no swarm had ever
  run. Import is now thread-free; the first session starts one shared timer,
  the last removal cancels it, and exact-generation checks prevent a canceled
  callback from resurrecting or detaching a newer timer. TTL, producing-agent
  protection, durable cleanup, and the 20-session ceiling are unchanged.
- Replaced the authenticated Codex model catalogue's fixed three-minute poll
  with semantic adaptive backoff. The live personal server logged 528
  successful refreshes of the same nine-row directory in roughly 27 hours;
  unchanged HTTP 200/304 responses and failures now step through 6, 12, 24,
  48, then 60 minutes, while login or a real normalized-row change resets to
  three minutes. Steady-state request volume falls from 480 to 24 per day
  (95%) without increasing the one-hour cache-freshness bound. Launch without
  a Codex credential creates no worker, credential removal ends it, and
  distributed roles decline the legacy ownerless token until an owner-scoped
  OAuth/catalogue repository exists.
- Made online-skill verification workers batch-scoped without weakening the
  process-wide ceiling. Concurrent explicit ClawHub searches still share one
  four-worker generation; the final active-batch lease now waits for bounded
  leftovers, closes every thread, and only then permits a lazy replacement.
  One Settings/model search therefore no longer leaves four `skill-online`
  workers resident until process exit, and diagnostics expose active batches
  plus live workers.
- Added idle retirement to the shared project refresh lane. Summary, status,
  and watch each previously retained two daemon consumers forever after their
  first event, even though their work is reconstructible and already bounded
  and coalesced. They now expose live/start/retirement counters, atomically
  arbitrate submit versus timeout exit under one condition, and rebuild their
  full capacity after a launch-probed 30..300-second personal quiet window
  (600 seconds distributed; explicit zero keeps the consumers resident).
- Made the two-worker project tree-index builder batch-scoped. A background
  warm previously left its `tree-index` executor resident forever after the
  filesystem walk and local index publication completed. Concurrent roots
  still share the same two-build ceiling and each build retains its bounded
  inner walk, but the exact executor generation now exits when `_building`
  reaches zero; stale refreshes recreate it lazily. An operational snapshot
  exposes active builds, executor state, and live worker count.
- Made subscription-route probe workers batch-scoped. A single cold route
  race previously left every thread it had grown (four in the observed live
  process, with a bounded maximum of 32) resident for the rest of the server
  lifetime. Per-host/route singleflight and the 32-way active race are
  unchanged, but the last exact-generation future now atomically detaches its
  executor and lets all workers exit; the next stale or cold route batch
  recreates capacity lazily. Topology reset also retires the old generation,
  whose late results remain unable to mutate current health.
- Added bounded idle retirement for the serving loop's burst-grown worker
  generations. The live process retained all 16 `tofu-sync` and eight
  `tofu-agent` threads after their work ended; CPython's executor workers never
  expire, and the process also showed many resident 8..65 MiB anonymous
  mappings. The loop now tracks local pending/active work (including
  cancellation before worker entry), retains at most two warm sync threads and
  zero idle agent threads, and publishes an equal-capacity lazy replacement
  only after a launch-probed 300..1,800-second personal quiet window and an
  `active=queued=0` boundary. Accepted old work still drains, future work uses
  the new generation, and metrics expose resident/retired threads; distributed
  mode uses 3,600 seconds and explicit zero disables retirement.
- Made the launch-probed numeric worker budget a real process-wide ceiling.
  The live server inherited `OMP_NUM_THREADS=62` while
  `TOFU_NUMERIC_THREADS=4`; because library-specific variables previously won
  unconditionally, a later NumPy/OpenMP import could still create a host-sized
  native pool. OpenBLAS, OpenMP, MKL, and NumExpr values are now clamped to the
  canonical 1..32 budget before optional imports, while smaller deliberate
  per-library limits remain intact.
- Added a bounded idle lifecycle for local MCP stdio process trees. The live
  personal server had seven npm/uv MCP groups resident for about seven hours,
  together holding roughly 1.07 GiB RSS even though only one credential probe
  ran every 15 minutes. Startup still discovers each authoritative tool
  catalog once; after a launch-probed 180..600 second personal idle window the
  owner task, pipes, launcher, and child exit while the small catalog/config
  snapshot remains logically connected. The next real tool call performs one
  serialized transparent reconnect, revalidates its catalog, and refreshes
  activity. Long calls hold an in-flight lease, remote transports are excluded,
  parked servers are skipped by liveness and credential sweeps, and the API
  reports `parked=true`. MCP maintenance no longer leaves the event loop's
  implicit `asyncio_0` worker resident after its first parking/reconnect;
  bounded calls own a transient one-worker generation, while periodic
  credential probes use per-server singleflight one-shot threads. Distributed
  mode uses an explicit 1,800-second warm window and every override is capped
  at one day.
- Added a physical budget gate for new SQLite recovery copies. The live data
  tree currently allocates about 637.6 GB including its backup subdirectory;
  retained verified backups plus the last deep-clean rollback point account
  for 463.5 GB. A new 87.6 GB same-volume snapshot would project recovery
  copies to 551.1 GB even though the shared 22 PB filesystem reports ample
  free space. Personal defaults now reserve at most half of the launch-probed
  data volume for recovery copies, with a 4 GiB floor, 512 GiB zero-config hard
  cap, 64 GiB lean probe-failure fallback, and an explicitly bounded override;
  distributed mode has a separate 1 TiB default. Backup admission counts
  allocated verified snapshots and retained rollbacks only when they share the
  target device, so an explicit NAS/Compose backup mount keeps its own budget.
  Rejection happens before a temporary copy is created and reports retained,
  projected, and limit bytes; no authority, verified backup, or final rollback
  point is automatically removed.
- Added bounded online self-healing for pre-typed task events on established
  SQLite authorities. The live index-only census found 3,365,519 blank-type
  rows out of 3,589,168 task events (93.8%); because blank metadata fails
  closed into the 30-day structural class, historical streaming deltas never
  reached their six-hour TTL. All 2,000 oldest/newest payload samples decoded
  successfully and 95.5% were deltas. After ordinary typed backlog drains,
  the legacy retention transaction now classifies one indexed page at a time:
  expired streaming rows are deleted, structural rows receive only recovered
  type/kind metadata, and opaque rows are retained with an internal progress
  marker. The ordinary maintenance caller keeps its 25-row commit page; stored
  payload materialization has an independent 4 MiB budget (one oversized row
  may progress under the existing 64 MiB event ceiling), and the SQL plan is
  pinned by tests to the old retention index with no temporary sort. Project
  streams and payload authority are unchanged; reclaimed pages can be reused
  even where network-storage policy correctly forbids online file compaction.
- Stopped legacy task-event retention from pre-discovering every event type on
  every 25-row page. The 87.6 GiB live authority has only the compatibility
  `(stream_kind, event_type, created_at_ms)` index, 32 task event types, and
  51,151 expired streaming rows. Across the retained Sidecar console history,
  `event.prune` was the writer holder for 612 of 1,103 parsed acquisition
  timeout diagnostics, including 113 durable event batches and 21 task-result
  checkpoints. Compatibility discovery now deletes each exact type as it is
  found and returns immediately after the first non-empty page; for the live
  backlog's leading type this reduces type-discovery seeks from 32 to two while
  preserving the 25-row transaction, 64-type fail-closed ceiling, structural
  classification, and separately committed writer-fair paging.
- Returned freed long-task and Sidecar heap arenas to the OS at bounded idle
  lifecycle edges. A live personal process rose from about 0.48 GiB to 2.6 GiB
  RSS while terminal persistence was already nulling full message/endpoint
  snapshots; after shared-cgroup pressure subsided, no later owner called
  `malloc_trim`. Terminal releases now set one process-wide flag consumed by
  the existing 60-second task-maintenance tick, coalescing any number of
  completions into one trim outside the persist path. The idle Sidecar was
  separately holding 572 MiB of anonymous private memory despite a 32 MiB
  SQLite writer cache; it now trims only on a zero-active-RPC edge, above a
  profile-derived RSS threshold and at most once per five-minute cooldown.
  Sidecar metrics publish attempts, successes, reclaimed bytes and last
  before/after RSS; no worker, queue, storage, or task concurrency limit was
  increased.
- Connected adaptive compaction's expected remaining-round horizon to both
  exact Layer-2 cache-ROI checks. The opt-in strategy previously admitted a
  positive-value candidate with its bounded six-round default, then silently
  re-applied the fixed policy's one-round gate inside the summarizer. In the
  current 3,812-round production cohort, 31 tasks crossed 128K input tokens;
  90.3% continued for at least three model rounds and the median continued for
  30. Adaptive preflight/adoption and their retry lower bound now use the same
  request-local horizon. Fixed compaction uses its separate bounded observed-
  survival horizon described above. Receipts expose the chosen horizon and
  policy.
- Kept local multi-agent lifecycle schemas atomic under an explicit tool-schema
  budget. The final fitter previously required `spawn_agents` but could hide
  `await_agents` and `get_agent_result`, forcing an avoidable discovery round
  after work was already launched. All three controls now remain visible as a
  functional floor; execution authority and the default uncapped tool surface
  are unchanged.
- Added resource-profiled, digest-only reuse for repeated large-text token
  counts. Stable tool schemas and compaction projections previously re-ran the
  tokenizer every model round even though 98.0% of 2,544 observed schema
  transitions were byte-stable. An 80,099-byte production catalog measured
  6.13 ms per warmed uncached count and 0.059 ms per cache hit (about 104×).
  Entries retain only encoding, length, SHA-256 and count—not prompt text;
  strings below 4 KiB bypass the cache. Personal capacity scales from the
  launch probe, distributed defaults to 1,024, and every override is capped at
  4,096 entries.
- Added lossless private compression for large durable task-event payloads.
  Forty-eight recent `messages_snapshot` rows occupied 49,929,821 bytes; zlib
  level 1 reduced them to 15,674,269 bytes (68.6%) with 12.17 ms median encode
  and 4.76 ms median decode time for a roughly 1.13 MiB frame. Canonical JSON
  below 64 KiB remains byte-identical, and a larger payload uses the envelope
  only when it is smaller. Semantic event reads and natural-key replay remain
  unchanged across SQLite and PostgreSQL; decoded/stored forms stay within the
  64 MiB RPC ceiling, while truncated, corrupt, length-mismatched, or unknown
  codecs fail closed as storage integrity errors.
- Extended physical offline deep clean into one bounded maintenance pass over
  the active `storage_events` transport. The 3,365,519-row v21 migration cohort
  still has blank projected types, but 8,191/8,191 rowid samples carried a
  parseable top-level type. The sample projects 3,317,857 streaming rows
  (1.44 GB payload) as immediately TTL-eligible and 47,662 structural rows
  (13.24 GB payload) as retained; zlib level 1 reduced the structural sample
  to 24.8%, an estimated additional 9.96 GB saving. The 4,096-row keyset page
  rides `INTEGER PRIMARY KEY (rowid>?)` and measured 9.59 ms for 2.36 MB on the
  live authority. Each write page stays within 4,096 rows and 64 MiB stored
  payload, then checkpoints the WAL. Explicit types use canonical 6-hour/30-day
  TTLs; retained blank rows recover their type/kind and large retained legacy
  JSON receives the new codec. Opaque or malformed blanks keep the conservative
  structural horizon, non-task streams are untouched, repeat passes make no
  writes, and `--no-vacuum` skips the pass entirely. Read-only analysis now
  reports exact encoded payload bytes and actual row counts instead of treating
  sparse `max(rowid)` spans as live rows; it includes a capped exact event-type
  breakdown and rowid-hole evidence. One shared 60-second SQLite progress
  deadline returns explicit partial results on a slow authority, bounding the
  diagnostic's own I/O cost.
- Reclaimed bounded, expired transport history from the frozen pre-Sidecar
  `attempt_events` and `task_events` tables during physical offline deep clean.
  A live authority held 90,055 legacy attempt frames with a sample-estimated
  26.7 GiB table weight; preserving only the newest frame for each of its 19
  attempts retains 8.36 MiB of payload while making about 90,036 older frames
  reclaimable. The same authority held 1,042,953 streaming task frames
  (248.35 MB payload) and 281,402 structural frames (4.28 GB payload).
  Attempt cleanup only removes an expired frame when a newer sequence exists;
  task cleanup consumes the canonical 6-hour streaming and 30-day structural
  policy, and durable `task_results` are never targeted. Each transaction is
  capped at 900 rows and 128 MiB of UTF-8 payload, checkpoints the WAL, and
  runs only when verified-copy or low-space reclaim will return the pages;
  index-only `--no-vacuum` windows leave the frozen tables byte-identical.
- Bounded verified-copy deep-clean rollback artifacts instead of accumulating
  one full authority per maintenance window. A live retained rollback occupies
  436,914,016,256 logical bytes / about 407 GiB allocated, while the compact
  authority is about 82 GiB. Successful future publications now keep one
  rollback by default (configurable with a hard ceiling of four) and only then
  retire older copies. Read-only analysis reports exact logical/allocated
  weight and age, verified SQLite backup weight, and a 256-entry shallow list
  of operator-managed root artifacts above 1 GiB; an explicit stopped-server
  `--retire-rollback <basename> --confirm` path rejects traversal, links,
  live-authority aliases and WAL/SHM
  companions, quick-checks the current authority, fsyncs the exact unlink and
  verifies the postcondition. Offline publication now also proves every
  second-stamped candidate/rollback name and companion absent before mutation;
  failure cleanup is ownership-gated, so a same-second file or dangling link
  is rejected rather than overwritten or unlinked. No current recovery point
  is deleted automatically.
- Kept large idempotent Sidecar commands replayable without increasing durable
  receipt growth. A settled-turn response duplicated its projection between
  the public result and post-commit sync notice, so an 18,000-character tool
  result produced 74,570 bytes and the previous 64 KiB receipt check rolled the
  whole mutation back. Private receipts above 64 KiB now get one zlib level-1
  attempt: the sample stores in 1,717 bytes in 0.216 ms, while a 362,570-byte
  response stores in 3,184 bytes in 0.642 ms. Stored receipts remain capped at
  64 KiB, decoded responses at 4 MiB, legacy small JSON is byte-identical, and
  corrupt, incompressible, or over-budget data still fails closed.
- Added lossless, storage-only interning for duplicated tool arguments and
  results across turn ``segments`` and ``toolRounds``. On a live 196-tool
  long-task projection, canonical JSON fell from 3,794,888 to 2,709,283 bytes
  (28.61%, or 1,085,605 bytes). The codec transform averaged 0.368 ms to encode
  and 0.414 ms to hydrate; end-to-end validated dump time stayed effectively
  flat (12.798 vs 12.815 ms) while parse-plus-hydrate fell from 10.906 to
  5.946 ms because less JSON is parsed. Only byte-equal fields with a unique
  ``toolCallId`` are referenced; semantic reads restore the original public
  projection, malformed codec metadata fails as storage integrity, and
  existing rows migrate lazily on their next normal write without a startup
  rewrite. Recovery chunking now charges a backend-neutral hydrated byte upper
  bound instead of counting compressed SQLite bytes or PostgreSQL JSONB keys.
- Bounded root model API rounds by deployment profile instead of treating an
  unset or zero `maxApiRounds` as unlimited. One failed long-task sample made
  150 model calls, accumulated 21.29 million input tokens and 9,757 seconds of
  provider time, and recorded $8.46 before ending without actionable output.
  Across the newest 100 conversations, 941 turn attempts had a median of two,
  p95 of 50, p99 of 112, and maximum of 177 calls; several valid completions
  exceeded 128. Personal and distributed defaults are therefore 192 and 512,
  explicit requests remain capped at 1,024, and one model-visible finalization
  reminder fires with at most 64 rounds left. The hard gate runs before the
  next provider call and retains the existing typed budget-failure settlement.
- Made dynamic large-response compression topology-aware. On a 3,643,636-byte
  live rendered conversation, personal-mode Brotli q2 took 15.8 ms versus q4's
  26.28 ms while remaining 476,734 bytes; gzip level1 took 33.86 ms versus
  level6's 98.13 ms while remaining 975,060 bytes. Only uncached dynamic bodies
  at or above 1 MiB use the low-CPU personal profile. Smaller responses,
  distributed bandwidth policy, static cache quality, off-loop execution, and
  wire semantics remain unchanged.
- Coalesced simultaneous owner/conversation sync snapshots before their
  Sidecar read, JSON parse, validation, and stable-segment projection. A live
  long-task snapshot reached 3,018,932 bytes; on an equivalent 2,981,387-byte
  synthetic shape, four independent parses took 20.09 ms median versus 4.72 ms
  for one parse plus four request envelopes (76.5% less parse/envelope CPU).
  The contract-validated HTTP encode now uses compact `orjson`; a current
  3,643,636-byte live rendered document encoded in 1.83 ms median instead of
  25.2 ms (13.8x), with the framework encoder retained as a fail-soft fallback.
  The 8 ms, launch-budgeted gather closes before authority execution, retains
  no completed result or TTL, fails open at saturation, and keeps HTTP
  responses plus request-time `pushWithheld` hints isolated.
- Coalesced simultaneous owner-scoped conversation-catalog arrivals before
  their Sidecar read. Four live browser tabs repeatedly produced four identical
  41.4 KB list responses in one second; arrivals inside the 8 ms gather window
  now perform one metadata query while retaining four independent HTTP
  responses. The launch-budgeted registry closes before query execution, holds
  no completed result or TTL, separates owners/projection shapes, and fails
  open at saturation, so later refreshes cannot inherit an older snapshot.
- Stopped argument-changing agent loops on stable, explicit tool authority
  failures. One sampled browser worker spent 78 model rounds, 547.1 seconds,
  and 3,156,164 tokens while 28 `browser_execute_js` calls all hit the same
  missing write grant because changing tab IDs defeated the exact-call guard.
  Browser access and write-grant denials now return canonical V2
  `retryable=false` errors; the shared chassis halts after three consecutive
  all-tools rounds with the same terminal code and spends at most one final
  tool-less round reporting the limitation. Legacy, malformed, retryable,
  mixed-success, and changed-code rounds fail open and reset this new streak;
  the pre-execution identical-call guard remains unchanged.
- Replaced task-event retention's event-type-leading scan/sort with mutually
  exclusive age-only partial indexes and bounded cadence. On the 82 GiB live authority,
  an empty tier probe previously took about 2.64 seconds and both tiers ran
  every 15 seconds; drained tiers now run every five minutes, backlog runs every
  30 seconds, and streaming drains before structural retention. Existing
  SQLite authorities with the old compound index now use exact-type, 25-row
  indexed pages (at most 64 type seeks) until offline deep-clean installs v2
  and retires v1; the measured authority had 45,077 expired streaming rows
  (33.76 MB) that can now drain without a table scan. Its 32-type empty-tier
  probe took 13.2 ms cold and 0.188 ms warm median, versus about 2.64 seconds
  for the retired cross-type plan. `storage_deep_clean.py --analyze` reports
  the active retention mode and compatibility limit. Authorities with neither
  index and abnormal type cardinality still fail closed; the external
  PostgreSQL migration owns the same index transition.
- Coalesced provider text microchunks before durable task-event sequencing:
  the first chunk remains immediate, while later chunks have a 100 ms / 256
  character ceiling and flush before every structural or terminal boundary.
  Two production long-task samples project 55.5–64.4% fewer text-event
  transactions (median chunk size: four characters). The buffer and its one
  daemon worker are scoped to an active model stream and reclaimed on both
  success and failure; tool pre-execution keeps its original callback identity.
- Coordinated typed shared-project TPM 429 retries per provider/model instead
  of letting every task poll every 0.3 seconds. Production evidence contained
  239 upstream rejections across 87 rounds (152 repeat probes, with as many as
  12 in 14 seconds); the first retry remains at 0.3 seconds, later probes are
  spaced one second apart and capped at three seconds without cooling slots,
  polluting health, disabling provider fallback, or losing cancellation and
  queue-wait accounting. Quiet state is reclaimed and the coordinator has a
  256-family memory ceiling.
- Replaced proactive L2 compaction's fixed 8K/5% retry hysteresis with an
  optimistic break-even token bound. A replay of 64 observed warm-cache
  declines drops candidate rebuilds to 9, while a cooling cache, explicit or
  reactive request, and the real context-window safety gate still reconsider
  immediately.
- Kept the local `execute_tools` and read-only `spawn_agents` provider schemas
  byte-stable when per-round programmatic-read evidence activates or expires.
  The policy remains task-owned, while fixed conditional guidance prevents a
  telemetry transition from invalidating the entire cached prompt prefix.
- Reused the automatic L2 preflight measurement across retry hysteresis,
  summary preflight, successful-compaction analytics, deterministic fallback,
  and the token-budget reminder, removing repeated full-transcript scans on
  unchanged rounds. The token-counter's broad heuristic prefilter is now lazy
  on usage-cache and local-tokenizer hits and reused by its fallback; cache hits
  also verify a bounded tail without copying the full recorded prefix. Three
  consecutive verified-cold rounds now supersede a stale durable warm-cache
  baseline; the existing one/two-round transient-miss guard remains intact,
  and expected economic declines no longer emit a false summary-failure warning.
- Made the every-round L1 context pass read-free when it has no mutation and
  deferred its conversation-sized telemetry token count until a real tool
  result is compacted. Cold image placeholders now persist through the owning
  Turn, so base64 payloads do not reappear on later request rebuilds; no-op
  diagnostics moved from INFO to DEBUG.
- Reduced the always-loaded browser shell by moving Task Mode/orchestration and
  provider-settings presentation into feature-owned stylesheets, while keeping
  source manifests and byte budgets authoritative. Project status, summary,
  watch, translation, and undo work now use bounded/coalescing queues or LRU
  buffers whose defaults come from the launch-time resource profile.
- Tightened the retained browser-runtime debt ratchet to 3,652,967 bytes after
  reusing the canonical icon registry and replacing bundled finish, tool,
  swarm, and collaboration-bar history with concise rendering invariants and
  pruning redundant inline UI narratives;
  newly added accessibility, Skills API, and tool-result behavior remains
  inside the smaller budget.
- Split the largest turn-finalization and stream-settlement routines along
  named semantic boundaries and tightened their executable size ratchets.
  Conversation search now runs through an independently bounded, replayable
  Sidecar projection so user writes never wait for search materialization.
- Added an adaptive, owner/workspace/interpreter-scoped local CPython bytecode
  cache for repeated Python workloads on network filesystems. It preserves the
  real subprocess and command, avoids cold-prefix cost for one-shot scripts,
  is bounded by launch-time disk budgets plus LRU/TTL cleanup, and can be
  forced with `TOFU_RUN_PYTHON_CACHE=1` or disabled with `=0`.
- Extended declarative plugin storage with version-witnessed put/delete,
  bounded atomic batches, prefix pagination, and a manifest-pinned read-only
  legacy scan. This enables sidecar-native plugin cutovers without exposing a
  driver, database path, transaction handle, or plugin-supplied SQL.
- Retired `list_dir` from new model tool epochs and route simple `run_command`
  `ls` requests through its bounded local/remote directory reader. Removed the
  hidden Kimi-K3 4,000-token schema cap: every model is uncapped by default,
  while an explicit local Tool Search budget is model-neutral, applied once at
  the final projection, preserves a non-negotiable coding-tool floor, and keeps
  a discovery path for every omitted executable tool.
- Added one durable, append-ordered activity timeline to each Turn for model
  requests, retry/wait status, real tools, malformed-tool isolation, errors,
  and model switches. The projection is capped at 128 rows / 96 KiB, survives
  reconnect and cold reload, reuses rich tool rendering, and removes duplicate
  tool, live-status, and fallback-footer presentation without creating fake
  tool calls or adding diagnostics to model context.
- Added an opt-in transactional logical recovery stream for SQLite and
  PostgreSQL: encrypted same-transaction mutation capture, bounded asynchronous
  publishing to private or explicitly shared POSIX storage, crash-idempotent
  segments, capacity backpressure, atomic checkpointed replay, projection
  verification, and fail-closed canary/cutover gates. The database remains the
  authority unless an operator separately supplies and passes promotion
  evidence.

### Fixed
- Kept obsolete browser extensions fail-closed while removing their repeated
  protocol-upgrade poll 426 rows from the application incident plane. The
  first structured rejection and the access-log record remain authoritative,
  while a stale client can no longer fill `app.log` and `error.log` every
  three seconds before it is upgraded.
- Repaired the pre-deliverable watchdog so normalized non-blank reasoning
  renews a rolling semantic-stall window instead of being killed by total
  request age. Keep-alives, blank reasoning, signatures, and protocol metadata
  cannot fake progress; text/tool output permanently disarms the watchdog.
  Real stalls retain one alternate-slot retry per uninterrupted streak,
  `no_actionable_output`, and `autoRetryExhausted`, while active long reasoning
  consumes none of that budget. Diagnostics now separate request duration from
  last-progress age and reasoning size/chunks; attempt logs reserve `OK` for
  typed `provider_finished` streams. The empty abnormal-stream recovery path is
  a named semantic boundary with its tightened size ratchet.
- Enforced explicit owner identity across Paper, Slides, Swarm, VLM, Video,
  tool artifacts, and test fixtures; repaired Swarm terminal-event ordering,
  provider-stream truncation handling, cache-usage normalization, and
  conversation-project restore harnesses. Unit tests now isolate memory stores
  from operator data and keep live published-repository drift checks out of the
  deterministic unit lane.
- Made repository verification deterministic across asynchronous conversation
  search projection, skill-install approval/task cleanup, generated runtime and
  stylesheet freshness, and no-change poison detection. Search assertions now
  wait on semantic visibility, standalone renderer harnesses declare their icon
  dependency, and mutation tests operate in disposable repositories.
- Managed restart now gives a just-stopped child Sidecar a bounded 10-second
  lease-drain window before applying the existing second-authority fence. This
  removes transient false 409 failures while keeping unknown and maintenance
  lease holders fail-closed. Trading's migration/startup summary is also
  routed into the retained business log instead of losing INFO records in the
  third-party vendor stream.
- Repaired the Turn-native chat surface at its DOM contract boundary: every
  main-lane Turn once again owns the retained avatar/content/context-rail
  shell, narrow panes receive the inline context fold, empty prose/actions/
  finish shelves take no space, and compact branch lanes do not inherit the
  main transcript furniture. Proposed plans now keep translated and streaming
  translated bodies inside their source Turn with a conversation-bound
  decision shelf. Provider-bound tool schemas reject same-level
  `required`/`properties` mismatches before Moonshot/Kimi. Deterministic
  400/404/422 request rejections no longer masquerade as model outages or
  trigger fallback, while 429 capacity limits keep waiting by default; a
  positive `TOFU_429_SATURATION_SECS` remains an explicit operator override.
- Made the Terminal-Bench 2.1 runner selectable across Tofu, DeepSeek Minimal,
  Codex, and Claude Code with private ATIF-v1.7 trajectory collection, strict
  route/provenance scoring, and layered failure attribution. Rootless QEMU
  preparation and trial defaults now derive a conservative bounded concurrency
  from one CPU/memory/headroom/disk probe. DeepSeek Minimal 1.0.2 terminates the
  complete guest process group after Bash timeouts; affected 1.0.1 trials fail
  closed as harness-invalid, while only audited non-timeout 1.0.1 paths remain
  score-compatible. Collected ATIF copies also normalize zero-valued token
  aliases from the matching redacted host audit without rewriting source runs.
- Root-corrected recurring SQLite writer stalls on oversized network-mounted
  authorities. The Sidecar now labels the blocking transaction phase, exports
  commit/queue/fast-path metrics, applies an adaptive bounded writer cache, and
  restarts in bounded time when kernel-blocked commit I/O cannot be
  interrupted. Bulk freelists are routed to verified offline compaction (which
  now installs deferred indexes), log-aggregate retries/sweeps cannot flood the
  writer lane, fast-path seeding capacity-checks the whole authority, and
  ineffective shared-cgroup relief enters a bounded cooldown.
- Tightened the stability boundary across startup, task dispatch, and the chat
  shell. Managed startup now distinguishes process liveness from dependency
  readiness and fails closed on missing frontend artifacts; rejected task
  submissions settle durably instead of leaving ghost-running work. During an
  active turn, draft-bearing composers expose independent Send and Stop touch
  targets, lost command acknowledgements recover from authoritative attempts,
  and default shell titles, dates, placeholders, and hints follow the selected
  locale without rewriting stored conversation data. Cold conversation
  hydration now claims both its runtime lane and coordinator snapshot flight
  before publishing connection health, so a health-render re-entry joins one
  read instead of recursively launching full snapshots. The observed failure
  coalesced roughly 929/933 browser stack-overflow reports from two clients;
  two sampled hours recorded a lower bound of 126 heavyweight sync responses
  and 181.9 MB returned. Historical turn-search repair also yields the first
  startup minute to user and recovery writes. Explicit starts during offline storage
  maintenance now enter a visible queued state instead of spawning a Sidecar
  crashloop, then resume automatically when the project lease is released.
  A paused worker crash-loop is now latched without recounting the same absent
  process on every manager poll.
- Restored the conversation-title lifecycle after the Turn-native frontend
  migration: new shells no longer persist the `New Chat` display placeholder,
  successful main-lane assistant and planner settlements once again invoke the
  optional AI title generator, and catalog refreshes retain the derived title.
  Agent Mode's saved-workflow pickers now keep loading/failure notices mutually
  exclusive with the empty state on desktop and mobile; keyboard opening waits
  until the animated menu can receive focus.

## [0.17.0] - 2026-08-25

### Changed
- **Browser conversations now have one normalized Turn document and one DOM
  owner.** Conversation Sync v3 feeds `ConversationTurnStore`, typed selectors
  feed `ConversationSurface`, and catalog/IndexedDB shells carry metadata only.
  Rendering, streaming, diagnostics, actions, scroll state, branches,
  translation, plans, artifacts, cost, and swarm projections use stable Turn
  or block identities instead of a parallel `messages` array. Conversation
  settings now persist through one Turn-independent metadata path with
  transport-owned idempotency keys; local drafts remain cache-only until their
  first accepted Turn. Long histories retain every Turn in the store while a
  Surface-owned, anchor-preserving DOM window bounds rendered nodes. Large
  application-shell structures now live in closed-set frontend fragments
  assembled by a behavior-free server seam.
- **Long-agent Kimi execution now has an opt-in, paired Codex comparison
  contract.** Global context plans and rebuildable task state bound repeated
  history; compiled tool contracts, 8k/24k result envelopes, and owner-scoped
  expiring CAS artifacts bound schemas and tool output; adaptive compaction and
  four-shape orchestration are isolated experiment arms. A loopback-only
  Responses→Kimi proxy pins Codex 0.149.1's 272k/244.8k local-compaction
  baseline, handles namespace tools, and measures translation CPU separately
  without extra model calls. Failed artifact persistence returns a bounded
  honest preview instead of an unusable recovery reference; turn-native manual
  compaction stores one public authority block and projects private v1 markers
  only for legacy readers. The compatible benchmark v2 contract freezes
  the 1,845-task matrix, Kimi pricing, paired quality/family/safety gates, and
  85% cost/P90 thresholds. Request-owned ToolContractV2 documents now perform
  the final pre-dispatch validation for root, nested gateway/ToolScript, SSE
  speculative-read, and swarm paths; missing v2 documents fail closed and
  typed refusals remain rejected through settlement. Timer polls and every
  full/research paper runner now freeze the actual model-visible tool epoch and
  use the same documents for execution; dynamic policy removal and malformed
  arguments cannot reach an unattended backend. Shipped Paper result ingress
  uses 8k/result and 24k/round V2 envelopes; oversized evidence is recoverable
  only through owner-scoped semantic artifact range/search tools, never a disk
  path, and duplicate/empty call IDs cannot evade the aggregate cap. The
  registered result-envelope control can still reproduce the bounded legacy
  baseline instead of being silently upgraded to V2. Explicit long-agent Paper
  requests are model+config fingerprinted, never join another arm, and bypass
  canonical report/deepen cache reads, writes, and second-pass mutations. Paper
  Q&A aborts also remain aborted instead of being projected as done. Kimi prompt
  arms now retain requested/resolved/effective profile, content SHA-256 and
  token proof per request/round; missing or mismatched adoption rejects the arm.
  Orchestration telemetry now separates wire projection from actual adoption,
  retains reason and expected savings, and derives its status only from real
  program/agent/model trajectories. Benchmark v2 rejects contradictory task
  evidence, and the final release gate requires both program and agent traces.
  A new read-only release compiler refuses to create the formal 1,845-task
  manifest unless the pinned SWE/TB catalogs and all five content-hashed,
  path-confined private custom packs are complete; immutable output cannot be
  silently overwritten or substituted with count-only placeholders. The SWE
  catalog is locked at all 500 name-to-digest pairs, and registry preflight,
  definition-cache loading, and pure compilation reject same-count drift. A
  private, bounded per-arm run store now freezes pair/role/experiment arm,
  rejects cross-arm or unresolved task records, reprices every recorded model,
  compaction, and paid-tool cost from provider usage, verifies content-addressed
  raw trajectory and Codex proxy metrics, and emits manifest-ordered JSONL only
  after exact completion. Its pair audit compares every fairness control. A
  formal Harbor `codex-kimi` profile now pins and re-verifies Codex 0.149.1,
  owns the loopback proxy across start/resume, strips Kimi credentials from the
  Harbor child environment, exposes one QEMU-only control route, and audits
  reconciled raw JSONL plus provider-usage shards per trial. Provider face and
  non-secret slot ID, the resolved Harbor executable hash, source commit and
  runner revision are now immutable bindings. An idempotent Harbor→BenchmarkV2
  exporter stores raw/proxy/ATIF artifacts and rejects hidden Harbor retries
  because version 0.21 deletes failed evidence. Release-eligible Harbor runs
  now preclaim every task before dispatch; immutable attempt starts/failures
  retain failed usage, paid tools, artifacts, and retry wall time. Full stores
  cannot finalize, and the paired reporter cannot derive infrastructure rate,
  without complete oracle-ready terminals, so discarded failed runs cannot be
  represented as a clean surviving sample. A symmetric formal `tofu-kimi`
  profile now runs the production public AgentRuntime with host-only credentials
  and exactly two exclusive guest tools. Its exporter strictly reconciles native
  events, sanitized runtime evidence, raw tool audit, ATIF, prompt/runtime/schema
  digests, usage/cache/timing, compactions, final output, and verifier lifecycle;
  candidate wall time receives no Codex-proxy adjustment. Timeout and
  cancellation paths retain a sanitized partial runtime snapshot, and an
  immutable candidate failure cannot record main/compaction usage that differs
  from that artifact. These paired SWE/TB
  launchers do not substitute for the still-missing 900 private task assets,
  their simulator launch adapters, or the paid paired matrix. The paired
  reporter re-derives quality/family, cost-per-success, raw/corrected P90,
  token/cache/context/schema/result, compaction, tool-search, incidents,
  retries, blind judges and actual orchestration from finalized stores; pilot
  reports are structurally unable to emit a release claim. Full paper
  now keeps 29 owner-scoped executable tools behind a 3,702-token Kimi wire
  surface (344 tokens for the fixed search/execute pair); hidden contracts are
  searchable rather than removed. Gateway replay receipts distinguish Kimi's
  recycled positional IDs by arguments/round/world, preserve failure verdicts,
  and are bounded to 256 entries. Root chat and endpoint Worker turns now use
  the same `run_agent_loop` lifecycle as swarm: typed policy hooks preserve
  provider continuation, budget/protocol gates, abort cleanup, chassis-owned
  timeout counting, semantic progress stops and checkpoint placement without a
  private ReAct loop. No Codex-leading claim is made before the complete matrix
  passes.
- **Daily reports now require explicit ownership across routes, services,
  storage, jobs, cost caches, and scheduled generation.** Personal-mode startup
  uses a restricted system principal and idempotently copies legacy flat report
  files into owner-scoped storage without overwriting destinations or deleting
  sources. Distributed mode neither invents an owner nor reads the flat-file
  fallback.
- **Personal-computer resource use now has one probed deployment budget instead
  of server-sized zero-config defaults.** Personal mode takes the minimum of
  host, affinity/cpuset and cgroup CPU capacity, combines physical/cgroup
  memory capacity with current headroom, and probes the resolved persistent
  data volume (including relocated/XDG layouts) for log retention plus a
  bounded SQLite startup reserve. It derives bounded task, executor,
  SQLite/Sidecar, numeric, allocator, log and RSS budgets; failed probes fall
  back conservatively and explicit settings still win. Distributed mode
  retains stable independently larger budgets. Every server/desktop/storage
  launcher materializes one probe
  snapshot into its child environment and installs the allocator limit before
  the child starts, preventing large freed payloads from leaving
  dozens of mostly-empty arenas resident. Compose now defaults to a 4 GiB
  limit, 512 MiB reservation, and 512-PID personal envelope. Permanent turns
  and user data are unchanged, and the existing offline deep-clean path remains
  explicit for historical transport bloat. A verified incremental low-space
  mode can reclaim that bloat on a personal disk that cannot hold two full
  database copies; it requires an independent backup and never runs online.
- **Project Brain Git integration is now fail-closed, terminal-safe, and
  topology-aware.** Isolated workspace creation can no longer fall back to the
  shared tree; merged/discarded records cannot be resurrected; repair
  checkpoints re-anchor to a moved writer HEAD; and stable promotion requires
  explicit acknowledgement when canonical HEAD and candidate diverge. Candidate
  gates reject forbidden/dependency paths, enforce declared write-sets, require
  configured project tests for semantic application/code configuration changes,
  allow pure deletion of historical forbidden artifacts, and validate JSON.
  Browser JavaScript syntax validation now parses `.js`/`.mjs` as ESM and
  `.cjs` as CommonJS, eliminating false quarantines for valid imports while
  malformed gate commands fail closed with an actionable error.
  Stable promotion uses its stronger release command when configured and
  otherwise reruns the candidate project gate instead of refusing semantic
  changes merely because the optional release command is absent.
  A gated `reconcile-head` action can safely merge committed canonical history
  into candidate without moving the canonical branch.
  The operator UI adds discard and divergence-specific confirmation. Idle
  worker polls are read-only, terminal history skips per-worktree Git scans,
  status is response-bounded, and polling is reduced to 30 seconds. See
  `docs/modules/project_integration.md`.
- **Experiments are now immutable, owner-scoped, plugin-defined, and
  promotion-safe.** A typed `tofu.experiments` registry separates strategies,
  metrics, and analyzers from product adapters; resolved specs pin provider
  versions, implementation digests, allocation, and a precommitted assignment
  horizon under one SHA-256 fingerprint. Historical plugin versions can coexist;
  generation-aware request plans and per-scan metric plans remove repeated
  provider/spec resolution from hot loops. Context-cost reports aggregate by
  conversation and default-deny promotion on incomplete pricing, missing
  semantic oracles/latency, SRM, mixed or legacy specs, unverified exposure,
  malformed/truncated sources, a still-running/unfilled fixed cohort, or
  confidence intervals that do not establish
  quality non-inferiority and cost reduction. Owner and experiment filters now
  precede the storage row cap. Installed capability metadata is discoverable at
  `/api/v1/experiments/capabilities`. Stopping seals an experiment ID
  irreversibly, and inference always uses the same first fixed cohort; see
  `docs/modules/experiments.md`.
- **Logging is now bounded, redacted, correlation-aware, and directly
  consumable by debugging models.** A single stream-policy registry governs
  application, audit, browser, process-console, raw-SSE, PostgreSQL, watchdog,
  and desktop logs; startup plus periodic maintenance enforces file/family and
  global budgets. WARNING+ floods are adaptively coalesced while preserving
  occurrence deltas, CRITICAL bypasses coalescing, and a compact rotating
  `incident.jsonl` index works without the database. The admin diagnostics API
  and `python3 -m lib.log_diagnostics` return ranked, selector-aware reports
  under a hard byte ceiling; the legacy digest falls back to this index when
  storage is down. Durable formatters recursively redact credential shapes and
  bound each record, including raw SSE and first-run console output; managed
  evidence is owner-only (`0600`, log directory `0700`). Historical oversized
  rotations are atomically compacted to complete-line bounded tails, and the
  machine-readable maintenance report distinguishes those compactions from
  active-writer rotations and deletions. See `docs/LOGGING.md`.
- **Conversation mutations no longer turn recoverable storage pressure into
  generic frontend HTTP 500 popups.** Turn/attempt/search routes preserve the
  Sidecar error taxonomy: missing authority is 404, transient
  busy/unavailable/timeout is 503 with `Retry-After`, optimistic or permanent
  authority conflicts are 409, and only integrity/protocol/internal failures
  are 500. Generated frontend writes reuse the same command id and body for up
  to three bounded retries, while `conversation_authority_conflict` permanently
  switches a normalized conversation away from v1 message-array writes.
- **The large-authority write path is bounded by changed data, not transcript
  size.** Attempt transport payloads and command receipts are capped; event
  retention, checkpointing, replay dispatch, queue-board persistence, and page
  reclamation run in bounded/idempotent units. Manual `/compact` now commits
  through atomic `turn.compact`, including recursive branch/attempt/event
  deletion and a tiny `requiresSnapshot` sync invalidation. Turn-native search
  uses 10 KiB per-turn derived fragments with transactional lifecycle updates
  and a retrying maintenance-lane historical backfill instead of rewriting a
  frozen conversation aggregate.
- **Adjacent false-failure producers were removed at their source.** Nested
  tool execution now snapshots the assistant tool-call carrier together with
  its result, Codex dispatch honors alias/cell/model/default precedence, memory
  requests without a project are constrained to global scope, and periodic
  optional-provider autodiscovery 404s no longer masquerade as application
  faults. Designed retained-user/TODO/Brain context carriers survive private
  field stripping without false same-role producer alarms; genuine duplicate
  user turns, provider failures, and storage failures remain visible.
- **Frontend migration fallbacks stopped being shadow production owners.**
  Turn state/projection/commands, HTTP transport, send-start leases,
  listener/timer lifecycles, file-size formatting, cookie-capture consent, and
  orchestration diagnostics now each have one statically imported typed owner.
  The retained runtime injects only ambient UI dependencies; `TofuModules` is
  restricted to lazy feature
  commands and diagnostics. Turn-v2 resume UI no longer computes the legacy
  settlement before overwriting it with server data. Task Mode regression
  guards now execute shared native Vite graphs and real locale inputs instead
  of reconstructing the deleted `static/js` module graph. See
  `docs/FRONTEND_MIGRATION_DEBT_AUDIT.md`.
- **Conversation synchronization now has one generated v3 contract and one
  browser stream owner.** The unused attempt-scoped SSE client was removed;
  snapshot, opaque cursor, replay, recovery, and health now belong to the
  conversation-scoped coordinator. The canonical OpenAPI document generates
  both Python and TypeScript artifacts, and clean-checkout/export plus
  freshness tests prevent either side from silently drifting. Semantic turn
  mutations now append compact change records atomically with their authority
  rows; replay gaps reset through a bounded snapshot instead of timestamp
  overlap. Commands share one owner-scoped, idempotent service and use a
  register/bind/start handshake so a worker cannot outrun durable attempt
  ownership. Timestamp-only v2 catch-up is rejected before any heavy read,
  and the connection badge is driven only by the authoritative conversation
  transport. Visible orchestration takeover now performs one revision advance
  with an exact patch instead of a two-step jump that forced snapshot recovery;
  every advancing attempt event is fail-closed unless it carries a replayable
  patch. See `docs/CONVERSATION_SYNC_V3.md`.
- **Frontend source ownership now has a shrinking, model-readable default.**
  The Settings tool inventory moved out of the 5 MiB retained runtime into the
  typed, lazy `features/settings/tools-inventory.ts` owner; obsolete classic
  action names and the dead `misc.ts` filename registry were removed. The main
  entry is 1,185 gzip bytes smaller (the code now loads with Settings), while
  total emitted JavaScript is also 65 bytes smaller. Source-shape budgets now
  cap ordinary modules at 100 KiB and separately ratchet the retained runtime,
  HTML shell, and two monolithic stylesheet owners; the wire-format generator
  no longer targets the retired `static/js` tree.
- **Gateway 5xx outages are now waited out indefinitely instead of failing
  the turn (owner directive 2026-08-20).** `TOFU_GATEWAY_OUTAGE_BUDGET_S`
  now defaults to `0` (disabled): when the whole upstream returns only
  502/503/504, the dispatch loops keep rotating (0.3s cycles, `abort_check`
  every cycle, HUD shows the climbing `stream.phase.retryGateway` attempt
  counter) until the gateway recovers or the *user* cancels — stopping is
  the user's call, never the system's. This mirrors the 2026-08-03 ruling
  for 429 contention (`_saturation_budget_secs`, also default 0) and the
  budget reader was converted to the same per-call env pattern, so the
  legacy bounded give-up (free the worker thread during a total outage)
  can be restored at runtime by setting a positive budget. The
  `llm_fallback` upstream-error envelope branch stays as defense-in-depth
  for when the cap is re-enabled. Pinned by
  `tests/test_dispatch_gateway_outage_cap.py` (storm-not-capped-by-default
  + knob-defaults-disabled cases).
- **Turn-pipeline latency campaign (2026-08-21, measured on the live Turn
  Trace fold: 34 tasks / 7.2 h of production event logs).** Non-LLM wall
  time was ~29% of every turn; this batch removes the three recoverable
  sinks. (1) **Tool dispatch wait** (median 2.4 s per tool, 7.3% of all
  wall time): same-round read tools now start immediately instead of
  queuing behind the serial write lane, with a write-sensitive guard that
  keeps the read cache correct across same-round writes
  (`tool_dispatch/_pipeline.py`). (2) **Fixed per-turn tickets**: the first
  round used to pay ~2.5 s of serialized prep — the conversation is now
  loaded once per request (was 2–3 reads), the orphan-task reaper is
  throttled off the hot path, system-context blocks assemble in parallel,
  the skills index is mtime-cached, and the SSE streamer wakes on events
  instead of a 50 ms poll. (3) **End-of-turn**: the terminal `done` frame
  no longer waits out the 300 ms event-batch window, the modified-files
  filesystem walk moved into the async commit thread (arriving on
  `round_committed`), and a duplicate whole-conversation re-serialization
  before `done` was removed. Plus FUSE-tax cuts across the local tools:
  batched edits now commit one atomic write per file instead of one per
  edit, `run_command`'s pre-spawn snapshot is a single-pass scandir with
  one stat per entry, `list_dir` dropped its per-subdir counting readdir,
  and v2 delta frames persist only the cumulative content/thinking patch
  instead of re-folding the whole turn projection. Commits `7efab787`,
  `156a225b`, `fb381486`, `9524862b`, `dd1f9432`, `e140584f`.

### Added
- **Tofu is now a consumable developer runtime instead of a checkout-shaped
  dependency.** The `tofu-agent` wheel exposes a typed embedded Python runtime
  and a secure `tofu-agent serve` HTTP/SSE sidecar over the production agent
  kernel, with no database or ChatUI application lifecycle. Transient tasks
  carry an explicit principal but skip every durable
  birth/event/result/index path. A bundled bilingual `/setup` control plane now
  lets operators choose a Provider template, discover models, run a real probe,
  and hot-apply one managed default without writing configuration code. Secrets
  are encrypted in an atomic database-free settings file, never returned to the
  browser, and survive restarts; env/CLI ownership remains a deterministic
  read-only override. Applications retain only a Tofu URL/token and omit model
  selection, while callers may still send endpoint/key/model per request. A
  source-free OCI `agent` target, sync/async Python SDK, dependency-free
  TypeScript SDK, idempotent HTTP 202 submission, cursor-resumable task streams,
  clean-wheel gates, and tag publishing workflow complete the distribution
  boundary. See `docs/DEVELOPER_RUNTIME.md`.
- **Turn Trace — the unified per-task timing interface + flame view.** One
  pure fold (`lib/tasks_pkg/turn_trace.py`) derives the hierarchical span
  tree of any task — turn → round → llm / tool / retry-wait / compaction /
  approval — from the persisted `task_events` log (no new instrumentation;
  timing can never drift from what actually happened). Served at
  `GET /api/v1/tasks/<id>/trace` and rendered as a time-axis waterfall
  flame graph inside the Request Inspector drawer (「耗时分析」entry).
  Strict accounting is a CI-asserted contract invariant: the summary
  buckets are a disjoint partition of the turn interval and whatever
  cannot be attributed is an explicit gray gap row — never a silent hole.
  Declared duration budgets (`_TOOL_BUDGETS_MS` / `_KIND_BUDGETS_MS`) flag
  over-running local-tool spans as an optimization worklist while LLM
  deep-think, user `run_command` workloads, approval time and upstream
  rate-limit waits are declared unbounded (never flagged). Every
  registered chat phase must declare a trace rule (drift-guarded).
  Contract: `docs/TURN_TRACE_CONTRACT.md`; tests:
  `tests/test_turn_trace.py`, `tests/test_frontend_turn_trace.py`.

### Fixed
- **V2 live conversations no longer trigger multi-megabyte refetch storms or
  leave the network badge stuck on “重连中” (2026-08-23).** The incident had
  three amplifiers: timestamp-overlap catch-up repeatedly returned an unchanged
  growing turn, attempt replay rehydrated its full projection onto every page
  tail, and each cross-tab `conv_changed` hint eagerly hydrated background
  conversations. Native streams now carry revision-checked structural patches;
  catch-up requests dedupe by known projection revision before SQLite reads the
  heavy column; background notifications only mark stale state; and a live
  EventSource remains the sole ordered projection writer. Typed heartbeats and
  transport reopen now reset the shared silence clock immediately, while real
  `onerror`/storage-wedge states still show degraded. Legacy stream readers keep
  page-tail full-projection compatibility.
- **Turn Send/Regenerate no longer surface storage backpressure as opaque
  HTTP 500 popups (2026-08-23).** The incident was one failure chain, not two
  unrelated route bugs: high-rate `turn.event.record` writes were incorrectly
  scheduled on the interactive writer lane, and a drained SQLite group-commit
  batch marked every member as acquired before its operation began. A slow
  projection write could therefore starve a new turn for 4 seconds and a
  regenerate for 16 seconds. Event projections now use the event lane, each
  batch member arms acquisition only when it actually reaches the writer, and
  all turn-v2 storage failures retain their stable classification at HTTP
  (transient failures are typed `503 server_busy` responses with
  `storageCode` and retry advice). The generated conversation client snapshots
  the validated Send/Regenerate body and follows the OpenAPI-owned retry policy
  for network loss, 502/503/504, and typed transient storage failures, always
  with the original `commandId`, so a lost ACK cannot duplicate a turn;
  replay can also finish dispatch when the atomic create committed before its
  response was lost. The first-message conversation remains atomic: a timed-out
  create leaves no half-conversation, which explains the incident's missing
  second conversation ID without implying transcript loss.
- **The 441.7 GiB SQLite authority now actually drains its historical event
  backlog without recreating writer pressure (2026-08-23).** Production
  inspection found an 862,084-rowid legacy attempt-event span averaging
  484 KiB in stratified samples and a 4,109,666-rowid generic-event span.
  Attempt retention repeatedly selected the same
  oldest already-empty attempt window, so it stopped making progress; generic
  retention executed only one of its sixteen configured batches and wrote a
  permanent receipt for every cleanup tick. Candidate selection now requires
  a remaining transport row, generic cleanup commits and re-enters the fair
  maintenance lane between bounded batches, and backlog/reclaim work uses a
  30-second catch-up cadence before returning to the five-minute steady probe.
  SQLite incremental vacuum remains wall-bounded to single-page units.
- **High-frequency recovery state no longer floods permanent command
  receipts.** Seven days produced 330,952 receipts, including 203,250 task
  projection checkpoints and 47,362 mostly-empty workspace claims. Task
  results now use a dedicated version-witnessed checkpoint operation: an
  identical ambiguous-ACK replay returns the authority's existing version,
  while a stale different snapshot conflicts and cannot roll terminal state
  backward. This removes the extra whole-result JSON+SHA pass and the receipt
  insert from every streaming checkpoint. Clean `None` claim results, empty
  queue reaps, and naturally idempotent retention writes no longer create
  receipts; append, billing, counter, and dequeue operations retain their
  exactly-once protection.
- **Synthetic continuation turns preserve role alternation without deleting
  terminal metadata.** TODO/intent-stall continuation now persists the
  assistant response before its user nudge; empty assistant ghosts after an
  engine-authored user turn are filled with non-empty tombstone prose in place.
  Error envelopes, finish reasons, task IDs, provider replay blocks, and stable
  message IDs survive that repair, preventing both provider-visible `user,user`
  adjacency and the former loss of a stuck-task error bubble when a queued
  follow-up arrived.
- **Generation failures now explain themselves instead of leaking
  `generation_error`.** Turn-v2 projection carries the durable typed error
  envelope into the existing visible error card, while the finish chip uses
  its localized kind, message, and severity (including touch-accessible inline
  guidance) and clears stale errors after a successful retry. The high-volume
  exact tool-loop guard now gives the model one bounded strategy-correction
  round before stopping; if it still repeats, the card explains the repeated
  call, recovery options, and whether the failure is retryable. The terminal
  settlement boundary now normalizes every failure to a complete error
  envelope, legacy archive folding does the same, and a dry-run/CAS/backup
  migration repairs previously persisted malformed failures. A turn whose
  executor cannot start is now durably and idempotently classified as the
  retryable `task_start_failed` envelope instead of returning a partial 500.
- **Storage writer throughput collapse ("Storage writer acquisition timed
  out" floods, 2026-08-20).** Root-caused as a distinct failure class from
  the 2026-08-18 wedge: on a degraded network filesystem the single writer's
  one-fsync-per-commit ceiling fell below write demand, so every lane timed
  out at acquisition for 15+ minutes while no transaction was actually
  wedged. Three-layer root fix: (1) the writer now group-commits its drained
  backlog in one transaction with per-job SAVEPOINT isolation (one fsync per
  batch, automatic fallback to per-job transactions on batch-level failure);
  (2) a live v2 frame's replay-log row now rides inside the
  `turn.event.record` authority transaction instead of a second command
  (one frame = one transaction; skew between authority and replay log is no
  longer possible); (3) new opt-out storage fast path relocates the write
  front to a measured-local filesystem with a continuously-shipped durable
  shadow on the data dir — fail-closed activation probes (WAL semantics,
  free space, measured ≥3× fsync win), shipper-owned checkpoints, two-way
  boot reconciliation with a split-brain guard, bounded observable RPO. See
  `docs/TRB-fastpath.md`.

### Added
- **Generic tool-result viewer (chat UI).** Every settled tool round whose
  result has no dedicated renderer — `read_files` text, `grep_search`,
  `find_files`, `list_dir`, browser reads, MCP tools, etc. — used to collapse
  to a bare icon+title+badge line with the entire result invisible. These
  rows are now expandable (`<details>`): click to reveal the verbatim result
  in a monospace, scrollable pane with a stats (`N lines · M chars`) + copy
  header. JSON payloads are pretty-printed; over-long results are soft-capped
  at 120k rendered chars with a stated truncation note. Styling is
  theme-variable driven (dark/light/tofu) and covered by new characterization
  tests in `tests/test_frontend_tool_rounds_render.py`.

## [0.16.0] - 2026-07-31

> **Versions 0.11.0 – 0.15.2 were never released.** They exist as `VERSION`
> bumps (and, for 0.15.0 / 0.15.2, as orphan git tags) but no GitHub Release
> was ever published behind them — the last published release was v0.14.2, and
> the macOS build leg starved on a retired runner label before the release job
> could run. Rather than reconstruct changelog entries for releases that never
> shipped, their content is folded into this entry.
>
> This is a **minor** bump, not a patch: `VERSION` sat at 0.15.2 from
> 2026-07-23 while ~1250 further commits landed, adding six new top-level
> capability packages — four of which expose their own HTTP surface
> (`routes/api_v1/research.py`, `motion.py`, `skills.py`, `private_hosts.py`).

### Added
- **Auto-research pipeline (`lib/research/`, `routes/api_v1/research.py`).**
  Give it a research direction and it harvests recent literature into a local
  paper corpus (parsed once, then reused), surveys it to map what has already
  been done, and proposes scored ideas screened against that corpus so they
  are genuinely new rather than A+B recombinations. Rejected ideas are
  reported with the reason. Exposed as the `produce_research` tool.
- **Long-form research reports (`lib/longform/`).** research → outline →
  sections(×N) → assemble, published as a cited markdown artifact. The stage
  list is data-dependent (one stage per outline section), which the static
  video stage list never exercised.
- **Motion-graphics video pipeline (`lib/motion_video/`, `routes/api_v1/motion.py`).**
  Topic → researched script → real-TTS-timed storyboard → per-scene composed
  MP4 → concat → narration mux. Every fact card carries a real source URL and
  is credited on an end card. Per-scene authoring degrades to a template floor
  so a single bad scene can never fail the film.
- **Production Substrate (`lib/production/`).** The horizontal layer under
  every "one sentence → finished product" capability: a checkpointed stage
  graph where a stage's artifact is committed as soon as its gate passes, so a
  killed process resumes at the first unfinished stage. Crash-resume is a
  correctness contract here, not an optimisation.
- **Text-to-speech / narration (`lib/tts/`, `routes/api_v1/audio.py`)** and
  voice input (speech-to-text) with a mic button in the composer.
- **Skills as a first-class noun (`lib/skills/`, `routes/api_v1/skills.py`).**
  User-installed skill packages are now decoupled from model-authored
  memories: an always-visible `<available_skills>` index plus an
  `load_skill` progressive-disclosure loader. The model channel is
  read-only; install/uninstall/toggle are user-only.
- **Project Brain — cross-conversation coordination (`lib/conversations/`).**
  Charter with human-reviewed decisions, an epic board with claims and leases,
  an activity feed, direct peer messaging, path leases, and a "Needs you"
  attention surface that aggregates everything genuinely waiting on a human.
- **Request Inspector / debug panel.** Per-request snapshot store with
  server-side folding, a `</>` affordance on bubbles and tool rows, and
  incremental retention — so "what exactly went on the wire?" is answerable
  without a debugger.
- **Manual compaction (`/compact`).** Context compaction is no longer only
  automatic: an explicit command with a REST endpoint, a frontend card, and a
  live streaming summary.
- **Air / Pro / Studio capability dial.** One toolbar control replaces the
  separate Enhance/Tools/Mode toggles, selecting a coherent capability profile
  per turn.
- **Remote Worktree / Desktop Agent.** Run a project on a remote machine from
  the desktop app — per-user bridge tokens, a "Remote devices" picker group,
  tray connect, and `run_command` parity with the local path.
- **Auto-escaping `safeHtml` tagged template (`static/js/core/safe_html.js`).**
  The chat-render path built HTML by string concatenation, hand-wrapping every
  interpolation in `escapeHtml()` — correct but fragile (one forgotten wrap is
  an XSS hole). `safeHtml\`...\`` escapes EVERY interpolation by default, with
  an explicit `raw(x)` opt-out for trusted HTML. A lint rule in
  `tests/test_frontend_safe_html.py` blocks new bare template-string HTML
  sinks in adopted render files.
- **Frontend type-check harness (`tsc --checkJs`, no build step).** Root
  `tsconfig.json` + `static/js/globals.d.ts` catch cross-file global misuse —
  typos, stale renames, dead `typeof` guards — that the shared-`window`-scope
  design otherwise fails silently at runtime. Wired as `make typecheck` and
  enforced by a monotonically-decreasing error-budget ratchet.
- **Release gates.** `scripts/release_assets.py` (is the release complete? —
  per-platform assets plus a size floor that catches a hollow build) and
  `scripts/changelog_gate.py` (is this VERSION documented? — this file is now
  a build gate, which is why the nine-version gap above can never recur).

### Changed
- **SQLAlchemy Core table-definition layer (`lib/database/_core_schema.py`).**
  Tables are defined ONCE as Core `Table` objects and compiled to correct DDL
  and DML for BOTH backends (PG `JSONB`/`IDENTITY` ↔ SQLite `JSON`/autoinc,
  paramstyle, dialect-correct upserts), retiring the hand-maintained twin-DDL
  path. Compile-only: no SQLAlchemy Engine is opened; execution stays on the
  existing connection. Generated DDL is byte-equivalent to the legacy hand-DDL
  (`tests/test_core_schema_parity.py`). Adds `sqlalchemy>=2.0`.
- **Unified the LLM SSE streaming core.** `lib/llm/stream.py` (sync) and
  `lib/llm/astream.py` (async) each carried a ~480-line copy of the identical
  SSE parsing loop, so every fix had to land twice and the copies drifted.
  That logic now lives once in `lib/llm/_sse_core.py`; the two modules are thin
  transport shells keeping only retry/backoff and transport-native handling.
  Pure code-motion — anomaly fields are emitted byte-for-byte as before, locked
  by `tests/test_sse_core_parity.py`. Net −432 lines.
- **Account ↔ wire-face separation in provider config**, so one account can
  serve both OpenAI- and Anthropic-shaped endpoints without duplicate entries.
- **Web search and fetch extracted** to the standalone `tofu_search` package;
  the app seams via `lib/search_bridge.py`. `lib/fetch/` and `lib/search/` no
  longer exist in-tree.
- **Desktop release workflow is VERSION-driven, not tag-driven.** A tag is a
  product of releasing, not evidence of it; the gate now asks the Releases API
  whether a complete asset set exists.

### Fixed
- **Weak image-caption escaping in `renderMessage`.** The image tile's `title`
  tooltip escaped only double-quotes, leaving `<`/`>`/`&` unescaped. Now uses
  the full `escapeHtml()`.
- **Chat didn't re-render on language switch / debug-mode toggle.**
  `i18n.js::_onLanguageChange` and `settings/save_export.js::saveSettings`
  called `renderMessages()` behind a `typeof … === 'function'` guard, but that
  function never existed (the real repaint is `renderChat(conv)`), so the guard
  silently swallowed the no-op. Caught by the new `tsc --checkJs` harness.
- **Duplicate `common.close` i18n key** in `static/js/i18n.js` removed.
- **Desktop downloads 404'd during a release window.** URLs were built as
  `/releases/latest/download/<cached filename>`, whose two halves have
  different lifetimes. They now come from one API payload with the tag pinned.
- **Intel Macs could not install Tofu.** Only the arm64 DMG shipped; the macOS
  build is now a per-architecture matrix on live runner labels, and the release
  refuses to publish a partial asset set.
- Numerous fixes across tool lifecycle (per-tool completion events rather than
  round-barrier), streaming transport, cache accounting, MCP launching,
  scheduler, and the paper Reading Mode pipeline.

## [0.10.0] - 2026-05-09

### Added
- **Daily Optimizer (self-tuning loop).** New `lib/optimizer/` package mines the
  prior day's logs, audit events, and daily reports, asks an LLM for
  optimisation proposals, and either auto-applies whitelisted low-risk
  actions (currently `block_search_domain`, with TTL-based auto-revert) or
  stages everything else as `pending_review` for human approval. Runs nightly
  at 03:30 via the scheduler (`Daily Optimizer` task, auto-registered on
  boot). REST API in `routes/optimizer.py`; review UI in `static/js/optimizer.js`.
  Gated by `OPTIMIZER_ENABLED` setting.
- **Skills Store (curated catalogue + drag-and-drop installer).** Settings →
  Skills tab now has an App-Store-style layout (search + Catalogue/Installed
  scope tabs + category pills) backed by `lib/memory/catalog.py`. One-click
  install downloads a `.zip` over HTTPS (≤ 50 MB) and unpacks it via
  `lib/memory/installer.py`. Anthropic / OpenClaw / AgentSkills `.zip`
  packages can also be drag-dropped onto the tab; bundled `install.sh`
  scripts are surfaced as hints, never auto-executed.
- **Pluggable token counter.** New `lib/token_counter/` package routes token
  counting through provider-specific backends (Anthropic / Gemini / DeepSeek
  / HuggingFace / tiktoken / heuristic) with a usage cache, replacing the
  scattered ad-hoc estimators.
- **File-history store.** New `lib/file_history/` records per-file edit
  history so write tools and the diff viewer can show a coherent timeline
  of changes across a session.
- **Memory prefetch.** `lib/memory/prefetch.py` surfaces likely-relevant
  memories at turn start via the `<relevant_memories>` block, so the model
  doesn't have to call `search_memories` as a generic discovery step.
- **Compaction archive viewer.** New `routes/conversations_compaction.py`
  + `static/js/compaction-viewer.js` let you inspect the archived layers
  produced by 3-layer context compaction.
- **Conversation full-text search endpoints** moved into a dedicated
  `routes/conversations_search.py` Blueprint (extracted from
  `routes/conversations.py`).
- **Provider templates.** Added Meituan and Tencent provider one-click
  templates in Settings → Providers.

### Improved
- **`routes/chat.py` decomposition.** Extracted `chat_human_io.py`,
  `chat_queue.py`, and `chat_tool_state.py` so the chat blueprint is
  smaller and individual concerns (stdin/human-guidance responses,
  server-side message queue, tool-toggle PATCH) live in their own
  modules.
- **PDF parsing.** Added `lib/pdf_parser/docling.py` as an additional
  backend alongside the existing text/VLM/math paths.
- **Project tools.** New `lib/project_mod/gitignore_suggest.py` proposes
  `.gitignore` entries for files the indexer keeps re-scanning.
- **Multi-root workspace robustness.** Extra roots now persist across
  conversation switches (frontend sends `projectPaths`; backend
  `ensure_project_state()` accepts `extra_paths`). The system prompt's
  multi-root section explicitly warns about new-file creation in
  non-primary roots, since there is no auto-detection until the file
  exists.
- **`requirements.txt`.** Pin `lxml_html_clean>=0.4` so trafilatura keeps
  working on lxml 5.2+ where `lxml.html.clean` was extracted.

### Fixed
- Numerous small fixes in browser dispatch, conv_ref handling, image
  generation, LLM sanitisation, scheduler timer/manager, and trading
  decision routes (see file-level diffs).

## [0.9.3] - 2026-04-22

### Fixed
- **MCP launcher pre-flight check.** When an MCP server is configured with a
  `command` that is not on PATH (e.g. `uvx` without uv installed, `npx` without
  Node), we now emit a clear, actionable install hint instead of a cryptic
  `FileNotFoundError`. Covers uvx / npx / pipx / node / python3.

### Improved
- **Overleaf MCP auto-install resilience.** The catalog entry and migration
  rules now pin `overleaf-mcp-plus[compile]>=0.1.3`, the slimmer release that
  drops the unused playwright dependency (~100 MB faster first-run install).
- **Auto-migration upgraded.** Stale server entries from prior versions are
  rewritten on load even when only the args list differs — user-supplied env
  vars and credentials are always preserved.

## [0.9.2] - 2026-04-20

### Fixed
- Fixed Overleaf MCP server failing to launch with `FileNotFoundError: 'overleaf-mcp'`
  on machines where the package was not pre-installed. The curated registry entry
  now uses `uvx --from overleaf-mcp-plus[compile]` so the server is auto-fetched
  from PyPI on first launch, matching the behavior of the other MCP cards.

## [0.9.1] - 2026-04-20

### Improved
- Further optimized support for Claude Opus 4.7.

### Added
- Added support for the Overleaf MCP server in the curated registry
  (edit/read/compile/history on Overleaf LaTeX projects).

### Fixed
- Fixed incorrect retry behavior of the model when invoked by tools.

## [0.9.0]

- Previous release.
