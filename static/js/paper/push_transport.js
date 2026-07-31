/* ═════════════════════════════════════════════════
   paper/push_transport.js — the ONE push-vs-poll transport for Reading Mode
   (pt_f6aec3ad)

   ── Why this file exists ──
   Every Reading-Mode capability whose backend runtime declares
   ``push_channel='paper'`` already broadcasts each event on the unified
   /api/push WebSocket the instant it is appended. Three frontends consume
   those events, and each one originally polled instead of listening:

     report.js   POLL 1200ms  → fixed first (pt_67ffc2b7), inline
     qa.js       POLL  700ms  → this module
     arxiv.js    POLL  600ms  → this module   (recommend)

   So a tool that finished at t=0 kept its spinner turning for up to a full
   poll interval, for no reason other than that nobody subscribed to the
   channel already carrying the news. Fixing the second and third consumer by
   copy-pasting report.js's inline helper would have produced three
   near-identical implementations of the same contract — the shape this
   project keeps paying for. So the contract lives here once.

   ── The contract ──
   1. PUSH IS AN ACCELERATOR, POLL IS THE FLOOR. The poll loop is never
      removed: a client whose WebSocket is blocked by a corporate proxy has no
      push channel at all and must still converge.
   2. EXACTLY-ONCE. Both transports deliver the SAME events, so applying them
      naively double-appends every delta (the answer/report body rendered
      twice). Every event carries a monotonic ``seq`` (assigned by
      ``TaskRuntime.append_event``), which makes de-duplication EXACT rather
      than heuristic, and keeps the two transports ordered with respect to
      each other.
   3. AN EVENT WITH NO seq IS APPLIED UNCONDITIONALLY. Defensive: an older
      server, or a synthetic frame. Dropping it would be worse than a rare
      duplicate.

   Loaded before every paper/* consumer in lib/js_bundler.py (window scope,
   no imports).
   ═════════════════════════════════════════════════ */

/**
 * Ordered, exactly-once ingest gate.
 *
 * Call this INSTEAD of the raw per-capability apply function, from BOTH the
 * push handler and the poll loop. `state` is any object the caller owns; the
 * high-water mark is kept on it as `_seqSeen`.
 *
 * @param {object} state   the capability's stream state (mutated: `_seqSeen`)
 * @param {object} ev      the event
 * @param {function} apply `(state, ev) -> any` — the raw applier
 * @returns {any} whatever `apply` returned, or false when the event was a
 *                duplicate the other transport already applied.
 */
function paperIngestEvent(state, ev, apply) {
  if (!state || !ev || typeof apply !== 'function') return false;
  var seq = ev.seq;
  if (typeof seq === 'number') {
    if (state._seqSeen == null) state._seqSeen = -1;
    if (seq <= state._seqSeen) return false;   // the other transport had it
    state._seqSeen = seq;
  }
  return apply(state, ev);
}

/**
 * Bind the 'paper' push channel to a running task.
 *
 * Idempotent per (state, taskId): calling it again for the same task is a
 * no-op, so it is safe to call from every attach point (fresh start, resume,
 * re-attach after a refresh) without tracking which one ran.
 *
 * @param {object}   state    stream state; `_pushTaskId` is kept on it
 * @param {string}   taskId   the task to subscribe to
 * @param {object}   opts
 * @param {function} opts.isCurrent  `() -> bool` — false once this state has
 *        been superseded (paper switch, regenerate, a newer question). The
 *        same abandon guard the poll chains already use; without it a stale
 *        handler repaints into a dead view.
 * @param {function} opts.onEvent    `(ev) -> void` — apply + repaint. Should
 *        route through `paperIngestEvent`.
 * @param {function} [opts.isTerminal] `(ev) -> bool` — defaults to
 *        done/error/aborted. On a terminal frame the subscription is released
 *        automatically, so a long session cannot accumulate live handlers for
 *        finished tasks.
 */
function paperAttachPush(state, taskId, opts) {
  if (!state || !taskId || !opts || typeof opts.onEvent !== 'function') return;
  if (typeof pushSubscribe !== 'function') return;      // push module absent
  if (state._pushTaskId === taskId) return;             // already bound
  paperDetachPush(state);

  var isCurrent = opts.isCurrent || function () { return true; };
  var isTerminal = opts.isTerminal || function (ev) {
    return ev && (ev.type === 'done' || ev.type === 'error'
                  || ev.type === 'aborted');
  };

  try {
    pushSubscribe('paper', taskId, function (ev) {
      if (!isCurrent()) return;
      if (!ev || !ev.type) return;
      try {
        opts.onEvent(ev);
      } catch (e) {
        console.debug('[Paper:Push] handler failed:', e);
      }
      if (isTerminal(ev)) paperDetachPush(state);
    });
    state._pushTaskId = taskId;
  } catch (e) {
    // A failed subscription is NOT fatal — the poll floor still converges.
    console.debug('[Paper:Push] subscribe failed:', e);
  }
}

/** Release a state's push subscription (safe to call unconditionally). */
function paperDetachPush(state) {
  if (!state || !state._pushTaskId) return;
  try {
    if (typeof pushUnsubscribe === 'function') {
      pushUnsubscribe('paper', state._pushTaskId);
    }
  } catch (e) {
    console.debug('[Paper:Push] unsubscribe failed:', e);
  }
  state._pushTaskId = '';
}

/* Node-eval / test harness hook (no-op in the browser, where these are
   window-scope globals). */
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { paperIngestEvent, paperAttachPush, paperDetachPush };
}
