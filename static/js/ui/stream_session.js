/* stream_session.js — the live-stream session state (RENDER_CONTRACT Phase 3.5 §7).
 *
 * ONE per-convId runtime slice for an in-flight stream. It holds the state
 * that is REAL but must NEVER live in the message document (the SSOT) —
 * runtime facts about "what the model is doing right now", not turn content:
 *
 *   { phase: {phase, detail, detailKey, detailArgs, tools, toolContext, round} | null }
 *
 * ★ KEY CONTRACT (guarded by tests/test_frontend_convview_apply_guards.py):
 *   a session object may carry ONLY the `phase` key — forever. Adding
 *   `content`/`thinking`/`toolRounds` (or any new key) re-opens the exact
 *   "second fact source beside the message document" door the §7 retirement
 *   closed: a global mutable Map off-document is streamBufs v2 the moment it
 *   holds anything but runtime phase. Turn content/thinking/rounds project
 *   from the message document; the session is the ONE exception because
 *   phase has no document home. Extending the key set is an architectural
 *   decision — it must land with the guard updated in the same commit.
 *
 * WRITERS (the only allowed ones):
 *   - the SSE PHASE event handler (sse_pipeline.js) — live events AND
 *     warm-reconnect replayed events share that one dispatch path, so a
 *     warm reconnect re-seeds the session from the server event log
 *     (evidence: lib/chat_dispatch.py:636 replays task['events'][cursor:],
 *     which includes PHASE events — docs/RENDER_CONTRACT_PHASE3_5_PLAN.md §7.4).
 *   - the poll fallback (sse_poll_fallback.js) — server truth for phase.
 *   - VU streaming deltas (streaming_render.js) — phase clear/set mirror.
 * READERS (the full pinned surface — pinned by the read-surface guard):
 *   - health_stream_timer.js :824  _updateStreamTimerUI (the liveness banner)
 *   - health_stream_timer.js :943  _streamFrameArg (the updateStreamingUI frame)
 *   - health_stream_timer.js :997  _streamFrameArg checkpoint fallback
 *   - sse_pipeline.js        :1034 delta_reset frame phase
 *   - stream_lifecycle.js    :140  reconnect re-render
 *   (2 paint readers in health_stream_timer + 3 frame-projection reads)
 *
 * Presence semantics: an entry EXISTS only while its TURN is live —
 * clearStreamSession() is called by every stop/teardown path (twStop,
 * streaming-bubble removal). Note "turn", not "this tab's SSE": the two come
 * apart on a cold attach / socket-down window / poll-only lane, and the poll
 * lane is a first-class phase writer (see setStreamPhase). For "is a stream
 * live in THIS TAB" use activeStreams.has(convId); the session answers "what
 * is the turn DOING".
 *
 * This REPLACES streamBufs (deleted in the §7 retirement): content, thinking
 * and toolRounds now project straight from the message document; phase —
 * which has no document home — lives here.
 */
/* `var` (not const/let): the production bundle concatenates all modules into
 * one script scope, but the JSDOM test harness evaluates each file via a
 * SEPARATE indirect eval — only `var` + function declarations leak onto the
 * global object there. Same pattern the other shared registries rely on. */
var streamSessions = new Map();

/** Return the live session slice for convId, lazily creating a blank one.
 *  Blank = { phase: null } — a fresh cursorless reconnect shows the default
 *  waiting pulse until the next live PHASE event (accepted transient, see
 *  plan §7.4 verdict-C semantics). */
function getStreamSession(convId) {
  let s = streamSessions.get(convId);
  if (!s) {
    s = { phase: null };
    streamSessions.set(convId, s);
  }
  return s;
}

/** Write the phase for a turn that is STILL IN PROGRESS.
 *
 * The rule this enforces is unchanged in INTENT: a phase event arriving after
 * the turn ended must be dropped, never resurrect a session (the paint readers
 * would then keep rendering a turn that is over, and the Map would never be
 * reclaimed). What changed is the PREDICATE it asks.
 *
 * It used to ask `activeStreams.has(convId)` — "does THIS TAB hold an open
 * SSE?". That is a PROXY, and it comes apart in exactly the case this project
 * keeps meeting: the SSE is down (cold attach to an autopilot VU carrier, a
 * socket-down window, the poll-only lane) while the backend keeps generating.
 * `sse_poll_fallback.js` is the poll lane's ONLY phase writer, so every poll
 * delivered a phase and this function silently threw it away — the stage text
 * was STRUCTURALLY impossible on that lane (pt_a1b803793eb84925).
 *
 * So it now asks the real question — "is this TURN in flight?" — through the
 * SAME turn-level predicate the render gates use (`_convMainTurnInFlight`,
 * chat_render.js), which unions this tab's main stream, the optimistic
 * `activeTaskId` pin, and the server-authoritative `_authoritativeActiveTaskIds`
 * (including `#vu` carriers). "Who is running" is then one answer shared by the
 * Stop button, the action bar and the phase text.
 *
 * ★ DELIBERATELY the TURN-level predicate, NOT the conv-level `_convBusyAnyLane`
 *   / `computeConvBusy`: those also scan branch-stream keys (`conv.id + ':'`),
 *   and a live BRANCH does not write the MAIN turn. Routing phase through the
 *   conv-level union would make a branch put stage text on the main turn — the
 *   defect 94347aa7 removed from the render gates. (The ticket for this fix
 *   originally prescribed exactly that; it was corrected before landing.)
 *
 * Load order is pinned and asserted by
 * tests/test_frontend_stream_phase_poll_lane.py: conv_state_reducer.js (21) →
 * chat_render.js (55) → stream_session.js (66). The typeof guard keeps a
 * degenerate/partial bundle fail-CLOSED — without the predicate we fall back to
 * the old local-SSE-only behaviour rather than seeding sessions unconditionally,
 * because over-seeding is the direction that leaks the Map.
 */
function setStreamPhase(convId, phase) {
  if (!streamSessions.has(convId) && !_phaseTurnStillRunning(convId)) return;
  getStreamSession(convId).phase = phase;
}

/** Resolve "is this turn still in flight" for a convId.
 *
 * The session layer is keyed by convId while the predicate takes the conv
 * OBJECT, so this is the lookup seam — nothing more. It must NOT grow a second
 * copy of the liveness rule (charter #24): when the shared predicate is absent
 * (partial bundle) we fall back to the pre-fix local-stream test, which is a
 * strict SUBSET of it, never a second opinion. */
function _phaseTurnStillRunning(convId) {
  const _live = (typeof activeStreams !== 'undefined' && activeStreams.has(convId));
  if (_live) return true;
  if (typeof _convMainTurnInFlight !== 'function') return false;
  const _conv = (typeof getConvById === 'function') ? getConvById(convId) : null;
  return !!(_conv && _convMainTurnInFlight(_conv));
}

/** Drop the session slice (stream stop / teardown / bubble removal). */
function clearStreamSession(convId) {
  streamSessions.delete(convId);
}
