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
