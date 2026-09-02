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
label cannot survive reload.

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
- A reasoning block's active/complete state derives from the turn lifecycle,
  never from `segment.terminal` (that flag marks the terminal round's
  accumulator so the `thinking` channel projection can find it; inter-round
  reasoning segments are already closed). A settled turn renders every
  reasoning block complete and collapsed; on a live turn only the trailing
  reasoning block is active, and a block whose stream closes collapses
  itself. Reasoning is a slim muted disclosure, not a stack of cards.
- Tool rounds, segments, usage, translations, modified files, orchestration
  metadata, and error envelopes live inside the turn projection.
- A failed Turn renders the complete typed settlement error from the Turn
  authority, never the size-bounded activity-timeline copy. Its disclosure is
  expanded by default, remains user-collapsible, and preserves that local open
  state across a semantic repaint.
- Every tool round with a `toolCallId` owns a `tool_use` segment. The
  projection authority (`projection_with_stable_segments`) repairs a stale
  checkpoint-era timeline by re-assembling when a round is missing — without
  it, a round appended after the last checkpoint never renders, and a
  human-wait round (ask_human / approval / stdin) that blocks the executor
  would never show its interactive card. The check is one-directional:
  segments may be a superset of `toolRounds` (compaction folds cold rounds
  out of the round list while their render blocks stay).
- `activityTimeline` entries render as compact diagnostic rows anchored
  inline where they happened, never as one consolidated tail block and never
  reconstructed from transient phase state or another transport. A row with a
  matching `toolCallId` sits directly under that tool's inline block (the
  block itself owns the call/result; the row adds the failure reason/detail);
  every other row rides its 0-based `llmRound` anchor — after the last block
  of the same round, else after the last block of an earlier round, else the
  turn start for pre-request diagnostics such as preflight schema isolation.
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
  surfaces never duplicate a fact.
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
  masquerades as a reconnect problem.

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
- `tests/test_turn_projection_segments.py`
- `tests/test_turn_projection_rev_adoption.py`
- `tests/test_turn_activity_timeline.py`
- `tests/test_turn_store_owner_migration.py`
- `tests/test_conversation_sync_v3.py`

When adding a renderable field, update the turn projection schema/normalizer,
the typed projection, and a behavioral renderer test. Do not add a second
fingerprint, repaint callback, or transport-specific fold.
