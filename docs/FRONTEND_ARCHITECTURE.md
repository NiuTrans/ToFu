# Frontend architecture

Responsibility: source ownership, generated delivery boundaries, state
lifecycles, and the rules for extending the browser application.

The machine-readable ownership and shrinking-debt authority is
`contracts/frontend_conversation_architecture_v1.json`. The one-hop map for
conversation browser work is `frontend/src/conversation/README.md`.

## Source and delivery graph

```text
frontend/src/api + core + features + lifecycle
                    │ typed imports
                    ▼
              Vite module graph

frontend/src/runtime/sections + manifest
                    │ compose_frontend_runtime.mjs
                    ├───────────────┐
                    ▼               ▼
       app-runtime.js        manifest-owned lazy runtimes
          (generated)               (generated)

frontend/src/styles/{application,settings} + manifests
                    │ compose_frontend_styles.mjs
                    ▼
       static/{styles,settings}.css (generated)

frontend/src/i18n/locales/{zh,en}.json
                    │ gen_i18n_contract.mjs
                    ├────────────────────────┐
                    ▼                        ▼
       contract.generated.ts      generated compact JSON
                                             │ Vite ?url
                                             ▼
                                  content-hashed data assets

index.html + frontend/src/application-shell/fragments
                    │ application_shell_fragments.py
                    ▼
       served application shell (generic server assembly)
```

Edit sources, never generated outputs. Normal repository search ignores the
large artifacts so discovery lands on the semantic owner. Runtime section and
stylesheet order are explicit contract data in their manifests.

## Ownership rules

- New domain behavior is a TypeScript module under `frontend/src/`.
- A retained runtime section may inject ambient DOM/UI dependencies into a
  typed owner. It may not add a reducer, transport, command builder, error
  normalizer, timer authority, or persistence policy.
- The retained `Api` endpoint-name registry is built over the typed transport,
  registered in the private `runtimeScope`, and bound once as an ESM lexical
  compatibility port. Retained sections do not discover it through a browser
  global; the temporary public entry compatibility seam remains the only
  `window.Api` publisher. Neither facade implements `fetch`, affinity, request
  IDs, or errors.
- Fetch-like response projection into the status-preserving `{ok, status,
  data}` envelope and canonical result-error recovery live in
  `frontend/src/core/http-result.ts`. Retained orchestration consumers receive
  its immutable `HTTP_RESULT` port as a static lexical dependency; it is not a
  browser global or a second transport owner.
- Lazy features use the private runtime service registry. A retained feature
  that is not needed for application readiness belongs to a manifest-declared
  lazy runtime; the composer validates its imports and service dependencies.
  Core owners are static imports and are not republished as mutable globals.
- A typed feature that still consumes a retained presentation island names an
  explicit `runtimeScope` service; optional property access is not service
  registration. `npm run check:runtime` rejects closed, unreachable chains of
  top-level retained callables in every generated runtime, as well as stale
  composition and missing lazy dependencies.
- Every listener, timer, EventSource, observer, and subscription has a declared
  scope and disposal path.
- Backend process liveness and Sidecar storage readiness are separate verdicts.
  `backend-availability-monitor.ts` treats push/browser state as suspicion and
  requires two failed health probes before alarming; the independent
  `storage-availability-monitor.ts` owns one visibility-aware, single-flight
  recovery poll. Their overlapping `/api/health` reads share the no-cache
  `availability-health-probe.ts` coordinator: the opener's bounded request
  supplies one immutable status snapshot, and a lazy memoized `json()` lets
  independent consumers read the body without duplicating the wire request or
  consuming it twice. The flight is released on either settlement, so later
  recovery checks remain authoritative. Both verdict owners receive DOM,
  health, clock, copy, and logging ports at composition, publish no browser
  global, and are destroyed with the retained page composition lifecycle.
- Long-lived-tab frontend self-healing lives in
  `frontend/src/core/build-watch-controller.ts`. The DOM-free controller owns
  only one bounded pending-build record, the 30-minute busy defer, and the
  session reload guard. Served build identity arrives on the existing push
  pong every five minutes and on visibility resume; retained composition adds
  no health HTTP poll, timer, or visibility listener, replays the last valid
  identity to late subscribers, and remains inert against older servers.
- The Memory panel owns one active catalog request per open epoch and at most
  one trailing request after close/reopen invalidates presentation ownership.
  Repeated opens and scope-tab clicks share the active all-scope snapshot and
  filter it locally; generation checks still prevent a superseded view from
  rendering. Settlement releases both flights, so the feature retains no
  page-lifetime server-data cache.
- Push WebSocket RTT, per-ping timeout, half-open force-close, and offline
  verdicts remain one authority in `frontend/src/runtime/sections/push.js`.
  The adjacent `net-latency.js` topbar adapter is an event-only projection of
  that authority plus the typed conversation-stream aggregate: it owns no
  staleness clock, elapsed-time transport guess, extra connection, or endpoint,
  and releases both subscriptions with the retained composition lifecycle.
- Collaboration Bar summary ordering and its local peer mirror live in
  `frontend/src/features/presence-summary-controller.ts`. The DOM-free owner
  retains one displayed-scope summary, one same-scope shared request flight,
  a generation guard against rapid conversation/project switches, and at most
  32 project roots × 128 conversation IDs. The retained presence projection
  refreshes once after its late boot wiring, then only from explicit
  conversation/project funnels or push-invalidated 300 ms demand debounce; it
  owns no page-lifetime interval.
- Browser-console flush timing lives in the browser-global-free
  `frontend/src/core/client-log-flush-scheduler.ts` owner. It resolves the
  direct/constrained-proxy 15/60-second profiles, coalesces repeated log demand
  behind one asynchronous flush, and owns a timer plus visibility subscription
  only while the bounded relay queue is non-empty. Hidden demand releases its
  timer and keeps one resume signal; empty settlement, kill-switch clearing,
  and teardown release both. The retained adapter remains the sole owner of
  console patching, its 400-line / 200-entry / 800-character bounds, the
  never-amplify drop policy, and the existing pagehide beacon.
- The per-turn MCP context rail owns no transport or tool-schema cache. Its
  bounded server/count projection piggybacks on the already-required
  `server-config` first-screen response, then on MCP catalog and per-tool
  mutation responses while Settings is active. `applyMcpToolSummary` replaces
  the projection synchronously; a page that never opens Settings issues zero
  `/api/v1/mcp/tools` requests merely to count tools, and opening a server's
  tool-toggle panel remains the only browser demand for full schema rows.
- Deployment-flag loading lives in the DOM-free
  `frontend/src/core/feature-flags-loader.ts` owner. The normal first screen
  consumes the bounded snapshot carried by `server-config` and issues no
  `/api/v1/features` request. The owner accepts at most 256 valid boolean keys,
  requires the core debug/optimizer flags, ignores only API-envelope metadata,
  and commits only a changed snapshot. Missing/invalid piggyback data (including
  an older server) activates one shared compatibility request; concurrent
  callers join it and settlement immediately releases it. It owns no timer or
  page-lifetime cache. The retained adapter alone projects a committed change
  to badges, launch visibility, the sidebar, and the active conversation with
  `forceScroll:false`.
- Confirm, alert, prompt, and multi-option choice dialogs share the lazy typed
  `frontend/src/lazy-dialog-controller.ts` service and
  `frontend/src/dialog-controller.ts` DOM owner. The latter loads on first use,
  permits one active overlay, renders model/user-facing text with DOM text
  nodes, settles replaced dialogs to their safe default, and disposes its
  keyboard listener, animation frame, live-check interval, and exit timer.
  Retained consumers get only the four lexical service aliases composed in
  `_prelude.js`.
- Independent asynchronous fan-out uses
  `frontend/src/core/async-pool.ts`. Conversation offline recovery and
  the lifecycle-owned
  `frontend/src/conversation/application/conversation-wake-recovery.ts`
  controller share its explicit four-worker budget; pageshow/online probes
  select only live attempts, dispose their listeners, and isolate failures.
- Explicit refresh of one catalog conversation is the injected application
  command in `frontend/src/conversation/application/conversation-refresh.ts`.
  It resolves the live Turn hydrator, reports presentation failure through a
  port, and rethrows the authoritative error; it owns no retry state or
  `runtimeScope`/`window` API.
- Swarm push subscription, lifecycle, transient-Turn overlay, authoritative
  rebase, and terminal hydration live in
  `frontend/src/conversation/application/swarm-presentation-overlay.ts`.
  Retained reducers are injected presentation ports; they neither subscribe
  independently nor copy session telemetry into durable Turn state.
- Swarm stuck-panel recovery timing lives in the DOM-free
  `frontend/src/conversation/application/swarm-reconciliation-scheduler.ts`.
  The retained panel injects backend reconciliation, browser clock/visibility,
  and elapsed-ticker pause/resume ports but owns no scheduling policy. The
  typed owner has no boot timer or listener: rendering an unresolved panel
  creates one demand lifecycle, hidden pages pause it, all triggers share one
  request flight, backend backoff supplies the next exact deadline, and a null
  follow-up disposes both timer and visibility subscription.
- Command elapsed/deadline chips and Timer Watcher countdowns share the DOM-free
  `frontend/src/conversation/application/demand-scoped-presentation-ticker.ts`
  owner. The adjacent retained tool-round sections inject timeout, visibility,
  DOM-tick, and logging ports. They create no boot timer or listener: rendering
  either live clock demands one shared lifecycle, hidden pages pause its
  timeout, visible resume ticks immediately, and the first empty DOM tick
  disposes both scheduling and visibility subscription. Because both sections
  are retained in manifest order, no delayed-renderer upgrade scan exists.
- Metadata-only boot coordination lives in
  `frontend/src/conversation/application/conversation-startup.ts`. Its injected
  surface can load conversation/folder catalogs and converge the active view,
  but cannot hydrate or dispatch a Turn; folder failure/retry is independent
  and initialization still awaits both metadata paths. The retained folder
  owner has one shared request flight across boot, reconnect, push invalidation,
  and its bounded first-load recovery chain. `Api.folders.list()` distinguishes
  a valid empty catalog from transport or parse failure, so ordinary empty and
  failed startup each issue exactly one request: success replaces the
  projection (including with `[]`), while failure preserves the last good tree,
  remains observable to callers, and cannot trigger pinned-folder migration.
  Reading-library folders use the same required-list decoder and never turn an
  outage into authoritative empty data.
- Chat composer command serialization remains at the retained adapter in
  `frontend/src/runtime/sections/main/main_send_pipeline.js`. At most one
  submission may own preprocessing and acknowledgement at a time. A Send or
  Enter intent received during that flight is collapsed into one trailing
  intent and drained after authoritative acceptance; a stop, rejection, or
  uncertain failure cancels automatic draining and leaves the restored draft
  for an explicit retry. Thus command IDs remain idempotent without turning a
  temporary send lock into a silent no-op.
- Pre-send translating/connecting presentation lives in
  `frontend/src/conversation/application/send-preparation-overlay.ts`. It owns
  one stable transient Turn and remembers the initiating conversation so a
  later view switch cannot redirect teardown. Catalog/store/i18n/scroll are
  injected ports, and a detached scroll host cannot roll back the Turn update.
- Cookie-capture completion subscription and toast projection live in
  `frontend/src/core/cookie-capture-consent.ts`. The composition boundary
  subscribes after retained push initialization, owns unsubscription through a
  lifecycle scope, and exposes no manual frame handler or browser global.
- My Context preference acceptance/dismissal and undo DOM transitions live in
  `frontend/src/features/memory/preference-actions.ts`. The controller receives
  mutation, translation, icon, and failure-reporting ports; failed mutations
  restore actionable UI state before diagnostics run. Its two markup action
  names resolve through the central action table and are not browser globals.
- Shared fullscreen-image and generated-image download actions live in
  `frontend/src/image-viewer-actions.ts`. One controller replaces overlays,
  owns exactly one Escape listener while open, clears detached image callbacks,
  and removes temporary download anchors even when the click operation fails.
  Composition owns teardown; markup action names remain central dispatch keys.
- Fetch-response byte decoding and newline framing live in
  `frontend/src/core/sse-reader.ts`. The lazy arXiv ingest owner imports the
  reader directly and owns event interpretation; the transport primitive is
  neither a feature-registry service nor a browser global.
- Per-Turn translation start deduplication lives in
  `frontend/src/core/translation-claim-registry.ts`. Claims expire after three
  minutes, stale entries are pruned, and the page-lifetime registry fails
  closed at 256 live claims. Translation code uses its immutable lexical port;
  no claim state or diagnostic probe is published to `runtimeScope`.
- Translation display selection is projected directly from authoritative Turn
  content by `conversation-view-model.ts`, using only turn-keyed local display
  mode. The retained task adapter may resume IDs it started during this page
  lifetime, but one task ID owns at most one poll loop and a terminal push
  cancels a sleeping poll before its next request. Durable completion converges
  through Conversation Sync v3. There is no parallel message translation model
  or browser-side history sweep.
- Conversation title, unique-prefix/full-ID, and per-conversation
  auto-translate-default queries live in
  `frontend/src/conversation/application/conversation-catalog-queries.ts`.
  The retained composition wrapper injects the live catalog, current default,
  and localized untitled label; the pure owner reads no browser globals.
- Local catalog-change reconciliation lives in
  `frontend/src/conversation/application/conversation-catalog-reconciliation.ts`.
  It preserves authoritative activity timestamps while a conversation is
  busy, sorts the injected catalog, publishes one cross-tab wake hint, and
  permits at most one pending sidebar animation-frame callback.
- ZIP package upload is one lazy typed transport owner in
  `frontend/src/features/skills/package-installer.ts`, shared by the Skills and
  Memory presentation adapters. It validates both picker and drop inputs,
  permits one active upload, and installs one fixed page-lifetime set of four
  drag listeners per surface; scope, copy, diagnostics, and post-install
  refresh remain injected feature policy.
- My Day TODO and stream writes live in the lazy typed
  `frontend/src/features/myday/task-actions.ts` controller. The retained panel
  injects only its selected report, cache, render, calendar, and input ports;
  optimistic mutations roll back on rejection, while stream cycle status and
  created reports are adopted from the server response. The sibling
  `quick-action-launcher.ts` owns suggestion-to-composer intent and makes the
  project-preserving prefill-before-conversation order executable and tested.
- My Day's reconstructible report cache lives in the typed
  `frontend/src/features/myday/report-cache.ts` repository. Keys include the
  resolved owner; 96 reports at 512 KiB and 24 month overviews at 128 KiB cap
  estimated storage at 51 MiB. The idle-loaded `background-controller.ts`
  owns one cache-first digest probe and one afternoon reminder timer, a
  16-owner reminder ledger, and explicit teardown. The retained panel neither
  opens IndexedDB nor registers page-lifetime background work.
- My Day calendar, report, progress, and polling presentation is demand-loaded
  as the manifest-owned `myday-presenters` runtime by
  `frontend/src/features/myday.ts`. Its three shell entries and the retained
  Escape path resolve through late feature ports; its private panel state is
  never captured by the startup runtime. The typed task/quick-action owners
  compose in the same feature chunk, but `frontend/src/features/background.ts`
  has no presenter import: cache-first digest refresh and reminders can preload
  without parsing the report UI. Lazy presenter dependencies are explicit
  runtime services, and repository availability remains a demand-time registry
  read because the background owner is its lifecycle authority. Static empty,
  TODO, launch, delete, and unfinished SVG markup has one immutable typed owner
  in `frontend/src/features/myday/presentation-assets.ts`; retained renderers
  interpolate that trusted asset table instead of carrying duplicate literals.
- Conversation catalog instant-paint metadata lives in the typed
  `frontend/src/core/conversation-metadata-cache.ts` owner. Every operation
  resolves a positive authenticated owner; v6 drops the former ownerless v1–v4
  stores and the short-lived transitional v5 shape. The whole origin keeps at
  most 200 metadata rows at 128 KiB and 1000
  sidebar rows at 32 KiB (56.25 MiB estimated maximum). It opens lazily,
  closes with the retained composition lifecycle, never stores Turns, and does
  not ask the browser to persist this reconstructible cache under disk pressure.
  The small `conversation-metadata-cache-lazy.ts` proxy coalesces first demand
  and keeps the optional IndexedDB implementation out of the first-screen
  bundle, including when teardown races the dynamic import.
- Authoritative catalog request, retry, applied-snapshot, and bounded cache-write
  orchestration lives in
  `frontend/src/conversation/application/conversation-catalog-loader.ts`.
  Rows enter memory and render before the reconstructible cache is written;
  refreshes retain at most one cache write plus the latest pending replacement.
  A successful merge/render commits its ETag, total count, and applied row IDs
  together; a decoded response that fails projection earns no validator, so
  its next recovery is one unconditional request rather than `304` plus a
  duplicate full-page request.
  A conditional `304` is accepted only while its server snapshot is still fully
  represented in memory, otherwise the loader retries once without its ETag.
  Server-stamped `busy` rows whose conversations have no client-side live Turn
  are woken through `ConversationTurnStore.wakeConversation` under the bounded
  async pool, so a hard refresh restores the sidebar streaming projection of
  every live conversation without a manual open.
- Revision-aware catalog invalidation debounce lives in
  `frontend/src/conversation/application/conversation-catalog-revision-gate.ts`.
  A positive numeric `conv_changed.rev` is compared with the greatest revision
  already present in TurnState or the catalog shell. An already-applied frame
  is a no-op; a newer frame wakes Conversation Sync, then the gate rechecks
  after 150 ms and suppresses the 500-row catalog request if the ordered stream
  reached that revision. The ledger retains at most 64 conversation IDs. A
  revisionless metadata hint, unknown conversation, malformed revision, ledger
  overflow, or still-stale projection keeps the full authoritative refresh.
  Hidden-page expiry performs no request because the existing visibility-resume
  path revalidates the whole catalog; teardown cancels the pending timer.
- Paper and standalone Research install their required typed presentation
  modules through `frontend/src/features/paper/panel-owners.ts`. Those source
  owners remain focused modules but ship as one unconditional lazy chunk;
  adding another always-loaded per-owner dynamic import is architecture debt.

  The standalone Research surface is a full workbench, not a Paper-reader
  sub-panel. `research-runtime.ts` owns the one live task stream, bounded event
  replay, poll/push teardown, durable artifact hydration and terminal-quality
  projection. `research-view.ts` is presentation-only: it renders the five
  visible stages (harvest, survey, ideate, evaluate, package), ranked experiment
  briefs, independent review decision, rejection audit, and the reproducibility
  ledger. A researcher can inspect the running trajectory in the **Live evidence
  trail** (tool-round projection); after completion the same page retains the
  corpus, full survey/gap map, usage ledger and review revision queue. Starting
  a new direction detaches browser polling/push through the runtime disposer but
  does not cancel the checkpointed server job; explicit **Stop run** is the only
  cancellation action. Recent directions come from the durable research list,
  so refresh/reopen never depends on the in-memory task registry.
  The retained Paper presentation islands are also demand-loaded: report and
  reader markup live only in the manifest-owned `paper-reader-presenters`
  runtime, while podcast and video markup live in `paper-media-presenters`.
  `frontend/src/features/paper.ts` installs both before the typed panel owners.
  The typed report runtime owns start/regenerate, provisional Stop handoff,
  polling, and reopen policy. Per-kind generations fence stale success and
  failure continuations even for two starts on the same paper; cache-hit starts
  reuse the canonical metadata-only apply path. One explicit cache-resolving
  lookup preserves live-task precedence and selects the preferred/fallback
  report server-side. The backend projects that hit from one bounded
  owner-scoped Sidecar aggregate; only the selected language's additive
  artifacts cross the process boundary. Rebuttal drafts retain at most 32
  papers and 40,000 characters per paper, matching the request/backend limit.
  A known paper starts by hash only; the server reads at most its 120,000-char
  prompt source only after live/cache misses. The browser retries with bounded
  text solely for the explicit pre-dispatch `paper_source_required` 400, after
  rechecking the same generation fence; ambiguous failures are never replayed.
  Late owner ports and markup action receivers use the private
  `featureRegistry`. Mutable retained values use its explicit
  `readLiveRuntimeBinding` / `writeLiveRuntimeBinding` state port, because the
  owner override cache must not hide a later paper switch. A raw
  lexical-global dependency is not a compatibility seam.
- Settings retained presenters are demand-loaded as the manifest-owned
  `settings-presenters` runtime. `frontend/src/features/settings.ts` evaluates
  the typed Settings owners before that retained island, so every declared
  typed service is present when the generated runtime closes its dependency
  boundary. The shell routes `openSettings`, `closeSettings`, `saveSettings`,
  `switchSettingsTab`, and onboarding's immediate `_oauthLogin` through the
  feature bridge. Reassignable shell state and Settings working copies cross
  the private registry through live accessors; a captured array, set, boolean,
  or cache reference is not an acceptable lazy-runtime dependency. Optional
  panel presentation follows the same residency boundary: the Devices tab
  imports `frontend/src/features/settings/devices.css`, the Tools inventory
  owns its complete base/comfort rules in `tools-inventory.css`, and shared
  modal-only refinements live in `settings-comfort.css`. The feature imports
  those layers in their authored cascade order before provider-surface
  overrides. The project-folder remote-device picker stays in the eager
  stylesheet because it can render without opening Settings.
- Update, Timer, and Optimizer retained presenters are one demand-loaded
  `utility-panels` runtime behind `frontend/src/features/utility-panels.ts`.
  Their five pre-land entries have one feature domain, and the shell prepares
  that domain from its existing idle callback so ambient badges and pollers
  start after the critical first screen. A user action before idle imports the
  same owner immediately. `prepare()` emits the standard
  `tofu:feature-domain-loaded` event after evaluation so mobile panel wrappers
  recapture the real implementations. Stable conversation lookup and push
  functions cross explicit registry services; mutable active-conversation and
  feature-flag state is read live from `runtimeScope`, never captured when the
  chunk happens to load. Settings treats the update-status painter as an
  optional port and therefore has no load-order dependency on this domain.
- Creative Image Generation's single and batch presenters are one
  demand-loaded `image-generation` runtime behind
  `frontend/src/features/image.ts`. Its static toolbar actions and native Turn
  cancel/retry intents route through the same feature bridge, while upload and
  Settings callbacks probe optional live ports so ordinary attachments and a
  Settings visit do not pull the Image chunk into memory. The composer retains
  only its four small per-conversation selection scalars and exposes them,
  pending images, navigation identity, workflow mode, and hidden-model policy
  through the stable `ImageGenerationComposerState` accessor object. Lazy code
  must not write these mutable values directly through `featureRegistry`, whose
  module override cache would otherwise hide later conversation restores. The
  image-model Offering list has no startup timer: first Image demand owns the
  coalesced owner-scoped v2 request, and routing reconciliation refreshes only
  an owner that is already resident.
- The local Knowledge Workbench is the manifest-owned, demand-loaded
  `knowledge-presenters` runtime behind `frontend/src/features/knowledge.ts`.
  Its open/close feature domain is intentionally separate from generic `misc`
  commands, so write approval, stdin, Human Guidance, and cost interactions do
  not import catalogue/upload/search presentation. First evaluation installs
  the Workbench-only Escape listener and its generated private action set; no
  catalogue request or polling timer exists before demand. The chat drop owner
  reads `_tofuKnowledgeModalOpen` live from `runtimeScope`, while the lazy
  presenter is the sole writer, preserving drop arbitration without a browser
  global or captured state.
- The Project folder workbench is the manifest-owned, demand-loaded
  `project-presenters` runtime behind `frontend/src/features/project.ts`.
  Core project state, bar rendering, restore/SSE reconciliation, and background
  rescan remain retained because they run without the modal. A conversation
  restore carries its primary recent-path intent inside the same `setPaths`
  request; an explicit workbench apply carries a primary-first prefix of at
  most 32 selected roots without truncating the authoritative project list. The
  browser owns no second recent-path write, and background/status reconciliation
  omits the optional intent. Write approvals, subprocess stdin, and apply-code confirmation
  remain in the small retained `execution-interactions.js` section, so coding
  actions neither wait for nor retain the folder browser. The generated
  presenter imports its typed browse coordinator and static icon owner only on
  demand. Mutable conversation/project authority crosses one frozen
  `ProjectPresentationShellState` live accessor object; individual values
  must not be captured through `featureRegistry`. The retained clear path
  probes the optional `ProjectModalPresentation` cleanup owner and never
  invokes the open/close feature stub, preventing an idle clear from fetching
  the workspace.
- Local Control's modal, capability status probes, download/diagnostic
  presentation, three-second open-modal poll, and browser-assisted desktop
  relay are the manifest-owned, demand-loaded `local-control-presenters`
  runtime behind `frontend/src/features/local-control.ts`. The retained
  `local-control-state.js` section owns only the merged permission badge and
  reads reachability through the optional live
  `LocalControlPresentationState`; an absent owner means unprobed, not broken.
  The independent browser/desktop wire flags cross one frozen
  `LocalControlShellState` getter port and remain conversation-owned. Normal
  boot, generic `misc` commands, badge repaint, and modal close paths do not
  import the workbench. The exact `#tofu-agent-relay` native-agent deep link is
  the sole boot-time exception: the typed feature prepares the relay owner for
  thirty minutes without opening the modal. Opening the modal starts its
  bounded status poll and closing it clears that interval; modal open alone
  never scans localhost because Chromium may require Local Network Access
  permission.
- Debug Panel and Request Inspector DOM, task/payload/trace reads, live
  TurnStore subscription, and bounded poll are the manifest-owned,
  demand-loaded `diagnostics-presenters` runtime behind
  `frontend/src/features/diagnostics-presenters.ts`. The retained
  `core/debug_state.js` authority owns only evidence that must exist before a
  panel opens: the diagnostics/error bounds, per-task snapshot bound, shared
  clipboard fallback, and tool-round-to-task identity resolver. Mutable
  conversation identity, visibility, config, and bounded caches cross one
  frozen `DebugShellState` live port. Conversation lifecycle code probes the
  optional `DebugPresentationState`; it never invokes a feature stub merely to
  clear or switch diagnostics. The loaded owner also returns before any
  `/debug-messages` read while closed, so ordinary navigation has neither a
  chunk-load cost nor a hidden diagnostics API cost. First open owns task-list
  loading, subscription, and poll; close disposes both scheduling paths.
- Compaction snapshot drawer DOM, history/summary/raw renderers, copy/download
  behavior, and the byte-bounded archive payload cache are the manifest-owned,
  demand-loaded `compaction-viewer-presenters` runtime behind
  `frontend/src/features/compaction-viewer.ts`. Bounded history policy lives in
  the DOM-free `frontend/src/core/compaction-history-state.ts` owner; the
  retained `compaction-viewer-state.js` section is only its endpoint/context
  composition adapter. A 15-second freshness window and per-conversation
  single-flight suppress repeat navigation reads, while a 32-conversation ×
  64-row LRU and 32 tracked-request ceiling bound resident state. Records keep
  exact total cardinality separately from cached rows, so resource bounding
  cannot under-report the UI counter. Explicit drawer opens bypass freshness,
  keep the complete list only for the open lifecycle, and reject late list/
  selection responses after close or replacement. The loaded owner subscribes
  to the shared language-change event and owns no boot listener, timer, or API
  request before first inspection.
- Orchestration Studio and Task Mode are one demand-loaded feature graph.
  `frontend/src/features/orchestration.ts` evaluates the three typed owner
  barrels before the manifest-owned `orchestration-presenters` retained
  runtime, then publishes only the two typed Task Mode route entries through
  `featureRegistry`. The eager runtime imports the small typed Flow Picker,
  bounded saved-Flow catalogue, and generated endpoint client directly because
  the chat toolbar must list saved definitions before Studio is requested.
  `request-contracts.generated.ts` is the sole browser request projection of
  the backend endpoint registry; `api-client.ts` installs it into the stable
  retained `Api.orchestrations` placeholder without a browser global. The
  eager graph must not import the complete orchestration registry.
  Manifest-declared registry imports are lexical ESM dependencies: their
  member list is validated, collision-checked, and used as the only source for
  typed action publication. They do not create a browser-global registry.
- Marked parser configuration lives in `frontend/src/markdown-policy.ts`.
  Composition installs line breaks and a strict-GFM delete tokenizer before
  retained rendering code runs. A tokenizer miss returns `undefined` to stop
  Marked v12 from falling through to its permissive single-tilde rule.
- Product copy uses the generated `I18nKey`/`Translator` contract. Locale JSON
  files are the only key and placeholder authority; feature-local string-key
  translator interfaces are not new extension points. The generator also
  emits compact delivery copies; Vite content-hashes them as data assets and
  the browser fetches only the selected language through a two-entry bounded,
  request-coalescing cache.
- Project Brain content translation is a lazy typed owner. It skips source
  items above 12,000 characters, coalesces by text/language through one global
  6-request scheduler with at most 256 pending translations, keeps at most 128
  translations / 512,000 characters in its in-memory LRU, and prunes its
  timestamped IndexedDB cache to 512 entries. Its display overlay never
  replaces authoritative source text.
- HTML text escaping and trusted-template branding live in the pure typed
  `frontend/src/html-safety.ts` owner. Typed and lazy feature owners import it
  statically; retained main-runtime renderers receive `escapeHtml`, `safeHtml`,
  `raw`, and the temporary `_esc` alias only at the ESM composition boundary.
  No feature discovers or reimplements escaping through the mutable registry.
- Error shape normalization lives in `frontend/src/api/errors.ts`; localized
  labels, mojibake repair, bounded fallback-cause formatting, and safe error
  card markup live in the DOM-free `frontend/src/error-presentation.ts` owner.
  The ESM prelude injects the generated translator and typed icon renderer,
  then exposes only explicit compatibility bindings to retained consumers.
- Memory-prefetch, My Context, related-conversation, learned-preference, and
  MCP-login provenance markup lives in the DOM-free
  `frontend/src/conversation/presentation/turn-provenance.ts` owner. It accepts
  the Conversation Sync block fields directly, imports the shared escaping
  policy, and receives only the generated translator plus trusted icon port.
  Retained renderers consume lexical presentation functions and own no
  provenance payload translation, inline-Markdown policy, or action argument
  serialization.
- Structured write-freshness/read-before-edit refusals and their legacy badge
  fallback converge in the DOM-free
  `frontend/src/conversation/presentation/write-gate-refusal.ts` owner. It
  validates write-tool scope, freezes normalized facts, uses generated i18n
  placeholder types, and escapes paths/copy before rendering the warning badge
  and notice. Retained write/diff cards only place those two HTML results in
  their established slots; they do not classify refusal kinds or interpolate
  user-facing copy.
- Settled tool-result markup lives in the DOM-free
  `frontend/src/conversation/presentation/tool-result-presentation.ts` owner.
  It owns compaction visibility, bounded line diffs, write/single-edit/batch
  cards, operation pills, and the generic result viewer's 120,000-character
  display ceiling. Its inputs are the projected round, first-result metadata,
  generated translator, typed write-gate presenter, and explicitly named
  trusted header slots. The retained dispatcher owns branch order only; it
  carries no diff algorithm, JSON formatting, truncation, or edit-card policy.
- Tool-catalog, web/fetch, vertical-domain, and engine-breakdown markup lives
  in the DOM-free
  `frontend/src/conversation/presentation/tool-search-presentation.ts` owner.
  It clones values before vertical deduplication and ranking, permits links only
  for HTTP(S), and receives only the generated translator, trusted icon port,
  projected round/results, and explicitly named header slots. Its observable
  scan/display budgets are 512 catalog records / 64 cards / 8 arguments, 100
  web rows, 64 vertical records / 256 sources / 512 items / 12 rows per card,
  and 32 engines / 512 URLs. Localized limit rows disclose every truncated
  family. The retained dispatcher owns branch order, not search grouping,
  diagnostics, link safety, vertical merging, mutation, or resource policy.
- Read/inspect/preview thumbnails and generated/edited image cards live in the
  DOM-free
  `frontend/src/conversation/presentation/tool-image-presentation.ts` owner.
  It receives the projected round, first-result metadata, generated translator,
  trusted icon port, and explicitly named header slots. It permits image
  sources only from HTTP(S), local blob, explicit relative/root paths, or
  allowlisted base64 image media; SVG open actions permit only HTTP(S) or
  explicit relative/root URLs. Descriptor projection scans at most 64 records
  and renders at most 16 tiles, with a localized limit row. The retained
  dispatcher owns branch order, not image-family classification, localization,
  escaping, URL safety, projection mutation, or resource policy.
- Browser JavaScript execution cards live in the DOM-free
  `frontend/src/conversation/presentation/tool-browser-execution-presentation.ts`
  owner. It parses serialized arguments only through an 80,000-code-unit
  pre-parse gate, then bounds code at 65,536 units, descriptions at 4,096, and
  results at 120,000. Every truncation is localized and visible; query, round
  identity, arguments, status, and result text are escaped. The immutable port
  receives only the generated translator and explicitly named trusted header
  slots. The retained dispatcher owns branch order, not parsing, localization,
  escaping, mutation, status, or resource policy.
- Running and settled `run_command` / `code_exec` cards live in the DOM-free
  `frontend/src/conversation/presentation/tool-command-execution-presentation.ts`
  owner. It gates serialized arguments at 80,000 code units; bounds commands,
  descriptions, live-output tails, results, and legacy status tails at 65,536,
  4,096, 20,000, 120,000, and 2,048 units; and scans at most 64 QR descriptors
  to render 16. QR sources reuse
  `frontend/src/conversation/presentation/image-source-policy.ts`, including
  complete allowlisted Base64 grammar. Every elision is localized and visible.
  The retained layer owns timer ticks, interrupt I/O, and body/output expansion
  sets; it passes only boolean snapshots and named trusted slots to the owner.
  Vite emits these explicit typed tool-policy modules as one static
  `tool-presentation` dependency chunk: it is eagerly loaded with the main
  entry, counted once by the total-JavaScript budget, and independently cached.
- Pending write-tool cards live in the DOM-free
  `frontend/src/conversation/presentation/tool-approval-presentation.ts`
  owner. It scans at most 32 generic risk fields or 16 batch edits; bounds
  identifiers, descriptions, paths, preview inputs, visible lines, and command
  text before emitting HTML; and localizes every label and visible elision.
  Approval IDs are escaped into `data-approval-id`; the restricted action is a
  static `resolveWriteApproval(this.dataset.approvalId, …)` command, so
  untrusted IDs never become executable text. The retained dispatcher passes
  only trusted icon/query slots and owns no risk, diff, escaping, localization,
  resource, or action-string policy.
- Synthetic context-injection rows live in the DOM-free
  `frontend/src/conversation/presentation/tool-injection-presentation.ts`
  owner. One immutable port handles the closed swarm → peer → operator-steer →
  stall-nudge priority order. It scans/renders at most 16 preview records,
  rejects XML payload parsing above 65,536 code units, bounds Markdown/raw text
  at 16,384 units and stall prompts at 32,768, and makes every item/content
  elision visible. Translation, sanitized Markdown, file icons, and live
  conversation-title lookup are injected capabilities. Retained code owns
  timeline chronology and the peer-jump event lifecycle only; it carries no
  XML, sender-deduplication, escaping, localization, or resource policy.
- Human Guidance cards and compact outcome rows live in the DOM-free
  `frontend/src/conversation/presentation/tool-human-guidance-presentation.ts`
  owner. Its single immutable port closes the awaiting → skipped → submitted
  state order. Settled unanswered questions are read-only unless the exact
  Turn settlement offers `answer_guidance`; that late-answer projection keeps
  the original choice UI interactive after Stop. It admits at most 16 options,
  parses legacy option JSON only below 65,536 code units, bounds identifiers at
  512, questions at 32,768, labels at 1,024, descriptions at 8,192, and each
  option-owned note at 4,096 units, and makes every elision or unavailable
  response visible. Guidance IDs, ordered option indexes, and original choice
  labels remain escaped `data-*` values consumed by static restricted actions;
  none can enter executable action text. Translation and sanitized Markdown are
  injected capabilities. Delegated response behavior lives separately in the
  lifecycle-scoped `frontend/src/conversation/ui/human-guidance-actions.ts`
  owner. It reads only card-scoped static selectors, requires
  card/group/button/note datasets and ordered indexes to agree, rejects more
  than 16 option groups, and submits only the chosen label and that option's
  draft. It caps free-text replies at 32,768 code units before translation or
  transport and permits one in-flight submission per conversation/guidance
  identity. Choice rollback is exact; translation failure falls back to the
  bounded original; backend 404 triggers an expired repaint while transport
  failure keeps its distinct error. Composition owns the presentation store
  through an immutable lexical port. Retained code keeps only EN-to-CN arrival
  translation and two forwarding action entries; it publishes no Human
  Guidance state through `runtimeScope`.
- The chat-picker capability taxonomy lives in the pure, stateful
  `frontend/src/core/model-capability-taxonomy.ts` controller. It starts from
  the backend-parity fallback, validates and atomically replaces that set from
  server configuration, and returns defensive snapshots. It also projects the
  server's ordered `known_capabilities` list via `getKnownCapabilities()`; the
  settings capability-toggle grids (model edit form, key×model matrix editor)
  render that projection through `_allModelCapabilities()` instead of carrying
  their own list — the single bare-harness fallback literal lives in
  `sections/settings.js`, pinned to the backend by the parity test. Retained
  pickers may fail open when an isolated harness omits the port, but may not
  carry another exclusion list or mutable taxonomy.
- Vendor grouping for model pickers lives in the pure
  `frontend/src/core/model-group.ts` policy. The ESM composition boundary
  injects the application brand detector and publishes the immutable policy
  only through the private runtime scope. Toolbar and Settings consumers use
  it directly: wire protocols and OAuth/adapter credential kinds never become
  alternate grouping policies or browser globals.
- Brand detection and brand badge rendering live separately in
  `frontend/src/core/model-brand-detection.ts` and
  `frontend/src/core/model-brand-icons.ts`; model/provider text and natural,
  numeric-aware ordering live in `frontend/src/core/model-display-names.ts`.
  Catalog lookups are injected and remain live, while retained `_detectBrand`,
  `_brandSvg`, and model-sort names are module-private prelude aliases only.
  Conversation role image snippets are a fourth independent asset owner in
  `frontend/src/core/role-avatar-icons.ts`, composed lazily by the retained
  turn adapter after the application base path exists.
- Backend-authored alias/family metadata is projected by the pure
  `frontend/src/core/model-display-fold.ts` owner. It preserves entry identity
  and nested aliases without reading the DOM or storage. The toolbar's recent
  strip is a separate `frontend/src/core/recent-models.ts` controller with an
  injected storage resolver, validation on read, fail-open private-mode/quota
  behavior, and a hard five-ID persistence bound.
- The runtime action generator reads only literal `data-tofu-action*` values,
  including explicit `setAttribute` and `dataset.tofuAction` writes, across the
  retained authoring sections, HTML fragments, and authored TypeScript owners.
  The eager runtime and each manifest-owned lazy runtime intersect those shared
  references with their own top-level function declarations. A lazy runtime
  additionally intersects them with members of its explicitly declared typed
  registry imports; the composer then publishes each receiver automatically.
  Calls used to
  render an action's arguments or label never become browser-global action
  receivers merely because they occur in the same authored tag. This dual
  scan prevents a control moved behind an ESM import from losing its retained
  receiver at generation time.
- Large application-shell structures live in named frontend fragments rather
  than expanding `index.html`. The backend only replaces explicit markers and
  fails closed on marker/file drift; action and i18n checks scan the same
  fragment directory.

No new runtime section is added. A touched retained section should shrink or
move behavior into a typed owner.

`make architecture-check` measures the retained section count and bytes,
mutable message-document writes, `ConvView` application paths, positional DOM
identities, special streaming nodes, ambient stream state, and legacy content
fingerprints. Every limit is a one-way ratchet with target zero; increasing a
limit is an architecture change, not ordinary feature work.

## Browser capability policy

Browser capability resolution and invocation are both fail-soft boundaries.
Reconstructible browser storage degrades to memory or network and never gates
authoritative data. GPU, memory, core-count, pointer, and online-state hints are
advisory only; correctness cannot depend on them. Optional capability owners
bound queues, caches, timers, observers, and cancellation before considering
device-specific tuning.

Typed owners reuse `frontend/src/core/browser-storage.ts` and
`frontend/src/conversation/ui/animation-frame-scheduler.ts` rather than reading
fragile globals at each call site. Clipboard, object-URL, media, worker, and observer access remains
feature-detected with a user-visible or no-capability fallback. Persistent
animation includes a scoped reduced-motion rule. Consumer-boundary tests cover
denied property resolution as well as method failure; helper-only tests are not
sufficient.

## State owners

| State | Owner |
|---|---|
| HTTP request, timeout, abort, typed API error | `frontend/src/api/transport.ts` |
| Fetch-like response envelope and result-error recovery | `frontend/src/core/http-result.ts` |
| Bounded asynchronous fan-out and per-item failure isolation | `frontend/src/core/async-pool.ts` |
| Page/network live-attempt wake lifecycle | `frontend/src/conversation/application/conversation-wake-recovery.ts` |
| Stateless availability clock/log/health port shapes | `frontend/src/availability-monitor-ports.ts` |
| Overlapping health-request single flight and repeatable lazy body | `frontend/src/availability-health-probe.ts` |
| Backend process/network liveness verdict and offline presentation | `frontend/src/backend-availability-monitor.ts` |
| Sidecar readiness warning and bounded recovery poll | `frontend/src/storage-availability-monitor.ts` |
| Push RTT, half-open timeout, close/offline verdict | `frontend/src/runtime/sections/push.js` |
| Event-only Push/SSE network-badge projection | `frontend/src/runtime/sections/net-latency.js` |
| Timer-free served-build mismatch gating and reload guard | `frontend/src/core/build-watch-controller.ts` |
| Collaboration summary request ordering and bounded peer mirror | `frontend/src/features/presence-summary-controller.ts` |
| Demand-scoped browser-console flush clock and transport-profile delay | `frontend/src/core/client-log-flush-scheduler.ts` |
| Eager bounded client errors, diagnostics snapshots, and clipboard fallback | `frontend/src/core/debug-runtime-owner.ts`; retained dependency port in `runtime/sections/core/debug_state.js` |
| Lazy dialog services and single active DOM/Promise/focus/timer lifecycle | `frontend/src/lazy-dialog-controller.ts` + `frontend/src/dialog-controller.ts` |
| Swarm push subscription and transient-Turn presentation | `frontend/src/conversation/application/swarm-presentation-overlay.ts` |
| Demand-scoped Swarm reconciliation clock and single-flight recovery | `frontend/src/conversation/application/swarm-reconciliation-scheduler.ts` |
| Shared demand-scoped command/Timer Watcher presentation clock | `frontend/src/conversation/application/demand-scoped-presentation-ticker.ts` |
| Metadata-only conversation/folder startup and active-view convergence | `frontend/src/conversation/application/conversation-startup.ts` |
| Explicit authoritative conversation refresh | `frontend/src/conversation/application/conversation-refresh.ts` |
| Conversation-keyed send-preparation transient Turn | `frontend/src/conversation/application/send-preparation-overlay.ts` |
| Cookie-capture completion subscription and toast | `frontend/src/core/cookie-capture-consent.ts` |
| My Context preference resolution and undo UI state | `frontend/src/features/memory/preference-actions.ts` |
| Shared fullscreen-image and generated-image download lifecycle | `frontend/src/image-viewer-actions.ts` |
| Fetch-response byte decoding and newline framing | `frontend/src/core/sse-reader.ts` |
| Bounded per-Turn translation start claims | `frontend/src/core/translation-claim-registry.ts` |
| Translation content/display projection | `frontend/src/conversation/presentation/conversation-view-model.ts` |
| Page-lifetime translation task polling (retained migration debt) | `frontend/src/runtime/sections/translation.js` |
| Pure conversation catalog presentation/settings queries | `frontend/src/conversation/application/conversation-catalog-queries.ts` |
| Local catalog ordering, activity timestamp, invalidation and bounded sidebar scheduling | `frontend/src/conversation/application/conversation-catalog-reconciliation.ts` |
| Revision-gated full conversation-catalog refresh debounce | `frontend/src/conversation/application/conversation-catalog-revision-gate.ts` |
| Bounded lazy ZIP package upload shared by Memory and Skills | `frontend/src/features/skills/package-installer.ts` |
| Lazy My Day TODO/stream mutation and rollback policy | `frontend/src/features/myday/task-actions.ts` |
| Lazy My Day suggestion-to-composer intent ordering | `frontend/src/features/myday/quick-action-launcher.ts` |
| Owner-scoped, byte/entry-bounded My Day read cache and digest projection | `frontend/src/features/myday/report-cache.ts` |
| Disposable My Day digest/reminder lifecycle | `frontend/src/features/myday/background-controller.ts` |
| Demand-loaded My Day calendar/report/polling presentation and static assets | manifest bundle `myday-presenters` + `frontend/src/features/myday.ts` + `presentation-assets.ts` |
| Always-together Paper/Research lazy owner composition | `frontend/src/features/paper/panel-owners.ts` |
| Demand-loaded Paper report/reader retained presentation | manifest bundle `paper-reader-presenters` + `frontend/src/features/paper.ts` |
| Demand-loaded Paper podcast/video retained presentation | manifest bundle `paper-media-presenters` + `frontend/src/features/paper.ts` |
| Demand-loaded Settings retained presentation and live working-state ports | manifest bundle `settings-presenters` + `frontend/src/features/settings.ts` |
| Lazy Settings feature presentation styles | `frontend/src/features/settings/{devices,tools-inventory}.css` imported by `frontend/src/features/settings.ts` |
| Demand-loaded Image Generation presentation and live composer state | manifest bundle `image-generation` + `frontend/src/features/image.ts` + `ImageGenerationComposerState` |
| Demand-loaded local Knowledge Workbench presentation and modal/drop state | manifest bundle `knowledge-presenters` + `frontend/src/features/knowledge.ts` |
| Demand-loaded Project workspace with retained state/coding interactions | manifest bundle `project-presenters` + `frontend/src/features/project.ts` + `ProjectPresentationShellState` |
| Demand-loaded Local Control with retained merged badge | manifest bundle `local-control-presenters` + `frontend/src/features/local-control.ts` + `LocalControlShellState` |
| Demand-loaded Debug/Request Inspector with retained bounded evidence | manifest bundle `diagnostics-presenters` + `frontend/src/features/diagnostics-presenters.ts` + `DebugShellState` |
| Demand-loaded Compaction Viewer with typed bounded history projection | manifest bundle `compaction-viewer-presenters` + `frontend/src/features/compaction-viewer.ts` + `core/compaction-history-state.ts` |
| Generated Orchestration request projection and stable endpoint facade | `frontend/src/features/orchestration/request-contracts.generated.ts` + `api-client.ts` |
| Single-flight, freshness-bounded saved-Flow catalogue | `frontend/src/features/orchestration/flow-catalog.ts` |
| Demand-loaded Orchestration Studio/Task Mode graph and typed action registry | manifest bundle `orchestration-presenters` + `frontend/src/features/orchestration.ts` |
| Owner-scoped, byte/entry-bounded conversation catalog metadata cache | `frontend/src/core/conversation-metadata-cache.ts` |
| Lazy conversation metadata-cache loading and teardown race | `frontend/src/core/conversation-metadata-cache-lazy.ts` |
| Marked parser options and strict-GFM deletion policy | `frontend/src/markdown-policy.ts` |
| Idempotency-key creation | `frontend/src/api/transport.ts` |
| Conversation cursor, SSE and recovery | `frontend/src/core/conversation-sync.ts` |
| Authenticated browser-owner resolution | `frontend/src/core/current-user.ts` |
| Push-frame owner comparison | `frontend/src/core/frame-identity.ts` |
| Normalized Turn/attempt/phase reduction | `frontend/src/conversation/domain/turn-store.ts` |
| Ordered Turn reads | `frontend/src/conversation/application/conversation-read-model.ts` |
| Turn/block view model | `frontend/src/conversation/presentation/conversation-view-model.ts` |
| Tool execution/attempt grouping, attention and panel projection | `frontend/src/conversation/presentation/tool-execution-groups.ts` |
| Tool execution reader-owned disclosure interaction | `frontend/src/conversation/ui/tool-execution-disclosure.ts` |
| Tool family, label/color, icon-key and program-row presentation | `frontend/src/conversation/presentation/tool-round-presentation.ts` |
| Tool-round trusted SVG asset resolution | `frontend/src/conversation/presentation/tool-round-icons.ts` |
| Synthetic context-injection row presentation | `frontend/src/conversation/presentation/tool-injection-presentation.ts` |
| Human Guidance card and outcome-row presentation | `frontend/src/conversation/presentation/tool-human-guidance-presentation.ts` |
| Human Guidance delegated response DOM, single-flight and rollback lifecycle | `frontend/src/conversation/ui/human-guidance-actions.ts` |
| Turn provenance HTML, inline Markdown and safe action arguments | `frontend/src/conversation/presentation/turn-provenance.ts` |
| Write-gate refusal normalization, localized badge and safe notice | `frontend/src/conversation/presentation/write-gate-refusal.ts` |
| Compaction, write/edit diff cards and bounded generic tool results | `frontend/src/conversation/presentation/tool-result-presentation.ts` |
| Tool-catalog, web/fetch, vertical and engine search presentation | `frontend/src/conversation/presentation/tool-search-presentation.ts` |
| Read/inspect/preview and generate/edit image presentation | `frontend/src/conversation/presentation/tool-image-presentation.ts` |
| Browser JavaScript execution presentation | `frontend/src/conversation/presentation/tool-browser-execution-presentation.ts` |
| Run-command/code-exec presentation | `frontend/src/conversation/presentation/tool-command-execution-presentation.ts` |
| Shared image and external-asset source allowlist | `frontend/src/conversation/presentation/image-source-policy.ts` |
| Shared trusted SVG icons | `frontend/src/icons.ts` |
| HTML text escaping and trusted-template branding | `frontend/src/html-safety.ts` |
| Error-envelope shape normalization | `frontend/src/api/errors.ts` |
| Localized error cards, labels, fallback causes and mojibake repair | `frontend/src/error-presentation.ts` |
| Server-projected chat-model capability taxonomy and ordered capability-toggle list | `frontend/src/core/model-capability-taxonomy.ts` |
| Model vendor grouping and group labels | `frontend/src/core/model-group.ts` |
| Model brand detection | `frontend/src/core/model-brand-detection.ts` |
| Model brand SVG/color presentation (mono currentColor only — no url(#…) paint-server refs; every template/vendor key must resolve) | `frontend/src/core/model-brand-icons.ts` |
| Model/provider display names and natural ordering | `frontend/src/core/model-display-names.ts` |
| Conversation role-avatar snippets | `frontend/src/core/role-avatar-icons.ts` |
| Alias/family model display projection | `frontend/src/core/model-display-fold.ts` |
| Bounded recent-model persistence | `frontend/src/core/recent-models.ts` |
| Settings ProviderAccess summary and advanced Connection/Credential/Offering/Deployment editor | `runtime/sections/settings/provider_render.js::_renderModelRoutingProvidersTab` |
| Owner-scoped model-routing v2 Settings load/CAS/secret replacement | `runtime/sections/settings/core_panel.js::_loadModelRoutingAuthority` + `runtime/sections/settings/save_export.js::_saveServerConfig` |
| Provider-first chat picker, automatic Provider choice, and provider-scoped pending identity | `runtime/sections/main/main_toolbar_ui.js::_modelRoutingDropdownModels` |
| Model visibility/default projection from enabled v2 Offerings | `runtime/sections/settings/core_panel.js::_getAllModels` + `runtime/sections/settings/visibility_defaults.js` |
| Speech ProviderAccess bundle creation/update/deletion and encrypted credential handoff | `frontend/src/features/settings/speech.ts` + `Api.modelRouting` |
| RouteSnapshot serving-route and failover presentation | `frontend/src/conversation/presentation/turn-serving-route.ts` + `runtime/sections/ui/finish_info.js` |
| Authoritative Turn projection into catalog-shell metadata | `frontend/src/core/turn-projection.ts` |
| Turn commands | `frontend/src/core/turn-command.ts` |
| Turn runtime orchestration | `frontend/src/core/turn-runtime.ts` |
| Conversation DOM reconciliation | `frontend/src/conversation/ui/conversation-surface.ts` |
| Rich tool HTML and live Swarm subtree reconciliation | `frontend/src/conversation/ui/classic-conversation-renderers.ts` |
| Presentation-only state and scheduling | `frontend/src/conversation/application/conversation-surface-controller.ts` |
| Scroll anchor, follow suspension and DOM window | `ConversationSurface` viewport port |
| Conversation diagnostics projection | `frontend/src/features/diagnostics.ts` over `ConversationTurnRead` + active Surface |
| Catalog shells and settings persistence | `runtime/sections/core/conversation_catalog.js`; composer capture adapter in `runtime/sections/main.js` |
| Per-conversation connection badge health | `frontend/src/core/connection-health.ts` |
| Send startup cancellation | `frontend/src/core/send-startup.ts` |
| Send translating/connecting overlay | `frontend/src/conversation/application/send-preparation-overlay.ts` on the transient overlay |
| Composer submission echo | `frontend/src/conversation/application/optimistic-user-turn.ts` on the transient overlay |
| Resource cleanup | `frontend/src/lifecycle.ts` |
| Turn settlement identity/presentation | `frontend/src/conversation/presentation/turn-finish.ts` |

The adapter in `runtime/sections/main/conversation_turn_store.js` injects
cache, toolbar, settings, and DOM-host callbacks into the typed owners. It
holds no transcript, attempt, phase, transport, or renderer state.

## Backend and browser boundary

| Concern | Backend | Browser |
|---|---|---|
| Durable content and order | Owns Turns, lanes, segments, block IDs, revisions, settlement, attempts and commands | Reduces the generated contract without inventing content |
| Live generation | Owns attempt lifecycle and typed phase events | Keeps `livePhase` only inside TurnState and derives UI from it |
| Action authority | Validates ownership, CAS, idempotency and legal transitions | Emits intents carrying conversation/turn/block identity |
| Rendering | Sends structured data, never HTML | Selector builds typed blocks; `ConversationSurface` alone writes chat DOM |
| Interaction state | Never persists focus, expansion, optimistic labels or scroll | Lifecycle-scoped maps keyed by durable identity; disposed with the conversation |
| Offline cache | Remains the storage authority | The typed, owner-scoped bounded IndexedDB owner stores catalog/settings metadata only, never a transcript copy |

Feature adapters such as artifacts, Human Guidance, cost detail and timer
recovery may keep bounded presentation caches. They attach to stable Turn or
feature IDs, never mutate a Turn projection, and declare a disposal or size
bound. A new feature cannot add a conversation-level mirror merely because a
legacy endpoint happens to return message-shaped data.

## Conversation hydration

One v3 snapshot contains settings, revision, a Turn window, its attempts,
cursor, heartbeat policy, and an opt-in metadata-free artifact existence bit.
The generated browser requests the newest 96 main-lane Turns for a linear
conversation; the default public representation
remains complete, and branch-bearing conversations safely receive that full
representation until lane-directory paging exists. `turn-runtime.ts` applies
settings at the snapshot boundary, hands snapshot metadata to feature adapters,
then dispatches TurnState. A
`hasArtifacts:false` hint commits the empty artifact presentation immediately
and skips the legacy list request; `true` or a missing field keeps the list
request for rolling compatibility. A weak conversation-shell generation fence
prevents an older list response from overwriting a newer negative hint. The
pure selector reads Turn state directly and the controller commits it to the
keyed Surface. `turn-projection.ts` updates only
catalog/lifecycle metadata; it cannot materialize a parallel transcript.
Initial catalog boot is metadata-only: inactive catalog shells issue zero Turn
snapshot requests, so startup network and memory concurrency do not grow with
sidebar size. Opening the selected conversation owns its one on-demand
snapshot; it does not issue a second settings request and does not fall back to
an archived message body or IndexedDB transcript.

Settled Turns normally carry their authoritative cost stamp in that snapshot.
Legacy projections without one retain server-side pricing authority: all
synchronous Surface misses join one bounded 512-entry microtask batch, share
in-flight fingerprints, and request one authoritative repaint only after the
aligned result lands. The browser never emits one pricing request per Turn,
caches a partial response as no-charge, or performs pricing arithmetic.

`TurnState.historyByLane` owns the server total, `hasMore`, and exclusive older
ordinal for each loaded lane. `ConversationSyncCoordinator.loadTurnPage()`
requires the currently applied `syncSeq`, shares only an identical in-flight
request, restores reference dictionaries, and synchronously publishes only if
both server and local sequence still match. History pages are additive Store
snapshots, never deletion authority. The retained adapter merely forwards the
Surface's typed `load-earlier-turns` intent to this runtime method.

Push and BroadcastChannel notifications only invalidate the coordinator. For
a numeric `conv_changed` wake hint, the bounded catalog revision gate may skip
the redundant metadata-list request only after TurnState or the catalog shell
has reached the same revision. Revisionless metadata changes and every
uncertain/stale case still revalidate the authoritative catalog. The ordered
conversation stream or an authoritative reset snapshot is the only Turn
projection writer.

Diagnostics are a read-only projection of `ConversationTurnRead`, connection
metadata, and the active Surface dataset. Collecting diagnostics never issues a
conversation request, parses a message array, or creates a recovery state path.
Its collector is retained and bounded, while Debug/Request Inspector
presentation is demand-loaded; a closed owner performs no task, trace, payload,
or reconstructed-message read. Request Inspector keeps its pre-request
message/schema snapshot for exact
historical reconstruction and joins the matching bounded
`tool_wire_projection` event for provider-bound truth. The round list is
requests only (state mirrors are served per round through the payload
endpoint, never listed), and each round row names the tools that round
INVOKED — folded server-side from the next snapshot's new-message tail,
the same glanceability contract as the chat timeline's turn blocks. The
round detail exposes the wire projection: ordered final names,
schema-token estimate, an opaque exact-schema fingerprint, discovery backend,
explicit budget, and budget omissions; it never presents the larger assembly
snapshot as what the model received.
Its storage-only snapshot delta v2 shares one chronological `(task, turn)`
baseline across request/state kinds; the server rebuilds full kind-specific
payloads, while unversioned v1 histories retain their original interpretation.
The hot projector canonicalizes each current message once in a fused prefix
scan, retaining only full SHA-256 digests between rounds; the exact canonical
prefix hash in each row remains the rebuild-time integrity authority.
Provider raw archives ride the same selected diagnostic Turn as lazy blocks.
The drawer retains only one 256 KiB request/response window per visible block;
moving to the next chunk replaces that window instead of growing a second raw
payload cache. Archive metadata always names partial/quota/scrubbed evidence.

The level-1 task list is server-authoritative (`Api.tasks.byConv`,
cursor-paginated with `before`) and has two refresh drives: a TurnStore
subscription (status dispatches trigger a throttled silent refresh) plus a
live/idle poll (3s while any row is live, 15s otherwise) as the
cross-process backstop. Silent polls MERGE the newest page into the
accumulated list, so user-expanded history never collapses on a tick. Rows
group by the reply they produced: a resolved group carries a turn chip +
question preview header, its retries sort chronologically with `run N`
badges, and swarm children nest under their parent. Tasks whose turn
cannot be resolved fall into one trailing unresolved bucket that stays
newest-first and carries no run badges. A finished task whose event log
expired (structural retention: 30 days, docs/STORAGE.md) says so
explicitly — an empty round list is never presented as "no work happened",
and a storage read failure shows a retry affordance, never the empty
state.
## Conversation settings plane

Conversation settings are metadata, not transcript content. The active
composer captures its visible values into the active metadata shell and hands
that shell to one debounced persistence seam. Persistence is independent of
whether the conversation has any Turns:

- `_localOnly` shells update the typed owner-scoped metadata cache only. The
  first accepted Turn creates the durable conversation at backend authority.
- Server-owned shells pass raw snapshot + toolbar inputs to
  `frontend/src/conversation/application/conversation-settings-resolution.ts`.
  That DOM-free owner unwraps resolver envelopes, shares one config/settings
  resolve on send, and normally commits through
  `PATCH /api/v1/conversations/:id/settings/resolve`; it never copies backend
  merge rules. The plain settings PATCH remains only the rolling-upgrade
  fallback, and the retired chat tool-state route is not a browser write path.
- The owner holds one page-lifetime capability bit. A fused-write 404 probes
  only once and switches to the legacy resolve + PATCH pair; non-404 failures
  never fan out into a second mutation. An old config resolver that omits the
  requested nested settings similarly triggers exactly one compatibility read
  for that send.
- Config/settings resolution is a read-only POST and keeps the captured input
  snapshot across a bounded managed-worker recovery window: at most 20 attempts
  and 75 seconds, with a 10-second per-attempt timeout. Only network/timeout or
  gateway 500/502/503/504 failures without a backend request ID are retried;
  backend-attributed failures remain immediate. No mutation or task-start POST
  shares this recovery boundary.
- All retries for one logical PATCH reuse the same transport-owned idempotency
  key. A retry loop never mints a key per attempt.
- Lazy TypeScript features receive `captureActiveConversationSettings` through
  the private feature service registry. They do not copy composer state or
  call a settings endpoint directly.

Settings restore is paint-only. It normalizes mutually exclusive interaction
modes at the boundary but does not persist merely because a conversation was
opened. Turn hydration, catalog merge, and settings persistence therefore have
separate lifecycles and cannot gate or overwrite one another.

## Rendering and identity

- `turnId` is the only identity of a rendered Turn; `blockId` is the only
  identity below it. Array indexes and special streaming nodes are forbidden.
- Running and terminal revisions reconcile into the same keyed DOM nodes.
- Scroll capture/restore and follow suspension belong to the lifecycle-scoped
  `ConversationSurface` viewport port. The retained adapter injects the
  viewport element only; conversation shells contain no renderer lifecycle
  flags. Catalog metadata may carry the explicit `_turnSnapshotRequired`
  invalidation marker only.
- Long linear histories start as a bounded 96-Turn Store tail while the Surface
  keeps an independent 80-Turn DOM window. Earlier/later controls first shift
  locally by stable Turn identity; exhausting the prefix requests one bounded,
  64-Turn sequence-CAS history page and additively merges it through the same reducer.
  The Surface anchors and reveals prepended Turns without creating a second
  render path. Durable authority remains in storage; no transcript copy enters
  IndexedDB.
- Catalog fingerprints are invalidation metadata only. They contain no content
  document and cannot suppress a Surface commit for the active conversation.
- Backend-authored live phase stays in TurnState. There is no phase Map,
  `activeTaskId` pin, ambient stream registry, or conversation-shell liveness
  field in browser production source.
- Translation activity, branch expansion, Human Guidance feedback and artifact
  bindings are presentation state. They never become projection properties.
- Failed turns surface the complete typed settlement error. A successful retry
  clears the earlier attempt error.

The renderer contract is [RENDER_CONTRACT.md](RENDER_CONTRACT.md); settlement
semantics are in [TURN_SETTLEMENT.md](TURN_SETTLEMENT.md).

## Plan decision UX

`projection.proposedPlan` renders as a typed transcript card. When that turn
is the completed tail of the composer-target lane (main, or the currently
expanded branch), the pure conversation selector exposes one `planDecision`
keyed by turn ID, plan ID, and projection revision. ConversationSurface mounts
the `plan-decision-bar.ts` renderer directly after that source turn's plan
blocks, with three actions: continue discussing, execute with current context,
or execute with fresh task context. The decision therefore scrolls and windows
with the plan that authorizes it; it is not a composer-level floating state.

Automatic translation remains presentation-local while it is running. Partial
translation frames repaint the same proposed-plan block with a live caret;
only the completed Turn projection is durable, and neither partial nor
translated prose can mint or replace executable plan authority.

The conversation lifecycle activates that owner synchronously on every open
or new-chat transition. Its rendered model and in-flight command both retain
the originating conversation ID; inactive commits are ignored, and settlement
from an older conversation cannot repaint or unlock the newly active one.

The composer is never replaced or locked by a ready plan. The decision bar
disables only its own actions while the execute command is in flight. A fresh
task context is presentation-independent and non-destructive: the conversation
remains visible, while the backend controls the model-history boundary.

The Agent mode control is one per-conversation radio state, not independent
toggles:

| Surface choice | Existing wire authority | Turn behavior |
|---|---|---|
| Standard | `planMode=false`, `autopilot=false`, no `activeFlow` | one assistant turn per accepted message |
| Plan Mode | `planMode=true` | read-only exploration and a proposed plan |
| Autopilot | `autopilot=true` / persisted `autopilotEnabled=true` | virtual-user continuation loop |
| Saved workflow (Debug only) | persisted `activeFlow=<definition id>` | Studio-authored FlowExecutor graph |

Selecting any row atomically clears the other loop owner, a selected
orchestration workflow, and direct image-creation mode. Plan also enables Human
Guidance; explicitly disabling Human Guidance exits Plan. Chat/Studio remains
an orthogonal capability dial.

The Agent selector is also the Debug-only saved-workflow selector. The
Autopilot graph remains a Studio template but does not appear again as a
workflow row. The selector is disabled while a turn/start/stop command is in
flight, because the accepted task owns an immutable config snapshot. Alternate
exits through Plan-required Human
Guidance or direct image mode use the same guard, so they cannot paint a mixed
state behind the disabled selector. The composer itself stays usable, so
queued messages retain that same visible mode. Between turns, mode changes
apply to the next accepted message and persist only on that conversation.
Restore is paint-only; stale conflicts normalize as Plan → Workflow →
Autopilot. Continuing from the plan decision bar explicitly reselects Plan;
accepted execution switches to Standard only after the server accepts the
exact plan command.

## User experience failure rules

- Never show an optimistic success after an ambiguous command result.
- A command with an idempotency contract may recover from a lost ACK using the
  same validated payload and command ID.
- Cancellation stays visible until the authoritative terminal projection.
- A composer submission clears the captured draft and paints an optimistic
  user echo on the transient overlay immediately; the acknowledgement swaps in
  the authoritative human turn, and an uncommitted or aborted command removes
  the echo and restores the exact draft. The durable TurnStore is never
  written before acknowledgement.
- Snapshot or stream corruption fails closed into one reset; it never applies a
  plausible partial projection.
- Loading failure preserves safe cached paint, marks it stale, and shows an
  actionable error. It never switches to a second state owner.
- Background task transport may report `task-sse` health, but it cannot
  overwrite an existing `conversation-sse` health state.

## Build and checks

```bash
npm run generate:runtime
npm run generate:styles
npm run generate:conversation-sync
npm run generate:i18n
npm run check:frontend
python3 scripts/frontend_budget.py
```

`check:frontend` verifies generated runtime, styles, conversation and i18n
contracts, all literal i18n calls/attributes, actions, TypeScript, and the
production build. The Vite manifest carries the locale-source digest; the
deployment controller and packaging gates refuse a graph whose language chunks
predate the locale source. The ASGI lifespan validates only the complete
published graph, which is also the atomic request-serving commit: authoring
source edits cannot withdraw the last validated graph during process recovery,
and requests adopt new bytes only after a complete manifest publish.
Composition tests prove the manifests recreate the checked-in delivery bytes;
size budgets apply to authoring sections as well as shipped resources.

## Extension checklist

1. Locate the state owner above.
2. Change the contract first when the wire changes.
3. Implement behavior in a typed module with injected effects.
4. Add a behavioral owner test and one boundary test where data crosses into
   the retained renderer.
5. Regenerate artifacts and run the focused test plus `npm run check:frontend`.
6. Remove the superseded runtime branch and its source-text regression tests.
