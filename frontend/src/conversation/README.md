# Conversation browser domain

Responsibility: converge Conversation Sync v3 into one normalized browser
state, one typed presentation model, and one owner of the conversation DOM.

## One-hop map

The machine-readable authority is
`contracts/frontend_conversation_architecture_v1.json`. During migration the
existing owners remain under `frontend/src/core/`; new conversation code lands
under this directory and follows the target layers declared by that contract.

```text
snapshot / ordered event
  -> infrastructure sync adapter
  -> domain TurnStore
  -> presentation selectors
  -> ui ConversationSurface

users.me payload
  -> core/current-user.ts
  -> composition-owned current owner lifecycle

authenticated owner + push-frame owner
  -> core/frame-identity.ts
  -> retained invalidation adapter (temporary, no ownership policy)

UI intent
  -> application command service
  -> generated API client
  -> authoritative acknowledgement / ordered event

catalog conversation ID + explicit refresh intent
  -> application/conversation-refresh.ts
  -> injected core Turn runtime hydrator
  -> authoritative snapshot/cursor path

pageshow / online wake signal
  -> application/conversation-wake-recovery.ts
  -> bounded live-attempt selection and injected Turn wake port

swarm push frame
  -> application/swarm-presentation-overlay.ts
  -> transient Turn overlay -> authoritative terminal hydration

browser metadata boot
  -> application/conversation-startup.ts
  -> catalog/folders + active presentation (no Turn hydration/dispatch port)

catalog/settings metadata
  -> retained catalog adapter
  -> metadata-only shell + IndexedDB metadata cache

authoritative catalog request/retry + applied-snapshot validation
  -> application/conversation-catalog-loader.ts
  -> retained synchronous merge/render port + bounded best-effort cache port

catalog title/full-ID/default-setting query
  -> application/conversation-catalog-queries.ts
  -> injected live catalog + default + localized untitled label

local catalog metadata change
  -> application/conversation-catalog-reconciliation.ts
  -> timestamp policy + sorted catalog + cross-tab wake hint
  -> at most one pending sidebar animation-frame callback

ordered segments + explicit model-round / attempt / task scope
  -> presentation/round-outline.ts (contiguous runs; never diagnostic chronology)
  -> ui/round-outline.ts (keyed action state and reader-owned evidence disclosure)
  -> existing keyed ConversationSurface leaves (answers always outside folds)

compatible rich-card markup through the existing rendering/safety boundary
  -> ui/rich-card-reconciliation.ts (retained node/state, semantic action keys)
  -> classic-conversation-renderers.ts (same mounted renderer owner)

tool rounds + ordered narration segments
  -> presentation/tool-execution-groups.ts
  -> retained HTML adapter (temporary, no grouping/attempt-label policy)

tool name + projected round metadata
  -> presentation/tool-round-presentation.ts
  -> presentation/tool-round-icons.ts
  -> retained HTML adapter (temporary, no family/label/color/icon/program policy)

Conversation Sync provenance block
  -> presentation/turn-provenance.ts
  -> retained lexical render ports (temporary, no payload/markup/action policy)

write-tool result metadata / legacy refusal badge
  -> presentation/write-gate-refusal.ts
  -> retained write-card slots (temporary, no refusal/copy/escaping policy)

settled tool round + first-result metadata + trusted header slots
  -> presentation/tool-result-presentation.ts
  -> retained ordered dispatcher (temporary, no diff/compaction/truncation policy)

tool-catalog/web/fetch round + projected results + trusted header slots
  -> presentation/tool-search-presentation.ts
  -> retained ordered dispatcher (temporary, no search grouping/deduplication/bounds policy)

read/inspect/preview/generate image round + first result + trusted header slots
  -> presentation/tool-image-presentation.ts
  -> retained ordered dispatcher (temporary, no URL/localization/bounds policy)

browser_execute_js round + first result + trusted header slots
  -> presentation/tool-browser-execution-presentation.ts
  -> retained ordered dispatcher (temporary, no parsing/localization/bounds policy)

run_command/code_exec round + first result + interaction booleans + trusted slots
  -> presentation/tool-command-execution-presentation.ts
  -> retained timer/interrupt/expansion lifecycle (no parsing/QR/render policy)

pending write-tool round + approval risk metadata + trusted header slots
  -> presentation/tool-approval-presentation.ts
  -> retained ordered dispatcher (no risk/diff/localization/action-string policy)

escaped data-approval-id + static resolveWriteApproval dataset action
  -> restricted action registry
  -> retained approval authority (id never interpolated into action code)

synthetic inbox / peer / operator-steer / stall-nudge round
  -> presentation/tool-injection-presentation.ts
  -> bounded XML/text/Markdown/title projection with one closed lane order
  -> retained chronology + peer-jump lifecycle (no rendering policy)

ask_human round + presentation-local translation overlay
  -> presentation/tool-human-guidance-presentation.ts
  -> bounded awaiting / expired / skipped / submitted projection
  -> escaped datasets + static delegated response actions
  -> ui/human-guidance-actions.ts
  -> bounded card-scoped DOM + single-flight response/rollback lifecycle
  -> retained EN-to-CN arrival translation only (lexical presentation port)

untrusted image or external-asset source
  -> presentation/image-source-policy.ts
  -> typed image/command presentation owners

untrusted HTML interpolation
  -> ../html-safety.ts
  -> direct typed/lazy feature imports
  -> retained main-runtime composition aliases (temporary, no escaping policy)

typed or legacy error value
  -> ../api/errors.ts (shape normalization)
  -> ../error-presentation.ts (localized, safe, bounded display policy)
  -> retained composition aliases (temporary, no error policy)

server capability_taxonomy
  -> ../core/model-capability-taxonomy.ts
  -> retained/lazy picker ports (temporary, fail-open but no copied taxonomy)
```

## Surface ownership

`ui/conversation-surface.ts` composes one keyed DOM lifecycle. Its collaborators
have explicit, independently readable responsibilities:

| Owner under `ui/` | Responsibility |
|---|---|
| `surface-contracts.ts` | Renderer, intent, window and scroll ports; types only |
| `surface-windowing.ts` | DOM budgets and option validation; store history stays intact |
| `conversation-viewport.ts` | Follow suspension, visible-content scroll anchoring and listener disposal |
| `surface-presentation-equality.ts` | Bounded semantic repaint decisions; no DOM |
| `surface-dom.ts` | Keyed children and singleton slots; preserve unmanaged editor hosts |
| `surface-default-renderers.ts` | Text-safe fallback markup; production renderers are injected |

The composition entry re-exports its existing public ports; collaborators
import the type owner directly and never import the composition to discover policy.

The viewport owns the only follow latch and all automatic scroll writes.
`followLatest()` restores every mounted lane/block window to the logical tail;
`followLatest({force:false})` preserves reader suspension and never rebuilds the
transcript. `isNearLatest()` includes unmounted later content. Retained buttons,
send/layout nudges and mobile keyboard geometry forward these same intents.
Surface commits publish bounded `mountedTurnIds` to presentation consumers;
task-time reads do not observe the DOM or rescan it. The controller coalesces
graph and asynchronous presentation updates through one fenced commit path;
explicit disclosure/display actions flush that path before another UI action.
Cancellation, host replacement and inactive-conversation checks guard every
delivery. Scroll anchors retain separate leaf and Turn fallback offsets.

The equality owner charges each compared value against 4096 visits and stops at
depth 32. Matching identity remains constant-time; oversized or cyclic values
request a safe repaint. Queue receipts use the same semantic comparison rather
than serialized copies, so object key ordering does not repaint an unchanged
receipt. DOM slot ordering walks siblings without filtered arrays and viewport
capture reuses the first visible anchor geometry. These are optimizations, never
permission to omit visible changes or truncate authoritative history.

Executable specs: `ui/surface-presentation-equality.test.ts`,
`tests/frontend/test_frontend_conversation_surface_vite.py`, and
`tests/visual/test_visual_round_outline.py`.

## Renderer ownership

`ui/classic-conversation-renderers.ts` is the public composition port: block
kind dispatch and disposal only. Content families live under `ui/renderers/`;
[its guide](ui/renderers/AGENTS.md) names every semantic owner and verification
path. Markdown, localization, media resolution, rich markup and lazy-result I/O
remain injected through `renderers/ports.ts`. The facade re-exports those types
for existing consumers but never becomes another rendering-policy owner.

The content graph is acyclic, enforced by `npm run check:renderer-boundaries`. Common glyphs, localized copy and stable HTML
primitives have dedicated owners. Activity rows reuse the canonical compaction
receipt subprojection, and queue and input surfaces share background-result
markup. Thinking disclosure retains its explicit WeakMap cleanup; lazy tool
results retain request cancellation through the existing lifecycle owner.
Unknown runtime block kinds render safely empty; a new typed kind must update
dispatch before the module can compile. Each content owner writes only the
Surface-owned node passed to it and never hydrates or mutates TurnStore.

Failure receipt copy uses its own document's clipboard capability, falls back
to a temporary textarea, and releases that node in `finally` even if native or
compatibility copy fails. The fallback restores the reader's focus; copy failure
leaves the control actionable and the full diagnostic evidence selectable.
Terminal activity errors reuse `error-presentation.ts` for localized, escaped
reason and guidance; the complete envelope remains in a separate technical
disclosure whose open state and summary focus survive repaint. Raw JSON as the
primary explanation was rejected because it bypassed the shared display policy.

The delivery budget classifies `main.ts` and `admin.ts` by their explicit
primary-entry identities even when Rollup also marks an entry dynamically
reachable. Their existing entry ceilings still apply; ordinary feature chunks
retain their smaller ceiling, and every graph byte counts once toward the total.
Applying both entry and ordinary-chunk ceilings to the same primary row was
rejected because a graph traversal flag does not change its startup role.

Executable specs: `renderers/failure-receipt.test.ts`,
`tests/frontend/test_frontend_conversation_surface_vite.py`,
`tests/background/test_background_completion_ui.py`,
`tests/frontend/test_system_intervention_ui.py`, and the real-browser round,
context-note, activity and long-conversation navigation checks under
`tests/visual/`.

## View-model ownership

`presentation/conversation-view-model.ts` composes the lane tree, cross-Turn
attempt/queue joins, background-command projection and whole-Turn memo. Content
projection lives under `presentation/view-model/`; [its guide](presentation/view-model/AGENTS.md)
maps each owner. `types.ts` owns presentation contracts independently of the
facade. `turn-blocks.ts` owns pipeline order and its weak per-Turn block memo.
Both graphs are checked by `npm run check:renderer-boundaries`. Projection
remains DOM-free; neither a split nor an index may mutate durable records.

**Problem:** program-parent insertion repeatedly scanned the remaining rounds
and spliced the block array, making long program histories quadratic.
**Decision:** a uniquely owned gateway is replaced in its authored segment
slot by the program parent, once per occurrence. A projection-local call/round/
scope index admits a unique parent in constant lookup work; reciprocal placement
prefers a scoped gateway over an unstamped shell and retains equally supported
ambiguous gateways. Parent block identities are claimed in durable sidecar order,
not gateway arrival order. Historical `execute_tools` without parents bypass the
lookup entirely. Only remaining sidecars enter
the temporary linked sequence: declared-child lookup, then the last authored
attempt/task/model-round anchor (with unique compatible historical scope), then
suffix minima for later-program bounds and legacy later-tool fallback. This keeps
partially hydrated sibling programs in sidecar order. This placement pass takes
O(blocks + rounds + child IDs) work and
rebuilds the caller-owned output once. All nodes and indices expire at return;
no resident cache or concurrency limit is introduced. **Alternatives rejected:** array-splice insertion retains
quadratic shifts even with an index; a persistent placement cache duplicates
immutable Turn memo ownership and needs another invalidation lifecycle.

Measured on 2026-10-03 using the exact preceding implementation, 2,000 childless
parents reduced suffix tool evidence reads from 1,999,000 to 2,000 (78.76 ms to
1.74 ms); 4,000 reduced 7,998,000 to 4,000 (309.04 ms to 1.46 ms). Timings are
single local observations; deterministic read counts establish the scaling.
10,000 seeded mixed-order cases matched preceding output, including repeated
call IDs and round counters. Co-local tests retain chronology and linear-work
contracts. Whole-Turn memo hits also skip terminal error normalization; source
identity changes still produce fresh finish details.

Executable specs: `presentation/view-model/program-blocks.test.ts`,
`presentation/view-model/turn-memo.test.ts`,
`tests/frontend/test_frontend_conversation_surface_vite.py`,
`tests/conversation/test_conversation_view_model_note_grouping.py`, and the
real-browser round, activity, note-group and navigation suites.

## State scopes

- Durable document facts: server-owned turns, attempts, settlement, content
  blocks, ordering, revisions and action capabilities.
- Live authority: backend-authored attempt and phase changes reduced into the
  same TurnStore; connection health is lifecycle-scoped browser state. There is
  no conversation-level phase or task mirror. Page/network wake listeners have
  one disposable application controller and share the bounded async pool.
- Swarm telemetry is a page-lifetime overlay with one idempotent push
  subscription. It rebases onto authoritative Turn revisions and is removed
  only after terminal hydration; it never mutates the durable projection.
- Local interaction facts: focus, selection, scroll anchor, translation
  activity, Human Guidance feedback, artifact binding, block expansion and
  inline turn-edit sessions, always keyed by stable identity and never copied
  into the durable model. An edit session owns a persistent host element that
  `reconcileTurnInlineEditors()` re-attaches after each surface commit, so an
  open draft survives authoritative repaints without touching TurnStore.
  The
  surface's per-commit content-part ordering indexes only managed parts, so it
  never displaces the host (moving a focused node would blur it), and a
  genuine remount restores the caret position recorded at the last input.

  Translation display mode follows the same rule: the typed view model reads
  translated/original content from the authoritative Turn projection and the
  selected mode from a turn-keyed local map. The retained task adapter resumes
  only IDs started in the current page lifetime; Conversation Sync v3 delivers
  durable completion. Do not recreate a message translation model or scan
  conversation history to infer missing work.

  Images pasted into an edit session attach to the turn, not the composer:
  the caller's `onImageAttach` port runs the shared compress/upload core and
  the session keeps an optimistic chip list whose payloads `onSubmit` hands
  back for the caller to merge into the turn projection. A still-processing
  chip blocks save because its projection payload does not exist yet.

## Dependency and mutation rules

- Domain and presentation modules are pure and do not read browser globals.
- Infrastructure implements application ports; application does not import an
  infrastructure implementation.
- UI receives a view model and emits intents. It performs no fetch, retry,
  persistence, settlement inference or event folding.
- `ConversationSurface` is the only content-derived writer beneath the chat
  root. Running and terminal content share one `data-turn-id` node.
- Catalog shells and IndexedDB contain metadata only. They never expose a
  second transcript document to feature code.
- Settings persist through one catalog seam regardless of Turn count. A local
  shell remains `_localOnly` until the first accepted Turn; lazy features call
  the injected settings-capture port instead of owning a second write path.
- Scroll capture/restore, follow suspension and the bounded DOM window live in
  the lifecycle-scoped Surface viewport port. The adapter injects only the
  viewport element; a conversation shell cannot carry renderer lifecycle
  flags, and windowing never truncates TurnStore.
- Diagnostics read `ConversationTurnRead` and the active Surface dataset. They
  never fetch or parse a second transcript.
- `ConversationTurnRead` is a pure lookup over an already-created runtime. A
  catalog/sidebar read may not call `ensureRuntimeStore`, create a sync
  coordinator, publish health, or trigger rendering; 500 metadata shells must
  still allocate zero Turn runtimes until an explicit hydrate/command path.
- A presentation cache must have conversation disposal or an explicit bound.
- Retained runtime is a temporary one-way adapter. Every touched path must
  shrink a legacy-debt metric in the architecture contract or remove the path.
- Explicit refresh orchestration lives in `application/conversation-refresh.ts`;
  catalog lookup, Turn hydration, retry presentation, and logging remain
  injected ports, and the command is never published as a browser global.
- Pre-send translating/connecting presentation rides the optimistic assistant
  Turn: the send pipeline re-labels that single bubble in place via
  `withOptimisticAssistantPreparation` (preparing → connecting → translating),
  so preparation never stacks a second agent row. Only steer sends, which
  paint no optimistic pair, fall back to the standalone status Turn owned by
  `application/send-preparation-overlay.ts`; it keys teardown to the
  conversation that initiated the send and receives catalog, transient-store,
  i18n, and scroll behavior through explicit ports.
- Transitional rich tool HTML is applied only by
  `ui/classic-conversation-renderers.ts`. Live Swarm subtrees reconcile there
  in place so animation nodes, reader disclosure, focus, and scroll survive
  projection updates; retained runtime helpers do not mutate Surface DOM.
- Catalog title, unique-prefix/full-ID, and auto-translate-default queries live
  in `application/conversation-catalog-queries.ts`. They are pure over explicit
  inputs; the composition adapter supplies live catalog/default/localization
  values without publishing query functions to `runtimeScope`.
- Local catalog-change reconciliation lives in
  `application/conversation-catalog-reconciliation.ts`. Its injected ports
  keep busy-conversation activity timestamps authoritative, sort the metadata
  catalog, publish invalidation, and bound sidebar rendering to one pending
  animation frame. It owns no transcript, storage, or Turn hydration path.

## Verification ladder

1. Pure reducer/selector/component owner test.
2. Generated conversation contract check.
3. Store-to-surface boundary test in the real Vite graph.
4. Browser journeys for switch-during-stream, reconnect, retry, translation,
   branches, long history and scroll anchoring.
5. `make architecture-check`, `npm run check:frontend`, and the frontend
   source/bundle budgets.

## Tool-card header

| Stage | Owner |
|---|---|
| Pre-execution title | `lib/project_mod/tools.py::project_tool_display` |
| Settled metadata and `editSummaries` | `lib/tools/meta.py::build_project_tool_meta` |
| Structured refusal stamping | `lib/tasks_pkg/handlers/project.py`; per-edit outside-workspace facts in `lib/tools/meta.py` |
| Header projection | Pure, DOM-free [`createToolHeaderPresentation`](presentation/tool-header-presentation.ts): `queryText` normalizes recognizable historical batch-count titles; `artifactHeaderHtml` delegates saved-output copy; `renderBadgeHtml` owns badge selection and HTML |
| Saved-output continuation | [`tool-artifact-presentation.ts`](presentation/tool-artifact-presentation.ts) owns localized action/source/search-term copy and requested scope inside the existing result disclosure |
| Repo-map requested scope | [`tool-repo-map-presentation.ts`](presentation/tool-repo-map-presentation.ts) shares localized identity and visible supplied `path`/`focus`/`max_tokens` across live, settled and historical headers; missing fields never imply defaults |
| Refusal interpretation and rendering | [`write-gate-refusal.ts`](presentation/write-gate-refusal.ts) |
| Card/diff body | [`tool-result-presentation.ts`](presentation/tool-result-presentation.ts) |

Generic terminal failures use the same authoritative/deferred result disclosure
as settled outputs. `tool-failure-presentation.ts` owns safe diagnostics (512
UTF-16 units, 32,768-unit JSON parse budget, depth 6 and 32 visits), including
historical artifact summaries containing MCP-prefixed serialized errors. Object
`toolContent` and results-only evidence follow the existing result-source owner.
Private artifact references, digests and cursors, malformed/oversized JSON and
unknown wrappers never become header or tooltip copy; raw evidence stays folded.
Missing evidence still shows an honest failed verdict, without reexecution.

The generic diagnostic marks its disclosure with `data-tool-failure-evidence="collapsed"`;
`ui/round-outline.ts` keeps raw envelopes folded while the failure reason and
attention verdict remain visible. Reader choices survive replay; structured
interactive/error evidence retains its existing first-paint behavior.
**Problem:** legacy errors dumped giant serialized output inline and in titles.
**Decision:** share bounded diagnostics with artifact cards and reuse lazy evidence.
**Alternative rejected:** clipping raw JSON still leaks storage metadata and hides
useful nested failure messages. Specs: `tool-failure-presentation.test.ts`,
`tool-result-presentation.test.ts`, `tests/frontend/test_generic_tool_failure.py`.

Repo-map requested scope stays visible even while its result is folded. Object/JSON
arguments parse within 32,768 units; strings are escaped and visibly clipped at
512 units. Missing fields never imply defaults; malformed/missing historical
arguments retain a bounded useful original title. Pointer selection does not
toggle the result; native keyboard disclosure, result statistics and output stay intact.
**Problem:** a fixed explanation hid request identity. **Decision:** separate scope
from statistics. **Alternative rejected:** hover-only parameters fail on touch/copy.
Specs: `tool-repo-map-presentation.test.ts`, `tests/tools/test_tool_header_presentation.py`
and `tests/visual/test_visual_repo_map_parameters.py`.

Projected rounds flow through these presentation owners to retained
`runtime/sections/ui/tool_rounds.js`, which delegates header policy;
`runtime/sections/_prelude.js` composes the ports (paths relative to `frontend/src/`).
Normalize redundant counts in recognizable historical titles without mutating
stored records. Settled batch headers show one success/total badge, green only
for complete success and red otherwise; custom error copy and refusal diagnoses
stay in detailed entries/notices rather than competing with that count. Structured
per-edit statuses outrank legacy counted badges. Unknown totals stay unknown.
Preserve ambiguous titles and all body evidence. Single-write and non-edit badges
are unaffected. The edit-list owner removes the nested generic result rail/inset.


Saved tool-output continuations show a localized action title, a meaningful
source label once when provenance exists, and labeled search terms on a separate
wrapping, selectable line; pointer selection never toggles the disclosure.
Technical aliases/digests, source round, requested byte offsets, JSON selectors
and byte/match limits are inside the same existing result disclosure, outside
the replaceable lazy result body; no nested wrapper or hover-only control.
Opaque cursors remain private and mean continuation, never a byte/page claim.
This is requested scope, not returned coverage. Heterogeneous batches do not
inherit the first result's provenance. Known sparse legacy labels normalize;
malformed/ambiguous historical arguments retain their original title. Argument
parsing is capped at 32,768 units, item scans at 16, terms at 200, source at 512
and selectors at 1,024; text clipping and omitted items are visibly marked.
Problem: repeating aliases and cursors per pattern obscured the action and made
error-shaped search terms look like failures. Decision: one pure typed owner
separates intent from technical evidence while preserving stored results, lazy
loading, failure reasons, focus and open state. Alternatives rejected: shorter
hashes still describe storage rather than purpose; hover-only +N patterns hide
requested intent on touch devices; deleting scope loses diagnostic evidence.
Executable specs: `tests/frontend/test_artifact_continuation_chip.py` and
`tests/visual/test_visual_tool_artifact_readability.py`.
Header normalization is capped at 8,192 text units, 120,000 argument units and
128 edits; oversized or ambiguous titles remain verbatim. Explicit badge copy
is capped at 2,000 units. The refusal owner bounds its projection to 32 paths
of 512 units and unknown kinds to 160 units, with visible ellipses; stored
facts are untouched. Terminal-error headers use the same refusal-first policy.

Focused verification: `npm run check:tool-header` covers
`tests/tools/test_tool_header_presentation.py`, label/gate tests, and the existing
wire-parity test.

### Intent-first action stream

`ui/history-process.ts` is the Surface-owned historical disclosure boundary.
Settled assistant turns show the last explicitly authored final-answer scope and
other deliverables, with compact important result/error receipts outside. All
nonfinal process evidence remains in authored order inside one native `details`.
This intentionally replaces the previous historical “tools/narration always
visible” contract. Close/unmount cancels existing lazy result readers and drops
all process leaves; expansion mounts a viewport-budget content page (at most
200 entries). Explicit previous/next buttons browse all evidence; contained inner
scroll never shifts outer turn/block history windows. UI-only choices are keyed
by presentation/block identity, retained across repaint/eviction, and cleared on
conversation reset/disposal; no projection/result mirror or second store exists.

`ui/round-outline.ts` still lets each mounted action own its summary and keyed
evidence disclosure. Live/failed/waiting/interactive/background-unresolved turns
retain this original actionable stream, never a fabricated completed answer.
Legacy/missing final boundaries keep narration with an explicit fallback notice.
`presentation/round-outline.ts` supplies accepted scope and action state;
`tool-execution-groups.ts` remains the risk/attention classification authority.
Executable historical contracts: `tests/frontend/test_frontend_history_process.py`
and `tests/visual/test_visual_historical_process.py`.


`program-gateway-presentation.ts` normalizes old exact native `execute_program`
rows with serialized arguments for the existing unified card, without a second
owner or durable copy. Synthetic ownership suppression stays occurrence-scoped.
Only complete authoritative results (including a full snapshot preview with
matching declared character witness) support retained-input directory counts.
Missing counters are unavailable; gateway settlement says Returned. Script
return shapes never supply host outcomes or statistics. Raw evidence remains
independently disclosed, including existing lazy result hydration.
Executable specs: `ui/program-gateway-card.test.ts` and
`tests/visual/test_visual_reported_tool_cards.py` (raw history plus synthetic pair,
desktop/mobile with reload).

`presentation/command-card-chrome.ts` shares escaped full intent and localized
secondary execution context across terminal and browser presenters. Each
presenter supplies one verdict and independent code/output controls, preserving
bounded fields, refusal precedence, live controls, and reader expansion state.
`styles/application/23-reading-execution.css` owns the token-based grid:
wrapping selectable purpose first, compact outcome second, context/evidence
controls beneath. Native checklist summary keeps progress; revisions/history
remain in its body. Only proven self-contained cards lose nested outer chrome.

Executable specifications: `tests/frontend/test_frontend_round_outline.py`,
`tests/tools/test_tool_{command,browser}_execution_presentation.py`, and
`tests/visual/test_visual_intent_first_actions.py` (isolated live app, both
locales, desktop/phone, light/dark/Tofu themes, keyboard and replay state).

## History-search receipts

Problem: a fixed past-search label discarded the keyword, repeated the source
badge, and rendered English empty-result prose while losing pagination health.
Decision: `ui/project-receipt/search.ts` renders same-query `conversationSearch`
metadata from `lib/conv_ref/_query.py` through the normal tool handler. The sole
summary carries keyword, actual scope and returned count; the body localizes
zero-hit versus incomplete/unknown coverage and states indexed-field limits.
Records without metadata retain raw output without invented coverage/counts.
The backend consumes v2 pages (including empty continuation pages), bounds work
to four pages, uses metadata-only owner/project hydration, and refuses explicit
project scope without context. Tool parameters/results remain outside the index.
Alternatives rejected: prose-parsed success, unconditional widening, foreground
transcript loading, and treating the first empty page as complete absence.
Specs: `ui/project-receipt/search.test.ts`,
`tests/conversation/test_conv_ref_search_receipt.py`, and
`tests/visual/test_visual_conversation_search_receipt.py`.
