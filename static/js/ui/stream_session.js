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
 *
 * CONDITIONAL FOLD (module-owned, NOT a raw writer): foldStreamPhaseIf()
 * below is the ONLY sanctioned out-of-band fold — today used solely by the
 * compaction_done SSE handler (sse_handlers_misc.js) to retire a live
 * 'compacting' phase the moment the compaction's own terminal lands
 * (pt_f222e9ed: the phase has no later lifecycle event of its own, so
 * without the fold it outlives the compaction for hours). The session READ
 * stays inside this module, so neither the reader- nor writer-surface guard
 * grows for that caller.
 * READERS (the full pinned surface — pinned by the read-surface guard):
 *   - health_stream_timer.js :824  _updateStreamTimerUI (the liveness banner)
 *   - health_stream_timer.js :943  _streamFrameArg (the updateStreamingUI frame)
 *   - health_stream_timer.js :997  _streamFrameArg checkpoint fallback
 *   - sse_pipeline.js        :1034 delta_reset frame phase
 *   - stream_lifecycle.js    :140  reconnect re-render
 *   (2 paint readers in health_stream_timer + 3 frame-projection reads)
 *
 * DERIVED CONSUMER (module-owned, NOT a direct reader — the read-surface
 * guard stays at the 3-file allowlist): ui/conversation_list.js mirrors the
 * in-answer "限流中" (rate-limit) phase chip into the sidebar dot/tag via
 * convRateLimitPhase(), the module's exported read-only predicate over the
 * live phase. setStreamPhase/clearStreamSession repaint the sidebar
 * (renderConversationList, typeof-guarded) only when the rate-limit VERDICT
 * flips, so the mirror can never drift from the phase truth and needs no
 * per-lane clearing hooks (finishStream / twStop / conv-switch all call
 * clearStreamSession, which flips the verdict back off → one repaint).
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

/* ── Rate-limit sidebar mirror (derived consumer, owner feature) ─────────
 * "Rate-limited" is true exactly when the live phase is a `retrying` phase
 * whose cause is a 429 / rate-limit cooldown — the SAME honest-label ruling
 * the in-answer retry banner (streaming_ui.js) renders:
 *   • detailKey 'stream.phase.retryRateLimited' — _on_retry(429) AND an
 *     all-slots rate-limit cooldown wait (lib/llm_dispatch/retry_i18n.py:
 *     retry_phase_fields fed by cooldown_wait_label);
 *   • detailArgs.reasonKey 'stream.retryReason.waitingForModel' or
 *     'stream.retryReason.rateLimited' — a typed 限流 cause riding a
 *     generic retry frame (e.g. the first-byte heartbeat while the picked
 *     slot is parked for rate_limit, manager/_stream.py::_on_waiting).
 * Quota / backoff / upstream / shared-project-contention waits do NOT
 * qualify — labelling those 限流 is exactly the lie the backend's
 * honest-label ruling (retry_i18n.py) exists to prevent. */
function _phaseRateLimited(p) {
  if (!p || p.phase !== 'retrying') return false;
  if (p.detailKey === 'stream.phase.retryRateLimited') return true;
  const a = p.detailArgs;
  return !!(a && (a.reasonKey === 'stream.retryReason.waitingForModel'
               || a.reasonKey === 'stream.retryReason.rateLimited'));
}

/** Derived read for the sidebar: {model, attempt} while the conv's live
 *  phase is a rate-limit wait, else null. The phase shape stays module-
 *  owned — consumers never touch streamSessions directly, so the read-
 *  surface guard (test_frontend_convview_apply_guards.py) is untouched. */
function convRateLimitPhase(convId) {
  const s = streamSessions.get(convId);
  const p = s && s.phase;
  if (!_phaseRateLimited(p)) return null;
  const a = p.detailArgs || {};
  return { model: a.model || '', attempt: p.attempt || 0 };
}

/** Repaint the sidebar ONLY when the rate-limit verdict flipped. Beats that
 *  keep the same verdict (attempt 1→2→3, waiting heartbeats) carry no new
 *  sidebar-visible information, so repainting them would be pure churn (the
 *  heartbeat fires every ~5s; rendering it each time would jitter the
 *  sidebar). typeof-guarded: degenerate/partial bundles (JSDOM harnesses)
 *  simply skip the mirror. */
function _mirrorRateLimitFlip(wasRl, afterPhase) {
  const isRl = !!_phaseRateLimited(afterPhase);
  if (wasRl === isRl) return;
  if (typeof renderConversationList === 'function') renderConversationList();
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
  const _s = getStreamSession(convId);
  const _wasRl = !!_phaseRateLimited(_s.phase);
  _s.phase = phase;
  _mirrorRateLimitFlip(_wasRl, phase);
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
  const _s = streamSessions.get(convId);
  const _wasRl = !!(_s && _phaseRateLimited(_s.phase));
  streamSessions.delete(convId);
  /* Turn ended → the rate-limit verdict flips back off; repaint once so the
   * sidebar dot/tag clears even if no further PHASE event arrives. */
  _mirrorRateLimitFlip(_wasRl, null);
}

/** Fold the live phase IFF it is exactly `phaseName` (no-op otherwise).
 *
 * The out-of-band terminal fold: some phases own a lifecycle that ENDS on a
 * non-PHASE event — 'compacting' ends on compaction_done, which is not a
 * phase event and therefore never retired the HUD on its own (pt_f222e9ed:
 * the pill then outlived the compaction for hours). Keeping the read +
 * conditional clear INSIDE this module means the caller is neither a raw
 * session reader nor a raw writer, so the RENDER_CONTRACT pinned surfaces
 * (test_frontend_convview_apply_guards.py) do not grow. Never folds an
 * unrelated live phase; never creates a session entry for a conv without
 * one (Map-leak guard, probe-pinned). */
function foldStreamPhaseIf(convId, phaseName) {
  const _s = streamSessions.get(convId);
  if (!_s || !_s.phase || _s.phase.phase !== phaseName) return;
  setStreamPhase(convId, null);
}
