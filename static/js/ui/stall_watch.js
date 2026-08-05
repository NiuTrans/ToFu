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
     * A ``_selfTick`` frame never refreshes ``lastReal`` — it proves
       the dispatcher thread is alive, not that the tool is producing.
       It DOES stamp ``lastTick`` (the heartbeat-flow marker): while
       ticks flow, a tool is verifiably executing and no banner shows
       (regime split below).
     * When ``now - lastReal`` exceeds the threshold while the stream is
       still attached AND NOT EVEN self-ticks are arriving, the render
       seam in streaming_ui.js paints an amber banner ("已停滞 · 静默
       {n}s") with a Stop button. A REAL event (or heartbeats resuming)
       flips it back off (self-healing — a late-finishing tool recovers
       the card exactly like the swarm stalled-card does).

   F5-safety: a reconnect replays the backlog through the SAME dispatch
   seam, and replayed frames carry the backend ``emittedAt`` clock — so
   the stall base survives a reload instead of restarting from zero.

   ★★ REGIME SPLIT (owner ruling 2026-08-04): the banner is reserved for
   the TRUE freeze — NO frames at all, not even a heartbeat self-tick,
   past the threshold. While self-ticks ARE flowing a tool is verifiably
   EXECUTING (the backend ticker only runs while a tool blocks), and
   silence there is NORMAL — a find/grep over the FUSE mount legitimately
   runs minutes with zero output, and the tool row already counts
   "Running command… (Ns)" live. A genuinely wedged tool is the BACKEND
   reaper's job (silent >30min ⇒ killed with an explicit error event,
   pt_8524e0ec); duplicating that alarm at 5min in the frontend was pure
   noise — self-ticks only EXIST while a tool is in flight, so the old
   300s banner fired exclusively during healthy command execution.

   Load order: leaf module (document/window only) — load BEFORE
   sse_pipeline.js (the feed seam) and streaming_ui.js (the render seam).
   ═══════════════════════════════════════════════════════════════════ */

/* Threshold (seconds) of "NOTHING arrived — not even a self-tick"
 * before the banner shows. Generous on purpose: the card is
 * informational, never a kill — the user's own judgement ends the turn.
 * Override in tests. */
const _STALL_THRESHOLD_S = (typeof window !== 'undefined' && window._STALL_WATCH_THRESHOLD_S) || 300;

/* A self-tick younger than this means the tool heartbeat is FLOWING — a
 * tool is verifiably in flight, so silence is execution, not a freeze
 * (regime split, owner ruling 2026-08-04). 4× the backend's 15s default
 * TOOL_HEARTBEAT_INTERVAL; if that env is ever raised toward ~60s this
 * window must rise with it. Override in tests (0 = every tick counts as
 * instantly stopped, which neuters the gate). */
const _TICK_FLOW_WINDOW_S = (typeof window !== 'undefined' && window._STALL_WATCH_TICK_WINDOW_S != null)
  ? window._STALL_WATCH_TICK_WINDOW_S : 60;

/* taskId → { lastReal: ms-epoch, lastTick: ms-epoch, shown: bool, convId: string } */
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
     * claim a silence as old as the oldest frame it holds — honest; if
     * the tick stream then dies too, the frozen attach reaches the
     * banner with the full measured silence). */
    const w0 = (_stallWatches[taskId] ||
                (_stallWatches[taskId] = { lastReal: 0, lastTick: 0, shown: false, convId: convId || '' }));
    if (convId && !w0.convId) w0.convId = convId;
    if (!w0.lastReal) w0.lastReal = _evClock(ev);
    /* The heartbeat IS flowing → a tool is verifiably executing (the
     * backend ticker only runs while a tool blocks), so the banner must
     * stay OFF (owner ruling 2026-08-04). And if it was somehow up — the
     * tick stream had genuinely paused past the window — heal it now:
     * heartbeats resuming is the same self-heal as real output. */
    w0.lastTick = Date.now();
    if (w0.shown) {
      w0.shown = false;
      if (typeof twUpdate === 'function' && w0.convId) twUpdate(w0.convId);
    }
    _stallWatchTick();
    return;
  }
  const w = (_stallWatches[taskId] ||
             (_stallWatches[taskId] = { lastReal: 0, lastTick: 0, shown: false, convId: convId || '' }));
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
    stalled: !!w.shown && silentSecs >= _STALL_THRESHOLD_S && !_ticksFlowing(w),
    silentSecs: silentSecs,
    convId: w.convId || '',
  };
}

/* Is the tool heartbeat CURRENTLY flowing? A tick younger than the flow
 * window means a tool is verifiably in flight — the regime where silence
 * is normal execution, never a stall (owner ruling 2026-08-04). */
function _ticksFlowing(w) {
  return !!(w.lastTick && (Date.now() - w.lastTick) / 1000 < _TICK_FLOW_WINDOW_S);
}

/* The metronome: called by the feed on every self-tick AND by a light
 * interval — flips the banner on once the threshold is crossed (the
 * self-tick stream is the natural tick source for the incident shape;
 * the interval covers the no-frames-at-all shape). */
function _stallWatchTick() {
  for (const taskId of Object.keys(_stallWatches)) {
    const w = _stallWatches[taskId];
    if (!w || w.shown || !w.lastReal) continue;
    /* Heartbeats flowing = tool executing: silence here is NORMAL (a
     * quiet find over FUSE, a build). No banner — the tool row already
     * counts the seconds, and the backend reaper owns the genuinely
     * wedged case (silent >30min ⇒ explicit error, pt_8524e0ec). */
    if (_ticksFlowing(w)) continue;
    if ((Date.now() - w.lastReal) / 1000 >= _STALL_THRESHOLD_S) {
      w.shown = true;
      if (typeof twUpdate === 'function' && w.convId) twUpdate(w.convId);
    }
  }
}

/* DOM setInterval returns number; under node (the jsdom harnesses) it
 * returns a Timeout with .unref(), which _ensureStallTimer feature-detects
 * so a harness run never hangs the process on the metronome. The dual
 * shape is real, so the handle is typed `any` rather than lying either way. */
/** @type {any} */
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
