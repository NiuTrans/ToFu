# Frontend architecture
Responsibility: source ownership, generated delivery boundaries, state
lifecycles, and the rules for extending the browser application. Boot status uses a non-modal dock with expandable, selectable phase diagnostics. `boot-splash.ts` preserves unchanged text nodes and phase rows during polling; rebuilding unchanged content was rejected because the 800 ms poll erased native selection. `boot-readiness.ts` combines the existing boot and restart owners at composer admission (button and Enter), without blocking drafts or reading. The restart observer retains identity/approval ownership; its dock offers explicit reload after readiness to preserve drafts.

Task-time reads follow visible demand and explicit invalidation: forced paints amplified unchanged reads; paint force now preserves settled reads while server wake hints refresh them. A throttle was rejected because it still amplifies requests and delays updates. The machine-readable ownership and shrinking-debt authority is
`contracts/frontend_conversation_architecture_v1.json`. The one-hop map for
conversation browser work is `frontend/src/conversation/README.md`. Update status cards use component-scoped `--space-4` sibling spacing in `02-update-shell.css`: the local-code restart notice and release verdict remain separate without global badge margins; `tests/visual/test_visual_update_spacing.py` verifies all themes and narrow layouts.
## Optional plugin page delivery

Problem: optional pages depended on deleted classic API/vendor assets and
implicit global initialization. Decision: `frontend/authoring-inputs.json`
declares host entries once for Vite, publication and server rendering.
`plugin.ts` loads shared fonts and hands `PluginHostV1` ports to the same-mount
module declared by the page. `plugins/ports.ts` composes the existing endpoint
registry/transport and locked Markdown parser/sanitizer; `plugins/page.ts`
awaits `mount(host)` and exposes loading, ready and visible failure states.
The async `tofu_plugin_assets()` Jinja helper uses the existing manifest owner.
Trading binds these ports before its feature modules and initialization run.
Ordinary trading JSON calls use the transport's typed error boundary, keeping
backend detail and request IDs; the former private parser reduced v4 failures
to an HTTP status. Raw responses keep body ownership with streaming callers.

Alternatives rejected: loading the full application taxes an independent page;
copied vendor files fork dependency updates; another API facade forks prefix,
envelope and error behavior. Tests exercise root/proxy mounts, actual packaged
assets, Markdown sanitation, fonts and startup failures in Chromium.

Build accounting follows every entry's transitive static imports, deduplicated
by emitted file. Only declared immediate dynamic roots join that startup cost.
Problem: limiting static traversal to the main entry hid shared dependencies of
other entries. Decision: correct both required publication and optional budget
accounting without changing ceilings. This exposes real entry cost; moving a
dependency into a shared chunk is not a performance saving.

Problem: grouping conversation presenters in a manual chunk also absorbed the
shared error normalizer. Admin and plugin entries then downloaded and evaluated
conversation code through their ordinary HTTP transport. Decision: let Rollup
derive chunks from the actual entry and import graph; source ownership stays in
the typed modules, independent of emitted filenames. Shared modules retain one
instance, and a standalone entry cannot initialize an unrelated presentation
owner merely by importing a shared dependency. `frontend_chunk_ownership.test.cjs`
builds and executes all three entries to verify that boundary and later chat
initialization. Existing build accounting still includes every required import.

Alternatives considered: restricting manual chunk membership fixes dependency
absorption but keeps unnecessary fragmentation and extra compressed bytes in
the full graph; deleting the grouping improves all three entry measurements.
Copying error/transport helpers into standalone pages would fork their contracts.

Publication keeps candidate admission separate from previous-generation
retention. Problem: applying the new required entry set to an older manifest
discarded its pin when a host entry was added. Decision:
`scripts/vite_asset_manifest.mjs` validates candidates against the current entry
contract and snapshots previous generations against their own declared entries.
Both validate every reference, asset path and file, including URL workers; an
incomplete or malformed prior graph retains the existing refusal behavior.
The complete previous graph stays pinned under the unchanged retention policy.
`scripts/vite_asset_publication.mjs` awaits graph, budget and browser admission
before copying assets, then commits the serving manifest after every asset is
present. Filesystem tests cover refusal at each admission phase and copy failure;
the serving manifest and old assets remain intact on these failures.
Alternatives rejected: requiring new entries in old graphs makes migrations
lose old-tab resources; accepting unchecked prior manifests hides corruption.

## Background agent dock

**Problem:** composer popovers obscured agent execution evidence.
**Decision:** `agent-sidebar-dock.ts` renders conversation-level background activity inside
the current main round input's existing information column, below model/tools/workspace.
Round inputs are the user-role Turns the round directory counts (human, critic and
virtual_user). A swarm settlement or autopilot continuation opens its own round, and the
surface mounts only the current round. Hosting on human Turns alone left that round's
visible rail empty while agents ran.
A stable context-content slot prevents fact repaint from destroying agent disclosures.
Narrow panes use that Turn's existing context fold region, not a floating panel.
Each agent row answers what the agent does, its state and how far it has run: a state glyph
in the tool rows' outcome inks (live dot, queued ring, check, cross, ban, attention
triangle), the objective's opening line (`swarmAgentBrief`, shared with the swarm card's task
brief) clamped to two lines, then the localized state word, the model round count and the
agent ID as a quiet monospace identifier. The count is the worker's `round`, as live progress,
status polls and the durable snapshot (`master._build_agent_snapshot`) all report it, so a
finished or reloaded row keeps it; a row without that evidence shows no count. An
agent without a known objective reads its ID as the purpose line. Rows and counts remain
readable after foreground settlement; only execution details collapse. The latest authoritative main input selects the host;
unmounted or foreign Turns are never replaced by an older history guess.
Keyed native disclosures reveal the canonical bounded swarm execution card body through
`agent-sidebar-dock-projection.ts`, with no note above it; the card marks omitted calls and
truncated previews in place. Activity enriches
an exactly run-bound or uniquely unsettled session owner without mutating Turns or
reopening evidenced terminal states. Ambiguous reused identities remain separate.

One dock update shares its selected Turn/activity input with run communication.
`agent-sidebar-dock-projection.ts` resolves assignment evidence before creating
deferred renderer closures: invocation-local run/session/agent indexes narrow
candidates, while the existing execution/attempt and missing-stamp predicates
retain final authority. No index survives through a lazy card renderer.
The retained `_recoverSwarmAgents` owner in `ui/streaming_swarm_panel.js` reads
snapshots/launch handles/sibling results and returns new agent objects; it does
not rewrite frame or round identity, so invocation-local identity reads remain
stable across recovery. Work counts, memory scope and paired measurements live
in [Turn rendering performance](TURN_RENDERING_PERFORMANCE.md#swarm-receipt-projection-work).

The detail renderer receives the same merged roster and exact-run messages; updating only headers would leave open details stale.
Open rows remain mounted on completion until the reader closes them.
**Stop (owner report 2026-11-02).** Problem: once the spawning turn settled, nothing in
the browser could stop a background agent the owner knew was no longer needed. Decision:
an opened running row offers Stop beside its conversation control (`.agent-dock-actions`).
The projection grants it only while the owner snapshot lists that exact run's agent running
(`liveStop`): canonical Turn evidence alone, a settled verdict or a queued agent (no live
invocation to signal) shows none. The port calls the [swarm agent stop](contracts/API_CONTRACT.md#swarm-agent-stop)
route; an accepted signal keeps the button busy until the snapshot settles the agent. The
route's own `swarm_agent_not_found`/`swarm_agent_not_running` answers re-arm it quietly;
any other failure, including a server without the route, re-arms it with an error toast.
Rejected: a control on the collapsed summary (a control nested in the native disclosure
summary, one misclick from ending a long run, against the quiet row); reusing the
whole-swarm abort route (it stops every sibling and deletes the durable session instead of
settling one agent); optimistic local status (the snapshot stays authority).
Communication lives in each exact-run agent card, collapsed independently from tool
execution; all-run communication is a collapsed footer rather than a second chat feed.
A fold, and a run's section inside the footer, appears only once it holds a message;
empty or incomplete history is not narrated.
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
existing turn information rail. Keeping the human-only host and falling back to the last
mounted human rail would place the section off-screen in an earlier round. Automatically opening tool details taxes history rendering.
Rows led by the agent ID over `running · round 91` (2026-11-01 owner report) named neither
the task nor any state the glyph does not already carry. The round count then moved into the
opened detail; the 2026-11-02 owner follow-up asked for it while collapsed, so it returns as
its own quiet field after the state word, never folded into the state label or leading the row.
A one-line row with a trailing state cut the purpose to a few words at rail width;
untruncated objectives turn a dozen agents into a wall of instructions. A note above the
detail ("recent records … not a full transcript") and "no messages" / "history unavailable"
notices (2026-11-02 owner report) explained evidence instead of showing it.
Another poller, fabricated full transcripts and per-message activity chrome duplicate owners.
Surface commits and the rail track pass (info-rail.js) remount the section into the rail's
sticky card from the same snapshot without reads.
Specs: `agent-sidebar-dock.test.ts`, `agent-sidebar-dock-projection.test.ts` and
`tests/visual/test_visual_agent_sidebar_dock.py`.

## Folder configuration and workspace defaults

Problem: the create editor used generic chrome and centered full-path buttons,
producing an oversized single-column path wall unlike Project CO-PILOT, and its
read-only directory picker (location field plus Go/Parent/Home/Use buttons)
could neither filter, create nor delete folders.
Decision: use the native kit's `appearance: 'project'` shell with the Co-Pilot
workbench layout: identity on top, the shared server-folder browser (below)
beside a rail of default roots and searchable recent projects; narrow layouts
stack them. Opening reads the browser listing (primary root, else `~`) and the
recents once each; nothing is written before Save. Root rows are compact: a
primary star toggle, name plus start-truncated full path (exact path in the
DOM and tooltip), read-only toggle, up/down and remove. Recent rows add on
click, report missing paths, and can locate the path in the browser. Field
content scrolls independently from the save/cancel row. Empty Add errors clear
on editing or valid name-only save. Deleting a browsed directory that is a
default root unstages it with the confirmation and restores it if the server
refuses.
Rejected: copying legacy modal DOM would lose native focus/lifecycle ownership;
truncating stored paths would lose directory identity; disclosure-gated recents
hid the primary pick surface behind an extra click.

Browser specification: `tests/visual/test_visual_folder_settings.py` covers
three themes, Chinese/English, long paths, 390px layout, native focus/Escape,
name-only creation and directory selection using the existing local HTTP fixture.
API/store journeys remain in `tests/e2e/test_folder_settings.py`.

### Shared server-folder browser

Problem: the Co-Pilot pane rendered its browser as HTML strings carrying global
action names from the retained runtime, so the typed folder editor could not
reuse it and kept a weaker picker; both looked unlike the surrounding cards.
Decision: `frontend/src/features/project/folder-browser.ts` is the one browser.
It owns breadcrumbs, a bounded name filter that also opens typed paths (`/`,
`~`, drive letters) on Enter, inline folder creation that lands inside the new
folder, an inline delete confirmation with optimistic removal and server-truth
refresh, hidden-folder toggle, roving keyboard focus, and the server's problem
`detail` on refusal. Reads go through a host-owned `ProjectBrowseCoordinator`
(session cache, cancellation, stale fencing); a cached listing the server can
no longer confirm is dropped with its error, and a deleted folder's own entry
is invalidated. Hosts keep workspace membership and pass ports (`isAdded`,
`onAdd` returning an optional refusal the browser shows in place, `onDeleted`
returning a rollback); the Co-Pilot section binds
`_mpFolders`, relink mode and its drop zone, the folder editor binds its draft
roots. Before reading those roots (Co-Pilot Apply, folder Save) a host awaits
`settled()`, which covers in-flight create/delete commands and their rollback
but not the follow-up listing read. `cancel()` (modal close) leaves the last
listing rather than a spinner; `relabel()` follows a UI language switch. Enter
that commits an IME composition never navigates, creates or adds. Styles live in `10-project-browser.css` on `--project-*`
roles; a host may retune only `--folder-browser-frame`.
Rejected: a "pick mode" in the Co-Pilot modal (couples sidebar defaults to the
active conversation's project apply path); two controllers sharing only CSS
(they drift); HTML-string rows with global actions (not reusable by typed
dialogs and they force runtime action exports).
Specs: `folder-browser.test.ts`, `folder-settings.test.ts`,
`tests/frontend/test_frontend_project_folder_browser_bridge.py`.


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

**Problem:** with programs on by default, a real-app probe showed the
`execute_program` row rendered as "Incomplete · Result: null · counters
unavailable" until the program started (only `searching` counted as running,
and a call still streaming its code had no round status at all); running
cards repeated "counters unavailable"; expanded cards repeated the result as a
prose preview plus tool chips; failures dumped the raw error object into the
header; and the result's lazy-load body drew a second rail, misaligned with the
code and wasting narrow-screen width. **Decision:** gateway rows share the
canonical active tool statuses (`isActiveToolStatus`), and the classic
renderer treats a round-less call as pending only while its Turn is live
(settled history keeps "Incomplete"); a running card leads with settled child
calls, never reports missing counters and never renders an in-flight
placeholder as a result; expanding a settled card opens its result first (code
and Technical details stay collapsed; gateway history stays closed for its
lazy body); failures read `code: message` through the shared failure-text
owner; the body carries no tool chips or preview line because the header
already names the tools; the result body sits on the card spine. Like other
active tools, the Round outline opens a running card on first paint.
**Alternatives rejected:** a separate program status vocabulary drifts from
tool grouping; treating every round-less call as running would leave
never-executed calls of settled Turns spinning; keeping the preview line
duplicates the result in proportional type; removing the result body wrapper
breaks lazy loading of gateway history.

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

Standalone math dependencies share the npm lockfile with the application.
Problem: independently maintained artifact files and export CDN literals can
keep an older renderer after the application dependency is upgraded. Decision:
`scripts/frontend_vendor_assets.mjs` validates the installed lockfile version,
captures its renderer, stylesheet and complete referenced font closure, then
generates the static delivery files and `lib/http_transport/generated_frontend_vendor.py`.
The artifact adapter is authored in `frontend/src/artifacts/`; its static copy
is generated. Report exports consume the generated versioned CDN URLs and
SHA-384 integrity values. They still require CDN access or a browser cache.

`npm run generate:vendor` updates declared outputs; the frontend gate and direct
build both reject drift. Generation preflights inputs, atomically replaces each
changed file, publishes metadata last and leaves unowned files alone.
`lib/http_transport/frontend_vendor.py` gives diagnostics exact byte verification without a
Node installation. Size thresholds and separate manually pinned versions were
rejected because neither proves that the tested dependency is the one served.
DOMPurify is a runtime dependency because `vendor-runtime.ts` bundles it into
the application. The artifact/export CSP and opaque-origin sandbox remain the
delivery boundary; dependencies do not gain access to application state.

Public font responses in `lib/static_serving.py` include anonymous CORS headers
on full, range and conditional responses. Opaque artifact origins need that
permission to use fonts; allowing same-origin frames or relaxing script CSP
would unnecessarily expose application state. Other static assets do not gain
this header.

Report Markdown math belongs to `lib/paper/report_markup.py`. Its preprocessor
runs after whitespace normalization and fenced-code extraction, before HTML
block parsing, and uses Python-Markdown's own HTML stash. The previous external
control-character placeholders were erased during normalization and leaked as
`MATH0` text. A separate placeholder replacement protocol was rejected because
it duplicates the parser lifecycle. Formula contents are escaped as text;
the existing report HTML/attribute policy and route error handling remain.

The standalone closure contains all three font formats referenced by upstream
CSS: 64 files / 1,380,831 bytes, replacing 23 files / 565,527 bytes. This is a
complete static dependency graph, not a cache or a new runtime retention policy.
Verify generation/drift, dependency trust handling, actual preview/export math,
fonts and sandbox isolation through the frontend vendor and browser specs.

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

frontend/src/i18n/locales/{zh,en}.json (authored sources)
                    │ translate_i18n_locales.py (LLM translate → review → validate)
                    ▼
frontend/src/i18n/locales/{ja,ko,fr,de,ru}.json (derived) + languages.json
                    │ gen_i18n_contract.mjs
                    ├────────────────────────┐
                    ▼                        ▼
  contract.generated.ts +         generated compact JSON
  languages.generated.ts                     │ Vite ?url
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

**Capability and conversation styles:** Problem: one 81,436-byte sheet mixed
browser setup, Local Control, Memory, branches, references, agent evidence,
toasts and reasoning controls. Decision: ten explicit owners (largest 18,987
bytes), with unchanged copied rules admitted by exact manifest hashes.
Memory body and Local Control styles use their existing feature imports and
Vite's before-visible CSS admission. Memory frame/palette rules remain critical
before shared mobile rules; shared modal links and capability toggles have a
critical owner. Memory's responsive body rules move with its panel, and Skills
toast motion belongs to its Settings consumer. Unproduced Memory-toggle and
lazy-sentinel rules are removed. Critical application CSS falls 1,400,904 →
1,369,749 bytes (298,597 → 291,786 gzip); Settings gains 115 raw / 40 gzip bytes.
Deferred styles still count toward unchanged source budgets. Alternatives
rejected: deferring the entire Memory sheet overrides the shared mobile
full-screen frame; another loader duplicates feature admission; global
allowlist renewal permits new raw declarations. Real-browser specifications
cover all themes, mobile geometry, CSS loading, reuse and failed-load recovery.

**Application interactions:** Problem: the 1,733-line retained boot owner mixed DOM gestures, uploads, sizing and unowned resources. Decision: seven typed `application-shell/interactions/` owners (largest 69 lines) receive explicit ports; retained `main.js` is 1,391 lines and preserves boot order. Shared `lifecycle.ts` releases completed/cancelled tasks and fences teardown deliveries; partial installation rolls back owned resources. Alternative rejected: more classic fragments preserve ambient dependencies; parallel timer registries duplicate lifecycle authority; whole-entry deferral delays required input. Resource measurements and delivery tradeoffs live in `RESOURCE_CONTROL.md`; `check:application-interactions` plus shipped-app browser contracts verify behavior. The 1,747-line toolbar is now 1,164 lines. Model-picker UI is separately owned by `application-shell/model-picker/`: canonical projection, display planning, row/fold rendering, search index and scoped provider popup; retained toolbar actions keep persistence authority.

## Refresh critical path

**Problem:** waiting for the full runtime before fetching the selected language
serialized two large downloads, while idle features began network work before
the shell was ready. Uncompressed fonts also competed for the same connection.
**Decision:** the small `main.ts` entry starts i18n and `application.ts` together.
The application owns runtime composition, feature routing and the existing
`tofu:app-ready` event; idle feature preloads are scheduled after that event.
Locale, module-load and startup failures still reach `tofu:app-failed`.
`authoring-inputs.json` declares required startup imports, and both build and CI
budgets count these roots and their shared static dependencies once under the
existing ceilings. A dynamic import changes scheduling, not budget ownership.

Self-hosted font delivery uses lossless WOFF2 generated from the retained TTF
sources: the nine converted files total 259,548 bytes instead of 771,600 bytes.
Glyph maps, glyph order, advance widths and vertical metrics are preserved;
the existing font-display policies and first-paint preload selection remain.
**Alternatives rejected:** raising size limits hides startup cost; embedding both
locale dictionaries loads unused language data; waiting for localization before
requesting the application preserves the waterfall. Tests: `main.test.ts`,
`test_frontend_budget.py`, the shipped-graph canary, and browser refresh timing.

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

  Reload selection keeps the exact session/last-active ID even when the first
  metadata catalog omits a newly durable conversation. The retained boot adapter
  delegates that ID to ordinary `loadConversation` and its generation-fenced
  `project-conversation-navigation.ts` resolver. A deleted/denied target or a
  transient lookup failure remains an observable open failure, never permission
  to substitute another chat; recency is only a no-saved-ID fallback. A second
  restore store or waiting for catalog freshness would duplicate identity ownership.
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

  The standalone Research surface is a full workbench, not a Paper-reader sub-panel. `research-runtime.ts` owns the one live task stream, bounded event replay, poll/push teardown, durable artifact hydration and terminal-quality projection. `research-view.ts` is presentation-only: it renders the five visible stages (harvest, survey, ideate, evaluate, package), ranked experiment briefs, independent review decision, rejection audit, and the reproducibility ledger. A researcher can inspect the running trajectory in the **Live evidence trail** (tool-round projection); after completion the same page retains the corpus, full survey/gap map, usage ledger and review revision queue. Starting a new direction detaches browser polling/push through the runtime disposer but does not cancel the checkpointed server job; explicit **Stop run** is the only cancellation action. Recent directions come from the durable research list, so refresh/reopen never depends on the in-memory task registry. While a job is still in flight, entering Research mode re-attaches to it through `GET /api/v4/tasks?kind=research` (owner-scoped list → snapshot → poll/push re-attach) instead of stranding the researcher on the landing page; a job that finished in the meantime projects its terminal state and hydrates the durable archive. The retained Paper presentation islands are also demand-loaded: report and reader markup live only in the manifest-owned `paper-reader-presenters` runtime, while podcast and video markup live in `paper-media-presenters`. `frontend/src/features/paper.ts` installs both before the typed panel owners. The typed report runtime owns start/regenerate, provisional Stop handoff, polling, and reopen policy. Per-kind generations fence stale success and failure continuations even for two starts on the same paper; cache-hit starts reuse the canonical local report-adoption path. Report lookup, terminal replay and server-confirmed title repair project server-owned facts locally without writing unrelated library metadata back; explicit library edits retain their own persistence owner. The shared `report-article/metadata.ts` completion view reconciles recorded model, cost and token fields on initial paint and subsequent event/snapshot paints; Deepen uses the same owner after its source fence. Equal metadata markup preserves existing article and tag nodes. One explicit cache-resolving lookup preserves live-task precedence and selects the preferred/fallback report server-side. The backend projects that hit from one bounded owner-scoped Sidecar aggregate; only the selected language's additive artifacts cross the process boundary. Rebuttal drafts retain at most 32 papers and 40,000 characters per paper, matching the request/backend limit. A known paper starts by hash only; the server reads at most its 120,000-char prompt source only after live/cache misses. The browser retries with bounded text solely for the explicit pre-dispatch `paper_source_required` 400, after rechecking the same generation fence; ambiguous failures are never replayed. Late owner ports and markup action receivers use the private `featureRegistry`. Mutable retained values use its explicit `readLiveRuntimeBinding` / `writeLiveRuntimeBinding` state port, because the owner override cache must not hide a later paper switch. A raw lexical-global dependency is not a compatibility seam.
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
- The composer has one compact **conversation bar**, owned by `features/conversation-bar/{project-context,context-meter,turn-navigation}.ts` and `25-conversation-bar.css`. Project location is the left segment of a content-sized project cluster: Auto stays beside the names. Idle width lives in the strip between Auto and the meter, not inside a stretched trigger. Names that fit that leftover (strip minus Auto/meter/turns/chrome) join with a middle dot; `+N` only counts names that do not. Labels are measured at `max-content` so ellipsis stretch cannot collapse a fit to `+N`. A ResizeObserver on the strip/bar (and sibling mutations) refits the list and is disconnected on destroy. A literal context meter and fixed previous/count/next follow it. There is no pet, scene, pocket, separate project card or ornamental sphere. Native buttons open shared nonmodal `disclosure.ts` panels for full paths and permissions, context counts/compaction actions, and a TurnStore jump list. Named container queries compact labels by actual pane width, not only screen width. Narrow panes move Auto/Manual into project details; the bar never wraps. Panels become fixed before anchor measurement so body flex cannot shift the UI. Escape returns focus; native outside click/focus wins, and resize/scroll/ hidden-page dismissal releases the panel's temporary listeners. Retained project/context/turn adapters inject existing command and accounting ports, not another store. Exact rank jumps hydrate stable IDs through the v3 coordinator, then the Surface mounts its bounded 80-turn window and aligns through the viewport owner. Instant alignment and one cancellable layout correction replace competing native smooth scrolling; lazy-history frames are invalidated before a jump. The directory shows the authoritative round total and mounts at most 50 rows with numeric/first/last access, not loaded-body arithmetic. Late history responses are fenced by view generation; project permission/clear completion shares the project controller's navigation generation. Manual compaction is single-flight per page and its progress is visible only in the originating conversation. UI previews cap text at 24,000 characters; durable summaries stay server-owned. Specs: `node --test tests/context_meter.test.cjs tests/turn_navigation.test.cjs tests/project_location.test.cjs`, `tests/conversation/test_conversation_bar_browser.py`, Surface window/reveal tests and `tests/visual/test_visual_turn_jump_scroll.py`.
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
- The local Knowledge Workbench lives in Settings → Knowledge Base, without a
  top-bar or mobile-sheet launcher. Its existing corpus, retrieval and access
  controls are embedded in `frontend/src/settings-panels/knowledge.html` rather
  than duplicated as a second modal. `features/settings/knowledge-tab.ts`
  demand-loads the manifest-owned `knowledge-presenters` runtime on first visit,
  gates delayed publication on the current Settings session, and deactivates
  the workbench on tab switch or Settings close. Revisiting refreshes it;
  closing stops its scheduled polling. `openKnowledgeBase` is owned by the
  Settings domain and resolves to `setting:knowledge`; no catalogue request or
  polling timer exists before
  demand. The chat drop owner reads `_tofuKnowledgeModalOpen` live from
  `runtimeScope`, while the lazy presenter is the sole writer, preserving drop
  arbitration without a browser global or captured state.
- The Project folder workbench is the manifest-owned, demand-loaded `project-presenters` runtime behind `frontend/src/features/project.ts`. Core project state, bar rendering, restore/SSE reconciliation, and background rescan remain retained because they run without the modal. A conversation restore carries its primary recent-path intent inside the same `setPaths` request; an explicit workbench apply carries a primary-first prefix of at most 32 selected roots without truncating the authoritative project list. The browser owns no second recent-path write, and background/status reconciliation omits the optional intent. Write approvals, subprocess stdin, and apply-code confirmation remain in the small retained `execution-interactions.js` section, so coding actions neither wait for nor retain the folder browser. The generated presenter imports its typed browse coordinator, the shared folder browser (`features/project/folder-browser.ts`) and static icon owner only on demand; the section only binds the staged workspace, drop zone and remote-device group to that browser. Mutable conversation/project authority crosses one frozen `ProjectPresentationShellState` live accessor object; individual values must not be captured through `featureRegistry`. The retained clear path probes the optional `ProjectModalPresentation` cleanup owner and never invokes the open/close feature stub, preventing an idle clear from fetching the workspace.
- Local Control's modal, capability status probes, download/diagnostic
  presentation, three-second open-modal poll, and browser-assisted desktop
  relay are the manifest-owned, demand-loaded `local-control-presenters`
  runtime behind `frontend/src/features/local-control.ts`. The retained
  `local-control-state.js` section owns only the merged permission badge,
  which counts the desktop switch (the browser is always on and has no
  switch), and reads reachability through the optional live
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
- Debug Panel and Request Inspector DOM, task/payload/trace reads, live TurnStore subscription, poll and six Inspector CSS owners belong to the demand-loaded `diagnostics-presenters` entry. Native `diagnostics-message-disclosure.ts` supplies UI disclosure buttons. CSS preload completes before markup; closed panels load neither these sheets nor the runtime. The retained
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

  **Problem:** the flat feed rebuilt every message on refresh, discarding
  selection and nested disclosures; the separate streaming updater compared
  lengths/counts and could leave equal-size tool arguments or text stale.
  **Decision:** `diagnostics-message-reconciliation.ts` owns complete JSON wire
  comparisons and message-node reconciliation for both presenters. The existing
  `createBlock` remains the sole card renderer, injected through a narrow port;
  that port retires when the renderer moves to native Diagnostics. Identity
  matching reserves exact versions before changed duplicate occurrences, and
  refresh updates ordinals and lazy-render references. Unchanged nodes retain
  their bodies; changed cards re-render all badges and evidence while preserving
  outer/raw disclosure state and focused message controls. Surviving text endpoints
  preserve selection across moves; replaced evidence never inherits a selection.
  The legacy streaming caller explicitly keeps its positional update policy and
  structural-reset guard. Initial/inline views reset, while feed dividers,
  warnings, relative time, trace ownership and scrolling stay with the presenter.
  **Alternatives rejected:** length/count or object-reference equality misses
  real edits; fingerprint-only equality can collide; a second card renderer
  lets tool/compaction/brain presentation drift. Whole-feed replacement loses
  reading state and performs work proportional to all visible card markup.
  Versions are weakly keyed by card and never placed in DOM attributes; reads,
  polling and task/storage authority remain with their existing owners. See
  [RESOURCE_CONTROL.md](RESOURCE_CONTROL.md#request-inspector-history-working-set)
  for the measured workload and memory tradeoff.

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

  **Problem:** trace and detached-round continuations identified a selection
  by task/conversation strings; selecting another round or reopening the same
  task could let an older response replace current evidence. A completed poll
  could also rearm its timer after close, and an older live payload read could
  replace a newer cached snapshot after the task settled.
  **Decision:** `inspector-session.ts` owns the disposable opening, selected
  pane and existing polling/store-subscription resources. Each pane has its
  own identity and content node; replacement, return to feed and close retire
  it and release its DOM reference. `inspector-details.ts` captures that pane
  before trace/detached reads; loading and retry retain a working back button.
  Only its current continuation publishes. `inspector-payloads.ts` owns the
  existing forty-entry FIFO and pending cache-publication rights by exact
  task/kind/turn/round address. Later reads or live snapshots retire earlier
  publication rights. Live reads still bypass historical cache; callers retain
  their own results, while only the latest observation can update the cache.
  **Alternatives rejected:** task-ID equality cannot distinguish reopenings;
  cancelling a timer before an awaited poll finishes cannot stop rescheduling;
  ignoring stale DOM paints alone leaves the payload cache poisoned. Folded
  and full reads contain the same complete messages, so missing fold metadata
  continues through the existing exact previous-round diff rather than a
  second payload cache. State/request preference, optional-diff failure,
  retryable storage errors and the common message renderer keep their owners.
  Verify `test_inspector_read_lifecycle.py`, retained feed/inline contracts,
  actual browser close/reopen/retry controls and Inspector reading journeys.

  **Problem:** feed navigation kept an array index across history changes,
  permanently bound its scroller, and rebuilt focused jump rows on every
  refresh. Each scroll queried the full divider list once per Turn.
  **Decision:** `inspector-navigation.ts` owns selection by Turn identity
  (task identity for older unlinked rows), one canonical ordinal projection
  per refresh, and one index of the currently rendered dividers. Its shared
  lifecycle scopes retire scroll/highlight work on close, pane replacement
  and scroller replacement. Pending task landings wait for an actual divider;
  unavailable task addresses retain the newest-Turn fallback. A new opening
  starts at the newest Turn, while refresh keeps the selected identity.
  `inspector-navigation-list.ts` preserves keyed rows and focus, delegates
  placement/dismissal to the existing anchored disclosure, constrained to the
  drawer's visible horizontal bounds, and gives row
  keyboard activation the same feed jump as a pointer. The nested Turn chip
  still navigates the conversation, independently of its feed row. Detail
  panes suspend feed-only controls and restore them on return.
  **Alternatives rejected:** retaining array positions silently retargets a
  selection after history insertion; a second popover or scroll scheduler
  duplicates existing owners; rebuilding rows discards focus. Wire loading,
  history limits, common message rendering and diagnostic detail ownership
  are unchanged. Specs: `test_inspector_navigation.py` and its browser controls.

  **Problem:** retained Trace markup silently omitted depths above three,
  treated zero timestamps as absent, and invented a time axis for receipt-only
  responses. Its standalone stylesheet loaded on every page even while closed.
  **Decision:** `inspector-trace-projection.ts` uses the generated
  `TurnTimingTrace` vocabulary to project all represented depths, source and
  coverage notices, exact server summary buckets, status intervals and browser
  receipts. A server axis requires finite nonnegative start/total values and
  a summary; zero remains valid. Missing-clock responses keep their evidence
  without a server axis. `inspector-trace-view.ts` renders through common
  `safeHtml`; Trace CSS is owned by the diagnostics entry and uses theme tokens.
  **Alternatives rejected:** raising the fixed depth limit loses later nested
  evidence; allocating every intermediate depth makes sparse data expensive;
  another clock or reducer would invent server evidence; a second CSS loader
  duplicates feature preload. Original-pane reads, back navigation, retention
  and server folding keep their existing owners. Specs:
  `test_inspector_trace_projection.py` and `test_inspector_trace_browser.py`.

  **Problem:** inline tool lookup interpolated task IDs into CSS selectors,
  lost the matched Turn/phase, and picked the first row when several phases
  shared a round. Removed anchors could also mount invisible panels. Pending
  reads retained removed panel/body/title nodes; the close control declared
  a button role without native keyboard activation.
  **Decision:** `inspector-inline.ts` owns one mount and read identity, with
  exact `InspectorRef` task/round/turn comparison. A connected, matching entry
  wins; unstamped positional anchors retain the entry's explicit task/round
  arguments and their own phase. Lookup retains the matched phase, and ambiguous phase-less targets use the
  existing detached detail owner. Removed origins are resolved again. Mounts
  and pending reads carry weak panel/origin witnesses, so chat DOM replacement
  needs no additional observer. Each read checks its original mount before
  optional diff work and publication; removed retry/close controls cannot act
  on a successor. Inline opening still returns immediately after mounting.
  `inspector-inline-view.ts` uses shared native buttons/icons, restores focus
  through the owner on close, and shows loading immediately on retry. Payload
  state/request preference, exact prefix diffs and tail fallback retain their
  existing owners; the common Debug renderer still displays all evidence.
  The existing six-message / 300-KiB auto-expansion threshold controls initial
  disclosure only and never truncates payloads. Tool schemas remain in full
  drawer details.
  **Alternatives rejected:** selector escaping alone cannot resolve phase
  ambiguity; connected-node checks alone still retain DOM while awaiting;
  a new observer, payload cache or renderer duplicates existing responsibilities.
  Specs: `test_inspector_inline_owner.py`, structured-body and inline contracts,
  and `test_inspector_inline_browser.py` for real keyboard/GC witnesses.

  **Problem:** feed success respected the selected pane, but loading/failure
  branches could replace Trace or detached evidence. Returning before a first
  read settled showed an empty feed; copy metadata could still name the previous
  conversation or tools. Retained markup also duplicated notice/button creation.
  **Decision:** `inspector-feed.ts` owns one selected feed view and its publication
  rights. Loading, failure and success share the session's pane gate; background
  reads still settle through `diagnostics-wire-controller` while details remain
  selected. Returning to the feed projects its actual pending/failed/ready state.
  Each rendered action belongs to its original presentation and content node.
  `inspector-feed-view.ts` creates dividers/coverage notices and delegates every
  message to the existing reconciler, preserving expansion and scroll behavior.
  `inspector-pane-view.ts` shares native controls and clears obsolete copy metadata
  when showing loading or failure. Inline retries reuse the same control factory.
  **Alternatives rejected:** separate guards on each retained error branch leave
  multiple state owners; a second message renderer would duplicate reconciliation
  and evidence semantics. The read controller, session, navigation and payload
  cache remain independent existing owners. Specs: `test_inspector_feed_owner.py`,
  flat-feed/read-lifecycle contracts and the Inspector browser journeys.

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
  cannot under-report the UI counter; the count is unknown (`null`, rendered
  as Unknown) until a listing settles, never a fabricated 0. Every surface
  render passes the archive IDs its loaded Turns reference
  (`conversationCompactionArchiveIds`: compaction receipts and compaction
  Turns) to `observe`, which lists again, bypassing freshness, only for an ID
  no settled listing covered. A listing covers the IDs observed when it
  starts, so an ID the endpoint never lists (child scope, pruned, failed-attempt
  placeholder) costs one listing, and a failed listing retries only on new
  evidence. An automatic compaction inside a running Turn therefore updates the
  count without polling. Explicit drawer opens bypass freshness,
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
- Product copy uses the generated `I18nKey`/`Translator` contract. The source
  locales named by `frontend/src/i18n/languages.json` (zh, en) are the only key
  and placeholder authority; feature-local string-key translator interfaces
  are not new extension points. `languages.json` is also the only list of UI
  languages: picker order, BCP 47 `<html lang>` tags, endonyms, the default
  and the fallback; the server cookie whitelist and i18n digest read it
  through `lib/http_transport/ui_languages.py`. Settings shows one native
  language select styled like its neighbouring fields; the i18n owner renders
  its options from the manifest, each led by the endonym and, for the other
  languages, followed by the name in the active UI language from
  `Intl.DisplayNames` ("Deutsch — 德语"). Problem: seven flag pills wrapped
  raggedly beside the theme cards, flags denote countries rather than
  languages, and the markup duplicated the manifest. Alternatives rejected: a
  pill grid that grows with every language, and a custom popup that
  re-implements native select keyboard, focus and dismissal. Derived locales (ja, ko, fr, de, ru) are
  produced by `scripts/translate_i18n_locales.py` from both sources with a
  shared glossary, cross-family model review and mechanical checks; source
  fingerprints in `translation-sources.json` mark edited copy stale. A derived
  entry may use each placeholder at most as often as the sources, because some
  screens replace only the first occurrence; `{s}` is an English plural suffix
  that other languages replace with count-neutral phrasing. The generator
  emits compact delivery copies, completing missing or unusable derived
  entries from the fallback locale so a pending translation never breaks a
  build or shows a raw key; `check:i18n` fails until every derived locale is
  complete and current. Problem: per-key manual translation of thousands of
  strings across five languages does not stay consistent or current.
  Decision: one tool owns translation, review and staleness; the generator
  owns runtime safety. Alternatives rejected: failing the build on a pending
  translation blocks unrelated releases; ICU plural syntax would add a parser
  and runtime for 11 English-only `{s}` messages. Vite content-hashes the
  delivery copies as data assets and the browser fetches only the selected
  language through a request-coalescing loader that retains only the active
  catalog; the latest picker choice wins over an earlier, slower catalog
  request. The retained runtime paints while that boot fetch
  is in flight, so a lookup before any catalog has loaded is premature, not
  missing: it returns the key, is verified once `ready()` loads the catalog,
  and repaints through `tofu:language-change`. `[i18n] missing <lang>:<key>`
  is reserved for a key that a loaded catalog lacks (once per page and key);
  warning at first paint would flag present keys on every page load.
  Alternative rejected: deferring every runtime section until `ready()` would
  delay first paint and move ordering into dozens of owners.
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
  Vite follows the real import graph for delivery; these typed tool-policy
  modules load with their conversation consumers. Shared transport dependencies
  never pull them into independent Admin or plugin entries.
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
- Human Guidance cards and compact outcome rows live in the DOM-free `frontend/src/conversation/presentation/tool-human-guidance-presentation.ts` owner. Its single immutable port closes the awaiting → skipped → submitted state order. Settled unanswered questions are read-only unless the exact Turn settlement offers `answer_guidance`; that late-answer projection keeps the original choice UI interactive after Stop. It admits at most 16 options, parses legacy option JSON only below 65,536 code units, bounds identifiers at 512, questions at 32,768, labels at 1,024, descriptions at 8,192, and each option-owned note at 4,096 units, and makes every elision or unavailable response visible. Guidance IDs, ordered option indexes, and original choice labels remain escaped `data-*` values consumed by static restricted actions; none can enter executable action text. Translation and sanitized Markdown are injected capabilities. Delegated response behavior lives separately in the lifecycle-scoped `frontend/src/conversation/ui/human-guidance-actions.ts` owner. It reads only card-scoped static selectors, requires card/group/button/note datasets and ordered indexes to agree, rejects more than 16 option groups, and submits only the chosen label and that option's draft. It caps free-text replies at 32,768 code units before translation or transport and permits one in-flight submission per conversation/guidance identity. Choice rollback is exact; translation failure falls back to the bounded original; backend 404 triggers an expired repaint while transport failure keeps its distinct error. Composition owns the presentation store through an immutable lexical port. Retained code keeps only EN-to-CN arrival translation and two forwarding action entries; it publishes no Human Guidance state through `runtimeScope`. A non-blocking question (`guidanceBlocking: false` on a `done` round) renders the same inputs as a card that never expires, marked `data-hg-reply="chat"` with a neutral badge, a hint, and its trimmed question text (batch questions joined by blank lines) as an escaped `data-hg-quote` bounded at 4,096 units. The action owner routes that card's untranslated answer to the required `replyInChat` composition port instead of the guidance transport: an idle ordinary composer sends it as a quoted chat message (`sent` marks the card replied and collapses it to a read-only row); otherwise the quote and answer join the unsent draft (`drafted`, with a notice; the card stays open). A blocking question the user bypassed by chatting (`guidanceSuperseded`) becomes a read-only row.
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
| Demand-loaded local Knowledge Workbench in Settings and drop state | manifest bundle `knowledge-presenters` + `frontend/src/features/settings/knowledge-tab.ts` |
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
| Context row facts shared by typed and retained renderers (registry label, glyph, authority, glimpse, verbatim text runs) | `frontend/src/conversation/presentation/context-note-presentation.ts`; styles `23-reading-context.css` |
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
| Owner-scoped model-routing v2 Settings load/CAS/secret replacement | `frontend/src/features/settings/model-routing-authority.ts` + `save-export.ts`; reads coalesce, Save joins authority readiness, and committed responses enter the common picker projection |
| 本地部署 page: live hardware strip, curated catalog ranked against it (purpose-filtered, fit-tiered, download-hint cards that hand off to the chat agent), DeepSeek V4 Flash cloud escape hatch deep-linking the template wizard, managed-instance ledger with failure diagnosis → agent repair handoff, then the engine preset chooser / batch endpoint probe-and-stage / managed model-path handoff | `runtime/sections/settings/local_deploy.js::addLocalProvider` (read-only `Api.localServe` → `routes/api_v1/local_serve.py`; staging via `provider_render.js::_stageModelRoutingProviderBundle`) |
| Provider-first chat picker, automatic Provider choice, and provider-scoped pending identity | `runtime/sections/main/main_toolbar_ui.js::_modelRoutingDropdownModels` |
| Chat picker startup, committed routing projection and readiness recovery | `frontend/src/features/model-catalog/model-picker-startup.ts` + the retained toolbar bridge |
| Model visibility/default projection from enabled v2 Offerings | `runtime/sections/settings/core_panel.js::_getAllModels` + `runtime/sections/settings/visibility_defaults.js` |
| Speech ProviderAccess activation, configuration and encrypted credential handoff | `frontend/src/features/settings/speech-provider.ts` (pure policy), `speech.ts` (form/commands) + `Api.modelRouting` |
| RouteSnapshot serving-route and failover presentation | `frontend/src/conversation/presentation/turn-serving-route.ts` + `runtime/sections/ui/finish_info.js` |
| Authoritative Turn projection into catalog-shell metadata | `frontend/src/core/turn-projection.ts` |
| Turn commands | `frontend/src/core/turn-command.ts` |
| Turn runtime orchestration | `frontend/src/core/turn-runtime.ts`; explicit snapshot coalescing and shell handoff in `core/turn-hydration.ts` |
| Turn runtime types and command execution | `core/turn-runtime-contracts.ts` and `core/turn-command-execution.ts`; public types remain available from `core/turn-runtime.ts` |
| Attempt callback leases and live settlement observations | `frontend/src/core/turn-attempt-lifecycle.ts`; one owner per canonical store, using the existing sync coordinator |
| Turn DOM-commit witnesses and receive clocks | `frontend/src/core/turn-render-observations.ts`; sends observations through `core/perception-recorder.ts` |
| Conversation DOM reconciliation | `frontend/src/conversation/ui/conversation-surface.ts` |
| Rich tool HTML and live Swarm subtree reconciliation | `frontend/src/conversation/ui/classic-conversation-renderers.ts` |
| Presentation-only state and scheduling | `frontend/src/conversation/application/conversation-surface-controller.ts` |
| Scroll anchor, follow suspension and DOM window | `ConversationSurface` viewport port |
| Conversation diagnostics projection | `frontend/src/features/diagnostics.ts` over `ConversationTurnRead` + active Surface |
| Catalog shells and settings persistence | `runtime/sections/core/conversation_catalog.js`; composer capture adapter in `runtime/sections/main.js` |
| Per-conversation connection badge health | `frontend/src/core/connection-health.ts` |
| Send startup cancellation and automatic retry | `frontend/src/core/send-startup.ts` |
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

### Independent appearance and reconciliation

Problem: the Surface's keyed reconciliation and history residency code also
selected CSS classes, built styled controls, chose fallback renderers and wrote
localized labels. Changing appearance required editing the streaming hot path;
unchanged shell classes and labels were written again on every content update.

Decision: `conversation/ui/conversation-surface.ts` is the public composition
entry. It supplies `surface-design.ts` through the type-only
`surface-design-contracts.ts` port to `surface-engine.ts` and
`surface-turn-renderer.ts`. The design owns CSS loading, visual classes, labels,
default renderer selection and construction of round/history controls. The
engine owns stable IDs, keyed reuse, windows, scroll, intent delegation and
retirement. Semantic action attributes remain independent of visual classes.
Design updates compare actual DOM values before changing shell classes or
control text; they do not introduce a second cache or state owner. Content
renderers still own their own markup and cleanup, and custom renderer identity
continues to invalidate the corresponding part.

The server commits ordered changes before waking readers. Browser transport
reduces every accepted change into TurnStore; the existing Surface controller
coalesces only the latest paint demand. Drawing, visibility and perception
receipts never gate backend execution or durability. A renderer failure retains
uncommitted presentation demand without rolling back an accepted sync cursor;
a reducer/protocol failure still refuses that cursor and repairs from authority.
The browser can still spend time on its own main thread: this separation is not
a claim of parallel JavaScript execution.

Alternatives rejected: moving only CSS leaves control markup and renderer
selection in the performance owner; a second client transcript or backend HTML
projection duplicates authority; waiting for paint before consuming events
couples synchronization to visibility; dropping ordered events to reduce paints
breaks replay. No transport, protocol, persistence, capacity or tuning knob is
added.

Verification: `surface-design.test.ts` replaces appearance while preserving
paging, focus, keyed nodes and disposal. A deterministic 64-frame content fixture
reduces redundant class mutations from 640 to 0 while displaying every update
and retaining unchanged control text nodes. This measures DOM writes, not
browser frame latency. `turn-runtime-ingestion.test.ts` withholds all painting,
accepts 64 ordered changes, injects a design failure, accepts another change and
commits the newest graph with one initial snapshot. The renderer boundary gate
rejects CSS, visual writes and design composition imports in the engine. Backend
`test_conversation_sync_wake_budget.py` exercises 20,000 wake hints with a stalled
observer; the durable log and existing wake owner remain unchanged. Native
browser convergence and history journeys cover the complete renderer graph.

## Conversation hydration

One v3 snapshot contains settings, revision, a Turn window, its attempts, cursor, heartbeat policy, and an opt-in metadata-free artifact existence bit.
The generated browser requests the newest 96 main-lane Turns for linear conversations; the default public representation remains complete, and branch-bearing conversations receive that full representation until lane-directory paging exists.
`turn-runtime.ts` verifies the snapshot revision and ingests TurnState before applying settings and handing metadata to feature adapters. A rejected older snapshot cannot acknowledge a new replay cursor or publish stale settings; the coordinator retains its recovery barrier. `hasArtifacts:false` commits empty artifact presentation immediately and skips the legacy list request; `true` or absence keeps that request for rolling compatibility. A weak conversation-shell generation fence prevents older list responses overwriting newer negative hints.
The pure selector reads Turn state directly and the controller commits to the keyed Surface. `turn-projection.ts` updates only catalog/lifecycle metadata; it cannot materialize a parallel transcript.

**Problem:** a coalesced hydration retained its first catalog shell, and an outer
projection could repaint an older graph or change connection demand after a host
callback had already adopted a newer shell or state. **Decision:** the existing
hydration lane in `core/turn-hydration.ts` shares one snapshot while its latest
requested or bound shell owns
settings, metadata and binding. Each required callback can transfer that claim;
completion checks the same store lifetime. Projection and connection work check
both binding identity and the current canonical state after callbacks. Deferred
terminal repaint reads the current graph before releasing the matching attempt's
UI latch. Command acknowledgement also respects a newer pending hydration claim;
the original server outcome remains unchanged. **Alternatives rejected:** one read per shell duplicates authority IO;
blocking reentrant host operations loses newer navigation or accepted state;
a second connection scheduler duplicates the sync coordinator. Specs:
`core/turn-runtime-ingestion.test.ts` and the neighboring command/attempt/health
contracts.

**Problem:** the sidebar can consume a terminal Turn before its scheduled answer
commits, and dropping pending demand on a renderer exception leaves the body
stale until reload. **Decision:** the Surface controller retains only the latest
canonical graph until rendering, scroll restoration, mount notification and the
required commit receipt all succeed. Every scheduled or explicit flush shares
one failure owner: one immediate repair per conversation-local failure streak,
then existing canonical/health/UI demand retries without a new read or poller.
Disposal and host/conversation replacement reset that local budget; stale
callbacks cannot consume another conversation's work. Convergence ownership is
false while pending, failed or detached; the separate physical `isMounted` port
grants bounded artifact presentation reads without inventing a sync ACK. A
callback's new presentation demand has its own token, so finishing the earlier
frame cannot drop it or recursively commit it. An absent host retains demand
without self-scheduling; mount setup failures retain the same conversation/host
repair budget even before a Surface exists. The typed render scheduler races a preferred RAF
against one 100 ms fallback, cancels the loser and commits at most once even
when cancellation fails. Browser throttling or a blocked main thread can delay
both clocks; this is a convergence mechanism, not a guaranteed perceived latency.
The frame-only perception adapter still requires a real visible paint opportunity.
Turn command execution is composed through `core/turn-command-execution.ts`.
**Problem:** command payloads, HTTP responses and conflict adoption were mixed
with canonical store, stream and rendering lifecycles in one runtime closure.
**Decision:** keep command methods together behind explicit store, hydration,
binding and connection ports. The existing `turn-command.ts` still builds wire
requests; `turn-state.ts` still owns reduction and `conversation-sync.ts` still
owns snapshot/replay transport. Shared TypeScript contracts have no runtime side
effects, and the composition entry preserves its public type exports.
**Alternative rejected:** copying coordinator logic into endpoint helpers would
add competing snapshot and connection owners rather than clarify command flow.
Its composition boundary is `core/turn-render-observations.ts`. **Problem:**
mount identity, receive clocks and paint eligibility were spread through runtime
ingestion, rendering and teardown. **Decision:** keep these content-free witnesses
together, with explicit reads of canonical state, active ownership and connection
health. Stage receive clocks before dispatch because a forced render can commit
synchronously. Only actual mounted DOM commits grant evidence; snapshot restoration
cannot invent network timing. Existing retention remains 64 receive clocks and
128 mounted identities. `perception-recorder.ts` still owns paint scheduling,
receipt queues and retries. **Alternative rejected:** moving receipt delivery into
the renderer would duplicate retry/lifecycle ownership and couple DOM presentation
to transport. Specs: `tests/turn_lifecycle/test_turn_runtime_perception_commit.py`.
Healthy heartbeat callback deduplication applies only after the active Surface has
acknowledged its graph. A pending or failed commit retains the existing heartbeat's
repair demand; inactive or converged surfaces still avoid redundant paints. Ordered
changes keep their existing canonical render demand, not an additional health-driven
repaint per event. A genuinely replaced canonical graph arriving inside a failed
commit is scheduled after unwinding; same-graph presentation invalidation does not
reset the bounded failure budget or create a recursive retry loop. Direct history
reveal/latest window commits use the same failure boundary even with no pending
graph; a renderer error retains canonical repair demand and withdraws convergence.
**Rejected:** treating scheduled work as rendered evidence, changing only sidebar
badges, endless RAF retries, or adding a transcript poller creates false success,
unbounded work or a competing authority. Specs: `turn-runtime-ingestion.test.ts`,
`conversation-surface-controller.test.ts`, `animation-frame-scheduler.test.ts` and
`tests/visual/test_visual_terminal_render_convergence.py`.
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
snapshots; their only deletion is a continuous frontier page removing Turns that
a replacing tail-window snapshot retained below its window and the page omits
([Recovery keeps loaded history](contracts/CONVERSATION_SYNC_V3.md)). The
runtime starts those re-reads itself after the snapshot and again on every
stream the coordinator opens while the range is unverified, so a skipped or
failed re-read cannot leave a stale Turn mounted. The retained adapter
merely forwards the
Surface's typed `load-earlier-turns` intent to this runtime method. A continuous
page that chains from a page cut short by bytes doubles its `windowBytes` (pure
policy `nextHistoryPageBytes` in `core/conversation-load-budget.ts`, at most four
viewport budgets); detached jumps and unchained reads use the viewport budget.
Specs: `conversation-history-budget.test.ts`, `conversation-load-budget.test.ts`.

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
Problem: hashlib releases the GIL for every update of 2,048 bytes or more, so beside
busy threads each message waited about a switch interval, twice; projecting a 3.8 MB
history took 6.0 s next to two CPU-bound threads while other tasks queued behind the
process-wide projector lock. Decision: both digests feed hashlib slices below that
threshold (`lib/utils.py` `update_holding_gil`); digests and rows stay byte-identical, and the same
projection took 51 ms (`benchmarks/round_prep_body.py`). Alternatives: a cheaper
non-cryptographic fingerprint changes the retained digest contract; per-task locks would
stop the queueing but leave every scan as slow.
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
  shares this recovery boundary. The caller's signal ends recovery at once, so
  an abandoned send never keeps resolving in the background.
- All retries for one logical PATCH reuse the same transport-owned idempotency
  key. A retry loop never mints a key per attempt.
- Lazy TypeScript features receive `captureActiveConversationSettings` through
  the private feature service registry. They do not copy composer state or
  call a settings endpoint directly.

Send startup repeats transient failures itself. Problem: a 30-second
preparation timer bounded config resolution, shorter than the resolver's
75-second recovery window, so a forwarded-port stall failed the send while the
resolver was still recovering, and every transient failure required a manual
resend. Decision: `retryTransientSendStartup` runs
both the resolution and the Turn command as attempts. Each attempt owns an
AbortSignal and a 90-second deadline, longer than the resolver window, so the
deadline only reclaims a hung attempt. Transport network/timeout failures,
408/429/502/503/504, unattributed 500s and retryable storage codes repeat with
capped backoff (about 1, 2, 4, 8, then 15 seconds) behind a visible
`send.retrying` status, without an attempt cap, until the command is accepted,
a definitive failure surfaces, or the user presses stop. The command write
resends the frozen request with its `commandId`, so a lost ACK replays through
the idempotent `createTurn` contract instead of duplicating. The helper awaits
an attempt's own settlement rather than racing its signal, because an
acknowledgement can cross a stop and must still reach the pipeline. The send
lease keeps only the stop button's cancellation scope. Alternatives
considered: lengthening the preparation timer keeps two nested deadlines and
still ends in a manual resend after a longer stall; waiting without
per-attempt deadlines can hang on a stalled write; a durable outbox across
reloads would add browser-owned state that draft restore already covers.

Stop during a first send's startup must still settle on screen. Problem: Stop
aborted the command request while the server was still accepting it, so the
Turn committed but its ACK never arrived. Only the ACK path adopted the draft
shell; still `_localOnly`, the runtime paused the conversation stream after
the next change and the stopped Turn kept its startup phase until reload.
Stopping send-time translation also left the generation-start lease held, so
the control stayed a live Stop and every click re-sent the conversation abort.
Decision: an explicit hydration snapshot exists only for a conversation
authority holds, so it adopts its claimant shell before binding decides
connection demand; translation Stop ends the lease like any startup Stop.
Alternatives considered: clearing the flag only in the send pipeline's
recovery leaves every other lost-ACK hydration exposed; resuming paused
coordinators on catalog invalidation would add a second owner of connection
demand.

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
  locally by stable Turn identity. Reader demand toward earlier content requests
  the next bounded 64-Turn sequence-CAS history page while less than twelve viewports
  of loaded history (mounted Turns above the reader plus loaded, unmounted Turns at
  their mean height) remain above the reader, and additively merges it through
  the same reducer. A settled flight holds its lane until the Store render that
  carries the page retires it in the same anchored commit; failed pages retry only
  near the loaded top. The Surface anchors and reveals prepended Turns without
  creating a second render path. Durable authority remains in storage; no
  transcript copy enters IndexedDB. Specs: `conversation-surface-history.test.ts`,
  `surface-windowing.test.ts`, `tests/visual/test_visual_history_paging_latency.py`.
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

## Committed model-routing projection

Problem: Settings adopted a successful routing PUT only into its editor draft.
The subsequent config-only refresh left the composer on its previous catalog and
reasoning controls. Late reads could also replace a newly committed draft.

Decision: the existing typed model-picker startup owner accepts committed routing
responses from Settings reads and writes through a narrow retained-shell bridge.
It projects the complete server response, refuses older revisions, and fences
responses and failures from superseded reads. Equal revisions can refresh
availability. Before the initial config policy is available, publication keeps
only the latest accepted catalog and starts no extra reads. Config success paints
policy before that catalog; authorization refusal and disposal drop the pending
view. Full startup also joins any newer config refresh before projecting its
catalog; disposed owners accept no publication or recovery work. Config coordination
retains settlement only, not the response payload. A fresh-process GC probe
projects a synthetic 1 MiB response exactly once and verifies its release while
the picker remains alive. The same probe verifies that only the newest pending
catalog survives and that disposal releases it; this is a reference-lifetime
check, not an RSS claim.

`settings/model-routing-authority.ts` owns read/CAS lifetimes and serialized
persistence; `core-panel.ts` assembles the Settings surface and publishes its
existing bindings. Keeping the transactions inside panel assembly was rejected
because it obscured the owner of draft replacement and committed publication.

The editor remains separate from committed presentation: reads preserve drafts,
credential input and CAS revisions changed while awaiting the server. A failed
write publishes nothing; serialized writes still commit edits made during a
pending save. Subscription refresh also uses this committed projection and never
repaints the composer from the unsaved editor draft. Even synchronous read refusal
retires the shared in-flight promise
so the next action can retry.

Alternatives rejected: another whole-catalog GET wastes the successful PUT
response; painting only the model list leaves reasoning controls stale; sharing
the mutable draft with the composer publishes unsaved changes; dropping every
older-started read would also drop a provably newer server revision.

Specifications: `tests/model_picker_startup.test.cjs`,
`tests/frontend/test_settings_routing_projection.py`, and
`tests/model_routing/test_model_reasoning_settings_browser.py`. The browser
journey verifies model/provider inheritance and no extra catalog GET after Save.

### Credential replacement settlement

Problem: the Settings save coordinator replaced secrets outside the routing
write chain, advanced the editor revision from partial receipts, and cleared the
entire pending map. New input could disappear during a request, and a failed
batch retried credentials already acknowledged by the server.

Decision: `model-routing-authority.ts::persistModelRoutingCredentialSecrets`
owns one batch in the existing queue. It snapshots replacements, verifies each
credential identity and advancing revision, and removes only the acknowledged
value from the same editor/map. Changed values, newly staged keys and replaced
editors survive; unsent values changed after the snapshot are left for the next
Save. The batch advances a local CAS revision, then obtains one complete
projection, including after partial success. Failed or stale refreshes retain
the editor's previous CAS and never restore acknowledged pending values.
The server remains responsible for encrypted storage, atomic per-key CAS and
secret reclamation; a multi-key save is not an atomic backend transaction.

The save coordinator keeps Settings open when drafts remain or refresh did not
complete. It distinguishes saved work with new unsaved edits from a failed
refresh, using the existing localized notices. A complete response
also transfers surviving credential input bindings through the existing typed
reveal owner, preserving focus and newer text. A changed secret reference retires
the old plaintext baseline; pending reveals from the previous document cannot
populate the updated input. Closing the view releases plaintext without
cancelling a request already accepted by the server.

Alternatives rejected: a separate secret queue still races Provider mutations;
clearing the map only at batch completion replays successful rotations after a
later failure; assigning a partial receipt's revision to the draft risks writing
stale secret references; replacing the entire Provider DOM loses active edits.

Specifications: `tests/frontend/test_settings_credential_replacement.py`,
`tests/providers/test_provider_credentials_view.py`, and the Provider browser
credential replacement journeys exercise real edits during and after Save.

### Catpaw connection lifetime

Problem: Catpaw's retained status presenter reset the automatic-import latch
whenever it displayed a connection. Disconnect then deleted a Provider outside
the routing queue and its status read immediately imported it again. Independent
initial reads, interval polling and CLI-login continuations could also revive
retired views, and the disconnect promise finished before its deletion.

Decision: `settings/catpaw-connection.ts` owns one connection intent and view
lifetime. `catpaw-settings.ts` adapts the existing controls, typed API facade and
shared routing owner. Opening Settings starts its read independently from OAuth
account status. Overlapping reads and same-kind commands coalesce; polling has
one outstanding read and the existing fixed 60-check/3-second window. A slow
read does not extend that window, and replies after expiry cannot import.
`CATPAW_LOGIN_STATUS_CHECKS` and `CATPAW_LOGIN_STATUS_INTERVAL_MS` name that
existing liveness contract; the refactor adds no tuning controls.
Closing/removing the panel retires reads, login links and unconfirmed actions.
An old confirmation cannot block a reopened panel, while an accepted routing
write keeps settling through the existing shared queue.

An observed connection consumes automatic onboarding for this browser owner;
ordinary Provider deletion therefore does not rearm it. An explicit confirmed
disconnect suppresses automatic imports across panel reopen, and only a new
manual connect command changes that intent. This is transient browser intent,
not a new durable auto-import preference: a fresh application load still follows
the existing server-authorized local-import policy. The server retains local
credential issuance, encrypted storage, owner identity, admission and CAS.
The ordinary Provider view also signals its confirmed Catpaw deletion to this
owner before queue admission, so even a late initial onboarding read cannot
recreate the target. Its prepared removal still uses the common mutation queue.

Both Provider deletion views use `provider-mutations.ts::prepareProviderRemoval`
to capture the displayed Provider/access before confirmation, validate it when
approval returns, and recheck the access in the shared mutation queue. The
complete-response adopter protects unrelated drafts, pending keys and CAS;
refresh/render failures are separate from acknowledged endpoint writes and do
not invite an import/delete replay.

Alternatives rejected: a deletion-only queue adapter leaves polling able to
reimport; a second Catpaw queue races the routing editor; resetting an import
latch during rendering confuses observation with user intent; persistent local
storage would introduce another owner/authentication-policy authority.

Specifications: `tests/providers/test_catpaw_connection_lifecycle.py` exercises
the actual native adapter, shared authority and controls; Provider mutation
contracts cover confirmation targets and atomic deletion. Backend local-import
tests remain the service-policy authority.

### Speech settings save coordination

Problem: STT Provider writes bypassed the routing queue and replaced the editor
from a private GET, dropping unrelated drafts and leaving its saved JSON stale.
Every ordinary Settings Save rewrote the STT bundle, even without form changes;
an acknowledged credential remained in the form and could be submitted again.

Decision: `settings/speech.ts` captures the submitted form before waiting and
uses `model-routing-authority.ts::runModelRoutingMutation`. Its explicit
`persistDraft` policy commits the other edits authorized by the global Save
before the STT endpoint call, within the same queue slot. Commands may report
an explicit unchanged result without a write or catalog read. STT validation,
stored-secret reuse and bundle preparation read the current authority only
after entering the queue. Missing or disabled form controls never request
deletion; a global Save cannot treat an unfinished/failed load as user intent.
The STT owner retains its provider/grant/model policy and backend admission;
the shared owner alone adopts full responses and protects later drafts/CAS.

Only a validated advancing receipt clears the submitted plaintext, and only
from the same form lifetime when the value still matches. Later form edits
remain pending, including during the subsequent miscellaneous-config save.
Closing or repopulating the form retires its plaintext through the existing
lifecycle scope without cancelling accepted writes. A failed refresh keeps
Settings open and cannot become a reason to repeat an acknowledged secret.
`core-panel.ts` begins that form lifetime before showing Settings. Speech
controls stay disabled while its initial authority/config load is outstanding;
only the matching completion can populate and enable them. Load failure keeps
the controls unavailable, and closing/reopening invalidates older completions.
This prevents a late initial read from overwriting a seemingly usable toggle.
Configuration replacement retires pending replacements for credentials it
removes; an activation change retains all existing credentials and pending keys.

Alternatives rejected: a second speech write queue would still race Provider
commands; overwriting the editor from a private read loses work; validating
stored credentials before the queue observes stale authority; caching submitted
plaintext to detect duplicates extends its lifetime. Comparing the form with
the current routing projection avoids redundant writes without another store.

### Speech service activation

Problem: the speech enable switch deleted its Provider graph, despite promising
to retain saved credentials. Re-enabling rebuilt default grants and metadata.
Its description also promised an immediate global microphone switch even though
changes take effect on Save and voice availability includes every runnable
owner speech route.

Decision: `speech-provider.ts` owns pure routing projection and bundle policy;
`speech.ts` owns form intent, plaintext and view lifetime. An activation-only
command changes the projected access's existing `enabled` flag in a detached
authority document. It preserves connections, credentials, grants, offerings,
deployments, registration evidence and sibling accesses. The common routing
queue commits it with CAS and adopts the complete PUT response through the same
boundary as reads; no follow-up GET is needed. Late drafts retain their original
CAS, and a presentation failure remains distinct from a rejected write.
Configuration edits retain the existing Provider bundle admission path.
The access identity and activation intent are captured before queue admission;
new metadata committed by an earlier command cannot turn a toggle into a stale
bundle replacement. Saved model IDs outside a preset select remain selected
through a temporary option owned by the current form lifetime, preventing an
unrelated Save from silently selecting the default model.

The control says “Enable this speech service” and explains Save and credential
retention. It introduces no global microphone policy; another enabled speech
service can still provide voice input. Explicit Provider/credential deletion
retains its separate removal semantics. The retained speech panel's source now
lives in `frontend/src/settings-panels/speech.html`, composed to its existing
server-delivered fragment. Durable-secret measurements and cleanup ownership
are in `STORAGE_OPERATIONS.md`, “Model-routing secret reclamation”.

Alternatives rejected: deleting/recreating the Provider loses saved authority;
rebuilding a bundle just to toggle can discard grants, probe evidence and
sibling accesses; a browser credential cache creates a second secret store;
a new global setting duplicates the existing owner route-availability policy.

Specifications: `tests/providers/test_speech_routing_lifecycle.py`,
`test_speech_provider_persist.py`, `test_speech_settings_browser.py`, and
`tests/frontend/test_frontend_stt_settings.py` cover ordering, unchanged saves,
late edits, receipts, secret retirement and the live Settings registry boundary.

### Provider Settings read projection

Problem: every Provider card rebuilt joins over the entire routing draft and
four catalog dictionaries. Its supply matrix repeated that work and scanned all
provider deployments for each offering. Model picker reads lived in the same
retained file as credential operations and routing mutations.

Decision: `settings/provider-settings-projection.ts` owns these pure reads using
the generated routing types. One synchronous render builds shared indexes and
catalog metadata, then passes each provider context to its card and matrix.
Supply rows group deployments by offering once and retain first-seen request
names and their original global indices. Source rows remain the live draft
objects; grants, identity, pricing provenance and first-access selection are
unchanged. The retained context bridge creates a fresh projection for each read,
scoped to the selected provider so it does not index unrelated accesses.
The authority/CAS, credential, request-name editing and manual probe owners keep
their existing responsibilities. No draft is published to the composer here.

Alternatives rejected: a persistent draft cache needs invalidation for in-place
edits and async saves; copying rows breaks the editor's identity and mutation
semantics; adding per-surface selectors preserves duplicate policy and scans.
The projection adds only render-local indexes, with no retained cache or timers.

Verification: `tests/provider_settings_projection.test.cjs` specifies identity,
source indices, access isolation, legacy duplicates, pricing and picker
eligibility. `tests/providers/test_provider_model_add.py` covers the retained
integration; the request-name, reasoning and manual-probe browser journeys cover
the rendered surface. A seeded comparison against the pre-extraction readers
matched 203 documents. Paired Node 22 measurements (3 warmups, 15 samples) for
40 providers, 8,000 offerings and 24,000 request names reduced median projection
time from 211.09 ms to 12.22 ms. This measures pure projection, not DOM rendering,
network latency, whole-page performance or memory usage.

### Provider model-add transactions

Problem: model-add loops changed offerings, connections and grants before every
request-name face was validated. A later disabled face left partial draft rows
even though the UI reported failure. Contexts and request-name sets captured
before template reads also survived provider removal, credential replacement
and concurrent additions.

Decision: `settings/provider-model-staging.ts` owns preparation and synchronous
publication. It captures the editor identity before the capability read, fences
the selected access and connection identities/URLs after each await, and rereads
current catalog facts, credential rows and existing request names. Removed
providers or replaced drafts cancel the old action. Changed planner inputs or a
removed/retired selected model produce an actionable refusal. Credential changes
and catalog metadata updates are adopted from the current draft.

`provider-connection-staging.ts` prepares the existing face matching, disabled
connection refusal, missing metadata and connection grants without mutating
inputs. Alias edits and model adds share it. Model batches prepare replacement
rows and new arrays, so a later face or ID failure publishes nothing. Successful
publication preserves existing connection/credential row identities and grant
denials. Confirmed catalog associations and custom pending identities retain
their existing metadata negotiation, defaults and unprobed deployments.

After publication, render and immediate Save use the existing routing authority.
A failed CAS/save leaves the complete staged batch and any newer edits available
for that authority's retry; it does not roll the editor back. The preparation
holds no persistent cache, timer or alternate write authority.

Alternatives rejected: restoring an entire saved draft would overwrite edits
made while a request was pending; incremental mutation plus rollback duplicates
every changed field and can miss a later failure; independent face resolvers
would let alias and model-add routing rules diverge.

Specifications: `tests/providers/test_provider_model_staging.py` reproduces the
11 failure/concurrency cases before the fix. The native connection/model staging
tests cover pure preparation, fault injection, policy preservation and failed
Save semantics. The request-name browser journey adds a disabled-face model,
then saves Settings and verifies that no hidden partial supply reaches the PUT.


### Provider model picker lifetime

Problem: three retained globals represented every Provider model picker. A
pending add cleared newer choices, repainted another picker or discarded newly
typed input. Removed overlays retained event handlers that acted on the next
picker, and closing Settings left the overlay alive.

Decision: `settings/provider-model-picker.ts` owns one session per open. It
reads current eligibility through the Provider projection and submits through
the existing model-add transaction. Catalog completion removes only submitted
choices; preparation refusal preserves eligible choices for retry. Failed Save
keeps the transaction's complete draft and excludes those already-staged models
from the picker. Filtering never discards selections merely hidden by a query.

Custom completion clears an input only when both its value and edit revision
are unchanged; it restores focus only from the submitted input/button. A retired
session cannot repaint or focus its successor. Explicit close, Settings close
and external removal release the session's DOM, listeners and removal observer.
An authorized add continues and reports its outcome through existing notices.
Pending methods retain submitted values and a session whose DOM reference can
be cleared, rather than a captured input or overlay.

The retained view supplies existing markup, actions, localized hints and
notices. It preserves checkbox focus when eligible rows are refreshed. Empty
search results distinguish a query miss from an exhausted addable catalog.

Alternatives rejected: provider-ID checks cannot distinguish reopening the same
provider; a single busy lock would unnecessarily prevent new choices while Save
is pending; cancelling a write on close would alter the routing authority's
durability contract. Replacing the existing overlay with a native top-layer
dialog would also alter styling and its relationship with retained error dialogs,
so this change keeps the current view rather than introducing a second modal API.

Specifications: `tests/providers/test_provider_model_picker.py` exercises pending
Save, refused preparation, reopened sessions, focus and retired DOM events using
the actual retained view and native staging owner. Settings close/save tests
cover disposal wiring; the Provider browser journey covers the registered actions.

### Provider credential presentation and statistics

Problem: reveal callbacks retained replaced inputs, displayed plaintext after
Settings closed and moved focus away from newer edits. Comparing an edited
staged replacement to itself could also discard that replacement. Statistics
failures were cached as empty successes. Overrides issued before the namespace
read could write a bare provider bucket that the dispatcher never consulted;
legacy or foreign buckets could also shadow the server's explicit owner prefix.

Decision: `settings/provider-key-reveal.ts` owns only temporary reveal/edit
presentation. Pending replacements and persistence remain in the routing
authority. Each view binds the current draft and credential row; pending reads
hold releasable entries. Re-render, Settings close and external removal mask old
inputs and clear plaintext/DOM references through the shared lifecycle scope.
A late read cannot repopulate a retired view or take newer focus. A staged value
is not treated as the committed server baseline when undoing an edit. The
audited reveal endpoint and encrypted-envelope display policy remain unchanged.

`settings/provider-key-stats.ts` owns reconstructible statistics state and
runtime override coordination. Reads coalesce within the current view, fail
explicitly and remain retryable. A server-reported `key_namespace`, including
an explicit empty prefix, selects the statistics and write target. Without that
field, only a unique existing credential bucket supplies legacy evidence;
missing or ambiguous evidence refuses a write. No client-invented owner ID is
accepted as a substitute.

Writes reserve click order while namespace reads settle, then serialize at the
canonical server namespace/credential across draft refreshes. Different targets
remain concurrent. This is command ordering, not a capacity limit. Closing a
view preserves accepted writes; a draft replacement refuses old commands that
have not begun their POST. Completed writes update a reopened view only with
matching namespace evidence, and their receipts survive an older in-flight
statistics snapshot. Pending checkbox intent remains visible until its newest
command settles. Durable-disabled credentials still re-enable through the
existing staged Settings save, rather than a runtime override.

The retained adapter owns existing card markup and notices. These controllers
own neither dispatch admission/health policy nor general named credentials from
`credentials-vault.ts`. They start no timer or network work at module import.
Statistics refresh creates one Provider projection and one credential index per
provider for the pass, rather than rebuilding catalog metadata for every key.
It preserves focused controls when repainting their metrics/actions. In a
paired Node 22/jsdom run with 40 providers, 800 credentials, 8,000 catalog models,
8,000 offerings and 16,000 request names, the median refresh fell from
4,732.33 ms to 238.65 ms (2 warmups, 7 alternating samples). Both paths used the
same health renderer and produced identical DOM. This isolates projection/index
reuse in statistics refresh; it does not measure startup, network or whole-page
latency.

Alternatives rejected: a shared plaintext cache would outlive the view; treating
a staged value as the server baseline loses unsaved edits; merely ignoring an
older write response does not order the actual backend commands; a provider-ID
guess or arbitrary matching bucket is not owner evidence; serializing every
credential behind one queue would delay independent work.

Specifications: `tests/providers/test_provider_credentials_view.py` covers
retirement, draft replacement, staged edits, focus, failed reads, namespace
selection, ordered writes and independent targets. The Provider browser journey
reproduces a reveal response arriving after the real Settings close action.

### Immediate Provider commands and unsaved credential inputs

Problem: Enter and blur could submit the same key twice. Failed adds left the
input disabled; cancellation and replacement left plaintext in detached inputs.
Delete confirmations captured old rows but used a later draft revision. Provider
endpoint calls bypassed the whole-document save chain, and their subsequent GET
could erase edits made before or during the write, including pending secret
replacements. A failed refresh was indistinguishable from a failed write.

Decision: `provider-key-editor.ts` owns the input's listeners, plaintext and
submission promise using the shared lifecycle scope. Cancel handles the pointer
event before blur; Enter/blur join one submission. Closing/removing the view
releases references and plaintext without cancelling an accepted command.
Acknowledged adoption can carry newer inputs into the resulting draft; repaint
waits until those inputs settle, so an older response cannot discard a reopened
input. Failures restore only a current input and do not take newer focus.

`provider-mutations.ts` prepares detached Provider bundles and credential grants
from the current draft after entering the existing routing write chain. The
backend retains atomic Provider/secret storage and revision admission. The view
captures confirmation identity before awaiting the dialog; the command checks
the provider/access and credential identities again when it starts. Separate
inputs remain separate accepted adds; only duplicate submission of one input is
coalesced. A later error-dialog focus change cannot resubmit unchanged failed
text; Enter or an edited value supplies a new retry intent. Another dialog
taking focus is not a credential submission. The retained adapter owns card
markup, confirmation and notices.

`model-routing-authority.ts::runModelRoutingMutation` shares the queue used by
`persistModelRoutingNow`. A partial endpoint receipt is followed by a fresh full
projection read, with the editor guard captured before the POST. Failed refresh
does not turn an acknowledged add into a retryable failed write. Existing or
newer unsaved routing edits retain their draft and original CAS revision; pending
secret replacements survive metadata refresh. Only acknowledged deletion clears
that credential's pending replacement. A separate notice explains when the
operation committed but the view could not fully update. No automatic rebase or
blind retry claims authority over other actors' changes.

Alternatives rejected: a second Provider write queue still races the common
whole-document PUT; rereading the global revision after a confirmation silently
authorizes a different target; swallowing write errors strands the input;
treating every post-write refresh problem as write failure invites duplicate
secrets; merging a partial receipt into an arbitrary newer draft would invent a
conflict resolution policy without the full committed write response.

Specifications: `tests/providers/test_provider_mutations.py` covers input
retirement, confirmation identity, retry and ordering, draft/CAS preservation,
secret replacements and acknowledged refresh failures. The Provider browser
credential editor journey exercises real cancel, Enter, refusal, retry and
close-during-write actions; routing projection tests preserve the existing
whole-document read/save contract.

### Provider creation and the commit wake hint

Problem: template, custom-endpoint, discovery and local-deploy adds created the
Provider outside the routing write chain, assigned the receipt revision to the
editor draft, and joined any in-flight authority read. The repository publishes
`model_routing.changed` inside its commit, before the create endpoint returns,
so Settings live sync was usually reading already. That read saw the draft
revision change underneath it and declined the committed document; the add then
repainted the old draft, so the new Provider card stayed missing until Settings
was reopened. With no read in flight, the same path silently discarded unsaved
routing edits and pending key replacements.

Decision: `_stageModelRoutingProviderBundle` creates through
`runModelRoutingMutation` like every other Provider command and never writes the
editor revision. A refresh owed to an acknowledged write is satisfied once any
complete adoption, typically the wake-hint read that won the race, has brought
the editor to that revision: the revision only advances together with an adopted
committed document. The command's own declined read therefore no longer reports
the "saved, but the view could not update" notice; a draft the user kept unsaved
still reports it.

Alternatives rejected: suppressing hints for the page's own writes needs a
write-origin field in the push contract and leaves the receipt racing opening
and other-tab reads; waiting for live sync to settle adds a timing policy;
rebasing a kept draft onto the new authority invents a merge policy.

Specifications: `tests/providers/test_provider_mutations.py` drives the real
live-sync and authority owners through both read orders, draft/secret
preservation and write-chain admission;
`tests/frontend/test_settings_routing_projection.py` pins the refresh rule; and
`tests/e2e/test_provider_template_add.py` holds the receipt in real Chromium so
the wake-hint read is provably in flight while the new card opens.

### Surface projection and retirement ownership

Problem: keyed DOM caches and retained round controls kept superseded projection
objects after equivalent snapshots, and a failed tool cleanup could skip siblings
or retry an already-retiring body. Decision: `conversation/ui/surface-turn-renderer.ts` owns keyed Turn parts,
grouped reply entries and the tool panel header slot. The Surface supplies ephemeral window
ranges, panel state and current-identity navigation ports, retaining sole ownership of scroll
and residency. Presentation caches advance even when equality skips painting; round controls retain IDs,
not old projections. `conversation/ui/surface-node-lifecycle.ts` releases only
Surface-owned DOM references and finishes sibling cleanup before rethrowing the
first error. A retired panel toggle is inert (delegation is rooted in the live
Surface), and panel choices are keyed by presentation, never by node. Surface
disposal clears its graph, window maps, panel choices and public root.
Singleton slots and keyed children share one `createSurfaceDom` instance bound
to this retirement owner. It collects duplicate siblings before retiring them,
keeps the first node and its reader state, and finishes all duplicate cleanup
before propagating the first error. Raw removal is not an alternative for a
managed duplicate: it detaches DOM but leaves renderer requests/listeners and
projection references alive. The lifecycle specification covers both keyed
blocks/Turns and singleton content/lane slots with real abortable resources;
a failed duplicate cleanup still retires every duplicate exactly once.
Alternatives rejected: disabling keyed reuse loses reader focus/disclosure state;
a generic best-effort lifecycle scope would swallow required renderer failures.
Specs: `surface-lifecycle.test.ts` and `tests/conversation_surface_retention.test.cjs`;
the measured ownership boundary is documented in `RESOURCE_CONTROL.md`.


### Surface window preparation

Problem: every streamed frame traversed all loaded Turns to discover branches
and locate the first/last mounted Turn, although composition already owned the
lane and Turn-location indexes. Decision: `surface-window-preparation.ts` owns
pure lane discovery, budget allocation and stable-ID remapping. The Surface
reads geometry once and retains DOM, history-flight and frame lifecycles.
Composition shares its existing maps through an optional read-only `navigation`
port and publishes ordered child-lane IDs. Eligible local deltas reuse locations
and child IDs; structural changes rebuild them without mutating earlier views.
Only an exact indexed lane identity can use the fast path. External or serialized
views use their explicit Turn/branch graph; cycle-rejected graphs keep diagnostic
placeholders and omit navigation. Collapsed branches keep the same reachability
and allocation order. Windows, reader focus and tool disclosure remain unchanged.

Alternatives rejected: a Surface-owned index would duplicate graph invalidation;
flattening every frame keeps the history-sized traversal; trusting an index after
replacing its lane could display stale branches. `surface-window-index.test.ts`
covers bounded reads, graph equivalence, stable anchors, ordering and fallback;
`selection-deltas.test.ts` verifies incremental composition against a fresh view.
Equivalent-work measurements and their limits are in `RESOURCE_CONTROL.md`.

### Run-conclusion snapshot publication

Problem: the retained run-conclusion writer replaced an entry inside the same
summary map, while the lifecycle selector keys that input by map identity. A
concluded run could keep its old reason and unsent report until another change
invalidated selection. Decision: the receipt writer publishes a new map and a
new record, sharing untouched records. Settings hydration already replaces the
map. Existing report preservation, unsent/incomplete flags and rejection of a
bare stop after a clean report remain authoritative. The retained writer itself
now participates in the selector oracle and rendered-notice specification.
Alternatives rejected: scanning or serializing the complete summary map on every
streaming frame repeats work; a renderer force-refresh masks stale projection
and adds another invalidation path. No new cache, event, poll or transcript state
is introduced. Specs: `presentation/view-model/selection-deltas.test.ts` and
`tests/frontend/test_frontend_conversation_surface_vite.py`.

### Composer voice capabilities and media lifetime

Problem: the composer checked speech availability only at startup, separately
from Settings. Re-running its initializer reset active recording state. Repeated
clicks during permission acquisition could create multiple recorders, and a
recorder start failure left microphone tracks alive.

Decision: `features/voice/audio-capabilities.ts` coalesces composer/Settings
reads of `/api/v4/audio/capabilities`. Accepted model-picker routing responses,
owner-scoped routing push hints and reconnect invalidate older reads. A newer
invalidation prevents an older response or error from publishing; duplicate
revision hints cause no read. Equal-revision accepted responses still refresh
route health. This owner stores no provider/credential policy and adds no poller.
The open Speech panel subscribes through its existing view lifetime.

`features/voice-input.ts` owns projection and the existing action/boot bridge.
On the first microphone action it loads `voice/recording.ts` and
`voice/wave-encoder.ts`. A recording owns its permission request, stream,
recorder callbacks, audio chunks, prewarmed context and transcription signal.
Configuration updates preserve the Stop affordance and accepted recording or
transcription; an unavailable response retires only an ungranted permission.
Failure/disposal releases each track and context, ignores late callbacks and
prevents a late decode from uploading. Disposal aborts in-flight transcription
and cannot inject a late result. No transcription is sent automatically as a
chat turn: successful text still enters the shared composer at its selection.

The existing conservative codec rule remains: only all-endpoint routes may use
raw recorded audio; mixed, chat and unknown modes use 16 kHz mono PCM16 WAV.
Encoding failure retains the original blob and surfaces any eventual API error.
The existing optional voice timing diagnostic remains available.

Alternatives rejected: calling the old initializer after every save interrupts
recording; separate per-view readers can publish inconsistent availability; a
second model-routing event bus duplicates the shared push/commit owners; eagerly
loading codec code adds optional media work to startup. Tests cover read races,
permission denial/retirement, recording failures, teardown, codec parity and the
real Settings-save → microphone projection boundary.

### Settings initial reads and presentation lifetime

Problem: closing and reopening Settings retired Speech and Catpaw views, but
miscellaneous config, health and desktop reads still published into the same
DOM. Old responses could replace newer presets/pricing or hide a current
installer card. Save could also collect prior-opening server fields before
initial config finished, and exporting config mutated the editor's cache.

Decision: `settings/panel-read-session.ts` owns one opening's read scope and
initial readiness. Close, pagehide and the next opening abort its reads and
retire callbacks; DOM identity is checked as well. The existing model-routing
read/write owner remains shared: retiring a view does not cancel accepted
routing writes or create a second routing store. Server-backed preset/search/
network/advanced/Feishu/translation panels remain inert until their config is
projected. A fast Save waits for initial readiness before collecting or writing;
a retired wait produces no command and a failed initial read keeps Save from
writing stale server fields. General local controls remain interactive.

`server-config-reader.ts` coalesces pending reads in the same opening and
publishes config/pricing only from the current request/session. Its pending
record retires inside the read before resolving waiters. Export uses a separate
raw read and can finish after close without altering the active editor.
`client-downloads.ts` owns readonly desktop/mobile markup and version projection;
late failures cannot hide a later card, and unavailable iOS links return to an
inert coming-soon row. Browser architecture probing shares one page-lifetime
promise; it affects display only. The existing API facade forwards cancellation
without changing endpoints, timeout/retry policy or backend authority.

Alternatives rejected: checking only the modal's open class accepts old results
after reopen; only checking DOM presence does not distinguish reused elements;
clearing or aborting the shared routing owner on close can disrupt accepted
writes; per-tab config caches duplicate authority. The common read session
leaves specialized form/write lifetimes with their existing owners.

### Settings save input and feedback ownership

Problem: Save collected later feature flags, Speech fields and miscellaneous
server settings after earlier writes awaited. Edits made meanwhile, including
inputs from a reopened modal, could join the older save. Completion could clear
a newer search key, close a newer window, or replace its failure/status text.

Decision: `settings/save-input.ts` captures feature choices and detached JSON
payloads before the first write. The Speech owner captures its form, activation
intent and panel scope at the same boundary. Local client settings apply
synchronously. `save-export.ts` coordinates those inputs through existing feature,
routing and config API owners. The routing aggregate retains its established
serialized draft-drain policy and per-key acknowledgement; this change creates
no second routing transaction or CAS state owner.

`settings/save-feedback.ts` owns one save latch and the originating view's
feedback. Close/pagehide/reopen retire presentation while accepted writes settle;
busy state is released when that save finishes. Input capture follows initial
readiness, and edit monitoring starts after capture, so edits already included
in the submission are not reported as pending. New form edits keep the window
open. Search-key acknowledgement clears only a matching submitted key and gateway
in the current view. Committed application policy still refreshes independently
of the editor. Rejected or missing config acknowledgements surface as failures.
The old delayed success-text clearing timer is removed: successful unchanged saves
close the window, while failure and unsaved-work notices remain actionable.

Alternatives rejected: collecting each tab after its predecessor finishes mixes
different user intents; aborting accepted writes on close confuses settlement
with presentation; freezing every form for the whole network transaction prevents
useful editing; recreating removed classic save helpers to satisfy old tests would
leave the real typed coordinator unverified. Native tests and browser journeys
exercise delayed features, later inputs, credential acknowledgement and reopen.

### Settings import and subscription presentation lifetime

Problem: file import and OAuth completions treated a still-mounted Settings
node as proof that their original view remained open. Closing and reopening the
same modal let old imports reopen the editor, old confirmations issue commands,
and old login callbacks paint or clear new work. Status and device polling had
independent requests and timers. A subscription refresh could also adopt over a
draft edited before that refresh began.

Decision: `settings/config-import.ts` uses the existing Save latch and feedback
owner. Closing retires a pending file read before command submission. An accepted
import remains pending until its acknowledgement; it refreshes application policy
but only reopens its original, unchanged editor. Read, JSON and acknowledgement
failures release the latch and remain visible in that editor.

`settings/oauth-panel-lifetime.ts` reuses each Settings opening's lifecycle scope.
Status, device observation and transient egress/reset reads share one pending
request; close aborts reads and releases presentation timers. The existing
2-second/8-attempt transient read policy and 3-second device observation cadence
are unchanged. A device status projected on reopening resumes observation.
Commands invalidate older status reads so a pre-command response cannot repaint
over an acknowledged login. The retained OAuth presenter supplies transport and
render ports; remove those bridges when migrating the remaining presenter.

OAuth preparations, confirmations, popup handlers, clipboard feedback and device
fallback continuations capture their original opening. Accepted exchanges and
writes still settle after it retires. A flow completion clears only its own
callback gate and keeps its captured browser-exchange parameters. Codex login
reuse captures a committed-policy callback independently from its original
presentation callback. The original origin/state checks, manual-code distinction,
server-first exchange and constrained-proxy fallback remain unchanged.

`refreshCommittedModelRouting` uses the routing authority's existing fresh-read,
dirty-draft and credential-preservation policies after a subscription command.
It publishes committed composer data even when the originating view retired.
`_refreshSubscriptionModelCatalog` takes that captured view; it cannot adopt
miscellaneous config or render a newer Settings window on the old command's
behalf. No additional model catalog or routing-write queue is introduced.

Alternatives rejected: checking connectivity or the modal's `open` class cannot
distinguish two openings of the same DOM. Cancelling accepted writes would lose
their acknowledgement without reversing server effects. Keeping independent
pollers would preserve overlapping requests and duplicate cleanup. Resetting the
editor from the login response would discard user work and bypass routing's
committed-state authority.

Verification: `tests/frontend/test_settings_import_oauth_lifecycle.py` exercises
retired reads, confirmations, accepted writes, flow replacement, captured PKCE
parameters, shared requests, timer release and retained drafts. Existing OAuth
gate/exchange/reset/fallback specifications retain their real authorization code;
classic fragment fixtures explicitly mount the real typed presentation scope.
`test_settings_oauth_lifecycle_browser.py` verifies close/reopen and device resume
through actual controls with synthetic intercepted responses.


### Settings independent tab activation and MCP reads

Problem: opening General also loaded MCP, Skills, tool inventory, context and
Database. Revisiting context could replace unsaved edits; closing Settings did
not retire Database or tool-inventory reads. MCP catalog/update/tool responses
could repaint the reused modal after close/reopen, while concurrent requests
repeated the same work.

Decision: `settings/tab-activation.ts` tracks first visits in the existing
`panel-read-session.ts` and registers each activated feature's cleanup there.
MCP, Skills, Tools and context load once per opening; Database preserves its
existing refresh-on-visit behavior. Configuration required by Save still loads
at opening, and independent tabs do not wait for that configuration. Context
also fences its follow-up memory read; inventory aborts and fences its own read.

`settings/mcp-settings-reads.ts` coalesces catalog, update and per-server tool
reads and checks opening, DOM and request identity before publication. Its
breaker refresh and countdown use the existing 3–15-second and 1-second policy,
but only while MCP is visible. Closing retires reads and observers; returning
to a failed catalog allows an ordinary fresh read. No new polling or retry
policy is introduced.

An acknowledged command needs a fresh post-write catalog even after closing.
That refresh remains independent of view cancellation, updates the common tool
summary and publishes markup only to its captured current opening. Ordinary
refreshes join it; older reads cannot replace it. Reconnect/update invalidates
cached schemas and reloads expanded tool lists. Install, connection, toggle,
rollback and confirmation authorities remain with their existing command owner.
The retained presenter supplies explicit transport and projection ports rather
than another request/timer store.

Alternatives considered: making every tab load on every visit loses edits and
repeats work; deferring shared config would weaken Save readiness; aborting all
post-write reads on close would leave the application tool summary stale; merely
checking DOM presence cannot distinguish successive openings using the same
nodes. A second Settings state machine is unnecessary because each feature
already has an explicit lifecycle.

Verification: native activation/read tests exercise lazy visits, coalescing,
retirement, late errors, hidden timers and accepted-command settlement. Actual
Chromium controls verify network activation, close/reopen and preserved context
edits with synthetic endpoints. Retained MCP optimistic/error contracts and
neighboring Save, OAuth, Tools, context and Database suites remain applicable.

### MCP command input and presentation ownership

Problem: installation continuations read the mutable current dialog target;
closing or replacing the dialog could redirect polling or close a later form.
Old confirmations still issued destructive commands after Settings retired.
Custom server creation claimed successful connection without checking its
response. Concurrent per-tool full replacements could settle out of order, and
rollback could overwrite a later edit.

Decision: `settings/mcp-commands.ts` owns install, connection, removal, update
and custom-server commands through the existing MCP API facade. Inputs are
captured before awaiting. Repeated submissions in the same dialog join its
pending command; pending card state has operation identity so one completion
cannot clear another operation. Accepted installation observation retains the
existing 2.5-second cadence and six-minute deadline, even if its view closes.
Acknowledged writes refresh the common catalog/tool summary independently of
view lifetime. A created server remains committed when its connection fails;
the form reports that failure and catalog refresh still occurs.

`mcp-command-view.ts` attaches feedback and dialog resources to the common
Settings opening. Closing retires callbacks/timers and clears temporary form
values. Delayed success only closes its original unchanged form. An unchanged
successful submission remains disabled during that delay; a later edit cancels
the close and permits a new explicit submission. Confirmations must retain the
same view before authorizing a write. Retired failures remain in debug output
without replacing a later dialog's feedback.

`mcp-tool-selection.ts` retains only outstanding user intents and the last
acknowledged tool policy for each server. It sends one full replacement at a
time for that server, immediately projects queued edits, and rebases them after
success or refusal. Different servers remain independent. Fresh tool rows
adopt pending intent by tool name. Failed rendering cannot undo an acknowledged
write or strand a queued edit; idle server records are released before callers
resume. This is command ordering, not a new admission or resource limit.

Alternatives considered: aborting all commands on close loses accepted effects;
checking only overlay visibility accepts a replacement form using the same
nodes; discarding older responses does not fix out-of-order server writes;
capturing whole optimistic snapshots carries a refused earlier edit into later
requests. A second catalog or server-policy store is unnecessary: presentation
rows and API authority remain with their existing owners.

Verification: native command scenarios cover capture, stale confirmations,
close/reopen, acknowledgement, duplicate submission, partial connection failure,
independent servers, failed-write rebase and projection fault injection. Their
drivers require a completion receipt, since a pending Promise alone does not
keep Node alive. Existing optimistic/error and read-lifetime contracts remain
applicable; real-browser tests exercise the actual dialog/action composition.
