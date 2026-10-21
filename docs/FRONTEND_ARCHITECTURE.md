# Frontend architecture
Responsibility: source ownership, generated delivery boundaries, state
lifecycles, and the rules for extending the browser application. Boot status uses a non-modal dock with expandable, selectable phase diagnostics. `boot-splash.ts` preserves unchanged text nodes and phase rows during polling; rebuilding unchanged content was rejected because the 800 ms poll erased native selection. `boot-readiness.ts` combines the existing boot and restart owners at composer admission (button and Enter), without blocking drafts or reading. The restart observer retains identity/approval ownership; its dock offers explicit reload after readiness to preserve drafts.

Task-time reads follow visible demand and explicit invalidation: forced paints amplified unchanged reads; paint force now preserves settled reads while server wake hints refresh them. A throttle was rejected because it still amplifies requests and delays updates. The machine-readable ownership and shrinking-debt authority is
`contracts/frontend_conversation_architecture_v1.json`. The one-hop map for
conversation browser work is `frontend/src/conversation/README.md`. Update status cards use component-scoped `--space-4` sibling spacing in `02-update-shell.css`: the local-code restart notice and release verdict remain separate without global badge margins; `tests/visual/test_visual_update_spacing.py` verifies all themes and narrow layouts.
## Background agent dock

**Problem:** composer popovers obscured agent execution evidence.
**Decision:** `agent-sidebar-dock.ts` renders conversation-level background activity inside
the current main human Turn's existing information column, below model/tools/workspace.
A stable context-content slot prevents fact repaint from destroying agent disclosures.
Narrow panes use that Turn's existing context fold region, not a floating panel. Names,
localized status, execution rounds and counts remain readable after foreground settlement;
only execution details collapse. The latest authoritative main input selects the host;
unmounted or foreign Turns are never replaced by an older history guess.
Keyed native disclosures reveal canonical bounded swarm execution cards through
`agent-sidebar-dock-projection.ts`; omission/truncation remains explicit. Activity enriches
an exactly run-bound or uniquely unsettled session owner without mutating Turns or
reopening evidenced terminal states. Ambiguous reused identities remain separate.

The detail renderer receives the same merged roster and exact-run messages; updating only headers would leave open details stale.
Open rows remain mounted on completion until the reader closes them.
Communication lives in each exact-run agent card, collapsed independently from tool
execution; all-run communication is a collapsed footer rather than a second chat feed.
Participant filters and receipt IDs/timestamps are opt-in; consumed input never implies task acceptance or completion.
Native message disclosures show a collapsed preview or the expanded body once, with a Collapse summary; receipt diagnostics fold independently.
 Reused agent IDs cannot mix run messages.
Finished history folds success and cancellation without equating their verdicts;
errors, interruptions, stalls and unknown states remain attention rows. Snapshot
disappearance never proves success. Explicit child IDs enable conversation navigation.
The existing activity controller is the sole poll owner: no additional reads,
clocks, globals or durable state. Lazy serial rendering fences conversation changes
and disposal; rich-card reconciliation preserves reader folds/focus. History mounts
on demand in twenty-row increments. Background commands remain in the composer.
**Rejected:** a separate dock column, avatar cards and added chat margins fragment the
existing turn information rail. Automatically opening tool details taxes history rendering.
Another poller, fabricated full transcripts and per-message activity chrome duplicate owners.
Surface commits and the existing rail follower remount from the same snapshot without reads.
Specs: `agent-sidebar-dock.test.ts`, `agent-sidebar-dock-projection.test.ts` and
`tests/visual/test_visual_agent_sidebar_dock.py`.

## Folder configuration and workspace defaults

Problem: the create editor used generic chrome and centered full-path buttons,
producing an oversized single-column path wall unlike Project CO-PILOT.
Decision: use the native kit's `appearance: 'project'` shell; desktop identity
sits above workspace/recent panels, narrow layouts stack them. Recent cards
show the basename and one truncated secondary path, retaining the exact path
in their accessible name and tooltip. Selected roots retain full selectable
paths and ordering/read-only controls. Field content scrolls independently
from the save/cancel row. Empty Add errors clear on editing or valid name-only
save. Disclosure still gates recent reads; no startup request is added.
Rejected: copying legacy modal DOM would lose native focus/lifecycle ownership;
truncating stored paths would lose directory identity.

Browser specification: `tests/visual/test_visual_folder_settings.py` covers
three themes, Chinese/English, long paths, 390px layout, native focus/Escape,
name-only creation and directory selection using the existing local HTTP fixture.
API/store journeys remain in `tests/e2e/test_folder_settings.py`.


**Problem:** repeated outlined project cards overwhelmed navigation, and only
projects reserved a settings column, misaligning their counts with Inbox.
**Decision:** `31-folder-settings.css` owns borderless project navigation across
all themes. Selected rows use a subtle fill and existing location indicator;
counts are muted tabular text, not unread badges. Inbox and projects share a
fixed trailing slot: hover/keyboard focus replaces the count with settings
without layout movement. Coarse pointers retain separate aligned count/menu
slots; compact rails retain icon-only navigation and context access. Zero
counts remain omitted by the existing renderer. **Rejected:** badge pills imply
unread urgency; permanently separate desktop menu columns waste name space;
hiding touch menus excludes discoverable configuration access.
Spec: `tests/visual/test_visual_project_rail_list.py`.

Rail colors consume theme surfaces and primary/secondary text tokens, never the
legacy beige gradient or brown/amber text overrides. Selection uses weight,
subtle fill and an accent location mark rather than accent-colored small text.
Visible navigation names and counts must reach 4.5:1 against their composited
browser background in idle, selected, hover and focus states across all themes;
secondary information is quieter by size/weight, not illegible contrast.

Problem: replacing the create tile with a design-system control dropped the delegated `.folder-tab` identity, so the visible plus did nothing. Compact configuration buttons also consumed a second row per folder and the create tile lost the common footprint. Decision: keep creation in the existing delegated selection contract; inbox, project and create tiles share one footprint. Configuration stays in the same project row, with double-click, context-menu/long-press and keyboard access to the existing settings dialog. Compact mode uses double-click and keyboard/context access rather than an overlapping settings button; streaming changes opacity only so tile dimensions stay fixed. Selection, folder membership, zero-folder creation and drag/drop keep their existing owners. Rejected: removing compact mode hides the layout defect; using double-click alone excludes touch and keyboard users; another dialog or event registry duplicates authority. Regression specs cover nested create targets, settings access and browser geometry in expanded/compact/drawer layouts. Problem: the rename dialog hid roots, discarded unadded path text and closed on failed saves. Decision: the separate keyboard/touch settings action opens the native design-system `conversation/ui/folder-settings.ts` dialog: labelled identity, full ordered roots, primary/read-only choices, name-only creation, inline retry and revision-conflict review. Save includes pending path text; deletion removes grouping, not conversations/files. Independent read-only directory browsing supports parent, home, location and hidden entries; selection requires a current successful response. Requests abort/fence on disposal; recent projects load on expansion with explicit errors. `application/folder-presets.ts` serializes mutations and installs only ACKed presets; stale list reads cannot erase ACKs. Defaults apply only to new conversations; custom or explicit private drafts cannot re-inherit on first send. Existing chats and moves keep roots/access. Saving presets grants no filesystem permission or project guidance. Rejected: another Studio/global project context, nested buttons and optimistic roots. Owners: `folder-settings.ts`, `folder-config-control.ts`, `folder-presets.ts`, `conversation-project.ts`; remove retained bridges when the sidebar migrates. Specs cover confirmed mutations, stale reads, explicit intent and browser interaction.

## Program execution disclosure

**Problem:** execute_program showed a large open card, zero-success headlines and
ambiguous byte telemetry; batch-wide child indentation also implied unrelated
siblings were program calls. **Decision:** the shared `program-execution-card.ts`
owner renders one native details row across retained/classic/default surfaces.
Normal completion is quiet and collapsed; error, incomplete and model-handoff
states reveal actionable evidence. Same-state updates preserve reader folds and
focus; newly actionable transitions open the existing shell without replacing
source nodes. New attempts, unsuccessful attempts and retained-result reuse are
separate facts; missing counters never become inferred successes or call counts
from distinct tool names. Human-formatted binary sizes, exact byte counts and all
raw programStats remain under collapsed Technical details. EN/ZH copy is sourced
from locales. Execution counters and backend semantics are unchanged.

Only explicit execution parent IDs (`parentToolCallId` / `_programCallId`) receive
child rails. Receipts stay in original chronological slots, including unrelated
interleaved rows and repeated call IDs; artifact reuse origins are not execution
parents. Headerless program batches do not advertise a nonexistent collapse
control. **Alternatives rejected:** relocating children loses chronology; hiding
receipts destroys evidence; phase-keying the outer shell destroys focused code;
showing raw measurements in the summary makes pure computation look like failure.
Specs: `program-execution-card.test.ts`, tool-group contracts and
`tests/visual/test_visual_program_execution_surfaces.py` (real Chromium).

## Composer recent models

**Problem:** the recent-model strip kept its initial catalog-render snapshot and
recorded only manual picks, so continued use in another conversation looked stale.
**Decision:** `core/recent-models.ts` owns a bounded five-ID MRU in the existing
origin-local `tofu_recent_models` key. Manual picks and accepted composer commands
record the captured submitted model; rejected/cancelled sends and merely viewing a
conversation do not. All conversations and tabs on the same browser origin share
this key. The picker rereads it on open, rebuilds only when the IDs changed, and
refreshes selection independently; the current model remains in the recent strip.
Existing availability/hidden-model filtering and the large-catalog strip threshold
remain. No timers, subscriptions, new network requests or additional storage are
introduced. **Alternatives rejected:** a page-load snapshot is stale; reading live
composer config after asynchronous send acceptance can record a different
conversation's model; server synchronization is a separate cross-device feature,
not another authority for this browser preference. Specs: `tests/model_picker.test.cjs`
and `tests/model_routing/test_recent_models_browser.py`; full-app integration
also lives in `test_model_picker_startup_browser.py`.

## Shared agent machine-token sanitation

Problem: historical virtual-user settlement comparison copied the Python token
regex grammar into the browser. Decision: `scripts/gen_agent_machine_tokens.py`
reads the registry in `lib/agent_verdict/_handoff.py` through selected AST
declarations (no application import), generating the TypeScript sanitizer and
native Rust pattern constants. Unicode digit, whitespace and case-fold semantics
are translated mechanically; unchanged text retains its exact outer whitespace.
`npm run generate:agent-machine-tokens` regenerates both outputs;
`npm run check:agent-machine-tokens` is part of the frontend drift gate.
`tests/agent_verdict/test_generated_machine_tokens.py` compares the real Python
strip function with bundled generated TypeScript. Hand-maintained browser/native
regex copies were rejected because protocol additions would silently drift.

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
large artifacts so discovery lands on the semantic owner. Runtime and stylesheet manifests declare ordered sources and feature CSS residency. Database Settings markup lives in `frontend/src/settings-panels/database.html`; `compose_settings_panels.mjs` derives its server-injected static fragment during `build:frontend`, with `check:settings-panels` byte parity and authoring-digest coverage. Other legacy panels remain at their existing owner until explicitly migrated.

Database Settings uses the shared Settings `--s-*` palette, border and radius: compact engine/storage groups, unboxed storage facts and divider-separated native disclosure rows. Repeated edit captions and nested configuration cards are omitted; full-row summaries, status, required-index badges, immediate-save notice and existing apply/cancel safeguards remain. This avoids importing Project workbench chrome into Settings without changing storage authority or eager discovery behavior.
Problem: optional CSS blocked chat paint. Decision: defer owned sheets to explicit feature intent; reject eager/lazy duplication, manual import lists and blanket idle module loading. Measured on 2026-10-03: base/settings head CSS fell 1,662,481 → 1,334,924 bytes (353,588 → 287,713 gzip).

**Conversation decomposition:** Problem: mixed projection, content and lifecycle. Decision: [projection owners](../frontend/src/conversation/README.md#view-model-ownership), [renderer owners](../frontend/src/conversation/README.md#renderer-ownership) and `markdown/` form checked acyclic typed graphs; temporary program-placement indices preserve chronology in linear work. Markdown source normalization, HTML/math policy, code DOM, exact LRU identity and reader deferral have separate owners with explicit runtime ports; retained `core/markdown.js` is 75 lines. See `RENDER_CONTRACT.md` for measured cache/lifecycle decisions.
Rejected: arbitrary splits retaining ambient dependencies; repeated suffix scans and array splices. Equality has visit budgets; clipboard fallback always cleans up. Reading CSS now has ten semantic owners (all token-gated); one shared `--msg-measure` controls shell/composer/rails, defaulting to the 720px reading token. Prose is 16px/1.7; process is 13px. Rejected: another late override sheet, theme-specific raw paints and a second width cap that ignores explicit overrides. Browser proofs cover all themes, narrow screens, long evidence, keyboard disclosure and independent scrolling. This redesign adds 8,537 raw / 676 gzip CSS bytes (no new fonts, timers or observers); no budget was raised.

**Composer/Inspector styles:** Problem: `02-messages-composer.css` mixed 85,875 bytes / 920 lines of field, menus, updates, image receipts and diagnostics. Decision: 22 ordered owners plus the extracted shell owner (largest 83 lines); 17 unchanged copies retain exact admission hashes, while field/Inspector edits pass tokens without exemptions. Only five Inspector owners defer; visible tool anchors, persisted image receipts and Update remain critical. Measurement: critical CSS falls 1,296,877 → 1,275,273 bytes (279,294 → 274,293 gzip); all deferred authoring still counts toward budgets. Alternatives rejected: deferring the whole sheet breaks chat receipts; renamed raw edits evade token admission; a second CSS loader races native preload. Reading and interaction evidence is in `RENDER_CONTRACT.md`.

**Application interactions:** Problem: the 1,733-line retained boot owner mixed DOM gestures, uploads, sizing and unowned resources. Decision: seven typed `application-shell/interactions/` owners (largest 69 lines) receive explicit ports; retained `main.js` is 1,391 lines and preserves boot order. Shared `lifecycle.ts` releases completed/cancelled tasks and fences teardown deliveries; partial installation rolls back owned resources. Alternative rejected: more classic fragments preserve ambient dependencies; parallel timer registries duplicate lifecycle authority; whole-entry deferral delays required input. Resource measurements and delivery tradeoffs live in `RESOURCE_CONTROL.md`; `check:application-interactions` plus shipped-app browser contracts verify behavior. The 1,747-line toolbar is now 1,164 lines. Model-picker UI is separately owned by `application-shell/model-picker/`: canonical projection, display planning, row/fold rendering, search index and scoped provider popup; retained toolbar actions keep persistence authority.

## Ownership rules

- New domain behavior is a TypeScript module under `frontend/src/`.
- A retained runtime section may inject ambient DOM/UI dependencies into a typed owner. It may not add a reducer, transport, command builder, error normalizer, timer authority, or persistence policy.
- **Problem:** a 1,823-line retained API registry mixed 57 domains, retries and an unbounded partial-key conversation GET map. **Decision:** `api/endpoint-registry/` composes typed per-domain facades through explicit transport/language ports; target-state write retry, resolver recovery, abortable delay and bounded Project-path single flight have named owners. The retained `api.js` is a seven-line private `runtimeScope.Api` binding. Construction starts no request/timer. Conversation reads use transport-owned, settlement-scoped GET coalescing in direct and proxy profiles; complete query, headers, credentials, parse/error modes, deadline and RPC/affinity identity participate, while signalled/raw-response reads stay independent. **Measurement:** 386 method comparisons preserve paths, options and result projections; 160 distinct reads all proceed while only 128 eligible promises are retained. **Alternatives rejected:** splitting classic sections retains hidden globals; parallel transports duplicate authority; a partial-key or permanent cache conflates reads and can retain stale responses. The public entry remains the only `window.Api` compatibility publisher; `ApiError`, affinity, IDs and HTTP mechanics stay in `api/transport.ts`.
- Fetch-like response projection into the status-preserving `{ok, status, data}` envelope and canonical result-error recovery live in `frontend/src/core/http-result.ts`. Retained orchestration consumers receive its immutable `HTTP_RESULT` port as a static lexical dependency; it is not a browser global or a second transport owner.
- The typed HTTP transport assembles public error `message`, `detail`, and `hint` into `ApiError.message` so Toast callers retain the backend's useful diagnosis. Event errors use `errorEnvelopeMessage`, which appends public occurrence detail to the localized category title and falls back to localized log guidance when an internal event has no detail. A 500 includes its request ID for log correlation even when the server publishes no safe occurrence detail. The transport never promotes an arbitrary private exception body into a public response; the API boundary owns disclosure. Toast text is escaped before DOM insertion.

- **Problem:** the chat-send catch preferred `body.error.message` over `ApiError.message`, discarding the transport's 500 request ID and providing no conversation ID. **Decision:** the typed send-failure presenter retains the public transport message, appends the full request ID if absent and the full conversation ID, and projects them as selectable multiline escaped text in the error toast. No server exception body is newly disclosed. **Alternative rejected:** truncating IDs or relying on a sidebar badge prevents pasted toast text from identifying the failed conversation. Executable specs: `tests/send_failure_message.test.cjs`, `tests/frontend/test_frontend_toast_error_display.py`.
- Lazy features use the private runtime service registry. A retained feature that is not needed for application readiness belongs to a manifest-declared lazy runtime; the composer validates its imports and service dependencies. Core owners are static imports and are not republished as mutable globals.
- A typed feature that still consumes a retained presentation island names an explicit `runtimeScope` service; optional property access is not service registration. `npm run check:runtime` rejects closed, unreachable chains of top-level retained callables in every generated runtime, as well as stale composition and missing lazy dependencies.
- Every listener, timer, EventSource, observer, and subscription has a declared scope and disposal path.
- Background activity owns at most one timer/request, no hidden-page polling timers, and composition teardown; unchanged popovers refresh elapsed text without rebuilding rows or rerendering chat.
- Backend process liveness and Sidecar storage readiness are separate verdicts. `backend-availability-monitor.ts` treats push/browser state as suspicion and requires two failed health probes before alarming; the independent `storage-availability-monitor.ts` owns one visibility-aware, single-flight recovery poll. Their overlapping `/api/health` reads share the no-cache `availability-health-probe.ts` coordinator: the opener's bounded request supplies one immutable status snapshot, and a lazy memoized `json()` lets independent consumers read the body without duplicating the wire request or consuming it twice. The flight is released on either settlement, so later recovery checks remain authoritative. Both verdict owners receive DOM, health, clock, copy, and logging ports at composition, publish no browser global, and are destroyed with the retained page composition lifecycle.
- Long-lived-tab frontend self-healing lives in `frontend/src/core/build-watch-controller.ts`. The DOM-free controller owns only one bounded pending-build record, the 30-minute busy defer, and the session reload guard. Served build identity arrives on the existing push pong every five minutes and on visibility resume; retained composition adds no health HTTP poll, timer, or visibility listener, replays the last valid identity to late subscribers, and remains inert against older servers.
- Lazy feature-chunk load failures self-heal in `frontend/src/core/feature-load-recovery.ts`. A failed dynamic import is cached by the browser module map for the document's whole lifetime, so the DOM-free controller answers a JS/CSS asset-load-shaped failure with at most one reload per 60-second guard interval and hands the pending feature entry to the next boot through sessionStorage (5-minute replay expiry); anything else still falls through to the `feature.loadFailed` toast. The HTML boot handler leaves JS/CSS failures uncancelled as soon as the feature port exists, including the pre-ready window. `main.ts` routes both `invokeFeature` and `prepareFeature` domain loads through that seam and replays the pending entry via the retained bridge stub after `tofu:app-ready`; the classic `feature-bridge.js` module-graph-timeout path mirrors the same storage keys for the case where the failed ESM graph is exactly what the typed owner cannot survive.
 Two upstream changes make
  that recovery rare instead of routine. The asset publication retains
  multiple generations (`scripts/vite_asset_retention.mjs`): the current and
  previous manifest graphs stay pinned, dropped generations survive
  newest-first inside a bounded window (default 7 days / 48 MiB,
  `TOFU_VITE_ASSET_RETENTION_MAX_AGE_MS` /
  `TOFU_VITE_ASSET_RETENTION_MAX_BYTES`), every build re-stamps live mtimes
  so a dropped file's grace window starts when it leaves the manifest, and
  raw `vite build` never empties the output directory
  (`emptyOutDir: false`). Routed features load on explicit intent; idle owners
  carry day/subscription notices and update checks. One weakly keyed graph flight
  per declared loader keeps JS/CSS failures sticky until document teardown.
- The Memory panel owns one active catalog request per open epoch and at most one trailing request after close/reopen invalidates presentation ownership. Repeated opens and scope-tab clicks share the active all-scope snapshot and filter it locally; generation checks still prevent a superseded view from rendering. Settlement releases both flights, so the feature retains no page-lifetime server-data cache.
- Push WebSocket RTT, per-ping timeout, half-open force-close, and offline verdicts remain one authority in `frontend/src/runtime/sections/push.js`. The adjacent `net-latency.js` topbar adapter is an event-only projection of that authority plus the typed conversation-stream aggregate: it owns no staleness clock, elapsed-time transport guess, extra connection, or endpoint, and releases both subscriptions with the retained composition lifecycle.
- Collaboration Bar summary ordering and its local peer mirror live in `frontend/src/features/presence-summary-controller.ts`. The DOM-free owner retains one displayed-scope summary, one same-scope shared request flight, a generation guard against rapid conversation/project switches, and at most 32 project roots × 128 conversation IDs. The retained presence projection refreshes once after its late boot wiring, then only from explicit conversation/project funnels or push-invalidated 300 ms demand debounce; it owns no page-lifetime interval.
- Browser-console flush timing lives in the browser-global-free `frontend/src/core/client-log-flush-scheduler.ts` owner. It resolves the direct/constrained-proxy 15/60-second profiles, coalesces repeated log demand behind one asynchronous flush, and owns a timer plus visibility subscription only while the bounded relay queue is non-empty. Hidden demand releases its timer and keeps one resume signal; empty settlement, kill-switch clearing, and teardown release both. The retained adapter remains the sole owner of console patching, its 400-line / 200-entry / 800-character bounds, the never-amplify drop policy, and the existing pagehide beacon.
- The per-turn MCP context rail owns no transport or tool-schema cache. Its bounded server/count projection piggybacks on the already-required `server-config` first-screen response, then on MCP catalog and per-tool mutation responses while Settings is active. `applyMcpToolSummary` replaces the projection synchronously; a page that never opens Settings issues zero `/api/v4/mcp/tools` requests merely to count tools, and opening a server's tool-toggle panel remains the only browser demand for full schema rows.

- **Problem:** a fixed 232px per-turn context rail leaves unused gutter in a wide chat pane. **Decision:** `runtime/sections/main.js` resolves its width against the actual `.chat-container` pane: consume surplus space up to 480px, squeeze the rail below 232px before prose would fall under its 700px measure floor, and mirror the resolved width/gap into both the chat grid and composer. The `chatpane` query still grants the rail only when there is room; its 48px grid inset cap applies only in rail-granted panes, so narrow panes retain the compact fold and the composer's old centering. The geometry sweep in `tests/turn_lifecycle/test_turn_ctx_rail_geometry.py` covers sidebar/drawer states and keeps the rail contained. A narrow `chatpane` query forces zero rail furniture even if the observer still carries the previous wide-pane inline grant for one frame; otherwise the hidden rail's stale track can reduce the compact fold to 0px in a ~244px pane. **Alternative rejected:** a percentage or container-query width inside the rail grid track resolves cyclically against `.chat-inner` and can enlarge the track beyond the pane.
- **Problem:** in a wide rail, the model label's growing flex item pushes its effort badge to the far edge; for a long label, a merely shrinkable item can instead wrap the badge away. **Decision:** the header renders model and effort as one non-growing, shrinkable flex group: short names keep the badge beside the name, long names truncate within the group, and other mode badges may still wrap separately. **Alternative rejected:** changing only the model label's `flex-grow` fixes short names but leaves long labels and their badge on separate lines.
- Deployment-flag loading lives in the DOM-free `frontend/src/core/feature-flags-loader.ts` owner. The normal first screen consumes the bounded snapshot carried by `server-config` and issues no `/api/v4/features` request. The owner accepts at most 256 valid boolean keys, requires the core debug/optimizer flags, ignores only API-envelope metadata, and commits only a changed snapshot. Missing/invalid piggyback data (including an older server) activates one shared compatibility request; concurrent callers join it and settlement immediately releases it. It owns no timer or page-lifetime cache. The retained adapter alone projects a committed change to badges, launch visibility, the sidebar, and the active conversation with `forceScroll:false`.
- Confirm, alert, prompt, and multi-option choice dialogs share the lazy typed
  `frontend/src/lazy-dialog-controller.ts` service and
  `frontend/src/dialog-controller.ts` DOM owner plus its deferred CSS.
  First intent waits for JS/CSS; the document import cache retains one failure.
  The controller permits one active overlay, renders text with DOM text nodes,
  settles replacements safely and disposes keyboard/frame/live-check/exit scopes.
  Retained consumers get only four lexical aliases from `_prelude.js`; style
  ownership and failure policy live in [`RENDER_CONTRACT.md`](RENDER_CONTRACT.md).
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
- Swarm push subscription, lifecycle, transient-Turn overlay, authoritative rebase, and terminal hydration live in `frontend/src/conversation/application/swarm-presentation-overlay.ts`. Retained reducers are injected presentation ports; they neither subscribe independently nor copy session telemetry into durable Turn state. `conversation/presentation/swarm-agent-presentation.ts` owns shared launch receipt/roster selection and immutable bounded agent-evidence merging. Retained rendering and status recovery consume this same owner, so partial live cards cannot suppress newer snapshot model, clock or tool evidence. A settled durable swarm snapshot outranks stale live agent rows. Header, progress counts, result bodies, and timers must derive from the same resolved execution state; a completion header must not coexist with unresolved rows from an older overlay. Separate launches remain separate panels, while wait and result retrieval are not new executions. Late telemetry must not cross execution boundaries or reopen a terminal historical panel. Failed agent cards use a typed presenter for compact, expandable, escaped technical detail and contextual recovery. A structured `route_unavailable` code (or explicit legacy “No slot available” message) means that dispatch sent no upstream request, not that the provider is down or that earlier rounds did no work. Generic failures do not inherit that claim. Recovery never switches models or retries automatically; file-edit caution depends on recorded edit evidence, and partial output remains visible separately.
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
- A folder can bind DEFAULT project roots (`folder.projectPaths[]`, each ≤512 chars, ≤32 roots, schema-validated by `routes/api_v1/folders.py` create/update; the legacy scalar `projectPath` is kept mirroring the primary root for back-compat). The create and rename dialogs use a multi-root picker (`_buildFolderPathsPicker` in `main_folders_mobile.js`): a removable-chip list (first = primary) fed by the server-persisted recent-projects list plus a manual add-row — no forced absolute-path typing. The retained `folderDefaultProjectPaths(folderId)` resolver in `core/folders.js` returns the ordered, de-duplicated roots. A brand-new conversation created inside a folder inherits every root (first = primary, rest = extra roots): `newChat` pre-attaches them to the empty draft via `_conversationProjectController.chooseDraft` (so the project bar shows them immediately and the user can still change or clear them), and the send pipeline in `main_send_pipeline.js` stamps them onto the new conversation's empty workspace before the first submission binds roots. An explicit composer/project-modal attachment always wins — the folder default only fills an empty workspace, and existing conversations already in the folder keep their current binding. Clearing all roots removes the binding. Specs: `tests/frontend/test_frontend_folder_project_path.py` (resolver) and `tests/frontend/test_frontend_folder_paths_picker.py` (picker interactions); round-trip covered by `test_api_v1_integration.py::test_folders_crud_full_lifecycle`.
- Composer Option+Enter (macOS) and Alt+Enter (Windows/Linux) insert a newline
  at the caret, replacing selected text and emitting the normal input event,
  in both Enter-send and Ctrl+Enter-send modes. IME candidate confirmation takes
  precedence. Explicit insertion avoids platform-dependent modified-Enter
  defaults; treating this shortcut as send was rejected because it loses drafts.
- Chat composer command serialization remains at the retained adapter in
  `frontend/src/runtime/sections/main/main_send_pipeline.js`. At most one
  submission per conversation may own preparation and acknowledgement at a time. A Send or
  Enter intent received during that flight is collapsed into one trailing
  intent and drained after authoritative acceptance; a stop, rejection, or
  uncertain failure cancels automatic draining and leaves the restored draft
  for an explicit retry. Thus command IDs remain idempotent without turning a
  temporary send lock into a silent no-op. Submission lanes and uncertain
  command IDs belong to each conversation; another pending conversation cannot
  block sends or steal restored drafts. Preparation has a deadline and cannot
  submit late or retarget after navigation. 408/429/5xx retain retry identity.
- Background activity scheduling: `frontend/src/background-activity-controller.ts` owns one request/timer/trailing-wake bit (250 ms minimum spacing, 3/10 s active/idle fallback, failure backoff). Hidden/native-hidden suspends timers; resume refreshes; disposal fences late responses; refresh preserves open popovers. Background-command convergence: `frontend/src/conversation/presentation/background-command-completion.ts`. **Problem:** settled handoff is not completion. **Decision:** join canonical queue/receipt evidence by conversation + command ID; weak caches preserve Turns/blocks. **Rejected:** browser prose parsing, activity disappearance as completion, or chat hydration per poll. Semantic evidence—not tokens—wakes reads. Chromium specs assert zero repaints across 20 unchanged snapshots and stable queued→delivered card nodes.
- Thinking-depth presentation lives in `frontend/src/features/thinking-depth-slider.ts`.
  Desktop/mobile share off/low/medium/high/xhigh/max (default medium); the current level renders once, beside the heading, with no scale or explanatory footer.
  A 24px capsule groove carries a solid fill with a two-tone tick dot at each stop; a 26px card-colored knob overlaps the groove, and the fill reaches both rail ends, with 44/48px hit targets. Captured pointers bypass native touch slop; one geometry read per gesture preserves grab offset, fractional thumb/fill/label movement, no DOM reconstruction or saves while dragging.
  Release snaps and commits a changed level once through `selectThinkingDepth`; keyboard remains native integer range. Cancel/blur/focus loss/model sync release capture without saves, and a fresh keyboard gesture clears cancelled ownership even without pointerup. Teardown releases capture/listeners.
  Locale/ARIA and legacy ultra→Max are display-only; provider mapping stays backend-owned. Specs: `tests/thinking_depth_slider.test.cjs`, isolated real-Chromium `tests/application_shell_fragments/test_thinking_depth_slider_browser.py`.
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
  neither a feature-registry service nor a browser global. Its byte framer
  (`core/sse-line-framer.ts`) caps each line and cumulative event at 16 MiB,
  including comments and ignored fields; blank lines reset the event budget.
  UTF-8 fragmentation and CR/LF boundaries are normalized before delivery.
  Completion, callback errors, overflow, and explicit abort all release the
  reader lock; early exits cancel the body without awaiting an untrusted
  cancellation promise. EOF tail delivery remains caller-configurable.
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
- Sidebar search lifecycle lives in `frontend/src/sidebar-search-controller.ts`,
  with a narrow retained list adapter. Server hits render independently of the
  preloaded catalog and only join it on interaction; opening a deep hit preserves
  the global list's paging boundary. Debounced single-flight requests have abort
  and sequence fences, including failures, ABA query changes, clear and teardown.
  Same-query metadata reconciliation does not repeatedly fetch during rendering;
  relevant refresh is limited to 30 seconds. Explicit retry is immediate then
  coalesced to at most once per second. Errors never masquerade as no matches.
  Empty scan continuations advance at most four requests per action; explicit
  Load more continues. Browser retention is capped at 500 with a visible
  narrow-your-query notice; indexing/partial coverage stays visible. HTTP shape
  and budgets are owned by `contracts/conversation_search_v2.json`.
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

  The standalone Research surface is a full workbench, not a Paper-reader sub-panel. `research-runtime.ts` owns the one live task stream, bounded event replay, poll/push teardown, durable artifact hydration and terminal-quality projection. `research-view.ts` is presentation-only: it renders the five visible stages (harvest, survey, ideate, evaluate, package), ranked experiment briefs, independent review decision, rejection audit, and the reproducibility ledger. A researcher can inspect the running trajectory in the **Live evidence trail** (tool-round projection); after completion the same page retains the corpus, full survey/gap map, usage ledger and review revision queue. Starting a new direction detaches browser polling/push through the runtime disposer but does not cancel the checkpointed server job; explicit **Stop run** is the only cancellation action. Recent directions come from the durable research list, so refresh/reopen never depends on the in-memory task registry. While a job is still in flight, entering Research mode re-attaches to it through `GET /api/v4/tasks?kind=research` (owner-scoped list → snapshot → poll/push re-attach) instead of stranding the researcher on the landing page; a job that finished in the meantime projects its terminal state and hydrates the durable archive. The retained Paper presentation islands are also demand-loaded: report and reader markup live only in the manifest-owned `paper-reader-presenters` runtime, while podcast and video markup live in `paper-media-presenters`. `frontend/src/features/paper.ts` installs both before the typed panel owners. The typed report runtime owns start/regenerate, provisional Stop handoff, polling, and reopen policy. Per-kind generations fence stale success and failure continuations even for two starts on the same paper; cache-hit starts reuse the canonical metadata-only apply path. One explicit cache-resolving lookup preserves live-task precedence and selects the preferred/fallback report server-side. The backend projects that hit from one bounded owner-scoped Sidecar aggregate; only the selected language's additive artifacts cross the process boundary. Rebuttal drafts retain at most 32 papers and 40,000 characters per paper, matching the request/backend limit. A known paper starts by hash only; the server reads at most its 120,000-char prompt source only after live/cache misses. The browser retries with bounded text solely for the explicit pre-dispatch `paper_source_required` 400, after rechecking the same generation fence; ambiguous failures are never replayed. Late owner ports and markup action receivers use the private `featureRegistry`. Mutable retained values use its explicit `readLiveRuntimeBinding` / `writeLiveRuntimeBinding` state port, because the owner override cache must not hide a later paper switch. A raw lexical-global dependency is not a compatibility seam.
- Paper PDF rendering is owned by the instantiated
  `frontend/src/features/paper/pdf-reader.ts`; `pdf-viewer.ts` adapts existing
  Paper actions and library metadata. The shared lazy PDF.js loader supplies
  independent document lifecycles, bounded viewport rendering, selectable text,
  search, links and outline navigation. Zoom, resize and atomic document
  replacement preserve page/fraction anchors. Reader limits and verification
  are documented in [Paper builds](PAPER_BUILDS.md#browser-reader).
- Chat paper preview consumes committed file receipts through
  `features/paper/preview-changes.ts` and the core `onStateApplied` port.
  The lazy `features/paper-preview.ts` entry starts `preview-controller.ts`;
  generic TaskRuntime status and the typed paper-build API own execution.
  `preview-panel.ts` places the shared PDF reader in a desktop split or mobile
  chat/paper tabs. Closing persists user intent and detaches the exact viewer;
  conversation/mode changes release the panel and its PDF resources. Search,
  outline and logs open on demand; verified build locations can follow edits.
  See [Paper builds](PAPER_BUILDS.md#chat-preview) for bounds and verification.
- Settings retained presenters are demand-loaded as the manifest-owned `settings-presenters` runtime. `frontend/src/features/settings.ts` evaluates the typed Settings owners before that retained island, so every declared typed service is present when the generated runtime closes its dependency boundary. The shell routes `openSettings`, `closeSettings`, `saveSettings`, `switchSettingsTab`, and onboarding's immediate `_oauthLogin` through the feature bridge. Saving keeps its busy latch until feature-toggle writes are acknowledged; a rejected, missing, or failed acknowledgement reaches the footer error and leaves Settings open for retry. Routing-authority reads are coalesced and retried before dependent speech/routing persistence. Reassignable shell state and Settings working copies cross the private registry through live accessors; a captured array, set, boolean, or cache reference is not an acceptable lazy-runtime dependency. Optional panel presentation follows the same residency boundary: the Devices tab imports `frontend/src/features/settings/devices.css`, the Tools inventory owns its complete base/comfort rules in `tools-inventory.css`, and shared modal-only refinements live in `settings-comfort.css`. A manifest-generated CSS entry imports ordered base sections before those refinements; Vite waits for CSS before invocation. The project-folder remote-device picker stays eager because it can render without opening Settings.
- The composer has one compact **conversation bar**, owned by `features/conversation-bar/{project-context,context-meter,turn-navigation}.ts` and `25-conversation-bar.css`. Project location is the left segment of a content-sized project cluster: Auto stays beside the names. Idle width lives in the strip between Auto and the meter, not inside a stretched trigger. Names that fit that leftover (strip minus Auto/meter/turns/chrome) join with a middle dot; `+N` only counts names that do not. Labels are measured at `max-content` so ellipsis stretch cannot collapse a fit to `+N`. A ResizeObserver on the strip/bar (and sibling mutations) refits the list and is disconnected on destroy. A literal context meter and fixed previous/count/next follow it. There is no pet, scene, pocket, separate project card or ornamental sphere. Native buttons open shared nonmodal `disclosure.ts` panels for full paths and permissions, context counts/compaction actions, and a TurnStore jump list. Named container queries compact labels by actual pane width, not only screen width. Narrow panes move Auto/Manual into project details; the bar never wraps. Panels become fixed before anchor measurement so body flex cannot shift the UI. Escape returns focus; native outside click/focus wins, and resize/scroll/ hidden-page dismissal releases the panel's temporary listeners. Retained project/context/turn adapters inject existing command and accounting ports, not another store. Explicit jumps reveal stable IDs through the Surface-owned 80-turn window and suspend follow; jump lists mount at most 50 rows, with explicit paging/loading for earlier server history. A `+` marks incomplete loaded-history counts. Late history responses are fenced by view generation; project permission/clear completion shares the project controller's navigation generation. Manual compaction is single-flight per page and its progress is visible only in the originating conversation. UI previews cap text at 24,000 characters; durable summaries stay server-owned. Specs: `node --test tests/context_meter.test.cjs tests/turn_navigation.test.cjs tests/project_location.test.cjs`, `tests/conversation/test_conversation_bar_browser.py` and Surface window/reveal tests.
- Timer creation settles an immutable `timerReceipt` through ordinary tool-result metadata; typed cards describe the background handoff after reload without a waiting-task spinner or browser timer. Live panel rows distinguish code predicates, ready dispatch, fired, failed and expired states; desktop/mobile evidence lives in `tests/visual/test_visual_timer_panel.py`. Update, Timer, and Optimizer retained presenters are one demand-loaded
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
- The Project folder workbench is the manifest-owned, demand-loaded `project-presenters` runtime behind `frontend/src/features/project.ts`. Core project state, bar rendering, restore/SSE reconciliation, and background rescan remain retained because they run without the modal. A conversation restore carries its primary recent-path intent inside the same `setPaths` request; an explicit workbench apply carries a primary-first prefix of at most 32 selected roots without truncating the authoritative project list. The browser owns no second recent-path write, and background/status reconciliation omits the optional intent. Write approvals, subprocess stdin, and apply-code confirmation remain in the small retained `execution-interactions.js` section, so coding actions neither wait for nor retain the folder browser. The generated presenter imports its typed browse coordinator and static icon owner only on demand. Mutable conversation/project authority crosses one frozen `ProjectPresentationShellState` live accessor object; individual values must not be captured through `featureRegistry`. The retained clear path probes the optional `ProjectModalPresentation` cleanup owner and never invokes the open/close feature stub, preventing an idle clear from fetching the workspace.
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
- Debug Panel and Request Inspector DOM, task/payload/trace reads, live TurnStore subscription, poll and five Inspector CSS owners belong to the demand-loaded `diagnostics-presenters` entry. Native `diagnostics-message-disclosure.ts` uses UI buttons and restores refresh focus by stable message identity. CSS preload completes before markup; closed panels load neither these sheets nor the runtime. The retained
  `core/debug_state.js` authority owns only evidence that must exist before a
  panel opens: diagnostics/error and per-task snapshot bounds, clipboard
  fallback, and tool-round-to-task identity resolver. Mutable
  conversation identity, visibility, config, and bounded caches cross one
  frozen `DebugShellState` live port. Conversation lifecycle code probes the
  optional `DebugPresentationState`; it never invokes a feature stub merely to
  clear or switch diagnostics. The loaded owner also returns before any
  `/debug-messages` read while closed, so ordinary navigation has neither a
  chunk-load cost nor a hidden diagnostics API cost. First open owns task-list
  loading, subscription, and poll; close disposes both scheduling paths.

  **Problem:** a transient event-store failure can leave turn dividers but no
  messages; marking those turns expired or overwriting a complete feed makes
  a recoverable fault look like lost history. **Decision:** the flat wire fold
  marks failed reads as `readError` (the per-turn gap is retryable), reserving
  `expired` for successfully read, absent snapshots. The DOM-free
  `frontend/src/features/diagnostics-wire-controller.ts` coalesces overlapping
  feed reads and retains one complete wire per open conversation epoch. Silent
  poll, TurnStore invalidation, and manual retry never replace that wire with
  partial or failed data; an initial partial feed remains visible with a retry
  warning. A conversation switch or close invalidates late responses and drops
  its cached wire. **Alternative rejected:** an unconditional "expired" divider
  or a silent poll that paints zero messages hides the storage fault and
  destroys the user's last reliable diagnostic view.

  A native `event.list` page can exceed the storage authority's 8 MiB
  materialization cap even though its requested row count is within 1000.
  The inspector retries only that classified page-size rejection, halving the
  page limit at the same indexed cursor down to one; a single oversized or
  corrupt event remains a visible failed read. An `event.inspector_summary`
  failure marks discovered turns unprobed/retryable instead of silently
  treating them as expired. Other failures never trigger page-size backoff.
  Verified prefixes and cache bounds follow [RESOURCE_CONTROL.md](RESOURCE_CONTROL.md).
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
- Orchestration Studio is one demand-loaded feature graph.
  `frontend/src/features/orchestration.ts` evaluates the three typed owner
  barrels before the manifest-owned `orchestration-presenters` retained
  runtime. Durable runs are observed through the conversation projection —
  the standalone Task Mode surface was retired (see
  `docs/ORCHESTRATION_PRODUCT_SURFACES.md`). The eager runtime imports the
  small typed Flow Picker, bounded saved-Flow catalogue, and generated
  endpoint client directly because the chat toolbar must list saved
  definitions before Studio is requested.
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
- Human Guidance cards and compact outcome rows live in the DOM-free `frontend/src/conversation/presentation/tool-human-guidance-presentation.ts` owner. Its single immutable port closes the awaiting → skipped → submitted state order. Settled unanswered questions are read-only unless the exact Turn settlement offers `answer_guidance`; that late-answer projection keeps the original choice UI interactive after Stop. It admits at most 16 options, parses legacy option JSON only below 65,536 code units, bounds identifiers at 512, questions at 32,768, labels at 1,024, descriptions at 8,192, and each option-owned note at 4,096 units, and makes every elision or unavailable response visible. Guidance IDs, ordered option indexes, and original choice labels remain escaped `data-*` values consumed by static restricted actions; none can enter executable action text. Translation and sanitized Markdown are injected capabilities. Delegated response behavior lives separately in the lifecycle-scoped `frontend/src/conversation/ui/human-guidance-actions.ts` owner. It reads only card-scoped static selectors, requires card/group/button/note datasets and ordered indexes to agree, rejects more than 16 option groups, and submits only the chosen label and that option's draft. It caps free-text replies at 32,768 code units before translation or transport and permits one in-flight submission per conversation/guidance identity. Choice rollback is exact; translation failure falls back to the bounded original; backend 404 triggers an expired repaint while transport failure keeps its distinct error. Composition owns the presentation store through an immutable lexical port. Retained code keeps only EN-to-CN arrival translation and two forwarding action entries; it publishes no Human Guidance state through `runtimeScope`.
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
| Demand-loaded Orchestration Studio graph and typed action registry | manifest bundle `orchestration-presenters` + `frontend/src/features/orchestration.ts` |
| Owner-scoped, byte/entry-bounded conversation catalog metadata cache | `frontend/src/core/conversation-metadata-cache.ts` |
| Lazy conversation metadata-cache loading and teardown race | `frontend/src/core/conversation-metadata-cache-lazy.ts` |
| Marked parser options and strict-GFM deletion policy | `frontend/src/markdown-policy.ts` |
| Endpoint facades / declared retry / complete GET identity | `frontend/src/api/endpoint-registry/` / `frontend/src/api/transport.ts` |
| Conversation cursor, SSE and recovery | `frontend/src/core/conversation-sync.ts` |
| Authenticated browser-owner resolution | `frontend/src/core/current-user.ts` |
| Push-frame owner comparison | `frontend/src/core/frame-identity.ts` |
| Normalized Turn/attempt/phase reduction | `frontend/src/conversation/domain/turn-store.ts` |
| Ordered Turn reads | `frontend/src/conversation/application/conversation-read-model.ts` |
| Turn/block view model | `frontend/src/conversation/presentation/conversation-view-model.ts` |
| Tool execution/attempt grouping, attention and panel projection | `frontend/src/conversation/presentation/tool-execution-groups.ts` |
| Tool execution reader-owned disclosure interaction | `frontend/src/conversation/ui/tool-execution-disclosure.ts` |
| Tool family, label/color, icon-key and program-row presentation | `frontend/src/conversation/presentation/tool-round-presentation.ts`; pure `frontend/src/conversation/presentation/tool-header-presentation.ts::createToolHeaderPresentation` owns `queryText` historical batch-count normalization and `renderBadgeHtml` badge selection; [header ownership chain and retained delegation](../frontend/src/conversation/README.md#tool-card-header). |
| Tool-round trusted SVG asset resolution | `frontend/src/conversation/presentation/tool-round-icons.ts` |
| Synthetic context-injection row presentation | `frontend/src/conversation/presentation/tool-injection-presentation.ts` |
| Human Guidance card and outcome-row presentation | `frontend/src/conversation/presentation/tool-human-guidance-presentation.ts` |
| Human Guidance delegated response DOM, single-flight and rollback lifecycle | `frontend/src/conversation/ui/human-guidance-actions.ts` |
| Context grouping taxonomy and passive provenance placement | Registry-generated `system-note-presentation.generated.ts`; `presentation/context-note-grouping.ts`; `presentation/context-provenance.ts` (one owner inside note disclosures, separate pending actions) |
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
| Model brand SVG/color presentation (mono currentColor only — no url(#…) paint-server refs; every template/vendor key must resolve, enforced by `tests/providers/test_provider_template_brand_icons.py`) | `frontend/src/core/model-brand-icons.ts` |
| Model/provider display names and natural ordering | `frontend/src/core/model-display-names.ts` |
| Conversation role-avatar snippets | `frontend/src/core/role-avatar-icons.ts` |
| Alias/family model display projection | `frontend/src/core/model-display-fold.ts` |
| Bounded recent-model persistence | `frontend/src/core/recent-models.ts` |
| Settings 服务商 (Providers) tab: provider-scoped supply only — ProviderAccess summary with in-card Connection/Credential editor and per-provider 模型管理 dialog (Offering enable/alias/remove, no tabs); classic two-row key card (hint-only secret + today's per-key stats/override toggle) fed by `GET /api/v4/dispatch/key-stats` (`key_namespace` composes the owner-scoped provider id that override writes must target). Never re-renders the 模型 tab. | `runtime/sections/settings/provider_render.js::_renderModelRoutingProvidersTab` + `_renderV2KeyCardStats` |
| Provider template catalog: searchable official/third-party/local groups of compact brand+name tiles (endpoint host on the tooltip; seed metadata powers search only, never renders), single-flight fresh reads, keyboard/teardown lifecycle, credential-first empty-inventory discovery; never infers account availability from template membership. Specs: `tests/provider_template_picker.test.cjs`, `tests/providers/test_provider_template_picker_browser.py`. | `frontend/src/features/settings/provider-template-picker.ts` + API adapter in `frontend/src/features/settings.ts`; retained `_stageModelRoutingProviderBundle` owns the existing CAS create path |
| Settings 模型 (Models) tab: global, read-only Creator/Model registry grouped by model developer — kept up-to-date by world-catalogue sync + subscription sync; provider supply, credentials, offerings, deployments, aliases, routes never render here (the Creator/Model-only projection handed to the feature is the data boundary, not a view convention). Flows that reload the v2 authority or mutate creators/models repaint both tabs via `_renderModelRoutingTabs()`. | `runtime/sections/settings/model_catalog_tab.js::_renderModelCatalogTab` + `frontend/src/features/model-catalog/panel.ts` |
| Owner-scoped model-routing v2 Settings load/CAS/secret replacement | `runtime/sections/settings/core_panel.js::_loadModelRoutingAuthority` + `runtime/sections/settings/save_export.js::_saveServerConfig`; concurrent Settings reads coalesce, and Save joins or retries authority readiness before feature persistence |
| 本地部署 page: live hardware strip, curated catalog ranked against it (purpose-filtered, fit-tiered, download-hint cards that hand off to the chat agent), DeepSeek V4 Flash cloud escape hatch deep-linking the template wizard, managed-instance ledger with failure diagnosis → agent repair handoff, then the engine preset chooser / batch endpoint probe-and-stage / managed model-path handoff | `runtime/sections/settings/local_deploy.js::addLocalProvider` (read-only `Api.localServe` → `routes/api_v1/local_serve.py`; staging via `provider_render.js::_stageModelRoutingProviderBundle`) |
| Provider-first chat picker, automatic Provider choice, and provider-scoped pending identity | `runtime/sections/main/main_toolbar_ui.js::_modelRoutingDropdownModels` |
| Chat picker startup and readiness recovery | `frontend/src/features/model-catalog/model-picker-startup.ts` + `runtime/sections/main/main_toolbar_ui.js::_loadServerConfigAndPopulate` |
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


### Chat picker startup recovery

**Problem:** one-shot startup reads left a selectable placeholder after `server_booting` 503 or a restart disconnect.
**Decision:** the startup owner fetches config/routing concurrently, applying config policy before routing/effort controls.
The adapter gives each read a 15 s deadline and abort signal. Transient network/body-read, timeout and gateway failures
receive six active-page retries (1/2/4/8/16/30 s); hidden pages never poll. Boot-ready, backend-restored and visible/online
wakes recover without reload, remembering in-flight readiness; one readiness probe closes the missed-event race.
Latched `server_boot_failed` waits for explicit boot-ready or manual retry. Authorization/validation failures never
poll; any 401/403 takes precedence across both reads and clears selectable cached models. Last-good routing remains
visible and re-filtered during refresh/transient failures. Empty authority is distinct, never a hardcoded fallback.
Status rows use `data-i18n` for delayed locale repaint. Catalog arrival refreshes reasoning without converting a
provisional display into selection or overwriting offering identity. Teardown aborts reads/timers/listeners and ignores late results.
**Alternatives rejected:** global transport retries/forced reloads hide unrelated failures, lose state and spin on failed boot;
one-shot retry-only recovery misses disconnects and delayed responses. Specs: `tests/model_picker_startup.test.cjs`,
`tests/thinking_depth_slider.test.cjs`, `tests/model_routing/test_model_picker_startup_browser.py` (real browser).
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

One v3 snapshot contains settings, revision, a Turn window, its attempts, cursor, heartbeat policy, and an opt-in metadata-free artifact existence bit.
The generated browser requests the newest 96 main-lane Turns for linear conversations; the default public representation remains complete, and branch-bearing conversations receive that full representation until lane-directory paging exists.
`turn-runtime.ts` applies settings at the snapshot boundary, hands metadata to feature adapters, then dispatches TurnState. `hasArtifacts:false` commits empty artifact presentation immediately and skips the legacy list request; `true` or absence keeps that request for rolling compatibility. A weak conversation-shell generation fence prevents older list responses overwriting newer negative hints.
The pure selector reads Turn state directly and the controller commits to the keyed Surface. `turn-projection.ts` updates only catalog/lifecycle metadata; it cannot materialize a parallel transcript.
Initial catalog boot is metadata-only: inactive shells issue zero Turn snapshot requests, so startup network/memory concurrency does not grow with sidebar size. Opening a conversation owns one on-demand snapshot: no second settings request or fallback to archived bodies or IndexedDB transcripts.

Settled Turns normally carry their authoritative cost stamp in that snapshot.
Legacy projections without one retain server-side pricing authority: all
synchronous Surface misses join one bounded 512-entry microtask batch, share
in-flight fingerprints, and request one authoritative repaint only after the
aligned result lands. The browser never emits one pricing request per Turn,
caches a partial response as no-charge, or performs pricing arithmetic.

The finish footer and lazy cost popover share `frontend/src/token-usage.ts` for **token presentation only**, mirroring `lib/cost.py`. Problem: partial/missing pricing formerly fabricated zero input. Decision: prefer explicit cost counts (including zero), fall back to usage, otherwise show localized “unknown”; money stays pending. Reject `Number(value) || 0` and price availability as evidence of token usage. Provider read/write totals remain distinct from estimated allocations (including unmarked legacy records); all structured reasons retain evidence strength, not provider causality. Verify with `token-usage.test.ts`, finish-info harnesses and `tests/visual/test_visual_cache_accounting.py` against the isolated app.
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

Diagnostics are a read-only projection of `ConversationTurnRead`, connection metadata, and the active Surface dataset. Collecting diagnostics never issues a conversation request, parses a message array, or creates a recovery state path. Its collector is retained and bounded, while Debug/Request Inspector presentation is demand-loaded; a closed owner performs no task, trace, payload, or reconstructed-message read. Request Inspector keeps its pre-request message/schema snapshot for exact historical reconstruction and joins the matching bounded `tool_wire_projection` event for provider-bound truth. The round list is requests only (state mirrors are served per round through the payload endpoint, never listed), and each round row names the tools that round INVOKED — folded server-side from the next snapshot's new-message tail, the same glanceability contract as the chat timeline's turn blocks. A delegating run (Goal mode) merges its swarm children's rounds into the same list: each child event log is folded independently (attempt/wire/tool joins stay scoped to that log's own `(turn, roundNum)` space), merged rows carry `sourceTaskId` plus the agent badge, and the drawer's per-round payload, previous-round diff base, and raw-archive reads all address `sourceTaskId` instead of the selected parent. At most 32 child streams are merged into one fold; overflow reports `coverageReason:child-limit` so the bounded diagnostic never claims full coverage. Sub-agent snapshots reuse the finalized request body's canonical messages, model, tools, and wire parameters rather than reconstructing a second approximation. Round detail exposes final wire names, schema-token estimate, exact fingerprint, discovery backend, budget and omissions; assembly snapshots never stand for input. At the existing 10,000-structural/200,000-scanned boundary, one-row lookahead proves whether more rows exist. Source-bound `eventReadBounds` share the byte-bounded cache and mark overflow partial; exact-boundary exhaustion stays complete. Truncated histories cannot establish latest snapshots or missing rounds: detail fails retryably, wire gaps stay unexpired, and prior evidence carries a window notice. Storage-only snapshot delta v2 shares one chronological `(task, turn)` baseline across request/state kinds; the server rebuilds full kind-specific payloads, while unversioned v1 histories retain their original interpretation. The per-round endpoint decodes one proxy-added escape layer only for minted `<phase>:<32-hex-token>` turn addresses and minted `parent#agent:child` stream IDs. A direct address stays exact; an arbitrary percent-escaped caller value is never widened. A failed event-store read returns 503, distinct from a confirmed missing/expired round (404). The inline Round panel shows a retry control for transport or storage failure and uses the request snapshot when the optional post-tool mirror was not captured.

**Problem.** A web proxy changed the encoded colon in an Inspector turn from
`%3A` to `%253A`; the server compared the resulting literal `%3A` with the
persisted colon, then both state and request fetches returned 404. The browser
also collapsed read failures into the same “mirror missing” text.
**Decision.** Normalize that one minted token shape at the route and preserve
404/503 as separate UI states. **Alternative considered:** dropping the turn
filter could land on a different invocation with the same round number, so it
is rejected.
The hot projector canonicalizes each message once in a fused prefix scan and retains
SHA-256 digests; each row's exact canonical prefix hash controls rebuild integrity.
Raw archives are lazy blocks on the selected diagnostic Turn. Each block retains one
256 KiB window, replaced on paging; metadata names partial/quota/scrubbed evidence.

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
  `PATCH /api/v4/conversations/:id/settings/resolve`; it never copies backend
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

Problem: retained chat helpers and mobile keyboard code duplicated the typed
viewport's follow latch, while the latest button only reached the mounted
window's bottom. Decision: all callers use the existing Surface presentation
port's explicit-latest, conditional-follow and near-latest semantics. Only the
viewport writes scroll geometry; Surface owns logical windows. One controller
commit path merges graph/presentation invalidation, retains forced paints and
fences cancelled callbacks by frame identity. Mounted Turn identities come from
keyed commits, replacing subtree observation and repeated DOM scans. Mobile
visual-viewport events share the existing scoped height owner. Alternatives
rejected: coordinating two latches, inferring latest from physical position,
and adding another observer/scheduler. Verification and measurements live in
`RENDER_CONTRACT.md` and `RESOURCE_CONTROL.md`.

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
semantics are in [TURN_SETTLEMENT.md](contracts/TURN_SETTLEMENT.md).

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
Guidance; explicitly disabling Human Guidance exits Plan. Project attachment is
optional context, not a Chat/Studio capability dial.

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

`check:frontend` verifies generated runtime, styles, conversation and i18n contracts, all literal i18n calls/attributes, actions, TypeScript, and the production build. Retained runtime composition also rejects import-time use of a later top-level lexical declaration; infrastructure registries such as `runtimeScope` are declared before eager composition and cannot depend on manifest concatenation order. Before any new asset enters the live directory, the unpublished minified graph is loaded in one bounded, headless Chromium canary with in-memory API fixtures. Publication requires `tofu:app-ready`, the conversation and folder bootstrap requests, and no unhandled page or fatal boot error. A failed canary leaves the existing manifest and all served assets untouched. The canary defaults `PLAYWRIGHT_NODEJS_PATH` to the build process's `process.execPath`, while preserving an explicit operator value. Problem: Playwright's bundled Node can require a newer glibc than the host even when Vite builds successfully. Decision: reuse the Node already executing the build. Alternative rejected: skipping the browser canary would publish an unverified graph. The Vite manifest carries the locale-source digest; the deployment controller and packaging gates refuse a graph whose language chunks predate the locale source. The ASGI lifespan validates only the complete published graph, which is also the atomic request-serving commit: authoring source edits cannot withdraw the last validated graph during process recovery, and requests adopt new bytes only after a complete manifest publish. Composition tests prove the manifests recreate the checked-in delivery bytes; size budgets apply to authoring sections as well as shipped resources.

## Extension checklist

Locate the owner, change wire contracts first, implement typed behavior with
injected effects, verify public outcomes and cross-boundary failure semantics,
regenerate artifacts, and remove superseded retained behavior in the same change.

## Independent research workbench

Research now enters through `features/research.ts`, independently lazy-routed
from `toggleResearchMode` in `main.ts`. Paper panel composition no longer imports
research owners. `research/api/reader.ts` is the explicit, lazy Paper navigation
port; opening a corpus paper loads the reader, not another research session.

- `research/api/ports.ts`: typed existing task/research API seams; no new transport.
- `research/state/{stream,progress}.ts`: ephemeral projection and optional generated
  `ResearchProgressV1` validation. Task snapshots remain lifecycle authority;
  `research_progress` carries factual activity/counts, never an inferred percent.
- `research/controllers/runtime.ts`: one push attachment and single-flight replay
  polling per current task, submission gating, cancellation confirmation, hydration
  and reattachment. Poll failure retains last confirmed execution state and exposes
  reconnection. A failed cancellation never marks the task aborted.
- `research/controllers/workspace.ts`: revision/CAS edits and research-action
  polling; generation guards fence workspace loads and action callbacks, exit
  clears the timer and retains the local editor draft. External write confirmation,
  bindings/schema hashes, evidence receipts and source digests remain intact.
- `research/components/{view,stable-dom}.ts`: stable telemetry patching preserves
  controls, focus/selection, open disclosures and the separate workspace DOM island.
  Overview, evidence, candidates, experiment and paper use native accessible
  buttons; experiment/paper share one editor and revision owner, not cloned forms.
- `research/pages/session.ts`: Research chrome and enter/exit lifecycle.
- `research/testing/workbench.test.cjs`: focused executable DOM and task authority
  regressions, run by `npm run check:research`.

Source styles remain manifest-composed in `styles/application/03-paper-reader.css`.
New status/navigation/count copy belongs to both source locales. The controller
still uses the existing private feature registry compatibility ports; extracting
all workspace HTML into smaller components and complete action reattachment after
navigation are remaining follow-ups, not a second store or transport migration.

### Conversation workspace projection

**Problem:** navigation-driven server project restoration and draft preservation
coupled an execution target to disposable composer state. **Decision:**
`conversation/application/conversation-project.ts` projects saved roots locally,
with no server `setPaths` call and no draft gate. Send captures that conversation's
complete binding, including explicitly empty roots, before asynchronous resolution.
New-chat attachments belong only to that draft. Late picker/hydration responses
cannot overwrite a newer explicit choice; global status enriches only matching
roots and never selects a project or changes access modes. **Alternative rejected:**
more synchronization guards around a shared current-project authority.
Status-only repaints preserve the navigation generation so pending authoritative
hydration can still update the view; only navigation or an explicit choice
invalidates that response. A cold-open integration test covers the overlap.

Everyday chat has one capability mode. Legacy Air/Pro/Studio selections normalize
to `chat`; `ui/optional-project-attachment.ts` paints the optional attachment action
without changing model, Plan, Goal or Flow controls. Automatic private output
roots are server-owned accepted-attempt configuration, never saved user attachments.
Specs: `conversation-project.test.ts`, `test_conversation_project_restore.py`,
`tests/conversation/test_automatic_workspace.py` and the workspace browser journey.

`conversation/application/conversation-model.ts` applies the same draft rule to models/presets, provider references and thinking depth. Only explicit model/depth choices and send commit selection; navigation/settings capture never do. Cold hydration checks selection identity and navigation generation. Catalog refresh never substitutes unavailable models; legacy preset aliases resolve at paint.

The Evolution studio is a lazy TypeScript workspace loaded from `main.ts` through
the feature/action registry. `application-shell/page-host.ts` owns hash navigation
and teardown; HTML comes from the registered `evolution_workspace` fragment.
Its authored stylesheet is imported by the lazy feature entry, not added to the
base chat stylesheet. Conversation details share the backend goal journal.

Authoring source demand — Problem: preflight read only a CPU/memory-sized thread window and held completed small-file bodies in Futures. Decision: the shared demand executor starts every independent source read; the launch probe sizes idle reuse only. Fixed-size read chunks enter one caller-owned anonymous spool, and Futures carry segment offsets/lengths. Canonical path ordering and SHA-256 framing remain identical; source read errors still invalidate preflight. Real thread publication failure reads once on the caller. Optional spool creation/write/read failure joins readers and uses the existing direct streaming hash path. Any failed hash cancels unstarted work, stops active read loops and joins before file close. Alternatives: larger windows still delay sources; retaining every response body inflates RAM; duplicated/unowned readers lose cleanup and freshness. Rendezvous, exact framing and physical I/O-fault tests verify the boundary. Measurement: a controlled 32-file / 2,097,152-byte rendezvous with idle hint 1 completed in 14.76 ms, matching the sequential digest and retaining zero threads or spool descriptors after return. Transient spool contents live only until the hash/fallback completes.

## Composer translation wait receipt

**Problem:** send translation was represented only by an assistant placeholder, conflating provider execution with command acceptance and storage waits. **Decision:** `conversation/application/composer-translation-status.ts` projects a compact, wrapping receipt inside the composer for send and manual Turn translations. The existing demand-scoped presentation ticker owns its one visible-demand timer and visibility listener; settlement, navigation away, and composition teardown release scheduling. Only pending operations and one terminal receipt are retained, never history. Elapsed seconds mean browser waiting (queueing/translation/saving/poll delivery), not provider execution duration. Actual returned model labels are shown on success; missing labels remain explicitly unknown. Cancellation and failure are distinct, terminal duplicate deliveries are idempotent, and task IDs fence stale terminal pushes. No transport or durable Turn write is added. **Alternatives rejected:** showing the cheap-pool candidate as the actual model is misleading; calling the whole HTTP round-trip model execution time hides storage contention. Provider execution duration for historical turns is unavailable and is not fabricated. Specs: `composer-translation-status.test.ts` and `tests/visual/test_visual_composer_translation_status.py`.

### Active Goal/flow progress context

**Problem:** visible phase rows can keep the original orchestration root running
while a later worker iteration owns conversation `livePhase`. A generic waiting
placeholder on the earlier root hides ongoing work from readers remaining there.

**Decision:** `presentation/view-model/live-turns.ts` derives read-only iteration
context from the newest active durable main-lane phase. An earlier running root
receives a summary only when both nonempty `runId` and current attempts' physical
`taskId` agree. Completed virtual users and settled phases never participate.
The newest worker retains live-phase ownership; root context delegates to
`presentation/live-phase.ts` for the same localized labels, tools and model route
as normal chat, without assigning the child phase to the root attempt. Copy says
“Run ongoing”; ordinary single-turn waiting and storage-warning priority remain.
`conversation-activity.ts` is the sole sidebar verdict selector for all modes.
`goal-run-affiliation.ts` joins both sidebar conclusions and run notices, fenced
by real human input; virtual users never acquire human authorization.

Existing launch/snapshot/card normalization enriches this summary; nested swarm
producer IDs are not physical Goal execution IDs. Skip settled launches and
collect at most eight active/pending summaries. Names, admission wait and latest
tool receipt stay separate evidence, never inferred blockers. Use escaped plain
text and EN/ZH ports; storage warnings win. No polling, Turn writes or new state.

**Alternatives rejected:** transferring phase authority to an older root;
separate Goal labels/status caches; cross-run iteration joins; silence verdicts.
Terminal segments and rich rounds stay independent. Historical VU duplicates
use `terminal-segment-presentation.ts`: only a superseded `text:terminal` with
exact machine-token sanitizer equivalence to a canonical completion is hidden.

Executable evidence: `conversation-activity.test.ts`, `live-phase.test.ts`,
`view-model/{goal-progress,terminal-segment-presentation}.test.ts` and Chromium
`tests/visual/test_visual_goal_progress.py`: run/actor fences, phase parity,
rich-round retention, narrow EN/ZH context, sidebar rate limits and settled cleanup.
