# RENDER_CONTRACT.md — the unified frontend-render specification

> **Status: Phase 1 (id-keyed reconcile) + Phase 2 (content version + single
> surgical repaint path) landed; Phase 3–4 pending.** This
> document is the alignment baseline for making chat rendering, backend sync,
> tool-round display, and auto-translation *traceable, concise, and robust*. It
> names the invariants that a stable frontend must obey and the phased migration
> that gets us there. See §5 for per-phase status.
>
> **Audience:** anyone touching `static/js/ui/*`, `lib/tasks_pkg/manager/_sync.py`,
> `lib/chat/persistence.py`, `lib/agent_core/events.py`, or the translate path.
>
> **Sibling docs (this one ties them together, does not replace them):**
> - [`EVENTS.md`](EVENTS.md) — the emit discipline (`build_event`/`EventType`) for the backend event vocabulary.
> - [`HEADLESS_API.md` §3.6.1](HEADLESS_API.md) — the wire vocabulary a client consumes.
> - [`CHATINNER_COMPLETION_REFACTOR.md`](CHATINNER_COMPLETION_REFACTOR.md) — the completed 3-phase work that unified the terminal-settle path onto a single `committedMessage`. This spec generalizes that success to the *whole* render pipeline.

---

## 0. The problem, in one sentence

The DOM is **not** currently a pure function of the message document. Three
subsystems (static render, streaming render, background repaint) each mutate the
same `#chatInner`, they decide *whether to repaint* by comparing **string
lengths** rather than content, and the persistence layer versions rows with a
monotonic `rev` but arbitrates concurrent writes on a **different** token
(`updated_at`). The result is the recurring bug family: **twin/collapsed
bubbles, "translation lands but the UI doesn't refresh", tool-round flicker,
and stuck "等待中…" placeholders.**

The evidence is in the code itself. `static/js/ui/chat_render.js::_msgFingerprint`
is a hand-maintained catalog of past bugs — each folded token carries a comment
that *is* a bug scar:

```js
// _translatedToolContent / _toolContentTranslating :
//   "the reported 'translation arrives but UI doesn't refresh' bug"
// swarmFp    : committed swarm panel mutated in place, invisible to sr.length
// _segTrFp   : narration-only translation would "diff as unchanged and never repaint"
```

Every new mutated-in-place field imposes a **hidden obligation**: "remember to
fold it into the fingerprint, or it silently fails to render." That obligation
is the root cause. This contract removes it.

---

## 1. The five invariants (normative)

These are written as MUST/NEVER rules. A change that violates one is a
regression regardless of whether a test currently covers it.

### Invariant 1 — DOM is a projection: `DOM = render(messages, rev)`

The rendered conversation MUST be a pure function of the authoritative message
document `(messages, rev)`. No subsystem may mutate `#chatInner` out of band.
Streaming, translation, swarm-panel updates, cost/file-change data — all land by
**mutating the message document first**, and the DOM re-derives. There is ONE
render function and ONE reducer; there are not "three owners of the DOM."

Corollary: a live stream is not a *separate* rendering mode. It is the same
message projected at a rapidly-advancing version. Finalizing a stream is a
version bump on an already-keyed node, NOT a "swap `#streaming-msg` → `msg-N` +
evict" dance.

### Invariant 2 — identity is a stable `id`; reconcile is id-keyed, NEVER index-keyed

Every message carries a stable identity (`_msgId`). The frontend MUST reconcile
the DOM as a **keyed list keyed on `_msgId`** (like a keyed virtual-DOM diff).
It MUST NEVER locate a row for update/removal by its array position
(`msg-${idx}`). Index-addressing under insertion/splice/lazy-window offset is
the documented root of twin bubbles.

- Client-minted `tmp_<uuid>` ids are provisional; they are reconciled to the
  server UUID exactly once, and the keyed node is re-tagged in place (no
  duplicate, no strand).
- `_assistantMsgId → _msgId` uniqueness is an invariant (see Invariant 4): one
  logical assistant turn ⇒ exactly one `_msgId` ⇒ at most one DOM node.

### Invariant 3 — repaint is driven by a per-message content VERSION, never by length

Whether a row repaints MUST be decided by comparing a **per-message content
version** (a content hash, or a monotonic per-message `v`), NOT by
`String(field).length` and NOT by a hand-folded catalog of sub-tokens.

The version MUST cover the *entire* renderable state of the message, including
every field that today is hand-folded or deliberately omitted:

| Field folded/omitted today | How the version covers it |
|---|---|
| `content` / `thinking` / `error` **length** | hashed by content, so equal-length edits repaint |
| `toolRounds[].{_translatedToolContent,_translatedQuestion,_toolContentTranslating}` (`xlFp`) | part of the message's renderable state → in the version |
| `toolRounds[]._swarm*` / `_swarmAgents[]` (`swarmFp`) | ditto |
| `segments[].translatedText` (`_segTrFp`) | ditto |
| `toolRounds[].{compactionLayer,compactedToChars}` | ditto |
| `_autopilotRunId` / `_isAutopilotSummary` / `_pendingQueued` | ditto |
| async cost / `modifiedFiles` / file-change data (**deliberately omitted**, needs `_bgRefreshChat`) | ditto — no separate background-repaint path |

When this holds, **`_msgFingerprint` and the entire `_bgRefreshChat` path are
retired**: there is one repaint trigger, and no field can be "forgotten."

### Invariant 4 — `rev` is the SOLE concurrency token; message-id uniqueness is a schema invariant

`conversations.rev` is a monotonic integer bumped only on a genuine `messages`
change (the `conversations_rev_bump_trg` trigger). It MUST be the **only**
optimistic-concurrency token used by **every** writer of `conversations.messages`:

- chat terminal sync (`_sync_result_to_conversation`)
- chat partial/checkpoint sync (`_sync_partial_to_conversation`)
- queued-user append (`append_pending_user_msg`)
- auto-translate + segment backfill
- any messages-as-rows dual-write

Today these CAS on `updated_at` (and segment-backfill already uses `rev`) — two
regimes that can disagree (two writers with the same-millisecond `updated_at`
both pass CAS). The contract: **CAS on `rev`, everywhere.** The DB-visible
version and the write guard become the same token.

Message identity uniqueness (`_assistantMsgId → _msgId` maps to at most one row)
MUST be enforced structurally, not by a single test. A follow-up/autopilot task
MUST NOT inherit a parent's `assistantMsgId` (the 16× collision class that
rendered Agent turns invisible).

### Invariant 5 — one authoritative producer per settled fact; the client folds, never re-derives

A terminal/authoritative fact MUST be produced in exactly ONE place and shipped
verbatim; the client projects it and never reconstructs it from stream/task
state:

- The settled assistant message is built once and shipped verbatim as
  `done.committedMessage` (**already true** — `CHATINNER_COMPLETION_REFACTOR.md`
  Phases 1–3). `state`, `done`, `/api/chat/poll`, and cold replay all ship *that
  exact dict*.
- "This autopilot run is over" is the single `autopilot_run_concluded` fact —
  the client never infers run-end from stream/task state (**already true**).
- Run-liveness / presence status words are fully formed server-side (**already
  true**).

This invariant is the general form of what the completion refactor proved works.
New authoritative facts follow the same shape: one producer, verbatim wire,
client folds.

---

## 2. The message document schema

The authoritative unit is the **conversation document**: `{ id, rev, messages[] }`.
`rev` is the document version (Invariant 4). Each entry in `messages[]` is a
message object with the fields below. This is the field list a `render(messages)`
projection is allowed to read; anything not here is not renderable state.

### 2.1 Identity & versioning fields

| Field | Meaning | Rules |
|---|---|---|
| `_msgId` | Stable message identity | Server UUID (via `_assign_message_ids`) or provisional `tmp_<uuid>`; reconciled once. THE reconcile key (Invariant 2). |
| `_assistantMsgId` | Client-minted id an assistant turn adopts as its `_msgId` so the live bubble == committed row | Adopted by `_new_assistant_slot`; slot located id-first via `find_message_by_id` (never a positional guess). MUST be unique per turn (Invariant 4). |
| `_v` *(proposed)* | Per-message content version (hash or counter) | Covers all renderable fields (Invariant 3). Replaces `_msgFingerprint`. |
| `role` | `user` / `assistant` / `tool` / … | — |

### 2.2 Renderable content fields (all covered by `_v`)

| Field | Meaning |
|---|---|
| `content` | Assistant/user text (the deliverable) |
| `thinking` | Reasoning text |
| `error` | Error envelope `{kind, message}` or string |
| `finishReason` | `stop` / `error` / `aborted` / `max_turns` |
| `toolRounds[]` | Tool-call rounds: `{roundNum, toolName, toolCallId, query, results[], status, content, isError, compactionLayer?, compactedToChars?, _translatedToolContent?, _translatedQuestion?, _toolContentTranslating?, _swarm?, _swarmAgents?}` |
| `segments[]` | Interleaved narration/deliverable timeline: `{type, llmRound, deliverable?, translatedText?}` |
| translation fields | `translatedContent` (deliverable) + per-round / per-segment translations |
| `images[]` / `pdfTexts[]` / `_igResult(s)` / `_igError` | Media / image-gen |
| `modifiedFiles` / cost | Async provenance (today omitted from fingerprint → the `_bgRefreshChat` scar) |
| `_pendingQueued` | Cross-device queued user row awaiting dispatch reconcile |
| `_autopilotRunId` / `_isAutopilotSummary` | Autopilot fold grouping |

### 2.3 The identity-alignment rule (end to end)

```
client mints _assistantMsgId  ──▶  _new_assistant_slot adopts it as slot._msgId
        │                                     │
        │ (live bubble keyed on it)           │ (committed row owns same id)
        ▼                                     ▼
   streaming node  ═══ same _msgId ═══▶  done.committedMessage  ═══▶  render()
```

The live bubble and the committed row share one `_msgId`, so finalization is a
version bump on a keyed node (Invariant 1), not a node swap.

---

## 3. The event vocabulary (already declared — this section pins the render-facing rules)

The vocabulary is **already** a single, versioned, drift-guarded registry:
`lib/agent_core/events.py` (`EVENT_CONTRACT_VERSION`, ~41 render-facing
`EventSpec`s, machine-discoverable via `GET /api/v1/capabilities`). Do NOT
duplicate the registry here — [`EVENTS.md`](EVENTS.md) is the emit discipline.
This section states the *render-contract* rules layered on top.

### 3.1 The reducer rule

The frontend MUST treat events as a **reducer over the message document**:
`messages = apply(messages, event)`. `apply` is the same logic for live stream,
warm resume (Last-Event-ID slice), cold replay (folded `task_events`), and
`/poll`. There are not four hand-aligned code paths producing `state`/`done` —
there is one fold function they all call. (This generalizes the single-builder
win from the completion refactor to the whole event stream.)

### 3.2 Render-facing event groups (from the registry)

| Group | Events | Reducer effect |
|---|---|---|
| Lifecycle | `state`, `phase`, `done`, `error`, `retry_reset` | `state` = full rebuild of the live message; `done` projects `committedMessage` verbatim; `retry_reset` clears content+thinking+rounds; `error` sets error envelope |
| Content | `delta`, `delta_reset` | append content/thinking; `delta_reset` clears prose (keeps rounds) |
| Tool | `tool_start`, `tool_progress`, `tool_result`, `tool_complete`, `tool_compacted` | open/fill/close a tool-round entry on the message |
| Context | `round_usage`, `round_committed`, `messages_snapshot`, `compaction(_done)`, `memory_prefetch`, `preferences_applied`, `preference_learned`, `related_conversations`, `workspace_root_added` | provenance chips/segments on the message |

*(`project_external_edit` stays on the wire as a pure file-history audit record but is deliberately NOT rendered — the drift toast advertised an undo the UI had no path for. Its consumer is the file-history timeline, not the user.)*
| Interaction | `human_guidance_request`, `write_approval_request`, `approval_required`, `stdin_request`, `stdin_resolved` | render a pending-input affordance; `requires_response=True` |
| Endpoint / Swarm / Autopilot / Presence / Steer | `endpoint_*`, `swarm_*`, `autopilot_*`, `presence`, `peer_inbox_inject`, `user_steer_inject` | fold into the message's panels / timeline chips |
| Transport | `ping`, `sse_timeout` | not rendered (`TRANSPORT_TYPES`) |

### 3.3 Two consistency defects to fix in the vocabulary (Phase 3)

Registered but not yet fully consistent — fix under the migration, guard with
`tests/test_event_registry.py`:

1. **Round-key drift.** Tool events use `roundNum`; `phase`/`round_usage`/
   `delta_reset` use `round`. Pick ONE (`roundNum`), keep the other as an
   accepted alias for one contract version, then remove.
2. **No explicit round boundary.** Round grouping is inferred from `roundNum`
   increments + `delta_reset`. Add `round_start` / `round_end` so the reducer
   groups tool rounds deterministically instead of by inference (root of
   tool-round flicker on cold reconnect).

*(These are additive; they do not bump `EVENT_CONTRACT_VERSION` unless a field
is renamed/retyped.)*

---

## 4. Persistence & concurrency contract

### 4.1 Writers of `conversations.messages`

| Writer | Location | CAS token today | Target |
|---|---|---|---|
| terminal sync | `_sync_result_to_conversation` | `updated_at` (+ 3× retry, "don't shrink") | **`rev`** |
| partial sync | `_sync_partial_to_conversation` | `updated_at` | **`rev`** |
| queued-user append | `append_pending_user_msg` | `updated_at` | **`rev`** |
| auto-translate | `auto_translate/_assistant.py` + `translate/commit.py` | `updated_at` | **`rev`** |
| segment backfill | `translate/segment_backfill.py` | `rev` (already) | `rev` |
| messages-as-rows dual-write | dual-write path | uncoordinated | **`rev`** or explicitly out of the messages-document CAS domain |

### 4.2 Rules

1. Every writer reads `(messages, rev)`, computes the new messages, and CAS-writes
   guarded on the `rev` it read. On CAS miss: re-read, re-graft, retry. The
   `rev` read-back (`persist_conv_messages` returning the post-write rev) is the
   authoritative version returned to callers and pushed on `conv_changed`.
2. Auto-translate stops being an out-of-band mutation. It is a normal versioned
   write: it bumps `rev`, and the client refetches by the standard rev-gate. No
   second CAS regime.
3. `conv_changed` push frames carry the real `rev` (already true for the
   send-path per the cross-device visibility work). `rev=None` remains
   *metadata-only* (title/folder/activeTaskId → sidebar only, no body refetch).

---

## 5. Migration plan (phased, incremental — no flag-day)

Each phase is independently shippable and independently revertible, gated by a
named test + a NEUTER. Phases 1–2 are pure frontend / low blast-radius. Phases
3–4 touch the wire and DB contracts and **require owner sign-off + migration
tests first** (per project convention).

| Phase | Scope | Change | First test + NEUTER | Risk |
|---|---|---|---|---|
| **0** | doc | This file. | — | none |
| **1 ✅ DONE** | frontend | Surgical-diff reconcile in `renderChat` now matches by stable `_msgId` (`_reconcileFindEl`), reusing a drifted node (re-stamping its positional `id`/index + reordering) instead of destroying+rebuilding it; `id="msg-N"` handle preserved for index-addressing consumers. | `test_frontend_id_keyed_reconcile.py`: a mid-history insert REUSES the shifted nodes (same node object + preserved per-node DOM state), no twin/collapse, order correct; NEUTER = force positional `getElementById('msg-'+i)` → reused-node/state-preservation checks FAIL. | med |
| **2a ✅ DONE** | frontend | `_msgFingerprint` (aliased `_msgContentVersion`) now HASHES `content`/`thinking` (FNV-1a) instead of `.length`, and folds the async-provenance fields it omitted (cost values, `modifiedFileList` PATHS, `_artifacts`) so the one surgical trigger repaints on them (Invariant 3, L1/L2). | `test_frontend_msg_content_version.py`: equal-length content/thinking edit moves the version; cost/modifiedFileList/artifacts landing moves it; stable when unchanged; NEUTER = revert content folds to `.length` → equal-length edits collide (no repaint). | med |
| **2b ✅ DONE** | frontend | L10 decoupled: `_msgElIndex(el)` resolves the live index at click time so action-button `onclick`s no longer bake the array index (output is index-independent). Async cost/file-change written back onto `msg` (`msg.cost` / `_fcResolvedFp`) + `_compactions` folded, so the version covers every field the output-diff did. `_bgRefreshChat` is now a thin SHIM onto the ONE surgical `renderChat(conv,false)` path (clears Guard-2 fp for mid-history lands + sets `_bgRepaint` so the anchor is a fixed step); its output-diff body is deleted — no second DOM-owner. | `test_frontend_action_index_decoupled.py` (index-independence + NEUTER re-bakes); `test_frontend_async_writeback_version.py` (cost/fc write-back moves version + NEUTER); `test_frontend_bg_refresh_scroll.py` migrated to the unified path (anchor + id-keyed reuse, double-NEUTER). | med |
| **3** | wire | one `apply()` reducer for live/warm/cold/poll; unify `roundNum`; add `round_start/round_end` (Invariants 1, 5 + §3.3). | byte-identical cold vs live projection; `test_event_registry` round-key; NEUTER = divergent fold path → mismatch. | **high — owner sign-off** |
| **4** | DB | all writers CAS on `rev`; auto-translate versioned; `_assistantMsgId→_msgId` uniqueness a schema invariant (Invariant 4). | migration test: two same-`updated_at` writers → exactly one wins on `rev`; follow-up id-collision structurally impossible; NEUTER = CAS on `updated_at` → both pass. | **high — owner sign-off** |

Ordering rationale: 1→2 kill the majority of *visible* instability (twin bubbles,
translation-not-refreshing) with no contract risk, so we can ship relief before
touching the wire/DB. 3 makes cold/warm/live identical (kills tool-round
flicker). 4 closes the last concurrency gap.

---

## 6. Latent-bug register (log-only — DO NOT fix in this doc; open a ticket)

Found while writing this spec. Recorded here for traceability; each needs its
own board epic and its own tests. **None is fixed by this document.**

| # | Symptom | Locus | Note |
|---|---|---|---|
| L1 | ~~Equal-length content edit never repaints~~ | `chat_render.js::_msgFingerprint` | ✅ FIXED (Phase 2a `f83a7dd`): content/thinking hashed via FNV-1a. |
| L2 | ~~New mutated-in-place field silently fails to render until hand-folded~~ | `_msgFingerprint` fold catalog + side caches | ✅ FIXED (Phase 2a + 2b): version folds cost/modifiedFileList/_artifacts/_compactions AND async cost/file-change are written back onto `msg` (`msg.cost`/`_fcResolvedFp`), so the lazy side-cache path is covered too. |
| L3 | Two writers with same-millisecond `updated_at` both pass CAS | `_sync_*` + translate | Subsumed by Phase 4. |
| L4 | `rev` read-back is a non-atomic separate SELECT, fail-open to `None` | `persist_conv_messages` | Acceptable degradation today; revisit under Phase 4. |
| L5 | `dual_write_conv` is a second uncoordinated store, not rev-gated | dual-write path | Decide: bring under `rev` CAS or declare out-of-domain (Phase 4 §4.1). |
| L6 | `_convRenderFingerprint` samples only the LAST message → a mid-history mutation at unchanged tail can be skipped | `chat_render.js` Guard 2 | MITIGATED (Phase 2b): the `_bgRefreshChat` shim clears the Guard-2 fp before delegating, so background repaints of mid-history rows are never skipped. Guard 2 itself still last-msg-only for the non-background sync path; full fix folds into Phase 3's reducer. |
| L7 | Round-key drift (`roundNum` vs `round`) across event families | `events.py` specs | Phase 3 §3.3.1. |
| L8 | No `round_start`/`round_end`; round boundaries inferred | `events.py` + reducer | Phase 3 §3.3.2; root of tool-round flicker on cold reconnect. |
| L9 | `test_frontend_autopilot_fold.py` FAILS on HEAD — asserts `_applyAutopilotRunFolds` *creates* folds, but the 2026-07-07 flatten directive made it *unwrap* them (see `test_frontend_autopilot_flat_render.py`) | stale test vs shipped behavior | Pre-existing (verified against committed HEAD), unrelated to Phase 1. Delete/rewrite the stale test to the flatten contract. |
| L10 | ~~`renderMessage` bakes the array index into action-button `onclick`s~~ | `chat_render.js::renderMessage` action-bar | ✅ FIXED (`473181a`): `_msgElIndex(el)` resolves the live index from the nearest `.message`'s `data-msg-id` at click time; all 16 onclicks + the fc-undo button route through it. Was ALSO a shipped correctness bug (a reused drifted node fired `deleteTurn` on the wrong turn). REMAINING sibling-owned surface: `translation_indicator.js:69` (board pt_39b79cc4). Stale-harness note: `test_frontend_swarm_realtime_fingerprint.py` / `test_frontend_segtranslation_fingerprint.py` eval `chat_render.js` WITHOUT `translation_model.js` → `translationFingerprint is not defined` (same class as L9; still open — fix by loading the dep or stubbing the global). |

---

## 7. What is already done (do not redo)

Anchoring the spec to reality so no phase re-litigates finished work:

- **Event vocabulary is already a declared, versioned, drift-guarded registry**
  (`events.py`, `EVENTS.md`, `/api/v1/capabilities`). Phase 3 *consolidates the
  reducer*; it does not create the registry.
- **Terminal settle is already single-producer + verbatim** (`committedMessage`,
  `CHATINNER_COMPLETION_REFACTOR.md` Phases 1–3). Invariant 5 generalizes this.
- **Autopilot run-end is already a single authoritative fact**
  (`autopilot_run_concluded`); the client already never infers it.
- **Cross-device send visibility already emits real `rev`** on the send path and
  lands a `_pendingQueued` row reconciled idempotently.
- **Startup ghost-reconcile is already server-side** (`reconcile.py` wired into
  `recover_stale_tasks_on_startup`), with JS classifiers kept only as the
  fallback for untouched convs.

These are the proof that the target architecture works in the small; this spec
is the plan to make it hold in the large.
