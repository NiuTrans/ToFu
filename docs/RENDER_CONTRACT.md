# Conversation rendering contract

This document defines the current browser rendering invariants. The storage
and wire authority is [CONVERSATION_SYNC_V3.md](CONVERSATION_SYNC_V3.md); the
frontend module boundary is [FRONTEND_ARCHITECTURE.md](FRONTEND_ARCHITECTURE.md).

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

Live phase text rides event payloads and is cleared by terminal settlement. It
is not durable turn projection state, so a stale “thinking” or “running tool”
label cannot survive reload. Independently, the timing-trace projector keeps a
bounded historical receipt of each phase transition (`detailKey/detailArgs`,
interval, repeat count) for postmortem inspection. That history is diagnostic
evidence only: it never repopulates live phase state or renders as a chat
timeline row.

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

## Projection and DOM rules

- The DOM is derived from `TurnState`; background code does not edit chat
  nodes directly.
- Reconciliation is semantic, not reference-based. A full snapshot may recreate
  JSON-shaped projection objects; structurally unchanged blocks must retain
  their DOM nodes, focus, disclosure state, and nested scroll position. The
  structural comparison is explicitly bounded and falls back to repainting
  when its depth or node budget is exhausted.
- Text and reasoning blocks update their retained content containers in place.
  Rich tool HTML is replaced only when its rendered value changes; a genuine
  tool-result update restores matching native `details`, `aria-expanded`,
  `.expanded`, focus, and scroll state after replacement. This is the stable
  paint contract that prevents streaming snapshots from collapsing content a
  user is currently reading.
- Markdown uses GFM line breaks, but deletion requires paired `~~` delimiters.
  A single tilde remains literal, including around CJK text; parser output still
  passes through the rendering boundary's sanitizer before DOM insertion.
- Links emitted by the Markdown renderer open in a new tab: the renderer
  rewrites every anchor without an explicit `target` to
  `target="_blank" rel="noopener noreferrer"`, so navigation never replaces the
  single-page app. In-page `#anchor` links keep same-tab scrolling, `mailto:`
  keeps its default handler, and the rewrite is part of the cached render
  output. The behavior spec is `tests/test_frontend_markdown_link_target.py`.
- Human-authored turns (`actor === 'human'`) are plain text, not HTML: the
  classic text renderer passes `escapeRawHtml` to the markdown port, which
  escapes every `<` while code spans/fences and math are still placeholders
  (code keeps its literal `<`, single-escaped by the parser inside `<code>`).
  Without it, CommonMark raw-HTML passthrough lets the sanitizer or browser
  swallow a typed `<tag>` as an unknown element. `>` is never escaped so
  blockquote syntax survives. Assistant output keeps the raw-HTML contract,
  and the option participates in the render-cache key so an assistant echo of
  the same text renders independently.
- A reasoning block's active/complete state derives from the turn lifecycle,
  never from `segment.terminal` (that flag marks the terminal round's
  accumulator so the `thinking` channel projection can find it; inter-round
  reasoning segments are already closed). The presentation selector coalesces
  adjacent reasoning segments into one disclosure when no reader-visible
  prose, tool, program, or diagnostic block separates them. It retains every
  projected `blockId` in `memberBlockIds` and keeps the first identity as the
  stable DOM key, so grouping never rewrites Turn/SSE authority and a live run
  can grow without replacing the disclosure node. A settled turn renders every
  visible reasoning run complete and collapsed; on a live turn only the
  trailing run is active, and a run whose stream closes collapses itself.
  Reasoning is a slim muted disclosure, not a stack of cards.
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
- Expanded tool results preserve field provenance. An own `toolContent` field
  on the occurrence-matched round is the browser's first result authority,
  including explicit empty/null values and structured V2 objects; only an
  absent field may fall back to that same round's `result.content`, then the
  materialized segment result. `results[]`, labels, snippets, activity details,
  and other presentation metadata may format that same call's result but may
  never substitute an unrelated read or revision. A tool card has exactly one
  disclosure: clicking its existing summary reveals either the authoritative
  result or a faithful human-oriented projection of that same settled call;
  renderers must not append a second nested “backend result” disclosure.
  Generic cards identify their source in the DOM. `get_conversation` follows
  the generic authority path even for old snapshots containing `convDigest`:
  a digest produced by a second repository read can describe another page or
  revision and is therefore never used as the settled call's expanded body.
- Tool receipts have audience-specific projections. Before explicit durable
  compaction, the exact backend result remains model-context and debug
  authority; afterward the honest placeholder plus its receipt is authoritative.
  Protocol repair text
  is not automatically browser copy. For `todo_write`, the model receipt keeps
  stable item IDs and exact `sync`/`replan` rejection reasons; the browser card
  renders the structured current checklist, labels rejected/no-op attempts only
  as “checklist unchanged,” and counts/history-lists accepted revisions only.
  A rejection never replaces the last accepted state or exposes remediation
  jargon such as missing IDs to the person reading the conversation.
- Conversation-scoped swarm push frames are presentation telemetry only. Their
  agent phase and bounded tool-call timeline are rebased onto the latest
  authoritative Turn on every Surface render; a cached swarm overlay never
  replaces that Turn or hides newer parent content/tool revisions. Terminal
  swarm presentation hydrates authority before the overlay is released. The
  overlay rebinds each `tool_use` segment to rich round data by exact
  `toolCallId`; `llmRound` is attempt-local and can repeat after continue or
  regenerate, so it is only an id-less legacy fallback when the match is unique
  within the segment's attempt/task scope. The durable snapshot keeps each
  child's complete final answer, but its
  reconstructible tool timeline keeps at most 30 rows and 32 KiB of serialized
  row/detail data per agent; recent evidence wins and every row/detail elision
  is explicit in projection metadata and the panel.
- A provider `toolCallId` is not a unique Turn-level presentation key. When the
  same call id appears more than once, ordered `segments` and ordered
  `toolRounds` pair by occurrence (first-to-first, second-to-second). Native and
  retained renderers must preserve that association for rich bodies, including
  each `inspect_image` crop arguments and `imageDataUris`; scalar first/last-win
  lookups are forbidden because they can show a valid but unrelated crop.
- A failed Turn renders the complete typed settlement error from the Turn
  authority, never the size-bounded activity-timeline copy. Its disclosure is
  expanded by default, remains user-collapsible, and preserves that local open
  state across a semantic repaint. When the typed envelope carries an upstream
  HTTP status, the synthesized terminal error activity preserves it and the
  visible facts include the exact code (for example, `HTTP 403`); permission
  copy must not claim a bad API key because model entitlement can be the cause.
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
  visible. Retained code owns timer ticks, interrupt authority, and expansion
  sets only; presentation receives boolean state snapshots and named trusted
  HTML slots.
- Pending write approval shares one immutable presentation projection. Risk
  fields, batch/single diffs, commands, and content previews are explicitly
  bounded; approval identity is escaped data consumed by a static restricted
  action rather than interpolated executable text. Retained code owns approval
  authority and dispatcher order only.
- The four synthetic injection lanes share one immutable presentation
  projection. Swarm XML is parsed only below 65,536 code units; no more than 16
  previews cross the renderer, Markdown/raw content is bounded to 16,384 units,
  and stall prompts to 32,768. Omitted records and truncated content are
  localized and visible. Conversation titles, Markdown sanitization, icons,
  and translation cross explicit ports; retained code owns chronological
  placement and peer navigation only.
- Human Guidance awaiting, expired, skipped, and submitted states share one
  immutable presentation projection. It bounds legacy JSON before parsing,
  scans/renders at most 16 options, limits identifiers/questions/labels/
  descriptions to 512/32,768/1,024/8,192 code units, and visibly reports every
  elision or fail-closed identifier. A settled unanswered `ask_human` is
  read-only unless that Turn's settlement explicitly offers `answer_guidance`
  for the same guidance ID; that late-answer lane preserves the original choice
  cards instead of replacing them with a terminal receipt. Each interactive
  choice owns one optional 4,096-unit note draft. Live response IDs, option
  indexes, and original labels remain escaped datasets read by static delegated
  actions, never interpolated executable text. The typed action owner resolves
  only within the originating card, requires card/group/button/note identities
  and ordered option indexes to agree, rejects more than 16 DOM option groups,
  and transports only the selected label plus its selected note. It caps free
  text at 32,768 code units, admits one in-flight response per
  conversation/guidance identity, restores the exact choice state after
  rejection, distinguishes an expired 404 from transport failure, and owns
  teardown. Retained code owns EN-to-CN arrival translation only and receives
  presentation state through a lexical composition port.
- A `_programSynthetic` tool round is the display-only parent of native PTC or
  local ToolScript children. It intentionally owns no `tool_use` segment and
  never re-enters model context; the presentation selector materializes one
  keyed `program` block directly from the round, before its declared child
  calls. Childless parse failures still render the authored code, terminal
  status, and aggregate error before their activity diagnostic row.
- `activityTimeline` entries render as compact diagnostic rows anchored
  inline where they happened, never as one consolidated tail block and never
  reconstructed from transient phase state or another transport. A row with a
  matching `toolCallId` sits directly under that tool's inline block (the
  block itself owns the call/result; the row adds the failure reason/detail);
  every other row rides its 0-based `llmRound` anchor — after the last block
  of the same round, else after the last block of an earlier round, else the
  turn start for pre-request diagnostics such as preflight schema isolation.
  `llmRound` is monotonic only within one execution attempt — a continued
  turn restarts the counter at 0 — so the round scan is confined to the
  chronology window the durable timeline proves (after the latest preceding
  tool row, before the earliest following one); only a row without any
  window keeps the legacy unbounded scan. Synthesized gateway
  `execute_tools` children are not raw timeline entries: they inherit the
  parent row's window instead of degrading to that scan.
  The visible rows are the durable facts only: warning/error entries such as
  retries, schema isolation, model switches, and failures, plus a settled
  `context_compaction` receipt. That receipt renders a distinct before → after
  token rail and links to its owner-scoped archive snapshot. Other info-level
  rows are display-filtered — the inline tool blocks and the live-status
  surface already own those facts — so a legacy projection recorded before
  the fold contract tightened renders identically to a current one.
- A `switched` timeline entry owns fallback presentation, so the finish footer
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
- A settled turn's finish footer renders a timing tag with the wall-clock
  duration, derived from the durable attempt timestamps (`createdAtMs` ->
  `settledAtMs`) carried on the finish projection — never recomputed from
  client clocks or inferred from streaming. The tag renders after the status
  tag, is absent while the turn is live, and the same value feeds the finish
  popover breakdown.
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
- Transport health is separate from model execution status. A degraded
  connection never rewrites a turn to “failed,” and a model failure never
  masquerades as a reconnect problem. For a live attempt, the browser records
  content-free degrade/recover and receive-to-paint receipts only after the
  health/status renderer has run and a paint opportunity has elapsed. These
  receipts append to the durable generation-attempt timing trace; they do not
  authorize DOM state or completion. If only browser receipts survive, Request
  Inspector renders those receipts with an explicit missing-server-detail note
  and no fabricated waterfall axis.

## Mutation rules

All conversation mutations are turn commands with explicit owner identity and
stable command ids. A successful command returns canonical turn/attempt
records. Lost acknowledgements replay the receipt; they do not append a second
message. Whole-document `conversation.upsert`, `conversation.replace`, and
positional message mutations are outside the runtime contract.

## Verification

The smallest relevant gates are:

- `tests/test_frontend_attempt_stream_vite.py`
- `tests/test_frontend_conversation_surface_vite.py`
- `tests/test_frontend_markdown_escape_raw_html.py`
- `tests/test_turn_projection_segments.py`
- `tests/test_turn_projection_rev_adoption.py`
- `tests/test_turn_activity_timeline.py`
- `tests/test_turn_store_owner_migration.py`
- `tests/test_turn_projection_swarm_carryover.py`
- `tests/test_tool_round_sibling_discriminators.py`
- `tests/test_frontend_tool_rounds_render.py`
- `tests/test_tool_attention_contract.py`
- `tests/test_frontend_inspect_image_render.py`
- `tests/test_tool_browser_execution_presentation.py`
- `tests/test_tool_command_execution_presentation.py`
- `tests/test_tool_approval_presentation.py`
- `tests/test_tool_injection_presentation.py`
- `tests/test_frontend_peer_inject_row_layout.py`
- `tests/test_frontend_approval_card_render.py`
- `tests/test_approval_dialog_renders_risk.py`
- `tests/test_action_registry_nested_calls.py`
- `tests/test_frontend_cmd_interrupt_button.py`
- `tests/test_frontend_qr_render.py`
- `tests/test_frontend_tool_rounds_wire_parity.py`
- `tests/test_conversation_sync_v3.py`

When adding a renderable field, update the turn projection schema/normalizer,
the typed projection, and a behavioral renderer test. Do not add a second
fingerprint, repaint callback, or transport-specific fold.
