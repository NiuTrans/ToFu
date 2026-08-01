/* ═══════════════════════════════════════════════════════════════════
   ui/stall_watch.js — the "no unannounced freeze" watch (pt_e0ea29f2)

   WHY (measured incident, 2026-07-31, conv ms8bx7089s3268): a task hung
   2.5h inside a run_command with ZERO output. The user saw a hollow
   streaming bubble with no phase text, no finish tag, no edit panel —
   nothing announced the freeze. Worse: the page was receiving ~1 event
   every 6s the whole time, but those were heartbeat SELF-TICKS (the
   tool-heartbeat pinging itself), not evidence the tool was producing.

   The wire contract (pt_8524e0ec B1, lib/agent_core/events.py): a
   heartbeat tool_progress frame now carries ``_selfTick: true``. This
   module is the frontend reader of that contract:

     * REAL progress (deltas / tool results / stdout chunks / retry
       phases / unmarked progress) refreshes ``lastReal``.
     * A ``_selfTick`` frame refreshes NOTHING — it only proves the
       dispatcher thread is alive.
     * When ``now - lastReal`` exceeds the threshold while the stream is
       still attached, the render seam in streaming_ui.js paints an amber
       banner ("已停滞 · 静默 {n}s") with a Stop button. A REAL event
       flips it back off (self-healing — a late-finishing tool recovers
       the card exactly like the swarm stalled-card does).

   F5-safety: a reconnect replays the backlog through the SAME dispatch
   seam, and replayed frames carry the backend ``emittedAt`` clock — so
   the stall base survives a reload instead of restarting from zero.

   Load order: leaf module (document/window only) — load BEFORE
   sse_pipeline.js (the feed seam) and streaming_ui.js (the render seam).
   ═══════════════════════════════════════════════════════════════════ */

/* Threshold (seconds) of "nothing but self-ticks" before the banner
 * shows. Generous on purpose: the card is informational, never a kill —
 * the user's own judgement ends the turn. Override in tests. */
const _STALL_THRESHOLD_S = (typeof window !== 'undefined' && window._STALL_WATCH_THRESHOLD_S) || 300;

/* taskId → { lastReal: ms-epoch, shown: bool, convId: string } */
const _stallWatches = Object.create(null);

function _evClock(ev) {
  /* Backend clock first (replay-safe), ingress clock next, now last. */
  if (ev && typeof ev.emittedAt === 'number') return ev.emittedAt;
  if (ev && typeof ev.receivedAt === 'number') return ev.receivedAt;
  return Date.now();
}

/* Feed EVERY dispatched stream event through here (one seam in
 * dispatchSSEEvent covers live + Last-Event-ID replay). */
function stallWatchFeed(convId, taskId, ev) {
  if (!taskId || !ev || !ev.type) return;
  _ensureStallTimer();
  const t = ev.type;
  if (t === 'done' || t === 'error' || t === 'aborted') {
    stallWatchClear(taskId);
    return;
  }
  if (ev._selfTick === true) {
    /* Heartbeat self-tick: transport keepalive, NOT evidence of tool
     * life (pt_8524e0ec). Never refreshes lastReal — this is the whole
     * grading rule. Two jobs though: (a) it IS the natural metronome —
     * check the threshold on every beat; (b) the FIRST frame we ever see
     * seeds the evidence floor (a replay that starts mid-stream can only
     * claim a stall as old as the oldest frame it holds — honest, and it
     * keeps the banner reachable for a stream that has produced nothing
     * but self-ticks since we attached). */
    const w0 = (_stallWatches[taskId] ||
                (_stallWatches[taskId] = { lastReal: 0, shown: false, convId: convId || '' }));
    if (convId && !w0.convId) w0.convId = convId;
    if (!w0.lastReal) w0.lastReal = _evClock(ev);
    _stallWatchTick();
    return;
  }
  const w = (_stallWatches[taskId] ||
             (_stallWatches[taskId] = { lastReal: 0, shown: false, convId: convId || '' }));
  if (convId && !w.convId) w.convId = convId;
  w.lastReal = _evClock(ev);
  if (w.shown) {
    /* Self-heal: real production resumed — drop the banner the same way
     * the swarm stalled-card flips back when a late agent completes. */
    w.shown = false;
    if (typeof twUpdate === 'function' && w.convId) twUpdate(w.convId);
  }
}

/* Read-only state for the render seam. Pure: computes staleness on read
 * so it is correct at any moment without a timer. */
function stallWatchState(taskId) {
  const w = _stallWatches[taskId];
  if (!w || !w.lastReal) return { stalled: false, silentSecs: 0 };
  const silentSecs = Math.max(0, Math.floor((Date.now() - w.lastReal) / 1000));
  return {
    stalled: !!w.shown && silentSecs >= _STALL_THRESHOLD_S,
    silentSecs: silentSecs,
    convId: w.convId || '',
  };
}

/* The metronome: called by the feed on every self-tick AND by a light
 * interval — flips the banner on once the threshold is crossed (the
 * self-tick stream is the natural tick source for the incident shape;
 * the interval covers the no-frames-at-all shape). */
function _stallWatchTick() {
  for (const taskId of Object.keys(_stallWatches)) {
    const w = _stallWatches[taskId];
    if (!w || w.shown || !w.lastReal) continue;
    if ((Date.now() - w.lastReal) / 1000 >= _STALL_THRESHOLD_S) {
      w.shown = true;
      if (typeof twUpdate === 'function' && w.convId) twUpdate(w.convId);
    }
  }
}

let _stallTimer = null;
function _ensureStallTimer() {
  if (_stallTimer || typeof setInterval !== 'function') return;
  _stallTimer = setInterval(function () { _stallWatchTick(); _stallWatchPaintCounters(); }, 15000);
  if (_stallTimer && typeof _stallTimer.unref === 'function') _stallTimer.unref();
}

/* Keep any mounted banner's seconds honest BETWEEN repaints (the banner
 * itself only re-renders when the phase key flips; the silence grows every
 * second). Driven by the same 15s metronome. */
function _stallWatchPaintCounters() {
  if (typeof document === 'undefined' || typeof t !== 'function') return;
  const nodes = document.querySelectorAll('.stream-stalled-text');
  for (const n of nodes) {
    const tid = n.getAttribute('data-stall-task');
    if (!tid) continue;
    const st = stallWatchState(tid);
    if (st.stalled) n.textContent = t('stream.stalled.banner', { n: st.silentSecs });
  }
}

function stallWatchClear(taskId) {
  const w = _stallWatches[taskId];
  if (w && w.shown && w.convId) {
    delete _stallWatches[taskId];
    if (typeof twUpdate === 'function') twUpdate(w.convId);
    return;
  }
  delete _stallWatches[taskId];
}

/* The banner's Stop affordance — mirrors the send-button stop: abort the
 * server task (specific) + abort-conv (backstop for anything racing),
 * then let the terminal frame tear the watch down. */
function stallWatchStop(convId, taskId) {
  try { if (typeof Api !== 'undefined' && Api.chat && Api.chat.abortTask) Api.chat.abortTask(taskId); }
  catch (e) { console.debug('[stall-watch] abortTask failed: %s', e); }
  try { if (typeof Api !== 'undefined' && Api.chat && Api.chat.abortConv) Api.chat.abortConv(convId); }
  catch (e) { console.debug('[stall-watch] abortConv failed: %s', e); }
  stallWatchClear(taskId);
}

/* Test seam only — never called by production code. */
function _resetStallWatchForTests() {
  for (const k of Object.keys(_stallWatches)) delete _stallWatches[k];
  if (_stallTimer) { clearInterval(_stallTimer); _stallTimer = null; }
}

if (typeof window !== 'undefined') {
  window.stallWatchFeed = stallWatchFeed;
  window.stallWatchState = stallWatchState;
  window.stallWatchClear = stallWatchClear;
  window.stallWatchStop = stallWatchStop;
  window._stallWatchTick = _stallWatchTick;
  window._ensureStallTimer = _ensureStallTimer;
  window._resetStallWatchForTests = _resetStallWatchForTests;
}
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { stallWatchFeed, stallWatchState, stallWatchClear,
                     stallWatchStop, _stallWatchTick, _resetStallWatchForTests };
}
