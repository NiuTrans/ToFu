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
                    ▼
       frontend/src/runtime/app-runtime.js (generated)

frontend/src/styles/{application,settings} + manifests
                    │ compose_frontend_styles.mjs
                    ▼
       static/{styles,settings}.css (generated)

frontend/src/i18n/locales/{zh,en}.json
                    │ gen_i18n_contract.mjs
                    ▼
       frontend/src/i18n/contract.generated.ts

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
- `window.Api` is an endpoint-name registry over the typed transport. It does
  not implement `fetch`, affinity, request IDs, or errors.
- Lazy features use the private runtime service registry. Core owners are
  static imports and are not republished as mutable globals.
- Every listener, timer, EventSource, observer, and subscription has a declared
  scope and disposal path.
- Product copy uses the generated `I18nKey`/`Translator` contract. Locale JSON
  files are the only key and placeholder authority; feature-local string-key
  translator interfaces are not new extension points.
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

## State owners

| State | Owner |
|---|---|
| HTTP request, timeout, abort, typed API error | `frontend/src/api/transport.ts` |
| Idempotency-key creation | `frontend/src/api/transport.ts` |
| Conversation cursor, SSE and recovery | `frontend/src/core/conversation-sync.ts` |
| Normalized Turn/attempt/phase reduction | `frontend/src/conversation/domain/turn-store.ts` |
| Ordered Turn reads | `frontend/src/conversation/application/conversation-read-model.ts` |
| Turn/block view model | `frontend/src/conversation/presentation/conversation-view-model.ts` |
| Catalog-shell metadata reconciliation | `frontend/src/core/turn-projection.ts` |
| Turn commands | `frontend/src/core/turn-command.ts` |
| Turn runtime orchestration | `frontend/src/core/turn-runtime.ts` |
| Conversation DOM reconciliation | `frontend/src/conversation/ui/conversation-surface.ts` |
| Presentation-only state and scheduling | `frontend/src/conversation/application/conversation-surface-controller.ts` |
| Scroll anchor, follow suspension and DOM window | `ConversationSurface` viewport port |
| Conversation diagnostics projection | `frontend/src/features/diagnostics.ts` over `ConversationTurnRead` + active Surface |
| Catalog shells and settings persistence | `runtime/sections/core/conversation_catalog.js`; composer capture adapter in `runtime/sections/main.js` |
| Connection badge health | `frontend/src/core/connection-health.ts` |
| Send startup cancellation | `frontend/src/core/send-startup.ts` |
| Composer submission echo | `frontend/src/conversation/application/optimistic-user-turn.ts` on the transient overlay |
| Resource cleanup | `frontend/src/lifecycle.ts` |
| Visible error identity/presentation | `frontend/src/api/errors.ts`, turn presentation |

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
| Offline cache | Remains the storage authority | IndexedDB stores catalog/settings metadata only, never a transcript copy |

Feature adapters such as artifacts, Human Guidance, cost detail and timer
recovery may keep bounded presentation caches. They attach to stable Turn or
feature IDs, never mutate a Turn projection, and declare a disposal or size
bound. A new feature cannot add a conversation-level mirror merely because a
legacy endpoint happens to return message-shaped data.

## Conversation hydration

One v3 snapshot contains settings, revision, turns, attempts, cursor, and
heartbeat policy. `turn-runtime.ts` applies settings at the snapshot boundary,
then dispatches TurnState. The pure selector reads that state directly and the
controller commits it to the keyed Surface. `turn-projection.ts` updates only
catalog/lifecycle metadata; it cannot materialize a parallel transcript.
Initial catalog boot is metadata-only: inactive catalog shells issue zero Turn
snapshot requests, so startup network and memory concurrency do not grow with
sidebar size. Opening the selected conversation owns its one on-demand
snapshot; it does not issue a second settings request and does not fall back to
an archived message body or IndexedDB transcript.

Push and BroadcastChannel notifications only invalidate the coordinator. The
ordered conversation stream or an authoritative reset snapshot is the only
projection writer.

Diagnostics are a read-only projection of `ConversationTurnRead`, connection
metadata, and the active Surface dataset. Collecting diagnostics never issues a
conversation request, parses a message array, or creates a recovery state path.
Request Inspector keeps its pre-request message/schema snapshot for exact
historical reconstruction and joins the matching bounded
`tool_wire_projection` event for provider-bound truth. Round rows therefore
show the final wire tool count, while detail exposes ordered final names,
schema-token estimate, an opaque exact-schema fingerprint, discovery backend,
explicit budget, and budget omissions; it never presents the larger assembly
snapshot as what the model received.

## Conversation settings plane

Conversation settings are metadata, not transcript content. The active
composer captures its visible values into the active metadata shell and hands
that shell to one debounced persistence seam. Persistence is independent of
whether the conversation has any Turns:

- `_localOnly` shells update the IndexedDB metadata cache only. The first
  accepted Turn creates the durable conversation at the backend authority.
- Server-owned shells use only
  `PATCH /api/v1/conversations/:id/settings`; the retired chat tool-state route
  is not a browser write path.
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
- Long histories stay complete in TurnStore while the Surface keeps a bounded
  DOM window. Earlier/later controls shift that window by stable Turn identity;
  they never truncate authority, refetch a transcript, or create a second
  render path.
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
