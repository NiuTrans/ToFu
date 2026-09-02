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

UI intent
  -> application command service
  -> generated API client
  -> authoritative acknowledgement / ordered event

catalog/settings metadata
  -> retained catalog adapter
  -> metadata-only shell + IndexedDB metadata cache
```

## State scopes

- Durable document facts: server-owned turns, attempts, settlement, content
  blocks, ordering, revisions and action capabilities.
- Live authority: backend-authored attempt and phase changes reduced into the
  same TurnStore; connection health is lifecycle-scoped browser state. There is
  no conversation-level phase or task mirror.
- Local interaction facts: focus, selection, scroll anchor, translation
  activity, Human Guidance feedback, artifact binding, block expansion and
  inline turn-edit sessions, always keyed by stable identity and never copied
  into the durable model. An edit session owns a persistent host element that
  `reconcileTurnInlineEditors()` re-attaches after each surface commit, so an
  open draft survives authoritative repaints without touching TurnStore.

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

## Verification ladder

1. Pure reducer/selector/component owner test.
2. Generated conversation contract check.
3. Store-to-surface boundary test in the real Vite graph.
4. Browser journeys for switch-during-stream, reconnect, retry, translation,
   branches, long history and scroll anchoring.
5. `make architecture-check`, `npm run check:frontend`, and the frontend
   source/bundle budgets.
