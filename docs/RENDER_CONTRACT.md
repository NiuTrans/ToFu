# Conversation rendering contract

This document defines the current browser rendering invariants. The storage and wire authority is [CONVERSATION_SYNC_V3.md](contracts/CONVERSATION_SYNC_V3.md); the frontend module boundary is [FRONTEND_ARCHITECTURE.md](FRONTEND_ARCHITECTURE.md).


## Navigation operation ownership and request-name refusals

**Problem:** a head advance or conversation switch during a rank jump invalidated
publication but also prevented its busy cleanup, permanently disabling navigation.
**Decision:** exact navigation serializes reads and jumps through one identity-owned
busy slot, independent of the publication generation. Both operations coalesce
refresh demand; only the slot owner releases it and starts the latest queued page.
Stale read/hydration results never scroll or publish. **Rejected:** resetting busy
on every update permits overlapping operations; generation-gated cleanup leaks the
slot. Specs: `tests/turn_navigation.test.cjs` and
`tests/visual/test_visual_exact_round_navigation.py`.

**Problem:** deleting an inherited template-face selector appeared successful but
routing silently restored it; adding a face alias could choose an owner-disabled
Connection and report success. **Decision:** route planning refuses removal of a
required template selector visibly, before editing identity, headers or route.
Ordinary unshared inherited headers remain removable; shared removals remain
refused. A matching disabled face is refused with an explicit instruction to enable
it in Connections; no silent enable or duplicate enabled face bypasses owner intent.
Validation and duplicate checks precede route staging, so refused edits leave the
owner draft unchanged. **Rejected:** silently restoring selectors hides a refused
edit; silently enabling connections overrides an owner decision. Specs:
`tests/request_name_edit_outcomes.test.cjs` and
`tests/providers/test_provider_request_names_browser.py`.

## Reasoning without a readable summary

**Problem:** a provider can report substantial internal reasoning-token usage
while returning no readable reasoning text; an absent thinking disclosure made
that look like reasoning was disabled. **Decision:** the common assistant header
shows a compact, text-only receipt row, outside historical Process and Thinking
folds, when readable reasoning is absent but recorded reasoning evidence exists.
It distinguishes the last returned effort from provider-reported token usage.
Effort comes only from validated selected-slot resolution receipts, never the
composer, model catalog or encrypted replay. Turn-level token counts are not
attributed to one effort setting; explicitly incomplete usage is labelled as a
lower bound, and legacy last-round fallback counts name that scope.

Completed replies with positive reported reasoning tokens say that the reasoning
summary is unavailable. Active replies say no readable summary has been received
yet; failed/interrupted replies never claim successful provider completion.
Unknown/unreported counts and efforts stay absent rather than becoming zero or
a default. Malformed, discarded, auxiliary, foreign and provably stale receipts
cannot supply effort. Unscoped historical receipts are labelled as last recorded,
not as the currently running request. During a live resumed/regenerated attempt,
inherited unscoped effort and aggregate usage remain absent until an existing
receipt explicitly matches the current attempt/task. The resume acknowledgement
and later unrelated projection revisions are not evidence of a new request.
Late fields retain the same effort → token count → availability reading order as
cold reload, without replacing unchanged status spans or answer nodes.
If readable reasoning arrives, its existing
safe Markdown, translation and disclosure path remains authoritative and replaces
the no-summary row. Empty or encrypted reasoning never creates an expandable
Thinking card. Chinese/English copy and small-screen wrapping share the same
projection/renderer owners; no reads, timers or durable browser state are added.

**Alternatives rejected:** synthesizing a reasoning explanation fabricates model
output; treating `thinking=0chars` as zero reasoning conflates text and usage;
showing the current composer effort misattributes old turns and retries; hiding
the receipt inside Process keeps the observed-work distinction inaccessible.
Specs: `tests/visual/test_visual_reasoning_summary.py`, the real app/storage/reload
journey `tests/e2e/test_e2e_reasoning_receipts.py`, and focused reasoning
presentation/header tests under `frontend/src/conversation/`.

## Repaired tool arguments

Problem: a repair badge alone hid which argument correction was applied. Decision: the shared tool-result presenter shows unique repair categories beside the existing badge, with three visible chips and remaining categories in one counted disclosure tooltip. Unknown categories retain their escaped machine name. A failed tool keeps the warning style and never claims a successful fix. Alternatives rejected: duplicating labels across retained renderers or treating argument repair as proof of tool success.

## Rejected tool calls

Problem: a pre-execution rejection showed only the violated rule (“Invalid length at $.edits[0].path”), so the request the model actually sent — a blank per-edit `path`, a foreign harness shape, a missing key — was invisible in the UI and had to be recovered from logs or the persisted wire. Decision: the rejected-call row renders the call's own `toolArgs` beneath its reason: short payloads open in place, payloads over 600 characters stay collapsed with their size on the summary line and disclose the first 4,000 characters, with the reason stating that the wire retains the rest. The block is always escaped text in a `<pre>`, never parsed affordances, and is omitted for a rejected call with no arguments. Alternatives rejected: parsing arguments into per-tool field hints (duplicates each tool schema in the renderer) and relying on the reason string alone (cannot distinguish a model omission from a harness-side drop). Specs: `tests/frontend/test_frontend_tool_rounds_render.py` and the wire-parity battery lock the shown/hidden and escaping behaviour.
## Selected text actions

Problem: startup-only translations leaked message keys; reading the selection again on click lost its text, and a created branch did not enter its composer. Decision: `selection-popup.ts` captures a passage from one rendered Turn, refreshes localized controls on opening/language changes, and uses the shared UI factories. Quote adds exact text once to existing composer quotes, preserves the draft and focuses input; previews disclose the whole passage and support removal. Existing send acknowledgement/rollback remains the quote-state authority. Branch uses the existing lane command and enters that acknowledged lane only in the originating conversation. Running Turns permit quotes but defer branching until settlement. Selection supports pointer, touch selection changes and keyboard Tab/arrow access; Escape, outside clicks, scrolling and disposal dismiss the bounded floating UI. Viewport clamping includes narrow layouts; no polling or startup reads are added. Alternatives rejected: raw key fallbacks, re-reading selection at click time, a separate branch transport and automatically sending a generated question. Verification: `tests/visual/test_visual_selection_popup.py` and the authoritative composer/Turn runtime contracts protect layout, focus, retry and ownership.

## Completed replies awaiting review

**Problem:** a completed model reply can contain only a plan or a partial fix;
removing the running indicator makes it indistinguishable from a reviewed reply.
**Decision:** the sidebar shows “待查看 / To review” for a canonical completed
main-lane assistant/planner reply observed by the client. A visible, focused
conversation Surface commit clears its receipt only when that reply is mounted.
Background tabs, failed loads, and merely selecting a catalog row do not clear it.
Running, waiting, failed, interrupted and background-work indicators keep priority.
The label signals an unseen reply, never acceptance or task success.

`conversation-review.ts` owns browser-local receipts scoped by authenticated user
and conversation. Turn/attempt identity rearms reminders after follow-up or
regeneration; metadata edits do not. Receipts survive reload and synchronize
between tabs on this browser; they are not cross-device read receipts. Cold
catalog shells reuse observed receipts without fetching transcripts. Completions
that this browser has never hydrated remain unknown until normal sync loads them.
One small record per encountered conversation stores identity/order/viewed state,
not content; memory follows the loaded catalog, with no polling or extra requests.
**Alternatives rejected:** treating completion as success misstates the result;
a second transcript fetch for every sidebar row defeats demand-driven loading;
marking read on selection hides reminders when the result fails to load.

## Interface copy

**Problem:** persistent introductions described architecture, storage and agent
workflows before the user could reach the controls or results.
**Decision:** default copy names the action, current state or consequence relevant
to the user's next decision. Remove redundant introductions and implementation
narration; use existing status projections and disclosures for progress and
diagnostics. Keep costs, authorization scope, destructive effects, pause/cancel
differences and evidence limitations explicit at the relevant control or result.
Maintain equivalent Chinese/English meanings and interpolation parameters.
**Alternatives rejected:** merely shrinking explanatory paragraphs leaves the
same reading burden; removing all hints conceals material operation consequences.

## Requesting revisions from an edit diff

Problem: users inspecting a tool's edit had to copy code and manually identify
the file before requesting a revision. Decision: successful write-tool diffs
offer **Request a change** inside the existing disclosure. A selection quotes
the complete intersecting code lines from exactly one diff; without a selection,
the action quotes the change. Quotes stop at 80 lines or 6,000 characters and
ask for a smaller selection rather than silently clipping evidence.

The existing removable reply-quote chips receive the frozen diff, file/root
and originating turn/tool-call/revision. The ordinary composer is focused for
the user's request; existing draft bytes remain unchanged and identical quotes
are not added twice. The action neither sends nor edits: normal sending, queueing,
approval and file freshness owners still govern execution. Quotes explicitly
require reading the current file and never claim snippet positions are absolute
file line numbers. Failed edits, unknown file paths and paper proposals do not
receive this action. Cross-diff selections, conversation switches and changed
bindings between pointer-down and click fail visibly without touching the draft.

Alternatives rejected: a separate review page duplicates navigation and draft
state; automatic submission leaves no place to express the desired correction;
line-number-only feedback misidentifies search/replace excerpts and stale files.
Owners: `conversation/ui/tool-diff-review.ts`, renderer `tool-blocks.ts`, and
`application-shell/interactions/diff-review.ts`. Two existing-scope listeners
and weak DOM bindings retain no history, background work or observers.

## Command working directory

Command cards show the supplied `working_dir` argument as a muted line inside
the command code pane, before `$ command`, in both running and settled states.
It folds with a collapsible command instead of taking a separate band outside
the pane. It is escaped and capped at 4,096 characters; missing arguments do
not invent a directory. This is requested execution context, not evidence
that a process started or resolved that path.

## Compact compression receipts

**Problem:** a full-width, four-line card repeated the unchanged main model and exposed a technical English trigger sentence as primary content. **Decision:** the default card uses a full-width header and one wrapping accounting row: before → after, reduction, recorded summary model, and a short localized trigger. Main-model/provider identity and the original trigger reason live in a native Details disclosure; failed/warning reasons stay visible. Disclosure state and keyboard focus survive updates. Unknown summary execution is explicit, never inferred from the parent or a selected-only route. Snapshot access and all stored receipt evidence remain unchanged; no new request or timer is introduced. **Rejected:** deleting diagnostics loses evidence; shrinking raw English or hiding failure reasons makes the card harder to understand. Specs: `activity-block.test.ts` and `tests/visual/test_visual_translation_compaction_attribution.py`.

## Translation attribution

**Problem:** a completed composer receipt obscured stored translation attribution.
**Decision:** show `translation.model` once beside the Translation fold or displayed
translated answer; missing evidence is explicit, never inferred from the main model.
Successful composer completion removes its row/ticker; concurrent pending work and
other failures remain scoped. Reader folds, focus, original text and escaped model
names survive replay/navigation/disposal. No attribution request or idle timer is added.
**Rejected:** picker-based guessing, duplicated badges and lingering composer receipts.

**Problem:** a re-run translation task can make an already translated body vanish and
stream back from its first chunk, because a plan preview ranked the lifecycle-local
partial above the completed body while every other block kind returns
`completed ?? partial` (`segment-blocks.ts`). **Decision:** a finished translation
outranks an in-flight preview in every block kind; `streaming` describes the painted
body, so it is false while the completed text is shown. The pending chip still reports
the running task. **Rejected:** hiding the chip (loses task state) and preferring the
partial behind a growth check (a retry is a re-translation, not a text prefix).
Spec: `presentation/view-model/translation-blocks.test.ts`.

**Problem:** inserting or queueing an instruction during generation created a
temporary assistant with a translating/connecting status, then removed it at
acknowledgement. **Decision:** the existing composer receipt retains the original
instruction and updates its preparation/translation phase in place. Only idle
ordinary sends create an optimistic input/output pair. Steer/queue sends,
including an idle-to-busy retry, create neither provisional transcript Turns nor
a standalone status assistant. The existing acknowledgement owns accepted
insertion/queue evidence; failure restores the draft and preserves the scoped
failure receipt. Navigation, stop and disposal use the existing send lifecycle.
**Rejected:** hiding the fake assistant with CSS retains duplicate transcript
state; a separate insertion spinner would duplicate the composer status owner.

## Content-first execution details

Mixed live/history content follows the shared [conversation reading
grammar](DESIGN_SYSTEM.md#conversation-reading-grammar): prose boundaries create
reading pauses, adjacent process leaves share a continuous activity surface, and
evidence opens under a common label column. Explicit answer text has a localized
rubric and a distinct type hierarchy after authoritative Turn settlement.
**Problem:** the live `text:terminal` accumulator already carries wire-level
`terminal`/`deliverable` flags, so decorating those flags directly flashes Answer
on pre-tool narration until a later projection or translation replaces it.
**Decision:** `selectTurnBlocks` projects `deliverable: false` while the Turn is
pending/running, without changing source flags, prose or translation. Settlement
and resume change that presentation bit even when the text is identical, so the
keyed Surface repaints the title, type hierarchy and bilingual alternative
immediately. **Rejected:** translation completion, timers, DOM inspection and
content heuristics cannot establish answer identity. These are decorations of the
mounted chronology; no leaf is moved into a new wrapper. Visual density never
changes block order, stable identity,
reader-owned disclosure, actionable states or lazy result ownership. Historical
pagination is visible only when there is earlier or later content to navigate.

**Problem:** round number, call count and a routine "response returned" label were the primary collapsed content, hiding the assistant's actual narration. A long checklist explanation also competed with the header in flex layout. **Decision:** settled assistant history is **final-answer first**. The Surface selects the last explicit terminal/deliverable text and its accepted attempt/task/model-round scope; earlier final-looking text from older attempts is process, not the answer. Other durable deliverables remain outside. Completed status alone is not a success claim. Tool results, exit codes, refusal reasons and diagnostic evidence remain inspectable inside Process. Problem: the repeated outer tool-result receipt list overwhelmed the answer. Decision: remove that list; one compact, initially collapsed Process disclosure sits in the explicit answer toolbar (or before the answer in a renderer without that toolbar). Its label includes an item count and a trailing chevron; its bounded reading area mounts only on expansion. Alternative rejected: merely hiding the repeated receipt list retains duplicate DOM and presentation policy.

`ui/history-process.ts` owns one native `details` per historical presentation
identity. Thinking, intermediate narration, tools/programs, notes and diagnostic
chronology stay ordered inside it. This supersedes the older historical rule
that **all tool headers and narration were always visible**. Closing removes
process leaves and invokes the existing renderer cancellation port; closed
process neither mounts rich leaves nor hydrates lazy outputs. Opening mounts a
bounded content page (the existing viewport-derived block budget, maximum 200),
with explicit previous/next content buttons. Pages replace, not append, leaves.
Inner scrolling is contained and has no external block/turn window sentinels or
history intents. Outer navigation continues to select actual turns, not tools.

The Process body uses a native nonmodal popover when supported, preserving its
Turn ancestry for delegated events while escaping scroll/paint containment. It
floats at the right on desktop and at the bottom on narrow screens, without
moving the answer. The existing history owner permits one open inspector per
Surface; close/Escape restores focus and releases lazy leaves. The queued top
layer mount checks identity, connectivity and open state before acting, so
unmount/disposal cannot revive a detached panel. Unsupported browsers retain the
inline native disclosure. The answer toolbar is retained across replay; the
optional heading menu in `renderers/answer-navigation.ts` reads current direct
Markdown headings, caps controls at 32, and navigates/focuses within that answer.
There is no new document listener, timer, transport or transcript cache.

Live, failed, waiting-command/approval, interactive and background-unresolved
turns retain their actionable stream; an unresolved action overrides a settled
Turn stamp. Missing/empty explicit final boundaries retain narration and expose
an honest localized fallback notice instead of dropping it or inventing an
answer. Within an open process each action still owns its sole evidence
summary; there is no model-round/bulk toolbar or duplicated round heading.

Surface-owned, presentation/block-keyed UI choices retain process open/page,
scroll, focus, nested disclosure and reader expansion across replay, close/reopen
and Turn DOM eviction. Snapshots contain choices, never HTML/tool-result copies,
and cap each block's snapshot at 64 relevant reader nodes. Conversation reset and
Surface disposal release readers and pending reads. Accepted projections remain
immutable in the existing store; there is no new transport or second store.
**Alternatives rejected:** CSS-only hiding retains hydrated expensive leaves;
per-action-only disclosure still makes tools compete with the final answer;
scroll-driven process windows confuse tool browsing with turn history navigation;
deleting intermediate narration loses audit/debug chronology.

Block-window limits still apply to live/failed/fallback streams; historical
process counts content entries, not model rounds. A tail cap may not hide fewer
than four leading blocks. Oversized rounds may span bounded windows without
hiding the tail or stalling navigation.
Executable specs: `tests/frontend/test_frontend_history_process.py`,
`tests/frontend/test_frontend_round_outline.py`, Surface contracts,
`tests/visual/test_visual_historical_process.py` and
`tests/visual/test_visual_round_outline.py`.

Tool SVG icon slots are transparent, without background tiles or shadow halos,
including live process groups and reopened historical inspectors. Marker geometry,
row hover/focus feedback and thinking-state indicators retain their own styling.
Browser specification: `tests/visual/test_visual_timeline_reading.py`.

Checklist headers keep their title, progress and count on one readable row;
rationale lives in the body, complete on both desktop and phone. The raw tool
record remains unchanged. Repo Map uses the shared conversation disclosure marker and keeps its localized explanation; supplied `path`/`focus`/`max_tokens` stay wrapping and selectable in live, stopped, settled and folded headers.
[Its request-identity owner](../frontend/src/conversation/README.md#tool-card-header) defines escaped argument/history bounds; file badges and original output stay intact. Run Command has no
budget boilerplate on untruncated content: an exact display cap appears only
when the presenter actually omits text; upstream-omitted output uses a neutral
omission note instead of inventing a 120,000-character bound.

**One action, one summary.** A structured checklist has one native disclosure, not an outer “Checklist: N steps” wrapper plus an inner progress row. Update history belongs inside the card. Terminal/browser intent is complete, selectable, and naturally wrapping within the existing 4,096-unit safety bound; it never relies on hover or ellipsis. Execution context uses the shared icon slot and the stored browser page title when known; redundant Terminal/Browser labels and numeric tab IDs are omitted. Code/output fold independently. One outcome chip reports the exit code or exceptional state; an extra routine “finished” chip is redundant. Refusal reasons, active timers/actions, failures, and background handoff remain distinct and inspectable. Generic actions keep lazy body formatting and authoritative single-flight result fetching behind their own disclosure. Diagnostics stay inspectable; refusals, repairs and truncation remain visible. Saved-output cards label search terms; request scope stays in [their sole disclosure](../frontend/src/conversation/README.md#tool-card-header). Unresolved/attention evidence opens on first paint without overriding later reader choices. Generic failure cards show a bounded diagnostic and attention verdict in their sole summary; their raw envelope/output stays collapsed (`data-tool-failure-evidence="collapsed"`) until the reader opens it. This exception avoids flooding historical MCP failures with serialized wrappers while preserving inspectable original evidence. Model-round identity is optional for showing/navigating an action; legacy navigation uses block identity. **Alternatives rejected:** hiding duplicate wrappers with CSS leaves empty controls and lazy shells; shrinking technical labels leaves intent clipped; removing code/output controls loses useful evidence.

**Edit failure disclosure.** Settled batch-edit headers show only one
successful/total operations badge (for example, `5/6`): green only when every
operation succeeded and no error/refusal verdict remains, otherwise red.
Error kinds, reasons and recovery hints belong to the individual detailed
entries, with structured refusal notices retained below the header. The expanded
edit list has no generic result rail or compounded left inset; each row retains
its own status, ordinal and operation. Missing historical counts are not invented.

A whole-call error without per-edit outcome lines must remain visible even when metadata contains empty failed rows. Each failed row falls back to the recorded whole-call reason, never to an unapplied diff; missing retained evidence is stated explicitly rather than rendered as an empty fold. Recorded per-edit outcomes, including multiline recovery hints, outrank metadata and unrelated whole-call text. Batch-edit presentation accepts declared historical lazy references through the same keyed, owner-scoped result loader as generic results (the current server's read-only lazy whitelist is unchanged): expansion fetches evidence once, closing cancels the read, reopening permits retry, and loading preserves the reader's outer fold and keyboard focus. No tool is re-executed or Turn data rewritten. **Alternatives rejected:** metadata-only folds hide available errors; adding a second result transport duplicates identity, cancellation and stale-read policy. Executable specs: `tool-header-presentation.test.ts`, `tool-result-presentation.test.ts`, `tests/visual/test_visual_edit_result_summary.py` and `tests/visual/test_visual_edit_failure_disclosure.py`.

Parallel launch failure is distinct from missing agent history. The persisted
launch error is escaped and expandable, marked "Launch failed", and cannot
create a fictitious child roster or recovery poll. A valid receipt/snapshot
roster outranks launch-looking quoted text. Missing evidence alone never
proves non-persistence, launch, or completion. The typed classification owner
is `swarm-agent-presentation.ts`; executable specs include
`tests/swarm_panel_authority.test.cjs`. Local context-compaction failures use `swarm-failure-presentation.ts` for cards/live notices: state that this request stopped before provider dispatch, preserve earlier-edit caution and disclose raw diagnostics without implying unchanged retry will help. Historical string receipts use the same localized reason. Verify `tests/visual/test_visual_swarm_context_failure.py`.

## Continuous history reading

**Problem:** activity toolbars and separate earlier-block/earlier-turn pills exposed rendering limits as manual paging and interrupted chronological reading. **Decision:** outer upward scroll reveals earlier turns (or live/failed/fallback content) at its actual boundary; downward scroll restores later windows. Settled process content uses its own explicit disclosure/paging contract above. Turn windows and live/failed/fallback block windows keep an overlapping bounded residency set, never truncate TurnStore, and preserve the first visible block's identity and viewport offset (with Turn fallback). Scroll work is animation-frame coalesced and disposed with the Surface. Streaming follows the tail only while the reader has not suspended follow; the latest action restores both windows. Keyboard boundary actions appear as quiet text only on focus, not floating pills. Remote pages reuse the coordinator promise: one flight per lane, localized loading/failure feedback, retry on explicit click or renewed scroll demand, and a navigation fence for late settlements. No additional transport or durable history state is introduced. **Alternatives rejected:** restyled pills still require manual paging; rendering all history creates unbounded DOM; anchoring a Turn top cannot preserve reading within a long response. Specs: `tests/visual/test_visual_conversation_scroll_history.py`, `tests/visual/test_visual_conversation_navigation_performance.py`, and the focused Surface/viewport contracts.

Latest is a semantic destination, including unmounted later Turns/blocks, not
the current window's physical bottom. The button and explicit send intent
restore all lane windows; ordinary layout/keyboard nudges respect the viewport's
single follow latch. Even a small upward wheel, first scrollbar movement or
PageUp suspends follow before a streaming write. Inner historical evidence owns
its gestures. Every anchor correction is instant and restores the exact prior
scroll style; a retired visible leaf falls back to the captured Turn offset.
Conversation disposal retires listeners and fences already-delivered frames.
`conversation-viewport.test.ts`, `conversation-surface.test.ts` and
`conversation-surface-controller.test.ts` under `frontend/src/conversation/`
exercise these boundaries; shipped-button visual tests exercise the real bridge.

Measured in real Chromium at a 700px viewport: a 450-block live/fallback stream retained
22 mounted blocks through four earlier batches; a 300-Turn conversation
retained 10 mounted Turns. The tracked reading item shifted at most 0.313px
in the desktop fixtures. Scroll demand adds one pending animation frame, no
polling timer or independent history transport.

## Sent child-agent messages

**Problem:** `send_agent_message` displayed internal receipt JSON instead of
what the parent actually sent. **Decision:** its card projects the target and
escaped, selectable `toolArgs.content`, with a quiet submission outcome. A
successful submission is not child input confirmation, acceptance or completion.
Running, failed, unconfirmed and missing-evidence states remain distinct; errors
and recovery instructions stay visible. Historical receipts use the same content
projection without rewriting saved Turns. The full durable receipt remains
canonical diagnostic evidence, while `contracts/tool_result_surfaces_v1.json`
owns the concise model outcome. Unknown fields/shapes and failures are not
silently compacted. **Alternatives rejected:** deleting receipt identities would
break reconciliation; echoing input text back to the model wastes context;
calling a send "delivered" invents consumption proof.

Inbound receipts are ephemeral display entries in the existing agent tool timeline, using its row classes, native disclosure and escaped detail renderer. They are not model tool calls and never enter `_toolCalls`. Run/agent/message identity keys the row; duplicate or stale receipts cannot downgrade confirmation. One inline status states the evidence, with the actual body folded beneath it even for the latest entry. There is no separate messages panel or disclaimer paragraph. The sender card stays live. Receipt content wins; sender arguments only recover missing text. Tool order is preserved; messages are inserted before the first later evidenced `startedAt`, using millisecond `submittedAt` (then `inputInjectedAt`). Missing-time receipts remain at the tail, ordered by durable `messageSequence` and identity; no browser timestamp invents chronology. Projection adds only the bounded receipt set to the bounded tool window. A separate inbox panel and synthetic tool calls were rejected because they duplicate presentation and misrepresent execution.
## Generated UI-navigation labels

The model-facing `setting:<key>` / `page:<key>` directory is generated by
`scripts/gen_ui_nav_targets.mjs` into `contracts/ui_nav_targets_v1.json` and
rendered by `lib/ui_nav.py`. Jump identities come from
`frontend/src/features/settings/setting-jump.ts` and
`frontend/src/features/ui-nav.ts`; names come from owning panel/tab markup and
the existing locale catalogs, not a second hand-maintained label map.

**Problem:** a byte-current generated catalog can still describe the wrong
control when extraction crosses a closed row or selects option/checkbox text.
**Decision:** parse markup as an inert DOM. Follow explicit label/ARIA
associations, then the actual field/toggle row or bounded section/disclosure
heading. Resolve `data-i18n`, `data-i18n-html` (as plain text), and literal or
composite labels. Descriptions must belong to that control's row/section or an
explicit `aria-describedby` association. Missing elements/translations and
ambiguous ownership fail generation; options, placeholders, actions and
neighboring descriptions are not fallback labels.
**Alternatives rejected:** fixed character windows and nearest textual row
openers ignore nesting; per-key generator overrides would duplicate UI copy
and mask structural drift.

`npm run generate:ui-nav` derives the artifact; `npm run check:ui-nav` checks
freshness only. `python3 -m pytest -q tests/ui_nav` additionally audits every
emitted settings key against reviewed bilingual label/description ownership,
checks the rendered directory, and runs the structural fixtures in
`tests/ui_nav/ui_nav_generator.test.mjs`. New setting keys require an explicit
semantic assertion, not merely regeneration. The fixture suite covers closed
siblings, rich/composite labels, long selects, toggle/provider separation,
section/disclosure boundaries and missing/ambiguous associations. Navigation
identity/routing remains guarded by
`frontend/src/features/settings/setting-jump.test.ts`.

## Composer selection on navigation

An empty composer restores the selected conversation's saved model and project; turn provenance is historical evidence, not a replacement settings source. Browser submissions carry the saved thinking depth, never the capability-derived `thinkingEnabled` display flag. Unknown or unsupported capability may hide a control but cannot turn a saved depth into an explicit off request; the server derives the legacy flag from the submitted depth. Draft protection counts sendable textarea content and pending attachments, not log-cleanup suggestions derived from an earlier paste. Those asynchronous suggestions can outlive the input and must not suppress both selection restores. The sidebar regression harness `tests/conversation_composer_switch.test.cjs` executes the retained pointer handler, lifecycle and restore adapters for warm and cold opens, and verifies that real text/attachment drafts remain protected.

## Sidebar background activity

The sidebar and composer background chip project the same owner-scoped activity
snapshot. Running or queued sub-agents and detached commands keep their own
conversation visibly active after its foreground Turn settles, including
inactive, newly rendered, and search-result rows. A compact localized background
badge shows the active count; queued-only work is labeled waiting, not running.
Foreground answering/preparation remains primary; settled errors remain visible
alongside background activity. Background work never changes `convIsBusy`, Turn
settlement, catalog timestamps, or composer Send/Stop state.

The existing single-flight, visibility-aware activity poll owns both surfaces;
its active cadence depends on all conversations, not just the open one. Status
changes patch existing sidebar rows without replacing titles, focus, or search
results or fetching Turns. Successful empty snapshots clear the background
status; transient read failure retains the last known state. No additional
poller, event stream, or durable browser state is introduced.

## Per-turn context rail geometry

**Problem:** user and assistant turns can both carry context; stopping only at next-round user input lets their absolute-positioned cards paint over each other. **Decision:** each card's travel and scrollable height end at the first next visible railed sibling or human-input sibling in its lane, or the content tail. Hidden/empty cards do not reserve space. Measure intrinsic height and unshifted host positions each pass so a previous cap cannot feed back into the next one. A track too small even for padding is nonpainting, not unbounded, and recovers when space returns; narrow panes still use the inline context disclosure. Structure/hidden-state mutations and pane resizing share the frame-coalesced follower, including absolute cards inserted without changing transcript height. **Alternatives rejected:** deleting assistant context loses valid provenance; changing z-index merely obscures overlapping facts; a next-rail-only boundary would let a card trespass into a new round with no rail. `tests/turn_lifecycle/test_turn_ctx_rail_overlap.py` covers same-round cards, scroll handoff, dynamic insertion, zero-space recovery, and narrow/wide changes.

## System interventions

Engine-authored interventions use the existing `system_note` segment and
`system-note` block, never assistant prose or synthetic tool executions.
The canonical fields/actions live in `contracts/conversation_sync_v3.yaml`.
Records carry stable attempt-scoped identities, their insertion round, a reason
code, and plain-text details. Actions distinguish appended context, requested
continuation, tool-policy changes, and execution stops. Appending a prompt is
not evidence of provider delivery or model consumption.

Blocks are collapsed by default; labels are localized and dynamic reason/body
text uses `textContent`. Live snapshots, settled repairs and reload preserve
ordered notes without duplicating stable IDs. Unknown historical kinds fall
back to a generic note. Display records never become assistant replay content.
New records bound bodies to 16,384 characters and reasons to 160; producers
retain their bounded trigger/retry budgets. Retired serial-efficiency prompts
are not reconstructed for old turns that retained only content-free witnesses.

**Context-note grouping (display-only).** Each context-injection note is its own injection round at its exact wire position — that per-note granularity is the timeline authority and never changes. But a *contiguous* run of context-injection / session-context notes (the session-start context tail, or several injected together mid-turn) renders as ONE merged disclosure with a count pill and a per-item list inside, instead of N stacked sibling rows. Grouping accepts any maximal adjacent run of ≥2 within the same explicit model-round/task/attempt scope, never crosses a non-note block or an intervention note, and never folds a reason-specialized intervention (active-checklist re-pin, modified-files recall — those keep their own row). Missing/invalid round identity is not evidence that two commits were simultaneous. Batch keys, attempt ordinals, wire positions, and member note text/order are untouched; both render paths (legacy `renderSegmentTimelineHTML` and the typed `selectTurnBlocks` → `system-note-group` block) apply the same rule for parity.

**Problem:** a hand-maintained frontend whitelist omitted
`programmatic_execution`, splitting ten consecutive startup witnesses into
6 + intervention + 3. **Decision:** `InjectionSpec` source lanes, explicit
`group_context` opt-outs, and `provenance_field` own the taxonomy.
`note_display_policies()` and `gen_system_note_presentation.mjs` generate the
same contract used by typed and retained renderers. Known legacy
`system-intervention` prompt witnesses get a display-only kind repair;
unknown reasons and non-prompt actions stay separate. Own-property lookups
fail closed for inherited JS keys. No wire note is renamed or rewritten.
**Rejected alternative:** adding one reason to the whitelist only postpones
another drift when a new context capability is registered.

**One context/provenance owner:** `context-provenance.ts` runs after grouping. A unique matching witness owns passive acquisition metadata (memory selection, applied My Context, tool discovery, path relocation). Unmatched/failed/skipped metadata remains visibly separate from injected text inside the leading context disclosure; provenance-only historical Turns use the same native context disclosure. Multiple matching retry witnesses are not guessed. Memory selection success is not provider delivery or model consumption. Pending MCP login approval and pending preference acceptance remain actionable outside the fold. The old external `turn-prov` / `tp-segs` strip is no longer rendered. Payloads, item bodies/order and stored counts are unchanged. Live equality includes reason/action/scope and acquisition fields; repaint preserves the native disclosure's open state. **Rejected alternative:** simply hiding the strip loses failure and provenance-only evidence; retaining both owners repeats the same fact in two places. Executable evidence: `tests/frontend/test_context_provenance_integration.py`, provenance status and escaping tests, and the Chromium grouping test below.

For a long session-start tail, the collapsed summary shows one muted localized
"Context notes" title and the count, without a redundant action badge;
expanding keeps each
member's reason, action and escaped verbatim text in arrival order. The
summary and member reason chips wrap on narrow screens rather than forcing
horizontal overflow. A previously opened browser tab may still run an older
hashed frontend asset until reload; the current asset preserves this rule.
The Chromium regression in `tests/visual/test_visual_context_note_grouping.py`
checks both render paths at desktop and phone widths. We keep adjacent-only
render grouping instead of dropping notes or coalescing wire records, because
intervention provenance and exact replay order must remain available.

**Injection disclosure visibility.** The same native `<details>` chrome is
used by human mid-turn steer, peer, swarm inbox, background-command and stall
injection rows. All start closed: only the summary line (count, status and
optional badge) occupies the timeline; original payload text and attachments
are inside the body and remain accessible on expansion. A flex-styled direct
child of a closed `<details>` can override the browser's default hide rule, so
both `.sw-inbox-row-body` and the context group's flex member list need an
explicit closed-state `display:none`. Desktop and phone Chromium checks assert
body invisibility *before* expanding, visibility after opening and hiding
again, not merely the absence of the `open` attribute. We keep the verbatim
payload rather than replacing it with a short lossy preview.

**Steer state identity.** A mid-turn human steer is durably accepted as a
`pending` injection block before the executor consumes it; after model
consumption the worker emits a `delivered` sidecar for that *same* blockId.
The cumulative Turn projection merges by blockId, upgrades pending to
delivered and never demotes a confirmed block on stale task replay. Distinct
operator messages are not coalesced by equal text or round. An unconsumed
steer at turn end instead follows the durable queue fallback as a new input
Turn; that is delivery, not a second rendered confirmation. Blind text-based
cross-Turn hiding would suppress legitimate retries and is forbidden.
## Authority

The browser renders an immutable projection of the v3 turn snapshot:

```text
Storage Sidecar turn rows
  -> ConversationSyncCoordinator
  -> reduceTurnState
  -> applyTurnStateProjection
  -> renderTurnStateInto
```

There is no conversation-sized mutable `messages[]` authority and no task-SSE
fallback. Retained JavaScript may adapt the typed runtime to existing UI
services, but it must not own transport, attempt state, settlement inference,
or a shadow reducer.

The implementation owners are:

| Concern | Owner |
|---|---|
| transport and reconnect | `frontend/src/core/conversation-sync.ts` |
| normalized state and event fold | `frontend/src/conversation/domain/turn-store.ts` |
| turn-to-view projection | `frontend/src/core/turn-projection.ts` |
| finish/resume presentation | `frontend/src/core/turn-presentation.ts` |
| keyed DOM reconciliation | `frontend/src/core/turn-render.ts` |
| Markdown parser policy | `frontend/src/markdown-policy.ts` |
| Markdown rendering, exact cache identity and optional-library lifecycle | `frontend/src/markdown/` (`index.ts` composes declared ports) |
| image-tool HTML projection | `frontend/src/conversation/presentation/tool-image-presentation.ts` |
| browser JavaScript execution HTML projection | `frontend/src/conversation/presentation/tool-browser-execution-presentation.ts` |
| command execution HTML projection | `frontend/src/conversation/presentation/tool-command-execution-presentation.ts` |
| pending write-approval HTML projection | `frontend/src/conversation/presentation/tool-approval-presentation.ts` |
| synthetic context-injection HTML projection | `frontend/src/conversation/presentation/tool-injection-presentation.ts` |
| tool execution grouping and attention projection | `frontend/src/conversation/presentation/tool-execution-groups.ts` |
| tool execution reader disclosure lifecycle | `frontend/src/conversation/ui/tool-execution-disclosure.ts` |
| Human Guidance HTML projection | `frontend/src/conversation/presentation/tool-human-guidance-presentation.ts` |
| Human Guidance delegated response lifecycle | `frontend/src/conversation/ui/human-guidance-actions.ts` |
| image/external-asset source allowlist | `frontend/src/conversation/presentation/image-source-policy.ts` |
| composition | `frontend/src/core/turn-runtime.ts` |

While a foreground `run_command` or `code_exec` is running, its card presents
independent **Interrupt** (stop only that process; return partial output) and
**Move to background** (keep process and output reader alive) actions. Both
require a resolvable owner task; an explicit handoff request is scoped to the
current command, not a later process on the same task. The handoff button waits
for server acknowledgement and then the authoritative round settlement; refusal
or transport failure restores the action and shows localized feedback. The
server, not the browser, decides if the process still runs or is awaiting stdin.

Command-tool settlement is not subprocess completion: a provisional
`exitCode='background'` receipt says **Moved to background**, without a failure
class, numeric-exit label, or invented terminal outcome. The later durable
completion carries `BackgroundCommandCompletion` evidence in queue snapshots,
fresh-turn input, and mid-turn injection previews. Queue membership means
**delivery unconfirmed**, not a still-running process. Cards show escaped,
expandable output bounded to 2,000 characters with explicit truncation and
**Cancel delivery** wording. Mid-turn receipts acknowledge model delivery only
after verified provider completion; fresh-turn evidence says submitted as input.
Snapshot/replay/reload use the same fields, not a browser command-state registry.

**Problem:** adjacent completed background commands repeated a full queued-message
header and separated each output with message-sized whitespace. **Decision:** the
Surface derives contiguous completion runs from the existing flat queue. One
localized status/count heading precedes compact, continuous result rows; ordinary
queued messages break a run and keep their own position/source presentation.
Each row remains a direct, keyed `queueId` child with its own output disclosure
and **Cancel delivery** action. Grouping never deduplicates equal command IDs,
changes queue order or acknowledges delivery. Group-count/position changes patch
retained evidence and controls in place, preserving reader folds, focus and
unchanged selected output. **Alternatives rejected:** a synthetic queue item or
bulk cancellation changes delivery identity; wrapping/recreating rows loses
reader state; CSS-only hiding leaves duplicated status content. No additional
transport, timer, durable state or result cache is introduced. Specs:
`ui/background-command-queue.test.ts` and
`tests/visual/test_visual_background_command_queue.py`.

A confirmed background receipt carries its exact delivery `queueId`. Its accepted
attempt event removes `removedQueueIds` in the same reducer update that installs
the receipt. Stale attempts, duplicate sequences and rejected/gapped patches
cannot remove queue entries. Cards are never hidden merely because another
block has the same command ID or text; unrelated delivery intents remain pending.
The server commits both facts together, with rollback/replay semantics specified
in [CONVERSATION_SYNC_V3.md](contracts/CONVERSATION_SYNC_V3.md#background-command-completion-evidence).

## Conversation selection and collapsed history

**Problem:** selecting a conversation refreshed queue state by downloading the whole snapshot again, while collapsed historical rounds still constructed Markdown, highlighted code and tool/media subtrees before hiding them. **Decision:** cold selection uses one coalesced snapshot containing queue items; warm selection resumes its ordered stream from the existing cursor. Explicit queue commands retain their authoritative refresh. Collapsed rounds mount stable block shells for ordering and ARIA targets; their content renders on disclosure using the latest view model. First paint also sizes the existing block tail window to two visible viewport heights (64 px estimated per block, at least 20, at most the configured block window); hosts without viewport geometry keep the existing default. The mounted Turn window likewise follows two viewport heights (160 px estimated per Turn, at least eight, capped by the existing 80-Turn budget), shared across expanded lanes. Larger viewports receive more content. Earlier-block controls expand the same authoritative presentation without dropping historical Turns. Live, background and attention rounds keep their existing expansion policy. Revealed content retains keyed nodes and reader state. Rich-card reconciliation captures reader state only for connected, populated subtrees: new cards cannot own focus or selection, and querying those facts while inserting each card forced repeated synchronous layout. **Alternatives rejected:** caching another transcript would duplicate authority; a loading animation would leave duplicate reads and hidden rendering costs. Verification: `tests/conversation_composer_switch.test.cjs`, `tests/frontend/test_frontend_round_outline.py` and `tests/visual/test_visual_round_outline.py`.

## Tool-group render ownership and indexing

**Problem:** retained tool UI combined batch chrome, rich-row routing, inspector anchors, narration and bounded panel tails. Every timeline batch scanned full context groups to recover its first matching occurrence; identity-only title memoization also hid same-array edits in legacy full-turn rendering. **Decision:** `conversation/ui/tool-groups/` has eight typed owners, at most 48 lines each. A synchronous render session indexes context groups once by object and call identity and derives title suffixes from those existing batches. First-match ordering includes repeated provider call IDs; it does not silently prefer an object match over an earlier matching ID. Narration keeps translated prose before its batch, and inspector references retain producing task/turn identity. Settled panels keep the existing 100-round threshold and 50-round initial tail; live panels retain all active rows. Sessions always see current inputs and expire with the render. Standalone modern rows keep the existing weak index for immutable canonical sibling arrays; a legacy append invalidates its length stamp. No timer, listener, request or durable projection is added. Sibling cluster keys use `(toolName, query)` tuples, avoiding ambiguous string concatenation. For 1,000 singleton batches, indexed membership reads 6,000 call identities instead of 500,500 former context-group probes. **Alternatives rejected:** a second grouping policy would drift across attempts; a page-global mutable context would retain stale titles; removing immutable projection memoization would make modern per-row rendering quadratic again. `tests/tool-group-presentation.test.cjs` covers repeated identities, title-cache lifetimes, tuple collisions, counts/tails, narration and inspector authority. Tool-row intent now uses the shared 13px UI scale and primary text, with a neutral token-based hover surface; the text clears 4.5:1 in each theme. The former mixed 618-line stylesheet has 12 semantic owners (one strict row owner and 11 exact frozen copies); changed row rules are removed rather than overridden. Code and evidence keep their own monospace typography. The browser gate exercises Enter/Space disclosure, nested inspector controls, focus and selected text during append in all themes at 390px and 1180px.

**Inspector and composer reading:** Problem: 860px drawer allocation squeezed medium desktop chat, theme-wide textarea rules reached editors, and refresh collapsed evidence. Decision: Inspector uses `min(860px,50vw)` with a fullscreen overlay at ≤900px; the composer fits the remaining pane, uses 16px/1.7 and a static token focus ring. Inspector evidence is 14px/1.7 with 12px captions and native UI disclosure buttons; manual/silent refresh restores open identity and header focus, while explicit navigation can reset position. Alternatives rejected: perpetual rainbow focus motion competes with text; smaller type conceals the width problem; positional restoration opens unrelated evidence. Six real-browser theme/390px/1180px cases measure actual paint contrast, wrapping, pane containment, lazy CSS delivery, Enter/Space, refresh focus and reduced motion; wire failure/retry and reading journeys retain coverage.
## Identity and ordering

- `turnId` is the only durable rendered identity.
- `attemptId` identifies one execution against one output turn; it is never a
  DOM key.
- `laneId` plus `ordinal` defines order. Array position and legacy message
  indexes are not identities.
- A renderer node carries `data-turn-id`. Reconciliation reuses that node for
  newer projection revisions and removes nodes absent from an authoritative
  full snapshot.
- Delta snapshots are patch authorities only. They cannot authorize deletion
  of identities they omit.
- The default Surface mounts at most 80 turns per lane. When the tail window
  advances, remove outgoing turns before placing new ones; retained subtrees
  stay in place. Advancing by one turn costs one removal and one insertion,
  not 79 DOM moves and repeated long-text layout. Windowing never deletes
  durable history. `test_surface_tail_window_keeps_retained_turns_in_place`
  verifies this mutation budget alongside the existing history/scroll tests.

- Conversation-bar jumps call the Surface `revealTurn` operation before DOM
  scrolling. Loaded-but-unmounted targets move the existing bounded window,
  never build a second transcript or unbounded DOM. Explicit navigation cancels
  pending follow-to-bottom work; `followLatest` first returns to the tail window.
  The controller flushes a pending state before reveal and rejects inactive
  conversation IDs. History loading stays with the v3 coordinator.

Goal phases use the same keyed Turn projection as ordinary chat. Producer task
identity and persisted `inspectorRef = {taskId, roundNum, turn}` survive live
handoff, settlement and cold reload; a display attempt never owns its child's
execution. A stamped Inspector anchor uses that exact owner-authorized address,
with post-tool state preferred to request input. Invalid references fail closed.
Completion must not replace earlier reasoning/narration or create a second tool
occurrence. Desktop/mobile proofs: `tests/visual/test_visual_goal_persisted.py` and
`tests/visual/test_visual_goal_evidence.py`.

## Revision rules

Every projected turn carries `projectionRevision`.

1. Lower revisions are ignored.
2. An event with a revision already folded advances only its attempt cursor.
3. A projection patch applies only when its `baseRevision` equals the local
   revision and its `targetRevision` equals the event revision.
4. Any gap triggers a full v3 snapshot. Guessing or best-effort patching is
   forbidden.
5. A full projection wins when a recovery frame contains both full and patch
   representations.
6. A pending/running snapshot or command response retains the committed
   segment array and mirror text at that revision. Read-time placeholder
   synthesis or mirror repair cannot precede a replay append; normalization
   belongs to the next versioned structural write. The renderer may display
   cumulative content/thinking from their channels, but never rewrites the
   reducer's patch base or deduplicates legitimate repeated narration by text.

These rules make reconnect, replay, and live delivery converge on the same
state without content fingerprints or string-length heuristics.

## Attempt event fold

The browser folds only the declared attempt event vocabulary:

- `status_changed`
- `projection_updated`
- `interaction_request`
- `terminal_settlement`

An event applies only to its turn's current attempt, except the first event of
a newly adopted attempt when the carried `turnState` proves the transition.
Events are idempotent by `(attemptId, seq)`. Events arriving before their turn
snapshot are buffered by `turnId` and replayed after the turn appears.

Live phase text rides event payloads and is cleared by terminal settlement. A
full snapshot may restore `activeAttemptPhase` from the latest retained event
of its current running main-lane attempt at the same read head. It is not
durable turn projection state. Independently, the timing-trace projector keeps a
bounded historical receipt of each phase transition (`detailKey/detailArgs`,
interval, repeat count) for postmortem inspection. That history is diagnostic
evidence only: it never repopulates live phase state or renders as a chat
timeline row.

A running Turn with no live phase does not prove worker startup or transport
recovery: older snapshots omit phases and semantic deltas clear them. Its
fallback says “Waiting for an execution status update…”, never claims a
resynchronization, and never uses `workerStarting`; that label requires an
explicit worker-entry phase. A streaming text/thinking tail suppresses the
empty-phase placeholder. Pending task binding still distinguishes scheduler
preparation from executor queue residence.

`llm_thinking` denotes an opened model-generation round, not evidence of
reasoning-channel output. Its live row preserves localized `detailKey` and
`detailArgs` (generating a reply or analyzing tool results), then raw detail or
label; a bare phase falls back to localized “Generating response…”. It never
gets a thinking character counter or thinking-specific styling. The distinct
`thinking_active` content-stream status uses the same “Thinking…” wording as
an active reasoning disclosure.

When an authored Flow role tier resolves to a model other than the one selected
for the conversation, every producer phase carries the structured
`modelRoute` selected→resolved decision. The live-status label discloses that
route before dispatch and keeps it alongside later wait/retry text. The
completed role Turn persists the same route under `orchestration.modelRoute`,
sets its top-level `model` to the resolved model, and renders a warning footer
tag. Goal/Autopilot pins its leaves to the selected model, so this switched
presentation applies only where role-tier routing is an explicit execution
policy; provider fallback remains the separate `model_fallback` contract.

A conversation-level `livePhase` attaches only to the newest durable pending or
running Turn in the main lane. A browser-lifecycle transient Turn carries its
own `transientPresentation` and therefore its own keyed `live-status` block; it
never competes for or consumes the durable Turn's phase. In particular, an
optimistic human echo followed by `transient:send-preparation` may render below
an already-running assistant, but that assistant must retain its status block
and may not become a header-only row during send/translation preparation.

Goal virtual-user settlement follows the same single-Turn rule. A terminal
backend segment timeline is authoritative chronology and must not be replaced
by stale live thinking/text segments. If the terminal payload omits rich tool
rounds, the transient overlay retains and settles its canonical rounds so tool
names, result bodies, Request Inspector identity, and timing remain available;
only a terminal payload with no segment timeline may synthesize segments from
those retained rounds.

## Projection and DOM rules

Search-result and engine-source disclosures use the shared delegated action
registry. Their `aria-expanded` value follows the actual containing class;
inspector links inside a search header operate independently and do not toggle
that disclosure. Producers use supported conditional blocks and native
`setAttribute` coercion rather than unregistered global calls or bare early
returns. Specs: `tool-search-interactions.test.ts` and the isolated app visual
journey `test_visual_browser_stall_evidence.py`.

Checklist and Timer receipts are composed by `conversation/ui/checklist-card/`
and `conversation/ui/timer-card/`. Problem: the retained rich section still
mixed checklist state projection, Timer metadata, poll evidence and DOM actions
in 498 lines. Decision: eleven TypeScript owners separate these responsibilities;
the 55-line retained adapter injects translation, icons, safe Markdown,
authoritative recovery presentation and the existing shared clock. Checklist
accepted history/context-rail policy and Timer recovery/scheduler authority
stay in core. The dependency gate rejects content-to-facade imports and cycles.
Alternatives rejected: line-based fragments, another card registry, another
clock, or moving recovery/network policy into a presenter. Checklist controls
are evaluated only after a list exists, preserving the absent-card path.

Checklist items project status and completion count in one pass. Timer receipts
count completed checks in one pass and copy only the newest five polls, rather
than reversing the full history. A 10,000-poll fixture copies five entries;
full parse-failure output, command evidence and tool traces remain available
within each visible check. Backend poll ID, otherwise poll number, keys both
the check and its detail during reconciliation: new checks cannot inherit a
reader's expanded state or replace the focused/selected older evidence.
Static instruction and check actions resolve against their own card/row,
including when the same round appears in multiple views, and support Enter and
Space with synchronized disclosure semantics. Nested copy controls and Markdown
links retain their own native activation rather than folding the parent.

Countdowns paint changed text in place, with no extra listener or timer owner.
Only positive finite timestamps on expanded cards retain demand; collapse,
invalid hints and removal release the shared ticker, while expansion demands
that same service again. A due timestamp remains a scheduling hint, never
trigger authority. The fake-clock lifecycle fixture observes at most one
scheduled callback and releases the visibility subscription with demand.

The checklist/wait stylesheet is admitted to strict token checking, removing
its legacy hash exemption. Checklist headings and complete explanations use
the shared 13px UI scale; item text uses 14px with the body line height.
Theme text roles, neutral count-chip backgrounds and no faded completion text
retain at least 4.5:1 contrast in all three themes. Native disclosure focus,
long-item wrapping, local keyboard actions and focused/selected evidence survive
real Chromium updates at 390px and 1180px. A 720-case parsed-DOM comparison
retains ordinary output after removing only formatting-only whitespace and the
intentional Timer identity/keyboard-action attributes; the 61-row wire battery
changes only its Timer header keyboard action and ID-chip button type.

Timer styling is owned by the three `08-timer-receipt-{shell,metadata,evidence}.css` files. Problem: two mixed stylesheets and repeated light/Tofu overrides painted small, translucent auxiliary text and washed-out instruction previews. Decision: one theme-token vocabulary separates neutral reading surfaces from state icons/borders, identity chips, instruction disclosure and evidence. Headings/check reasons use the shared 13px UI scale, instructions use 14px, and code/evidence use the 12px monospace scale. Verdict text remains primary on hover; semantic icons still distinguish ready, error and parse-error checks. Complete instruction and evidence stay readable and selectable, with no gradient mask or faded skipped-check rows. Caret rotation belongs to CSS disclosure state, rather than inline SVG styles. Alternatives rejected: another layer of per-theme patches, shrinking text to make metadata fit, and tinting the entire failure reason with a color that loses contrast on hover. Actual card owners, action registry and reconciler retain focus/selection, and text clears 4.5:1 at 390px and 1180px in every theme, including hovered checks.

- **Stylesheet ownership decision:** Problem: two 2,368-line mixed owners combined
  Swarm cards/injections, signals, Timer receipts, dialogs, project selection,
  optimizer previews and theme overrides. Decision: 32 semantic owners, at most
  178 lines each; three Timer owners use strict tokens and the 29 remaining
  copies retain exact original rule bytes with manifest-bound hashes. Remove
  those hashes when migrating a copy; they cannot admit edited declarations.
  The manifest preserves ordering and retires the duplicated Timer palette.
  Alternatives rejected: arbitrary line slices, expanding the legacy allowlist,
  or overriding old Timer paints at the bottom of another stylesheet.
- Application dialog CSS belongs to the already-lazy DOM controller. First
  paint fetches zero dialog styles; first intent waits for Vite's CSS preload,
  and repeated dialogs fetch that sheet once. The default document graph uses
  the shared module-flight cache: a failed stylesheet stays failed, so Vite's
  private preload set cannot bypass it on a second click. Both activations
  settle to the existing safe default without an unstyled overlay. Injected
  test/application loaders retain the facade's existing retry semantics.
  There is at most one retained default import promise per document and no new
  timer, listener, transport, or durable state. Alternatives rejected: an
  independent CSS loader and replaying a pending confirmation after an
  automatic page reload, which cannot safely preserve its caller's context.

Project coordination receipts are composed by `conversation/ui/project-receipt/index.ts`. Problem: the retained rich section mixed board, peer, charter, feed and commit presentation with an obsolete second conversation-digest projection. Decision: nine small TypeScript owners separate body families, human labels, read-folding policy, metadata types and activity time. The existing retained bridge injects translation, safe Markdown/inline Markdown, icons, title lookup and a clock; it owns no state, transport or lifecycle. The declared dependency gate rejects content-to-facade imports and cycles. Rejected alternatives: arbitrary line slices, another runtime registry, or rebuilding conversation catalog/title policy inside receipt renderers. Structured metadata retains its existing precedence; failed board mutations, advisory/denied delivery, pending human charter review, held-back files and commit verification mismatches remain explicit. Counts, translated peer captions and round attributes are escaped.

The removed digest renderer has no production entry: `get_conversation` stays
on the ordinary same-call backend result path, and backend tools no longer
emit `convDigest`. Its obsolete stylesheet, summary-title rule and 22 unused
locale keys are removed rather than migrating another compatibility view.
Project receipt headings use readable UI type at the shared 13px scale, with
natural wrapping rather than the generic code-label type. Disclosure retains
keyboard focus, selectable error/detail
text and narrow-screen layout across all three themes. Board failure text uses
a neutral theme background to retain at least 4.5:1 contrast; error badges and
borders still communicate the failure state. Copied-source fault
injection still verifies the load-bearing coordination branches.

Multimedia tool receipts are composed by `conversation/ui/motion-card/index.ts`; localized labels, validation/media/production bodies, safe markup and payload decoding have separate TypeScript owners. Problem: 18 unrelated tool outcomes shared the 1,576-line retained rich-render section and undeclared globals. Decision: strict presentation-input types and explicit localization/WorkUnit demand ports; the retained dispatcher keeps one narrow deletion-path bridge. The content dependency gate rejects facade imports and cycles. Rejected: arbitrary line slices, another runtime registry, and asynchronous placeholders that would drop receipt details during reconciliation. The migration adds no cache, timer, listener or transport; success, degraded narration, rollback errors, zero-valued media facts, call text and durable WorkUnit handles retain their existing markup and preview bounds. Round-number attributes, narration overflow fields and translated scene captions are escaped as data; string values cannot create markup inside a scene row. Screenshot review exposed bright fixed-color warnings on light themes. The owning `09-tool-project.css` now passes strict token admission (its frozen-copy exemption is removed), with theme feedback colors, neutral receipt backgrounds, readable captions and shared spacing/type/radius scales. Source-chip measure and receipt-label width are named geometry tokens. Browser checks cover all three themes at 390/1180px, warning/error contrast, selection and disclosure.


Accepted producer receipts delegate to `conversation/ui/work-unit/`. Problem:
collapsing a completed tool call concealed the still-running work and its human
review. Decision: explicit `workUnitRef` metadata keeps a persistent card outside
both historical Process and model-round disclosures; generic tools retain their
existing attention policy. The card shows current activity, visible outline or
storyboard review, partial previews, then published artifacts and scoped revision
actions. Process/request details stay collapsed. Status comes only from server
WorkUnit snapshots; review/abort/re-render acceptance is a receipt, not completion.
Keyed views preserve input, focus, disclosure and playing media across refreshes.
Preview failures and degraded/failed outcomes remain explicit. Revision prefills
preserve the composer draft and never send automatically. Scene re-render jobs
use the same mounted task owner and refresh their parent on terminal settlement.
Alternatives rejected: a separate producer app/state machine, raw stage logs as
the main view, and merely expanding a card that the parent Process still hides.
The replacement tracker coalesces duplicate mounted cards, fences late callbacks
by owner/entry, pauses hidden-tab polling, and releases observers/timers/media
when unmounted; `docs/modules/production.md` records the resource measurement.
`tests/work-unit.test.cjs` and the real-browser work-card fixture cover review,
preview, revision, failure, disclosure and teardown using shipped UI/CSS.

Tool presentation CSS is ordered by the application style manifest: the `09-*`
owners separate command controls/output, approvals/diffs, tool receipts,
guidance, failures/activity and WorkUnits. Problem: one 3,020-line sheet mixed
all these roles and shipped editor-only rules to every conversation. Decision:
21 owners (maximum 281 lines), with the slide editor imported by its lazy
feature. Tool-owner CSS remains byte-identical and ordered; legacy copies have
closed content digests instead of a blanket file exemption. Rejected: arbitrary
line-count slices, relaxed token gates, or eagerly importing editor styles.
The measured critical sheet drops 4,138 bytes and its editor selectors entirely;
no timer, listener or browser-owned state is added by this style migration.

The demand-loaded slide editor now has one TypeScript session authority under `features/slides/`; its entry receives the existing owner-scoped API and composer ports. Problem: closing via X leaked document keyboard listeners, and old reads/patch receipts could repaint a different page or reopened deck. Decision: unique page-visit identities fence all receipts, one active element read coalesces navigation to its latest trailing demand, and one active write remains owned until settlement. Close releases overlay, iframe selection style, document/frame listeners and pending read demand, then restores trigger focus. Server patch/rollback receipts remain authoritative; closing never cancels or replays a mutation. Rejected: adding flags to the shared classic state, hiding the leak behind Escape-only cleanup, and aborting writes without a receipt. Verification covers 30 repeated open/X cycles (one active key listener, zero after close), eight rapid page visits (two reads), late receipts, rollback and retry; production browser tests cover actual iframe selection and dismissal.

- The DOM is derived from `TurnState`; background code does not edit chat
  nodes directly.
- Reconciliation is semantic, not reference-based. A full snapshot may recreate
  JSON-shaped projection objects; structurally unchanged blocks must retain
  their DOM nodes, focus, disclosure state, and nested scroll position. The
  structural comparison is explicitly bounded and falls back to repainting
  when its depth or node budget is exhausted.
- Turn projection is memoized on the Turn object identity the reducer preserves
  while its projection is unchanged, plus the per-Turn inputs that live outside
  it (translation display mode/activity, task id, command-pending operation,
  client cost signature, inspector flag). A "plain" Turn with no branch lanes,
  no additive blocks and no live status memoizes its whole view-model object;
  anything with a cross-Turn or live input does not. `selectTurnBlocks`
  memoizes at the same identity boundary, so a reused `blocks` array reference
  is the Surface's proof that the mounted block subtree is already correct.
- The Surface skips a whole Turn when the incoming view-model object is the same one it last rendered and the block window, attached plan decision, and surface options also match; branch-owning Turns always reconcile because their child lanes own independent window state. Otherwise a reused `blocks` array skips only that Turn's structural walk, while a new Turn object (projection change or snapshot rebuild) recomputes the blocks and the bounded compare decides per block. A streaming delta is therefore proportional to the changed Turn, not the loaded conversation. Two further per-commit costs are avoided: the diagnostic mounted-Turn count is accumulated during the commit instead of scanning the whole transcript for `[data-turn-id]`, and the scroll anchor's per-Turn geometry scan is skipped while the view is following the tail (where `restore` re-pins to the bottom and never reads the anchor). Pin: `tests/frontend/test_frontend_render_projection_reuse.py`.
- Text and reasoning blocks update their retained content containers in place. Rich-card HTML crosses the existing rendering/safety boundary, then the mounted UI reconciles compatible nodes in place. Semantic action identity changes replace controls rather than retaining stale callbacks or drafts; compatible updates preserve native `details`, disclosure state, focus, selection, and scroll. Whole-card moves use the same reader-state transaction for native `moveBefore` and the `insertBefore` fallback; native focus retention alone is not proof of selection retention. Appended output retains valid unchanged selected text, including backward selection; edits to the selected content do not restore stale ranges. Surface update tests use immutable turn snapshots and assert the latest result, not only stable node identity. No-op snapshots do not rebuild the subtree. Pins: `tests/frontend/test_rich_card_reconciliation.py`, `tests/frontend/test_round_card_stability.py`, and `tests/frontend/test_round_card_stability_browser.py`.
- Non-terminal text/thinking blocks use `options.streaming`: highlighting/KaTeX stay deferred per delta, uncached; terminal rendering restores and caches both.
  **Streaming DOM decision — Problem:** tail-kind changes and terminal formatting rebuilt unchanged reader nodes. **Decision:** preserve exact parser/sanitizer-authorized HTML prefixes through completion and growing plain Text nodes while streaming.
  **Alternatives rejected:** tail-only parsing bypasses full Markdown/reference policy; full-body replacement discards unchanged selection. Noncanonical or unmatched prefixes still use full replacement.
  Pins: `tests/frontend/test_frontend_markdown_streaming_defer.py`, `tests/frontend/test_frontend_streaming_markdown_dom.py`,
  `tests/visual/test_visual_streaming_reader_interactions.py`.
- **Markdown cache decision:** Problem: sampled hashes returned stale equal-length edits; environment-blind hits reused project/link/library output. Decision: native exact-string keys, shared raw/escaped LRU capacity, explicit environment invalidation and streaming bypass. Reject sampled/full hashes as sole identity and a second transcript store. The composition retains the existing 300-entry ceiling as supplied policy; the typed factory accepts an override or zero. On 500 seeded migration cases output matched after removing new lifecycle attributes. One local 20,026-character/10,000-hit run measured 14.50ms → 2.34ms; this is a synthetic reuse measurement, not a browser latency guarantee. A 300-entry fixture tracked 5,234,000 source-plus-HTML characters (not heap bytes); clear/dispose released all entries. Scratch content fell 9,010 → 0 characters. Library work has one math flight and the closed grammar catalog; selected code/math defers enrichment behind at most one selection listener, removed when idle, document changes or disposal occurs. No timers/observers are added. Late completion preserves current text, focus and reader selection. Specs: `tests/markdown-rendering.test.cjs` and `tests/visual/test_visual_markdown_enrichment.py`.
- Markdown uses GFM line breaks, but deletion requires paired `~~` delimiters.
  A single tilde remains literal, including around CJK text; parser output still
  passes through the rendering boundary's sanitizer before DOM insertion.
- Links emitted by the Markdown renderer open in a new tab: the renderer
  rewrites every anchor without an explicit `target` to
  `target="_blank" rel="noopener noreferrer"`, so navigation never replaces the
  single-page app. In-page `#anchor` links keep same-tab scrolling, `mailto:`
  keeps its default handler, and the rewrite is part of the cached render
  output. The behavior spec is `tests/frontend/test_frontend_markdown_link_target.py`.
- Human-authored turns (`actor === 'human'`) are plain text, not HTML: the
  classic text renderer passes `escapeRawHtml` to the markdown port, which
  escapes every `<` while code spans/fences and math are still placeholders
  (code keeps its literal `<`, single-escaped by the parser inside `<code>`).
  Without it, CommonMark raw-HTML passthrough lets the sanitizer or browser
  swallow a typed `<tag>` as an unknown element. `>` is never escaped so
  blockquote syntax survives. Assistant output keeps the raw-HTML contract,
  and the option participates in the render-cache key so an assistant echo of
  the same text renders independently.
- A reasoning block's active/complete state derives from the turn lifecycle, never from `segment.terminal` (that flag marks the terminal round's accumulator so the `thinking` channel projection can find it; inter-round reasoning segments are already closed). The presentation selector coalesces adjacent reasoning segments into one disclosure only when their explicit model-round and attempt/task scope match, and no reader-visible prose, tool, program, or diagnostic block separates them. Legacy segments with no round metadata may still coalesce together; known round boundaries never disappear. It retains every projected `blockId` in `memberBlockIds` and keeps the first identity as the stable DOM key, so grouping never rewrites Turn/SSE authority and a live run can grow without replacing the disclosure node. A settled turn renders every visible reasoning run complete and collapsed; on a live turn only the trailing run is active, and a run whose stream closes collapses itself. Reasoning is a slim muted disclosure, not a stack of cards. **Problem:** completed, collapsed long reasoning still parsed Markdown and highlighted code during navigation. **Decision:** a closed completed body above 4,096 characters is materialized on its native disclosure's `toggle`; the latest projection/translation wins, and an unchanged reopened body is reused. Streaming remains immediate. Node retirement removes its listener and captured content. **Alternative rejected:** truncating reasoning loses inspectable evidence; retaining full hidden rich DOM taxes every cold open.
- **Semantic round outline (decision).** The former decorative per-block rail
  and dots had no execution meaning. Replace them with compact heading/button
  rows over contiguous `segments` runs with an explicit `llmRound`. Identity
  includes attempt/task scope and the first durable block ID; a counter reset
  or a noncontiguous recurrence never joins another section. Model round labels
  are one-based presentation of `llmRound`, not Inspector request ordinals.
  The owning selector is `presentation/round-outline.ts`; mounted disclosure
  state belongs to `ui/round-outline.ts`. The rejected alternatives were a
  CSS-only restyle (retains ambiguous semantics) and rebuilding the transcript
  from activity events (a bounded diagnostic mirror cannot own chronology).
- Round headings show tool count and returned/active/background/attention
  state in text, not color alone. Completed routine history starts folded;
  active work, any unresolved background result, failed results, and important
  or unknown tool semantics stay exposed by default. Background dispatch does
  not mean command completion. User disclosure choices override later defaults
  for the mounted section. Expand all, collapse completed, and latest activity
  operate on the current mounted block window; they neither load older pages
  nor mutate execution. Native buttons, heading structure, `aria-expanded`,
  `aria-controls`, and a visible focus ring support keyboard/screen-reader use.
- Final/terminal answer text, system notes, and diagnostic activity blocks are
  never hidden inside process disclosures. Missing round metadata remains
  visible and ungrouped, rather than borrowing a nearby diagnostic round.
  Changing turn lifecycle also refreshes outline defaults when the block array
  is reused. Stable leaf/header keys preserve reader state across cloned
  snapshots; a full remount starts from defaults (there is no durable preference
  or cross-virtual-window disclosure guarantee). Pins:
  `tests/frontend/test_frontend_round_outline.py` and
  `tests/visual/test_visual_round_outline.py` (live app styles, EN/ZH,
  desktop/narrow widths, light/dark/Tofu themes, keyboard and retained-node checks).
- Tool rounds, segments, usage, translations, modified files, orchestration
  metadata, and error envelopes live inside the turn projection.
- A file-change block is one disclosure row: its title, chevron, and available
  undo/redo command share the native summary line. Activating the command emits
  the stable Turn intent without toggling the file-list disclosure.
- Tool execution is an attention ledger, not a raw event dump. New rounds carry
  semantic `attentionKind`; active/error state overrides it, and unknown legacy
  tools fail visible. A settled panel collapses by default only when all four or
  more entries are routine; a parallel batch collapses only when all three or
  more siblings are routine. Important, interactive, active, error, and mixed
  work stays exposed. `parentToolCallId` draws program/artifact children under
  their source without duplicating execution. User disclosure state survives
  semantic repaint, and collapsing never removes data from Turn authority.
- `segments` owns render order and `toolRounds` owns the rich tool body. The
  generated browser snapshot may receive completed, uniquely matched tool
  segments as `roundRef` records to avoid carrying their input/result bytes a
  second time. `ConversationSyncCoordinator` materializes those fields from
  the same round object before TurnStore publication. Opaque provider-replay
  bodies and API-round stream/routing/pricing evidence are server-only and
  absent from this browser view; per-round token/cost/cache/quota/trace facts
  used by the context gauge and finish popover remain. Live/resumable/failed
  Turns, unmatched segments, and replay events keep the complete segment form.
- Before persistence or public projection, an explicitly L1- or frame-compacted
  round synchronizes its honest `toolContent`/status into the uniquely matched
  segment result. Tool name, input, attempt/task scope, and LLM round must be
  compatible; blank/reused IDs or any mismatch leave the complete segment
  untouched. This prevents a stale render mirror from restoring payload bytes
  that the durable round deliberately replaced, without making segments a
  second compaction authority.
- Expanded tool results preserve field provenance. An own `toolContent` field on the occurrence-matched round is the browser's first result authority, including explicit empty/null values and structured V2 objects; only an absent field may fall back to that same round's `result.content`, then the materialized segment result. `results[]`, labels, snippets, activity details, and other presentation metadata may format that same call's result but may never substitute an unrelated read or revision. A tool card has exactly one disclosure: clicking its existing summary reveals either the authoritative result or a faithful human-oriented projection of that same settled call; renderers must not append a second nested “backend result” disclosure. Generic cards identify their source in the DOM. `get_conversation` follows the generic authority path even for old snapshots containing `convDigest`: a digest produced by a second repository read can describe another page or revision and is therefore never used as the settled call's expanded body.
- A failed `write_file` shows its error in the collapsed header and its exact
  bounded failure/recovery result when expanded, never the unapplied input as
  an added-line diff. Its handler verdict also reaches the canonical result
  receipt and activity timeline; a later successful recovery is a separate
  tool occurrence and cannot rewrite the failed attempt.
- Tool receipts have audience-specific projections. Before explicit durable compaction, the exact backend result remains model-context and debug authority; afterward the honest placeholder plus its receipt is authoritative. Protocol repair text is not automatically browser copy. `todo_write` supplies a complete flat snapshot (`todos` with content/status, plus optional `explanation`); the browser renders structured result metadata in both retained and turn-native paths. Replacement may remove old items or start new pending work after completion. Progress and escaped `todoExplanation` need no item IDs, revision or nesting fields; explanations occupy the expanded body, outside the horizontal header. Explicit `todoRejected` / `todoNoop` metadata distinguishes “Update rejected” from valid “Checklist unchanged” no-ops in the collapsed label; accepted changes retain their checklist progress label. Every audit round remains visible, while update counts/history include accepted snapshots only, identified by `todoUpdateCount` or round identity. Historical revision identity, operations and breadcrumbs remain readable when explicitly present; new snapshots do not imply `sync` or show revision/nesting instructions. Historical rejections never become successful updates or replace accepted state. Their expanded card preserves the full refusal reason as escaped text; result prose never overrides an explicit verdict.
- Conversation-scoped swarm push is presentation telemetry: agent phases and bounded tool history rebase onto the latest authoritative Turn on every Surface render, never replacing it or hiding newer parent revisions. Terminal presentation hydrates authority before releasing the overlay. An unsettled roster is resolved before fencing history: a settled sibling with the same session/task ID cannot veto it. Bare/full-v2/sparse receipts share snapshot ownership and select the rich panel without live-only flags; launch failures select error panels, never invented rosters. Session status filters to the launch roster; session identity never overwrites task/attempt identity, and unknown late launches cannot reopen settled or overlay-ended history while hydration is pending. Across eligible Turns, Absent launches require the current live producing attempt; retained unsettled receipts permit detached recovery. Missing run evidence stays unbound, never an execution-id conflict. Each `tool_use` binds rich data by exact `toolCallId` and ordered occurrence within scope (attempt ID, otherwise task ID; unscoped pairs only). Repeatable `llmRound` is only an id-less fallback unique in that scope. Snapshots retain complete final answers; reconstructible tool history keeps at most 30 rows and 32 KiB row/detail data per agent. Recent evidence wins; every elision is explicit in metadata and panel.
- Unsettled cards merge durable/live evidence by agent ID; incomplete live rosters
  cannot hide recorded models, worker clocks or tool results. Empty placeholders
  cannot erase evidence; terminal tool/agent verdicts cannot regress to running.
  Terminal recovery replaces stale active phases; settled snapshots are final authority.
  Live/persisted rows share occurrence `callId`; legacy id-less rows remain readable without guessing. Overlapping recovery
  windows merge in occurrence order, not arrival order; repeated older windows cannot
  replace the latest call or inflate omissions. Disjoint windows retain the newer
  contiguous suffix using omitted/row count, never joining across an unknown gap.
- Worker-start/active phases normalize queued placeholders to running; stale pending
  overlays cannot demote activity. Launch-tool completion is not child completion:
  reload derives active/queued counts from the roster without live-only flags.
  Background and panel summaries distinguish running/waiting from those same rows,
  localize both languages and hide zero counts; generic queue labels cannot cover
  running work. Explicit backend pending/capacity evidence (including retry admission)
  clears old running phase/clocks; bare placeholders cannot. Request-local fingerprints
  fence nonterminal recovery replies racing newer live evidence; terminal evidence
  remains eligible. Rows explain capacity/dependency waits and share copyable
  `agent-<role>-<id>` log identity; transport uses the canonical spec ID.
- The background-agent dock counts logical executions, not launch-record copies.
  With an explicit swarm run, identity includes Turn, attempt, run and agent, plus
  producer and worker execution tasks whenever known, so adding a sibling cannot
  change an existing execution's key.
  Array indexes are not identity: durable, provisional and activity evidence for
  the same assignment merge before counting. Distinct runs/attempts/tasks remain
  separate; missing task stamps join known evidence only when unambiguous.
  An explicit-run launch may hydrate its missing producer from the single
  compatible live producer before matching completion; a late stamp must not
  leave an obsolete running row beside the completed execution.
  Legacy evidence without a unique owner stays unbound. All candidate task
  stamps from both durable and activity sources are partitioned before merging
  or enriching either source: ambiguous worker evidence cannot inherit another
  execution's objective or tool records. Already matched activity remains part
  of the ambiguity evidence; input order cannot make an unknown stamp unique.
  Durable settled verdicts outrank provisional end markers, and both outrank stale live copies,
  even when the settled outcome is unknown. Unique task-stamp hydration preserves
  reader disclosure state and keyboard focus; actual task conflicts need separate
  execution-specific interaction state. Re-keyed rows fence stale async details;
  snapshot recovery retains worker/child identity and normalized tool evidence.
  Conversation actions appear and retire with the current child identity without
  replacing the row; removed buttons and disposed rows cannot navigate using
  stale callbacks, even if an action later reappears.
  **Decision:** reconcile duplicate assignment evidence before joining activity.
  Per-launch row identity inflates rows and running counts during hydration;
  deduplication by agent ID alone would instead hide genuine reruns. Neither
  alternative preserves execution identity and evidence authority. A provisional
  launch never infers its producing task ID from a swarm run ID: legacy frames
  without explicit execution evidence leave the task unknown until durable
  hydration supplies it. Explicit task, attempt, run and roster conflicts remain
  reconciliation fences.
- Dedicated status recovery carries bounded recent tools even on live parent Turns:
  parent liveness does not prove child pushes arrived. Visibility-aware scheduler/
  backoff owns polling; tool changes reset it and the background chip stays summary-only.
  Eligibility uses the rendered merged roster; terminal-only partial live lists cannot
  suppress unresolved durable children. Scheduling and delayed replies share scoped
  launch identity (attempt, execution, run, tool occurrence and roster), never bare
  provider IDs or old indices. Overlay rebase matches launches one-to-one; absent,
  replaced, settled or ambiguous owners reject replies. Push owner selection fences
  explicit producer/attempt even without a run id; canonical `conversationId`, routing `taskId`, `executionTaskId` and run
  identity remain distinct. Browser coverage drops all pushes: recovery within 20 seconds for
  live-parent/receipt-only states, hidden-tab pause/visible resume, no authoritative mutation.
- Restart pause (`interrupted`, reason `server_shutdown`) is known, non-settled and awaits owner Continue—not Cancelled/Complete/running. Resumed children reuse the launch/card with cumulative totals and no downtime clock; Stop/completed cannot resume running.
- Running cards open with latest tools visible; earlier calls/instructions use native disclosures.
  Generic action controls never open/close internal task/history folds as outer evidence. Keyed DOM preserves reader collapse, focus and scroll when
  timers/tools/history appear, never copying intent by position. Queued cards stay readable; pulses target dots, never tool rows.
- A provider `toolCallId` is not a unique Turn-level presentation key. When the
  same call id appears more than once, ordered `segments` and `toolRounds` pair
  by producer attempt/task, then occurrence, through one shared matcher. Native and
  retained renderers must preserve that association for rich bodies, including
  each `inspect_image` crop arguments and `imageDataUris`; scalar first/last-win
  lookups are forbidden: trimmed history must never borrow another attempt’s crop.
- A failed Turn renders the complete typed settlement error from the Turn
  authority, never the size-bounded activity-timeline copy. Its disclosure is
  expanded by default, remains user-collapsible, and preserves that local open
  state across a semantic repaint. When the typed envelope carries an upstream
  HTTP status, the synthesized terminal error activity preserves it and the
  visible facts include the exact code (for example, `HTTP 403`); permission
  copy must not claim a bad API key because model entitlement can be the cause.
- One terminal failure = one structured receipt. The failed Turn's disclosure
  carries a receipt line with the durable execution identity — `Turn` /
  `Attempt` / `Task` ids plus the envelope's `failureAttribution`
  (`roundNum` / `toolName` / `phase`, see `docs/modules/task_engine.md`) —
  and a copy-diagnostics action that hands over the compact bundle (ids +
  error facts + settlement outcome/cause/evidence/finish reason and retry receipts).
  Copy omits duplicate settlement errors, localization keys/hints, null stream
  state and resume anchors (including prompt, thinking, segments and tool indices). Failure reporting never depends on a
  screenshot or a manual selection inside the envelope `<pre>`. The footer
  kind chip and the disclosure present the same settlement error; the
  disclosure is the complete record.
- Every tool round with a `toolCallId` owns a `tool_use` segment. The
  projection authority (`projection_with_stable_segments`) repairs a stale
  checkpoint-era timeline by re-assembling when a round is missing — without
  it, a round appended after the last checkpoint never renders, and a
  human-wait round (ask_human / approval / stdin) that blocks the executor
  would never show its interactive card. The check is one-directional:
  segments may be a superset of `toolRounds` (compaction folds cold rounds
  out of the round list while their render blocks stay).
- Within one tool batch, no two sibling rows ever render the same title. The
  backend composes each round's label per call (MCP: resource + container +
  up to two operation chips), unaware of siblings; the browser
  (`siblingTitleDiscriminators`, applied in `_renderToolSlot`) suffixes any
  within-batch cluster still colliding on `(toolName, title)` with the args
  that actually differ across that cluster (` · key=value`, at most two keys
  present in every sibling), falling back to the occurrence index (` #n`)
  for byte-equal calls. Suffixes key on durable `toolCallId`, never array
  position, and never mutate the projected round.

- MCP resource anchors consume only backend `_mcpLinks`, including canonical
  LLM canvas URLs verified in `llm-mcp` source. `mcp-resource-links.ts` serves
  retained and typed fallback cards: bounded labels/URLs, HTTP(S) only, no
  credentials/control bytes, escaped attributes, no nested anchors, and new-tab
  `noopener noreferrer`. Browser title text never invents a resource route.

  **Historical fork gap:** when metadata is absent, the shared `contracts/llm_canvas_links_v1.json` policy recovers call-local source/new links from one explicitly successful item. Its exact retained-source summary annotation is allowed only with the owning server's `toolResultEvidence` sidecar (`lib/tools/result_envelope.py::_evidence_projection`): v1 evidence, v2 result, ok execution/status, complete retention/view, summary_items, nontruncated, and retained artifact/result identity. `sourceCoverage=unknown` is permitted because it describes upstream completeness, not omission from the received result (`lib/tools/result_value.py`); partial/unavailable source coverage is refused. Unknown retention/view, unattested annotations, failures, dry runs, multi-items, conflicting IDs/URLs and contradictory sidecars cannot add a new link. TS consumes the row sidecar; Python display entry points take it explicitly, never from producer content. No summary-only inference, retained-source fetch, history rewrite or live MCP call is introduced. **Rejected alternative:** stripping annotations or accepting arbitrary summaries would erase completeness fences; use raw retained canonical results when the owning sidecar cannot attest the inline view.
- Image reading, inspection, browser preview, generation, and editing share one
  immutable presentation projection. It scans at most 64 attached descriptors,
  renders at most 16 image tiles, and makes truncation visible. Image sources
  admit only HTTP(S), local blob, explicit relative/root paths, and allowlisted
  base64 image media; SVG open controls admit only HTTP(S) or explicit
  relative/root URLs. All captions, paths, prompts, status copy, operation
  values, and round identities are escaped, while only named header/icon slots
  are trusted HTML.
- Browser JavaScript execution shares one immutable presentation projection.
  Serialized arguments above 80,000 UTF-16 code units are rejected before
  `JSON.parse`; projected code, description, and result text are limited to
  65,536, 4,096, and 120,000 units. Each elision produces a localized visible
  notice. Query, round identity, projected arguments, status, and result are
  escaped, while only explicitly named header slots and the typed chevron asset
  are trusted HTML.
- Running and settled shell/code commands share one immutable presentation
  projection. It rejects serialized arguments above 80,000 UTF-16 code units;
  bounds commands, descriptions, live-output tails, results, and legacy status
  tails at 65,536, 4,096, 20,000, 120,000, and 2,048 units; and scans no more
  than 64 QR descriptors to render 16 tiles. Image sources pass the shared
  allowlist and complete Base64 grammar. Every elision is localized and
  visible. Retained code owns timer ticks, command-control authority (interrupt
  and explicit background handoff), and expansion sets only; presentation
  receives boolean state snapshots and named trusted HTML slots.
- Browser JavaScript shares Run Command's compact header, inline description,
  and independently folded code/output regions. Both start closed and keep
  per-occurrence state through running-to-settled repaint. The tab chip uses
  an observed page title, never a numeric tab/device ID. The selected route's
  pre-execution title is bounded to 240 UTF-16 units in `results[].tabTitle`,
  without changing executable arguments or model output. A historical quoted
  query title remains usable; absent evidence uses a neutral browser-tab label
  rather than today's live browser state.
- Pending write approval shares one immutable presentation projection. Risk
  fields, batch/single diffs, commands, and content previews are explicitly
  bounded; approval identity is escaped data consumed by a static restricted
  action rather than interpolated executable text. Retained code owns approval
  authority and dispatcher order only.
- The five synthetic injection lanes share one immutable presentation
  projection. Swarm XML is parsed only below 65,536 code units; no more than 16
  previews cross the renderer, Markdown/raw content is bounded to 16,384 units,
  and stall prompts to 32,768. Omitted records and truncated content are
  localized and visible. Conversation titles, Markdown sanitization, icons,
  and translation cross explicit ports; retained code owns chronological
  placement and peer navigation only.
- Human Guidance awaiting, expired, skipped, and submitted states share one immutable presentation projection. It bounds legacy JSON before parsing, scans/renders at most 16 options, limits identifiers/questions/labels/ descriptions to 512/32,768/1,024/8,192 code units, and visibly reports every elision or fail-closed identifier. A settled unanswered `ask_human` is read-only unless that Turn's settlement explicitly offers `answer_guidance` for the same guidance ID; that late-answer lane preserves the original choice cards instead of replacing them with a terminal receipt. Each interactive choice owns one optional 4,096-unit note draft. Live response IDs, option indexes, and original labels remain escaped datasets read by static delegated actions, never interpolated executable text. The typed action owner resolves only within the originating card, requires card/group/button/note identities and ordered option indexes to agree, rejects more than 16 DOM option groups, and transports only the selected label plus its selected note. It caps free text at 32,768 code units, admits one in-flight response per conversation/guidance identity, restores the exact choice state after rejection, distinguishes an expired 404 from transport failure, and owns teardown. Retained code owns EN-to-CN arrival translation only and receives presentation state through a lexical composition port.
- Current hosted OpenAI and local program code uses JavaScript highlighting.
- A `_programSynthetic` tool round is the display-only parent of native PTC or local JavaScript children. It intentionally owns no `tool_use` segment and never re-enters model context; the presentation selector materializes one keyed `program` block directly from the round, before its declared child calls. Childless parse failures still render the authored code, terminal status, and aggregate error before their activity diagnostic row. Settled results, including successful aggregates, start expanded before collapsed source. Local observations include their reason as “returned for model judgment”, without implying a paused task or failed tool. Hiding successful aggregates by default was rejected because it obscures the requested findings. `programStats` displays measured private child-delivery/result byte counts, requested call stages and truncation when available; old/native runs without measurements show no fabricated savings. The typed owner is `presentation/program-execution-presentation.ts`. All retained, classic and default surfaces render through `ui/program-execution-card.ts`; internal backend names are not user-facing labels. Evidence and code have separate keyed disclosures that preserve reader choices across updates.

  Problem: local gateway adapters duplicated the program header and zero-call
  summaries concealed retained-result lookups. Decision: suppress only uniquely
  owned same-round/attempt gateway occurrences; unowned or ambiguous failures
  stay visible. One standard tool header includes code-visible direct helper
  activity, measured counts, tool names and a bounded return-value preview.


  Problem: removing a gateway before placing its childless program moved the
  program past the next round's thinking and narration. Decision: replace a
  uniquely owned gateway with its program parent at that exact ordered segment
  slot, once per owner. A more specifically scoped occurrence wins over an
  unstamped compatible shell; equally supported gateway occurrences remain
  visible rather than consuming the first one. Parent identities follow durable
  sidecar order, independent of gateway materialization, preserving disclosure
  ownership through incremental hydration. Sidecars without a gateway retain
  declared-child placement, then the last authored block in the same
  attempt/task/model-round, bounded by any already anchored later program.
  Missing historical scope stamps are compatible only when the anchor is unique;
  conflicting or ambiguous scopes cannot establish ownership. Only missing round
  anchors may fall back to a later visible tool. Different
  model rounds remain separate thinking disclosures. Alternatives rejected:
  using later-tool position despite an authored gateway, joining distinct rounds
  to disguise the gap, or rewriting stored segments. Specs: program-blocks unit
  tests, the store-to-Surface lifecycle test, and real Chromium chronology tests
  at desktop/mobile widths.
  Historical exact native `execute_program` gateways without a synthetic owner
  use the same card via `program-gateway-presentation.ts`. Bounded serialized
  arguments supply only call-local source; authoritative raw output (or snapshot
  preview with matching declared Unicode character count) supplies evidence.
  Gateway `done` means Returned, not whole-program completion or model handoff.
  Missing counters explicitly say not recorded / unavailable; returned JSON
  and lexical helper activity never manufacture counters or host outcomes.
  Failure diagnostics are safe concise copy, with raw evidence collapsed.
  Artifact/cursor metadata remains inspectable but excluded from previews.
  Rejected: synthetic-only rendering, arbitrary code-tool aliases, and inferred
  completion/statistics from script-returned data.

  Problem: zero-call wording made saved-result reads look like no work, and
  keeper receipt JSON exposed private references in the glanceable header.
  Decision: plain-language script labels describe lexical activity; measured
  retained reads say how many reads occurred, not how many distinct values.
  Zero source-call counts remain in technical details. Exact SDK keeper
  receipts show only a saved-result size preview; raw results and source stay
  inspectable. Rejected: hiding reads, inferring intent from scripts, and
  expanding internal references in the header.

  Complete SDK directory results are labelled as returned/total retained inputs; zero retained inputs never means zero experiment jobs. The original returned JSON stays independently inspectable in the result disclosure. `program-activity.ts` lexes at most 32 KiB without evaluating source, skipping strings/comments/templates/regexp text; labels say “Script” in plain language, never claim execution or inferred intent. Result display bounds depth, nodes and characters, marking excerpts explicitly. Rejected: name-only adapter hiding and speculative intent. Settled null and empty-string results remain inspectable as `null` and `""`, including observation data; absent output and waiting placeholders remain distinct. Truthiness is not evidence of absence. Hiding these valid values was rejected because it makes a returned value look like missing evidence. Problem: fallback cards exposed engine identifiers and expanded code with judgment evidence. Decision: one typed card owns all three paths. Rejected: maintaining separate fallback labels, markup and disclosure rules.
- `activityTimeline` owns compact inline diagnostics, independently of round outlines. Attempt/task-scoped exact occurrences win; missing, mismatched or ambiguous identities never weaken to recycled call IDs. Generic round fallback stays within one monotonic producer run and proven chronology window; tool-ordinal resets also fence unscoped resumes. Otherwise a visible neutral position wins. Gateway children inherit the parent's occurrence/window. Deduplication requires an actually rendered matching tool card and its rejection/error evidence; ambiguous gateway receipts remain visible, never borrow another attempt. Specs: `presentation/activity-anchor.test.ts` and `tests/turn_lifecycle/test_activity_occurrence_anchors.py`. Compaction captures its boundary at start: `anchorPosition` is before/after a producer-scoped `anchorBlockId`, or `producer-start` for initial preflight. Completion preserves that boundary rather than re-anchoring after future output. Compaction rounds are zero-based, unlike request ordinals. Already-projected legacy rows without a boundary stay neutral; sparse replay uses producer-start conservatively, never guesses after future same-round prose. `summaryModel`/provider require observed dispatch identity aligned with bounded receipt call evidence; selection-only metadata is separately labeled. `taskModel` is the main model, never summary attribution. A typed positive call count must match the complete bounded ledger before actual identities are displayed; malformed/oversized evidence stays unknown and credential prose is redacted. Missing/mixed dispatch evidence stays unknown; no per-card archive fetch or historical Turn rewrite is introduced. Archived receipts remain inspectable. Visible rows are warnings/errors and settled `context_compaction` receipts, with estimated before → after counts and an owner-scoped archive link. Elapsed time retains the preceding phase start/summary generation; archive creation is not its start. A unique enclosing completed span may increase legacy display duration only; missing/ambiguous/incomplete timing leaves it unchanged. Routine info rows stay owned by their inline tools/live-status surface.
- The fixed composer area shows the latest main-lane turn's serving model while
  running and after settlement. An explicit fallback decision adds a visible
  warning with original model, target and cause. It never appears in reply headers.
  Before the first fallback response, the decision target takes precedence over
  the previous round. New human turns, navigation and disposal clear stale notices;
  background conversation updates cannot paint the active composer's model.
  Problem: reply headers scroll out of view during long generation. Decision:
  keep this turn's model identity at the input area; header-only, folded timeline
  and toast-only notices were rejected because they leave the bottom reading area.
  The model picker continues to mean the requested model for the next send.
- A `switched` timeline entry owns the historical fallback receipt, so the finish footer
  omits its legacy fallback tag. The transient live-status block is
  independent: phase status text never becomes a timeline row, so the two
  surfaces never duplicate a fact. Request Inspector may show the same phase's
  historical timing receipt because its purpose is postmortem evidence, not
  inline conversation presentation.
- `lastRoundUsage` is the canonical latest response-authoring agent round. It
  carries the logical and dispatcher-resolved model route plus compact prompt
  usage; internal compaction calls and billed-but-discarded retries remain
  accounting rows and never replace it. The finish footer reads this fact
  through one typed serving-route projection. Historical `apiRounds` are
  inspected only by that bounded projection, which excludes auxiliary rows.
- A settled turn's finish footer formats the backend `timingTrace.totalMs`
  only for a matching terminal task with complete `server-monotonic` evidence.
  The completion date comes from that trace's `tEnd`, with the Turn timestamp
  as a date-only fallback. Missing, partial, legacy or mismatched evidence
  never becomes a duration by subtracting lifecycle timestamps or browser
  clocks. One typed presenter owns selection for both classic and native
  renderers; the tag is absent while the turn is live. The Inspector retains
  detailed coverage/gap evidence.
- The finish footer's cost tag reads the projection's authoritative `cost`
  total (server-folded from settled usage). Historical turns without it are
  filled by the client cost-cache batch; that fill mutates no Turn fact, so
  the batch publishes a per-turn cost signature through presentation state
  and the footer's re-render compare treats a signature change as a footer
  change. Turns with an authoritative `cost` need no signature — the
  projection-revision compare already covers them.
- A settled turn's visible status and available actions come from its durable
  settlement. The browser never infers success from missing errors or from
  whether text happens to be non-empty.
- Optimistic UI state is limited to command-pending affordances and is cleared
  when the authoritative command result or snapshot arrives.
- A proposed plan's decision controls render inside its source Turn, directly
  after the plan block. Streamed translation previews may repaint that block,
  but execution identity always comes from the original `proposedPlan` sidecar.
- Transport health is separate from model execution status. A degraded connection never rewrites a turn to “failed,” and a model failure never masquerades as a reconnect problem. While the sync channel is `recovering`, `degraded` or `offline`, the conversation surface shows one inline conversation-level banner (`viewModel.transportNotice`, warning tone — never error styling) explaining that the transcript may be stale; it clears on the next healthy health publication. For a live attempt, the browser records content-free degrade/recover and receive-to-paint receipts only after the health/status renderer has run and a paint opportunity has elapsed. These receipts append to the durable generation-attempt timing trace; they do not authorize DOM state or completion. If only browser receipts survive, Request Inspector renders those receipts with an explicit missing-server-detail note and no fabricated waterfall axis.

## Mutation rules

All conversation mutations are turn commands with explicit owner identity and
stable command ids. A successful command returns canonical turn/attempt
records. Lost acknowledgements replay the receipt; they do not append a second
message. Whole-document `conversation.upsert`, `conversation.replace`, and
positional message mutations are outside the runtime contract.

## Verification

The smallest relevant gates are:

- `tests/frontend/test_frontend_round_outline.py`
- `tests/frontend/test_rich_card_reconciliation.py`
- `tests/frontend/test_round_card_stability.py`
- `tests/frontend/test_round_card_stability_browser.py`
- `tests/turn_lifecycle/test_activity_occurrence_anchors.py`
- `tests/visual/test_visual_round_outline.py`
- `tests/frontend/test_frontend_attempt_stream_vite.py`
- `tests/frontend/test_frontend_conversation_surface_vite.py`
- `tests/frontend/test_frontend_render_projection_reuse.py`
- `tests/frontend/test_frontend_markdown_escape_raw_html.py`
- `tests/turn_lifecycle/test_turn_projection_segments.py`
- `tests/turn_lifecycle/test_turn_projection_rev_adoption.py`
- `tests/turn_lifecycle/test_turn_activity_timeline.py`
- `tests/turn_lifecycle/test_turn_store_owner_migration.py`
- `tests/turn_lifecycle/test_turn_projection_swarm_carryover.py`
- `tests/tools/test_tool_round_sibling_discriminators.py`
- `tests/frontend/test_frontend_tool_rounds_render.py`
- `tests/tools/test_tool_attention_contract.py`
- `tests/tools/test_tool_browser_execution_presentation.py`
- `tests/tools/test_tool_command_execution_presentation.py`
- `tests/tools/test_tool_approval_presentation.py`
- `tests/test_tool_injection_presentation.py`
- `tests/frontend/test_frontend_peer_inject_row_layout.py`
- `tests/frontend/test_frontend_approval_card_render.py`
- `tests/tasks_pkg/test_approval_dialog_renders_risk.py`
- `tests/frontend/test_action_registry_nested_calls.py`
- `tests/frontend/test_frontend_cmd_interrupt_button.py`
- `tests/frontend/test_frontend_qr_render.py`
- `tests/frontend/test_frontend_tool_rounds_wire_parity.py`
- `tests/conversation/test_conversation_sync_v3.py`

When adding a renderable field, update the turn projection schema/normalizer,
the typed projection, and a behavioral renderer test. Do not add a second
fingerprint, repaint callback, or transport-specific fold.

## Read-tool result disclosure and sibling indexes

**Problem:** large settled read-tool output taxed initial transport/decoding and each visible tool row created/decorated an entire sibling array, defeating the existing WeakMap title discriminator index. **Decision:** the snapshot contract publishes explicit lazy historical output references. The tool renderer shows the bounded preview and fetches exact output only when its disclosure opens. Mounted detail state is keyed by owner, conversation, Turn, call, round index and revision; retiring a block releases its request/listener/cache, and error/stale states stay visible. Full output uses the existing escaped, bounded result renderer and preserves open disclosure state. Sibling title/Swarm lookup receives the stable canonical round array. Only the currently displayed row receives local Human Guidance, settled-Turn and task provenance decoration; local answers/translations therefore remain fresh. **Alternatives rejected:** repeatedly copying all siblings defeats memoization; permanently caching decorated guidance makes optimistic state stale; loading all tool details after first paint merely moves the same freeze into scrolling. `tests/conversation_tool_rendering.test.cjs` asserts three displayed rows decorate three times with 1,000 siblings, preserve producing task identity and reflect changed local guidance. The real browser gate covers fetch-on-expand, retry, escaping, loaded-detail reuse and abort on conversation switch.

## Shared PDF and preview style residency

**Problem:** PDF geometry and chat/Paper preview styling were startup CSS even when no reader was opened; one 363-line preview owner mixed navigation, source import, editing, selection and proposal responsibilities. Research reused the reader and controls, so moving styles only to the outer Paper entry would omit one consumer. **Decision:** `pdf-reader.ts` directly imports its 63-line geometry sheet. `reader-controls.ts` imports the three shared shell/control/responsive owners; `preview-panel.ts` imports seven detail owners after that shared dependency. The ten preview owners concatenate to the original sheet byte for byte; exact manifest hashes freeze those copies and the remaining legacy Paper sheet. Two open token allowlist entries close without authorizing new raw declarations. No new timer, listener, warmup request or cache is introduced. Existing Vite CSS preload awaits each real component graph before construction, and the existing document-lifetime feature transaction retains failure until reload. Styles remain available after panel teardown for later activation; component resources, focus and split classes still retire through their existing owners. The isolated startup sheet drops 25,367 raw bytes (4,213 gzip bytes); authored CSS remains counted by the unchanged budget, including every deferred owner. **Alternatives rejected:** a Paper-only style entry breaks shared Research controls; hiding sheets in nested directories would evade authoring budgets; unloading CSS on close creates ordering races; moving all legacy Paper CSS would also defer shared chat media/sidebar visibility and change theme cascade. The real Vite browser test covers all three component activation orders, themes, narrow layout, one request per component stylesheet, focus restoration, and two activation attempts after a failed shared stylesheet without constructing UI. Real PDF.js journeys cover navigation, bounded page rendering, selectable text, column/pinch anchors, desktop/mobile preview controls and teardown.

## Final report article ownership

**Problem:** the 2,286-line retained report renderer mixed task events with article decoration, glossary matching, figures and navigation. Four lazy-load document listeners stayed for the document lifetime, and scroll-spy observers survived Paper exit. Document-wide heading lookup could scroll another report with the same stable heading ID; figure framing could discard paragraph text when an image carried alternative text. **Decision:** `features/paper/report-article/` separates value contracts, callouts, figures, glossary extraction, aliases, term matching, heading indexes, composition and host lifecycle. The lazy presenter manifest imports its typed surface. Task streams, paid generation, audit cards, reading estimates/anchors and snapshots retain their established owners. Per-article Map/Set indexes accept prototype-shaped terminology and reserve existing heading IDs; repeated decoration preserves one mention per row/section. Mixed image/prose paragraphs and multiple-image paragraphs retain their original DOM content. TOC links carry escaped IDs and use one host click listener, which resolves the heading within its article and focuses it after scrolling the inner reader. Workbench navigation passes the actual heading to the same owner. Mounted glossary/figure listeners and the one scroll-spy per host release on repaint, skeleton, ready, aborted/fallback paint and Paper exit. Observer setup failure rolls back every resource before propagating the error. **Measurement:** six browser cases cover three themes at 390/1,180 pixels. Forty remounts retain exactly two observers for two readers, and teardown returns to zero; no article mouseover/focusin/click/keydown listener attaches to document. Sixty compiled remounts retain one observer per host; injected observe failure returns to zero and leaves no image handler. The retained file drops to 1,851 lines; nine typed owners are at most 119 lines each. **Alternatives rejected:** splitting more classic sections preserves hidden globals; caching fully decorated Markdown shares mutable DOM/reading state; global heading lookup remains ambiguous; cloning an image-bearing paragraph without checking remaining content silently deletes prose. The compiled and real-browser tests cover escaping, aliases/math, duplicate IDs, resource rollback, local navigation, glossary focus and Enter/Space figure actions.

## Settled segment timeline projection

**Problem:** the retained tool renderer mixed checklist history, occurrence queues, injection positioning, canonical batch policy and HTML. Retry batches sharing an anchor emitted the same injection repeatedly. Context-only batch coalescing could also move an intervening injection to the tail, and numeric string round keys missed numeric anchors. **Decision:** `conversation/ui/segment-timeline/` separates immutable contracts, accepted checklist history/superseded identity, injection extraction, occurrence queues, result-to-batch attachment, context display folding and composition. The retained adapter injects the existing canonical execution grouper and tool-group session; it retains prose/note rendering and context-rail effects. One session lasts one render. Per-ID queues use cursors, so repeated provider IDs resolve to their own result and dropped superseded segments never consume legacy positional results. Terminal deliverables stay outside this timeline. Each injection anchor emits once before its first displayed batch; unmatched anchors remain visible at the tail. Context folding stops at anchored injection slots. Numeric wire forms normalize only for synthetic anchor matching. Checklist projection retains every accepted/rejected/noop occurrence, derives accepted history with explicit tuple identities, counts completed items without allocating filtered arrays, and clones only the latest display row/metadata. No projection cache, timer, request, document listener or durable state is added. **Measurement:** a 2,000-attempt render creates one grouping session, renders 2,000 real results and one steering chip; the former loop rendered that chip 2,000 times. Seven native tests cover occurrence/fallback/history and injections. The existing public timeline/translation tests retain their biting grouping, translation, marker-strip and superseded controls. Real Chromium cases cover three themes at 390/1,180 pixels, chronological retry results, escaped injection, tool counts, keyboard disclosure, focus, open state and selected text after append. **Alternatives rejected:** ID-to-one-result maps conflate repeated calls; retained-only splitting preserves hidden dependencies; caching stales updates; merging context across an injection slot changes wire order; hiding rejected/noop checklist occurrences deletes audit evidence.

### Sub-agent completion reports

**Problem:** completion injections carry `<result>`, while the inbox card read only historical `<preview>`; a 1,200-character server-side XML slice also discarded report closing tags. Users saw status and a log path only. **Decision:** inbox events and their reload sidecars retain the same full XML payload injected into model context. Cards prefer `<result>`, retain historical `<preview>` support, and render reports through the existing safe Markdown port. The existing disclosure contains the report and the audit log path. Presentation scans at most 65,536 UTF-16 units, displays at most 16,384 report units, and explicitly labels truncation; a report extending past the scan still renders its bounded prefix. Model delivery and acknowledgement remain unchanged. **Alternatives rejected:** slicing serialized XML before rendering silently loses reports; showing only the log requires users to reconstruct a deliverable from intermediate output. Full payloads increase the per-injection durable sidecar by the report length beyond the former 1,200-character slice; no new cache, poller, timer, or retained browser subscription is introduced. The 8,123-character multilingual regression report produces an 11,381-character XML payload; its UTF-8 JSON preview grows from 1,476 to 14,125 bytes. Retention continues to follow its owning Turn rather than a separate artifact lifecycle. Verification: `tests/test_tool_injection_presentation.py`, `tests/tasks_pkg/test_swarm_inbox_reports.py`, and `tests/visual/test_visual_swarm_inbox_reports.py`.

## Final answers after oversized settlement

**Problem:** a text-only terminal write can settle a large Turn while its
segment checkpoint still ends at a tool call. The saved answer exists in
`projection.content`, but a segment-only renderer hides it.
**Decision:** the view-model appends one display-only terminal text block from
a settled Turn's authoritative content when its segments have no terminal or
deliverable text. Existing terminal blocks retain their identity, translation
and proposed-plan behavior. Running Turns and compaction summaries do not gain
an answer block. This does not change storage, settlement or resume authority.
**Alternative rejected:** marking the completed Turn interrupted invents a
lifecycle outcome; read-time storage repair rewrites durable chronology.
Verification: `view-model/turn-blocks.test.ts` and the missing-final-answer
visual regression.

## Finish Tag reasoning mapping

**Problem:** the finish footer displayed requested depth as if it were the
runtime effort; current catalog declarations cannot prove an old response's
selected-slot resolution. **Decision:** display the response-authoring round's
compiler evidence (`lastRoundUsage.reasoningResolution`), e.g. `Medium → Max`
or `Medium → High`, using the actual compiled native effort. The compiler adds
`requestedValue`, `actualMode`, and `actualValue` to `_reasoning_resolution`;
stream and non-stream transports preserve it privately in returned usage before
wire translation strips internal fields. The bounded authoring snapshot and
`apiRounds[*].usage` retain it through settlement and cold reload. This is
request-control evidence, not a claim about a provider's hidden computation.

Historical `apiRounds` may supply evidence only from the latest authoring agent
round; compaction and discarded retries are excluded. An explicit authoring
snapshot without evidence cannot borrow an older round's resolution. Without
evidence the footer shows `Medium (requested)`, never an invented actual rung.
**Alternatives rejected:** mapping from model names/current metadata would
misreport route overrides and old history; persisting full request bodies would
inflate durable rows and expose unrelated data. Composer defaults are unchanged.

Specifications: `turn-reasoning-mapping.test.ts`,
`tests/llm/test_anthropic_reasoning_effort.py`, and
`tests/tasks_pkg/test_round_usage_live_projection.py`.

## Native rich responses and published assets

**Problem:** a generated file path in Markdown is not a browser URL; a local
preview cannot prove delivery. Free-form HTML also cannot establish action
permission, stable card identity or a consistent visual hierarchy.
**Decision:** `contracts/rich_response_v1.schema.json` defines bounded native
comparison, chart, timeline, table, image gallery, evidence, artifact-reference and action
blocks. Trusted TypeScript presenters own design-system controls, themes,
accessibility and numeric formatting. Producers publish assets separately with
owner/conversation identity; references carry stable IDs, never arbitrary paths.
Rich output retains readable prose as a fallback. Original payload identities,
measurements and actions are not re-derived from translated display text.

**Inline timing diagrams:** the `timeline` block draws labeled interval lanes and
point markers over an explicitly supplied numeric axis, using native SVG owned by
the presenter, not model-authored SVG. A semantic data table preserves every
label and coordinate for assistive technology. Labels remain inert text; axis
order, interval order and containment, marker containment and lane identities
are validated at both browser admission and publication. No animation, requests
or timers are needed. The same block works in inline `tofu-ui` fences, tool
results and artifacts. Invalid or incomplete fences keep the existing inspectable
fallback; ordinary code fences are never guessed into diagrams.

**General charts:** `data_chart` declares `kind`, optional axis labels/units and
bounded named series. Cartesian points require numeric `x`; categorical points
require labels. Pie evidence has one nonnegative series with positive total.
Both admission boundaries reject duplicate series and inconsistent shapes.
SVG domains include zero for bars, retain negative values and remain finite for
constant data. Series styling is presenter-owned; complete authored values are
preserved in semantic tables. No chart library, animation or worker is needed.

**Supplied-source citations:** `citations` provides bounded sources with unique
IDs, title, absolute HTTP(S) URL and optional publisher/excerpt. Credentials,
ambiguous separators and unsafe schemes are rejected. Explicit prose references
`[[cite:blockId/sourceId]]` become keyboard-accessible chips only alongside rich
fences, after sanitized Markdown rendering; code and anchor labels remain inert.
Unknown, duplicate or incomplete sources stay unavailable. The focused preview
is a shared-kit dialog with inert excerpt text and a safe external source link;
Escape restores focus. Updates/replay/disposal retain or retire the keyed view.
The frontend never fetches the source, embeds favicons, or claims verification.

**Durable choices:** `choice_prompt` contains an opaque `interactionId` only, not
question text, options, endpoint or permission. The production conversation host
binds it to the mounted owned conversation/turn and reloads the server `ask_human`
round. Single-choice tool rounds also render natively without requiring a model
to emit rich JSON while blocked. Radio/button factories own the controls; explicit
selection and submit are required. Authenticated `answer_guidance` commands own
durable acceptance, replay and recovery; the frontend reports a saved answer
only after that authority confirms it. Invalid, foreign, stale and unsupported
contexts fail closed; no localStorage answer is treated as durable truth.

Distributed legacy-gate markers carry owned turn routing, not answer authority.
Remote replicas still commit through the same durable operation, and a scoped
waiter treats every runtime-state notification as a wakeup to re-read the receipt.
The existing abort interval bounds receipt checks while a real question waits;
there is no extra resident frontend polling loop or persistent answer cache.

**Component extensibility decision:** the native renderer is the presentation
boundary, not an executable application supplied by a model. New component
families extend the closed schema first, then a focused presenter, admission
rules, localization and tests. Producers share the inline/tool/artifact routes
rather than introducing per-tool HTML dialects. Stateful controls reuse durable
conversation commands and their ownership checks; a model-provided URL or
identifier never authorizes a submission. Source excerpts are supplied evidence,
not proof that the frontend fetched or verified a page. Heavy chart packages,
remote preview fetching and arbitrary React/JavaScript were rejected: the
required data views can be rendered by bounded native SVG and semantic HTML
without startup, idle-network or remote-code costs. Complete proprietary
protocol compatibility is not inferred from screenshots.

**Authoring/discovery:** searchable tool guidance advertises native rich responses;
`publish_rich_response` exposes the generated contract and directs models to use
native components rather than ASCII or code-only diagrams when appropriate.
Prose remains a valid fallback. Discovery is not a guarantee that a model emits a
rich block. Metric comparisons retain their existing `chart` shape; general
`data_chart` supports line/scatter/bar/pie with declared series and numeric points.
Remote MCP Apps and arbitrary executable React components are not supported by
this contract. Screenshot similarity does not
establish compatibility with a proprietary frontend protocol.

Cards project the existing Turn/tool/artifact authorities; there is no parallel
transcript, durable browser business-data store or polling loop. Compatible keyed
updates retain reader disclosure and focus. Incomplete/invalid/version-unknown
payloads are inert, visible fallbacks rather than executable partial UI. Every
image load has an explicit ready/unavailable state and bounded reader-triggered
retry. New output does not return a filesystem path as an image source.

Only declared asset/artifact open/download actions are supported. Model markup,
JavaScript, resource URIs and tool names do not grant permission. Larger generated
HTML remains in the existing isolated artifact surface with restricted CSP;
native cards never inject arbitrary model HTML into the application document.

Markdown HTML also fails closed to escaped visible text when its sanitizer is
unavailable; optional-library failure cannot bypass content admission.
MCP native structured output may share this data contract, but a remote MCP Apps
resource cannot silently become executable trusted application code.

**Alternatives rejected:** exposing `/data` makes unrelated private files
reachable; global URL-rewrite guesses cannot establish ownership or publication;
asking a model to redesign controls on each answer produces inconsistent unsafe
UI; adding a second conversation state owner breaks reload and chronology.
The schema preserves discriminators, identifiers and cross-field validity,
without block or collection-count admission quotas. Optional image work belongs
to mounted cards and is released at teardown. Historical Markdown
remains readable and can explain inaccessible local-file references without
publishing them implicitly. Browser verification, not file inspection, is the
acceptance boundary for delivered assets and rich layouts.

Publication entry points are discoverable `publish_response_asset`, `publish_response_file` and `publish_rich_response` tools. Their shared spec is registered in the live assembly, not only the inventory projection. Publication is a non-cacheable write capability; Plan Mode and storage-free/ownerless runtimes cannot acquire it. The full schemas remain searchable so ordinary turns do not pay the rich schema cost. Search/discovery does not grant execution. Successful named image read/write operations may publish supplementary owned assets; shell output never grants file authority. Remote-device paths must first be explicitly transferred to the server. Artifact bytes use the existing immutable artifact codec: rich JSON is physical Markdown with a rich presentation facade, not a database format migration. Images carry verified MIME, dimensions, byte count and SHA-256; HTML/SVG bytes, symlinks, traversal, FIFOs, oversized and changing files are refused. Encoded image payloads add approximately one-third to byte storage (the pilot PNG is 135,856 bytes); dedupe reuses repeated bytes within a conversation. Durable assets are not a reclaimable browser cache. Response-image validation decodes complete supported images in a parent-owned process without a private byte or pixel ceiling; Pillow policy changes stay inside that worker. Rich JSON and assistant Markdown have no private aggregate byte or fence-count admission limit. All complete top-level tofu-ui fences can activate; nested ordinary code, incomplete fences, unsupported data and human-authored fences retain their existing inert behavior. Tool projection examines complete declared candidates and recognized wrappers, using object identity to stop cycles; remote resource URIs still grant no execution authority. Supported nonempty image blobs have no private browser byte ceiling. Each mounted image starts independently and retains abort, retry, object-URL revocation and late-delivery guards.

**Explicit existing-file delivery:** `contracts/response_file_v1.schema.json`
owns types, bounds and receipt metadata. Verified task roots admit immutable
UTF-8 documents and supported binary attachments; no route serves caller paths.
Every path component is no-follow; special, changing and renamed files refuse.
Documents keep their original bytes; binary bytes use a complete base64 envelope
inside the existing Markdown codec with a `file` presentation facade. Publication
returns metadata only, never echoes a large body into model context or events.
Delivery verifies one authorized full-body read instead of fetching it twice.
Source cleanup does not remove a durable snapshot. Dedupe cannot change owner,
MIME, format or private provenance. Images retain their separate codec/policy.

`GET /api/artifacts/{id}/download` rechecks the live conversation owner, including
legacy artifacts. Raw/view/export and lifecycle/metadata access enforce the same
owner boundary; generic raw and source views never reveal attachment envelopes.
Downloads use `attachment`, `nosniff`, and `private, no-store`. HTML/SVG default
to inert text; original-code downloads require the trusted warning action.
Untrusted Markdown cannot select executable delivery mode. Legacy panel downloads
use this same live-authorized route rather than cached Blobs. Metadata is rechecked
on reopen; byte caches, consent dialogs and interactive frames release on close.
Default preview blocks scripts; explicit opt-in stays opaque-origin sandboxed,
not network-isolated. Publish self-contained documents or archives deliberately;
no directory mounting or dependency resolution is implied.

**Unpublished references:** the Markdown presenter turns clear workspace-file
links into escaped, readable notices, never fetches or publishes them implicitly.
Labels and source paths remain selectable. External HTTP(S), app routes, proxy
prefixes, fragments, mail and ambiguous web-relative URLs retain their meaning.
Historical responses are displayed safely without rewriting stored Turns.

**Markdown image presentation:** Problem: a universal 3:2 reservation letterboxed screenshots, legacy thumbnail limits hid detail, and pointer styling promised a preview that plain Markdown images never dispatched. Decision: sanitized image projection marks admitted images for responsive natural-ratio sizing. Image-only paragraphs own one normal prose gap without an inline baseline or image margin; mixed prose and linked images retain their content/navigation. Unlinked raster images open the existing preview on click or Enter/Space through the scoped ConversationSurface, using the current allowlisted source, not model-supplied preview metadata. Keyboard listeners retire with the surface. The existing preview focuses its close control, contains Tab navigation, restores a connected trigger, and cancels delayed body cleanup on reopen. Explicit width and height attributes may reserve the real ratio; unknown dimensions are not guessed. No asset publication, permission, polling, observer or persistent state is added. Rejected: a permanent guessed ratio trades scroll stability for visible gutters; a second lightbox duplicates existing behavior; wrapping linked images in another action steals navigation. Specs: Markdown image unit tests and `tests/visual/test_visual_markdown_images.py`.

Private image delivery is authenticated, `nosniff`, and `private, no-store`, so
logout cannot replay a browser cache entry. The generic artifact raw route
cannot expose response image envelopes. Rich provenance collisions with legacy
artifact dedupe are rejected rather than downgrading access. Inline/tool/panel
artifact actions first resolve through the owner-and-origin-conversation boundary
`GET /api/response-assets/artifacts/{id}?conversationId=...`; missing context
is disabled, inaccessible references are reader-visible failures, and downloads
recheck authorization. Trusted native rendering does not execute remote MCP Apps
resources. MCP structured content can project this schema, but MCP Apps protocol
support is not claimed.

Executable specifications: `tests/artifacts/test_response_files.py`, `tests/artifacts/test_response_assets.py`, `test_response_asset_http.py`, `tests/tasks_pkg/test_rich_response_discovery.py`, Markdown workspace-link tests, `tests/visual/test_visual_rich_delivery.py`, `tests/visual/test_visual_coordinator_delivery.py`, `tests/visual/test_visual_rich_responses.py` and `frontend/src/rich-response/rich-response.test.ts`. Browser tests use real HTTP/Pillow/artifact storage and production rendering with fixture identity mapping; they cover direct/forwarded paths, three body-scoped themes, 320px layout, keyboard disclosures, permission denial, logout, retry and reload. The gallery artifact dialog demonstrates an authorized intent, not the full application artifact panel. The measured historical pilot lives in `designs/ui-gallery/rich-response.fixture.json` with regenerated screenshots; it is not a certification of the current database build.

## History-search receipts

Problem: the fixed history-search verb discarded the keyword and repeated a conversation source badge; English empty-result prose concealed actual scope and partial index coverage. Decision: `conversationSearch` metadata is produced by the same owner-scoped backend query and rendered by `conversation/ui/project-receipt/search.ts`. The sole header shows keyword, effective scope and returned count, without the redundant source chip. The body localizes zero-match versus incomplete/unknown coverage and names the indexed field boundary (titles/public text, not tool parameters/results). Explicit project scope without project context fails rather than widening silently. Empty continuation pages are resumed within the reference-tool work bound in `contracts/conversation_search_v2.json`; exhausting that bound never proves absence. Historical records lacking metadata retain raw same-call evidence without invented counts or coverage. No transcript reads, new network request, or client-owned search state is introduced. Alternatives rejected: parsing prose as success, unconditional scope widening, and treating a first empty page as an exhaustive search. Specs: `project-receipt/search.test.ts`, `tests/conversation/test_conv_ref_search_receipt.py`, and the desktop/mobile `tests/visual/test_visual_conversation_search_receipt.py` fixture.

## Composable task surfaces

**Problem:** fixed task pages cannot anticipate each experiment's metrics, queue
shape or questions, while unrestricted generated applications cannot establish
ownership or coherent interaction. **Decision:** the existing rich-response
schema/rendering owner admits `metric_grid`, `workflow`, `notice`,
`resource_panel` and `form_prompt`, composed by optional stack/grid/tabs groups.
Every block appears exactly once. Stable identities preserve tabs, disclosures,
resource filters, focus and pending form drafts across compatible updates.
Metrics distinguish null from zero; trends retain accessible source values.
Workflow state is producer-supplied evidence, never execution or approval authority.

Resource panels show timestamped historical/provider snapshots, separate GPU
kinds/units, searchable/sortable/paged rows and explicit failure/empty states.
Refresh failures retain the last snapshot with a stale warning. Only an owned
Turn or Evolution run can read current capacity. The closed card schema contains
provider/registered connection/queue identifiers, never endpoints or credentials.
The backend rechecks ownership after reads. Free-form HTML/JS, arbitrary tool
invocation and capacity-as-reservation semantics are rejected alternatives.

`form_prompt` references an existing server `ask_human` free-text/batch question.
Automatic tool presentation also supports batch questions. Options and eligibility
come from the owned Turn; explicit submission uses the existing durable
`answer_guidance` command and its idempotency receipt. Lost acknowledgements
preserve the draft and command identity; saved answers restore after page reload.
Reloading a pending question preserves its draft when question structure matches.
Free-text tool rounds retain the legacy rich Markdown/QR path by default.

Chat, tool output, artifact preview and Evolution evidence reuse this renderer.
Evolution program/inspector pages expose a lazy resource launcher: opening it
lists configured platforms; raw queue adapters require an explicit queue name.
Headless authorized Evolution producers may publish data-only rich results into
their existing task result; conversation assets/actions/questions still require
an owned conversation. There is no second execution or durable presentation store.

Verification: rich-response Vitest suites, `tests/artifacts/test_response_surfaces.py`,
real HTTP/browser `tests/visual/test_visual_dynamic_surfaces.py` and
`tests/visual/test_visual_rich_responses.py` (including durable batch answers).
`designs/ui-gallery/dynamic-response.html` uses the actual presenter and explicitly
illustrative data; its desktop/320px screenshots cover all three themes.
