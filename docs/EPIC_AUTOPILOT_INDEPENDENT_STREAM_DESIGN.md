# EPIC: Autopilot VU as an Independent Stream

Board epic: `pt_8dc030176bad450b`
Status: **DESIGN + MIGRATION-TEST PLAN complete; implementation HUMAN-GATED** (touches
the exactly-once baton delivery contract the owner personally owns).
Related, already-shipped: the *visible* "parent finish bar incomplete" bug is
root-fixed independently of this epic by projecting `parentMessage` on
`autopilot_vu_start` + a task-settled-fields fallback (commits `589cfaa` /
`b221921` / `9ce7d93`). **This epic is NOT needed to close that bug** — it is the
deeper structural cleanup ("an agent bubble is just an agent bubble").

---

## 1. What the epic asks for

> Stop the VU riding the parent SSE via `_VUEventForwarder` + the
> `_autopilot_deciding` latch; emit the parent `done` at parent-turn end and run
> the VU as its own task/connection, with the follow-up baton delivered on a
> channel independent of `done`. Preserve exactly-once handoff (poll fallback +
> SSE) — must re-verify `test_autopilot_poll_handoff.py`'s 4 race guards.

## 2. Current mechanism (code-verified)

The autopilot turn is **synchronous inside the parent's finalize**:

| Step | Site | Fact |
|---|---|---|
| Parent turn ends, done event built | `_finalize.py:834` `done_evt = build_event(DONE)` | finish fields settled on `task` (finishReason L621 / usage L622,917 / model L847 / apiRounds L925) |
| Pre-emit conv sync | `_finalize.py:1029` `_sync_result_to_conversation` | stamps `task['_committedMsg']` |
| **Decision latch ON** | `_finalize.py:1040` `task['_autopilot_deciding']=True` | gates the SSE generator's `_task_terminal()` so it will NOT synthesize a premature done |
| **VU runs SYNCHRONOUSLY** | `autopilot.py:949` `_run_single_turn(sub_task)` | the whole VU LLM turn (12–52s) blocks here |
| VU events ride parent SSE | `autopilot.py:922` `sub_task['events']=_VUEventForwarder(task, vu_msg_id)` | inner deltas wrapped as `autopilot_vu_event`, routed into the VU bubble created by `autopilot_vu_start` |
| Baton computed | `autopilot.py` returns `{next_task_id, vu_msg}` | successor task ALREADY spawned by `_start_followup_task` |
| Baton attached to done | `_finalize.py:1044-1046` `done_evt['autopilotNextTaskId']=…` | + stashed on `task['_autopilot_followup']` for the poll path |
| Late done emitted | `_finalize.py` `append_event(done_evt)` AFTER the hook | this is why `done` is "withheld" until the VU finishes |

**Why `done` is withheld is not arbitrary:** `done` must *carry the baton*, and
the baton can only be computed *after* the VU has run and decided
(continue-vs-`TASK_DONE`). The withhold is a direct consequence of running the
VU inline before emitting done.

**What is ALREADY independent:** the *successor* follow-up task. The frontend
`_attachAutopilotFollowup` (`main_send_pipeline.js:843`) opens a **fresh**
`connectToTask(convId, nextTaskId)` — the follow-up streams on its own SSE. Only
two things ride the parent stream today:
1. the **VU bubble's live streaming** (via `_VUEventForwarder`), and
2. the **baton delivery timing** (on the withheld `done`).

## 3. Why the two coupled parts are irreducible without a new channel

Releasing `done` at parent-turn end (before the VU runs) has a forced cascade:

1. If parent `done` fires early, the parent SSE **closes** → the VU bubble has no
   stream to forward onto → `_VUEventForwarder` is dead → the VU **must** become
   its own attachable task with its own SSE/push channel.
2. If the VU is its own task, the baton (which conceptually is "after this
   parent turn, a VU turn then a follow-up turn happen") can no longer ride the
   parent `done` — it needs an **independent delivery channel** that STILL
   satisfies exactly-once across BOTH transports (SSE + `/api/v1/chat/poll`
   fallback) AND the cold-replay/late-connect synthesis path.

So "emit done early" ⟺ "VU independent" ⟺ "new baton channel" — you cannot do one
without all three. This is why it is a contract change, not a local edit.

## 4. Target design — REUSE the existing supersede index; add NO new baton

The purest mechanism removes the corner cases rather than covering them. The
whole hand-carried baton (`autopilotNextTaskId`/`autopilotVuMessage` on `done` +
its poll mirror + the cold-replay synthesis stamp) exists for ONE reason: the VU
sub-task deliberately runs with `convId=''` "to stay out of the latest-task
registry" (`autopilot.py`). Because the VU (and today the follow-up) are invisible
to the server's conv→latest-task index, the frontend has no server-authoritative
way to discover "what task is live for this conv," so the successor id must be
hand-delivered on the terminal event of the turn before it.

**But that index already exists and is already the right primitive:**
`_record_latest_task(conv_id, task_id)` / `_latest_task_for_conv(conv_id)`
(`lib/tasks_pkg/manager/_state.py`) is a process-wide, **cross-replica**
(Epic C §4.3, mirrored into `runtime_state_store`) pointer to a conversation's
newest task. `routes/conversations.py:_conv_has_live_task` already reduces exactly
`_latest_task_for_conv(conv) is pending/running` to answer "is this conv live?".

So the target is simply: **STOP opting out.**

- Each turn of an autopilot chain (parent → VU → follow-up) is a **normal task
  registered under the REAL `convId`** — the VU no longer runs with `convId=''`
  and no longer forwards onto the parent stream. `_record_latest_task` fires for
  it exactly as for any user-initiated task.
- The frontend gets **ONE transport-agnostic reducer** (it mostly has it already
  via `connectToTask` + the `_conv_has_live_task` GET field): after any turn's
  `done`, if `conv.latestTaskId` (server-authoritative) names a *different*
  pending/running task, attach to it. That single rule covers parent→VU,
  VU→follow-up, AND VU-emitted-`TASK_DONE` (no newer task ⇒ nothing to attach,
  chain ends) — with NO enum, NO per-transition state, NO `vu_msg` on the wire.
- The VU message itself is already persisted to `conversations.messages`
  (`_isVirtualUser:true`) before its task starts, so the frontend loads it from
  the authoritative conv record on attach — it never needed to ride a baton.
- **Parent `done` fires immediately** at parent-turn end. `_autopilot_deciding`
  and the `_task_terminal` withhold branch are DELETED: there is nothing left to
  wait for, because the successor is discovered from the index, not stamped onto
  this event.

Net deletions: `_VUEventForwarder`, `_autopilot_deciding` + its `_task_terminal`
gate, `autopilotNextTaskId`/`autopilotVuMessage` on `done` + the poll mirror +
`_apply_autopilot_baton` cold-replay synthesis, and the `convId=''` opt-out.
Net additions: register the VU/follow-up under the real conv; one "attach to the
conv's newer live task after done" rule on the client. This is strictly LESS
code, and an autopilot run becomes a plain sequence of independent agent bubbles —
"an agent bubble is just an agent bubble."

### 4.1 The load-bearing HAPPENS-BEFORE invariant (this replaces the withhold)

Removing the withhold reintroduces the exact race it was defending against unless
one ordering is guaranteed. The old mechanism used "hold `done` until the baton
exists" to ensure the successor was already spawned before the client could see
the turn end. The new mechanism keeps the SAME safety with an ordering guarantee
instead of a hold:

> **INVARIANT (HB-1):** the backend MUST advance the supersede index to the
> successor — `_record_latest_task(convId, vuTaskId)` (and the VU task must be
> registered `pending/running` in the task registry) — **strictly before** the
> parent turn's `done` event is appended/emitted. i.e.
> `_record_latest_task(convId, vuTaskId)` **happens-before** `append_event(parent_done)`.

Why this is exactly-once WITHOUT a withhold or a re-poll: at the instant the
client observes the parent `done` (on SSE *or* poll *or* cold reload), a read of
`_latest_task_for_conv(convId)` is guaranteed to already return `vuTaskId`
(≠ the parent task id) because the write preceded the event the client is
reacting to. So the client's single reducer — "on done, if the conv's latest task
is a different pending/running task, attach to it" — can NEVER observe the empty
window `[parent done emitted] → [client reads index, sees only parent, declares
idle] → [VU registered]`. That window is precisely the "autopilot bubble suddenly
disappears" symptom; HB-1 closes it by construction.

The dangerous ordering that MUST NOT ship (the mirror of the visible bug):
```
  append_event(parent_done)          # client sees end-of-turn
  ...client reads index → only parent → finalizes, will NOT re-query...
  _record_latest_task(conv, vuTask)  # TOO LATE — VU bubble stranded
```
The correct ordering (HB-1):
```
  create VU task under REAL convId (status=pending)  # registry has it
  _record_latest_task(conv, vuTask)                  # index points at successor
  append_event(parent_done)                          # only NOW end the turn
```

Consequence for the cutover: the VU task must be **created + index-advanced
synchronously in the parent's finalize, before `append_event(done)`** — the VU's
LLM turn then runs on the VU task's own thread/stream, but its *registration* is
ordered before the parent done. This preserves the one useful thing the
synchronous-inline design gave us (successor exists before turn-end is visible)
while dropping everything else (event forwarding, the deciding latch, the hand-
carried baton). It is a strictly weaker, cheaper guarantee than the full withhold:
we no longer block `done` for the VU's 12–52s LLM call — only for the O(1) task
registration + index write.

## 5. Exactly-once invariants that MUST survive (charter Pillar-#6 + poll-handoff)

The migration is only acceptable if ALL of these hold. Note the mechanism change
makes several *trivially* true rather than separately-guarded — the point of the
simplification:

1. **A newer task is never stranded.** It is the conv's `_latest_task_for_conv`;
   the frontend attaches by reading that index — the SAME signal on SSE, poll,
   and cold reload, so there is no transport-specific baton to drop. (supersedes
   `test_baton_surfaced_when_followup_spawned`)
2. **A client attaching mid-chain lands on the live turn, never idle.** The index
   points at the running VU/follow-up task regardless of when the client connects;
   no decision-window withhold is needed because `done` no longer carries the
   handoff. (supersedes `test_status_gated_while_deciding` +
   `test_sse_holds_open_while_deciding_then_delivers_baton`)
3. **A plain non-autopilot done closes promptly.** Unchanged — `_latest_task_for_conv`
   equals this task, so there is no newer task to attach and the client finalizes.
   (supersedes `test_normal_done_task_unaffected` + `test_sse_normal_done_task_closes_promptly`)
4. **Cold-replay / late-connect is not a special case.** Reading the index +
   loading the persisted VU message is the SAME path warm and cold — the whole
   `_apply_autopilot_baton` synthesis branch is deleted, not re-implemented.
   (supersedes `test_sse_synthetic_done_carries_baton`)

Plus the charter front/back contract: the client attaches by the server-assigned
STABLE task id from the index, never array indices or transient client state.

### Exactly-once discipline this mechanism must still prove
Two hazards the baton's disappearance must not reintroduce:
1. **Ordering (HB-1, §4.1):** the index must advance to the successor BEFORE the
   parent `done` is emitted — else the client can observe end-of-turn with the
   index still on the parent, declare the conv idle, and strand the VU bubble
   (the mirror of the visible bug). This is a server-side happens-before, guarded
   below.
2. **Idempotency:** a turn's `done` may be observed on BOTH SSE and a poll
   fallback; attaching to `latestTaskId` must be a no-op when the client is
   already on it (dedup by task id, which `_attachAutopilotFollowup` already does
   via `_msgId`), so it cannot double-attach. This is a client-side idempotent
   reducer, not a new server channel.

## 6. Migration-test plan (write these FIRST, RED before any prod edit)

The suite asserts the ONE reducer — "attach to the conv's newer live task after
`done`" — plus the deletions. It is deliberately small because the mechanism is
small (there is no enum/state/baton to enumerate cases for):

Backend `tests/test_autopilot_chain_handoff.py`:
- `test_vu_task_registered_under_real_conv` — after the parent turn, the VU task
  is `_latest_task_for_conv(convId)` (NOT `convId==''`); the follow-up in turn
  supersedes it. This is the whole handoff — index advance, not a stamped baton.
- `test_index_advances_before_parent_done` — **the HB-1 guard.** At the moment
  the parent `done` event is observed, `_latest_task_for_conv(convId)` already ==
  `vuTaskId` (≠ parent id). Asserted by capturing the index value at the
  `append_event(done)` seam (spy on the done emit; read the index inside the spy)
  so the ordering is proven, not assumed. RED against the dangerous ordering.
- `test_parent_done_fires_immediately_no_withhold` — with the latch deleted, a
  parent SSE with autopilot armed emits `done` promptly (state → done, no
  multi-second hold) and carries NO `autopilotNextTaskId`/`autopilotVuMessage`.
- `test_done_carries_no_baton_fields` — neither the SSE `done` nor the poll body
  carries the retired baton keys (the fields are GONE, not merely empty).
- `test_conv_live_task_points_at_running_vu` — `_conv_has_live_task(convId)` is
  True while the VU/follow-up runs (the client's single attach signal), on both
  the warm and the cold-reload path (same index read).
- `test_plain_done_no_successor` — non-autopilot task: it is its own
  `_latest_task_for_conv`, no newer task, client finalizes; closes promptly.

Frontend jsdom `tests/test_frontend_autopilot_chain_attach.py`:
- after `done`, when `conv.latestTaskId` names a *different* pending/running task
  → the client attaches to it exactly once (dedup by task id; NC = same task id
  ⇒ no re-attach, no double-bubble).
- VU message renders from the loaded conv record (`_isVirtualUser`), NOT from a
  baton payload.

**Keep the existing `test_autopilot_poll_handoff.py` green until the cutover
commit**, then DELETE it in the SAME commit — its 4 guards describe the baton
world that no longer exists; the two mechanisms are mutually exclusive (the
withhold + hand-carried baton either exist or they don't), so a dual-run is not
possible.

## 7. Build order (each step green before the next; charter §strangler-fig ethos)

1. **Land this doc + the migration-test suite in RED** (tests express the target
   reducer; prod unchanged; suite `@pytest.mark.skip('epic pt_8dc030176bad450b
   not yet cut over')` so the collection gate stays green). ← *safe, additive,
   NON-contract-touching* — DONE (commit ef8826d; suite rewritten to the
   supersede-index mechanism, no `_autopilot_chain` descriptor).
2. Frontend FIRST (harmless without the backend change): make the client attach
   to `conv.latestTaskId` after `done` when it is a different pending/running
   task, idempotently, PREFERRING it over the legacy baton (which still fires,
   so behavior is unchanged until step 3 stops sending it). Ship behind nothing —
   it is a no-op while the VU still runs with `convId=''` (no newer conv task
   appears), so it cannot regress the current baton path.
3. **CUTOVER (the contract change — HUMAN-GATED):** register the VU/follow-up
   under the real `convId` (drop the `convId=''` opt-out), remove
   `_VUEventForwarder`, delete `_autopilot_deciding` + its `_task_terminal` gate,
   emit parent `done` early, and retire the `autopilotNextTaskId`/
   `autopilotVuMessage` baton + its poll mirror + `_apply_autopilot_baton`. Delete
   `test_autopilot_poll_handoff.py` and un-skip this suite in the same commit.
4. Cleanup: the abort-mirror thread's parent-stream assumptions (the VU now aborts
   via its own task like any task); JOURNAL entry.

There is NO "dual-write both batons" step — the pure mechanism reuses an index
that already exists, so there is nothing to write twice. That removed step is the
clearest evidence this is the simpler design.

## 8. Why implementation is BLOCKED here (not no-op'd)

Steps 1–2 are safe and additive (doc + skipped tests landed; the client-attach
reducer is a no-op until the backend stops opting out). **Step 3 (cutover)
mutates the exactly-once baton delivery contract** — even though it NET-DELETES
that contract in favor of the pre-existing supersede index, retiring a
load-bearing delivery path is exactly the kind of change that needs sign-off —
which:
- the charter marks as owner-personal (CAS/VU autopilot exactly-once delivery), and
- the owner explicitly gated earlier this conversation: *"值得做但另开 epic;若现在
  做需明确授权动 baton 契约,我会先写迁移测试再动"* (worth doing but a separate epic;
  doing it now needs explicit authorization to touch the baton contract, and I'd
  write migration tests first).

This is a genuine external gate an autonomous turn cannot clear itself: it is not
a sibling-commit dependency (no other conversation's file unblocks it) — it is a
**human authorization** to change a load-bearing delivery contract. Per the
dispatch protocol that is a `[human-gated]` block. The design + migration-test
plan (the "write migration tests first" precondition the owner named) is the
maximal safe progress; the cutover awaits sign-off.
