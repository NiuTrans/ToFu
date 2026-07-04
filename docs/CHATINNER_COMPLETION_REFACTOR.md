# chatInner completion-workflow consolidation

Owner directive (2026-07-03): migrate frontend `chatInner` update mechanisms to
backend-authoritative projection, then consolidate the too-many task-completion
mechanisms and fix the resulting bug class.

## Root cause (evidence-confirmed)

The task-completion workflow has ~17 backend + ~8 frontend mechanisms. They all
trace to ONE structural flaw:

1. **The frontend full-conversation PUT (`syncConversationToServer`, called at
   the end of `finishStream`) competes with the backend as a WRITER of
   `conversations.messages`.** To arbitrate the race the backend grew a
   terminal-CAS loop (`_MAX_TERMINAL_CAS=3`) + a "frontend-won" skip, and
   `finishStream` grew THREE skip-guards (queue-race, autopilot-inbound,
   server_offline).

2. **The settled bubble record is built FOUR separate times** — the DB dict
   (`_sync_result_to_conversation`), the `done` SSE event
   (`_finalize_and_emit_done`), the `state` snapshot (`chat_stream`), and the
   `/api/chat/poll` payload — kept in sync "by convention" (the
   `extract_task_meta` docstring literally warns 4 paths must match). The
   frontend keep-longer guards (`_snapshotLonger`) + poll-merge exist only to
   paper over their divergence.

3. **Emit-before-commit ordering** (non-autopilot path):
   `_finalize_and_emit_done` calls `append_event(task, done_evt)`
   (orchestrator.py:1014) BEFORE `persist_task_result` (orchestrator.py:1023).
   So the terminal event ships before the authoritative DB dict exists — the
   event a client receives is NOT the committed record. (The autopilot path
   already syncs first at orchestrator.py:917, proving the pattern works.)

## Target: single completion seam

- Backend `_sync_result_to_conversation` is the SOLE writer of settled
  `conversations.messages`.
- ONE builder produces the committed assistant dict; `done` / `state` / `/poll`
  all ship THAT EXACT dict (verbatim projection — closes the 4-way parity gap).
- Frontend `finishStream` is render-only: no full-conv PUT at that call site,
  no 3 skip-guards. Transient `streamBufs` render stays (legitimate in-flight).
- Startup ghost-reconcile moves to the backend.

## Phasing (reviewer-hardened, incremental — no flag-day)

### Phase 1 — close the parity gap + fix ordering (backend-only, additive)
- Hoist a SINGLE authoritative commit before `append_event(done_evt)` for ALL
  paths (unifies the pre-autopilot sync at :917 and the post sync at :1023).
- `_sync_result_to_conversation` RETURNS the dict actually committed (the
  written trailing-assistant, or the fresh row's tail on a genuine frontend-won
  skip, or `None` when there is nothing to write).
- `done_evt['committedMessage']` = that exact dict.
- **Invariant (S1-d):** builder reads the row actually committed (re-SELECT
  post-CAS via the returned tail); emit is ordered after the successful commit.
  Skip paths (freshness guard, `_inline_messages`, CAS-exhaustion) legitimately
  return `None` → no `committedMessage` is attached, and the frontend keeps its
  transient buffer (Phase-2 offline fallback).
- Golden-dict equality test + double-neuter.

### Phase 2 — finishStream render-only (surgical)
- Remove the full-conv PUT + 3 guards ONLY at the `finishStream` call site.
  KEEP `syncConversationToServer` the function (~39 other legit callers: edit,
  branch, regen, image-gen, folder, create).
- On terminal, project `done_evt.committedMessage` verbatim.
- **Invariant (S2-b):** retain the transient `streamBufs` render as the
  terminal fallback when NO committed dict arrived (server death mid-stream).

### Phase 3 — backend startup reconcile (gated behind Phase 1) — DONE
- NEW pure primitive `lib/conversations/reconcile.py::reconcile_conversation_messages`
  (buried-ghost sweep + tail delete/interrupt), ports the 3 JS classifiers.
- Wired into `recover_stale_tasks_on_startup` — persists the cleaned messages in
  the SAME commit that recovers the conv + stamps `settings._reconciledAt`.
- Frontend Case-D gated on `!conv._reconciledAt` → defers to the backend. The JS
  classifiers REMAIN as the fallback for convs the recovery loop didn't touch.
- Resurrect + auto-fire regressions are structurally impossible on the
  crash-recovery path (server-side persist in one commit; verdict is pure data).

PARKED (owned, sequenced): move `reconcile_conversation_messages` onto the
conversation GET/load path so EVERY conv (not just crash-recovered ones) is
reconciled server-side, then retire the JS classifiers entirely. CaseF's
`server_offline` verdict-clearing can move server-side in the same increment.

Each phase shipped with tests + double-neuter and a verification gate.

## Status (2026-07-03)

- **Phase 1 — DONE.** `task['_committedMsg']` stamped by `_sync_result_to_conversation`;
  hoisted commit-before-emit; `done_evt['committedMessage']` verbatim; frontend
  projects it. Tests: test_committed_message_parity.py, sse_dispatch #28/#29.
- **Phase 2 — DONE.** `finishStream` render-only: full-conv PUT + 3 skip-guards
  removed at that call site (function kept for its ~39 other callers). Offline
  buffer fallback retained. Test: test_frontend_finishstream_no_put.py.
- **Phase 3 — DONE.** `lib/conversations/reconcile.py` (pure primitive) wired into
  `recover_stale_tasks_on_startup` (persisted + `_reconciledAt` marker); frontend
  Case-D defers on the marker. JS classifiers kept as the fallback for convs the
  recovery loop doesn't touch. Tests: test_reconcile_conversation.py,
  test_startup_reconcile.py, test_frontend_reconcile_defer.py.
  - **PARKED (sequenced follow-up):** move reconcile onto the conversation GET
    path so EVERY conv (not just crash-recovered ones) is reconciled
    server-side, then retire the JS classifiers entirely.

