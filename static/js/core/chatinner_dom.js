/* ═══════════════════════════════════════════════════════════════════════════
   core/chatinner_dom.js — the ONE ordered-insert primitive for #chatInner

   WHY THIS IS ITS OWN FILE (the whole point — do not fold it back in)
   ------------------------------------------------------------------
   #chatInner holds two kinds of children:

     * MESSAGES   — `.message` nodes, a projection of conv.messages, ordered.
     * FURNITURE  — `#_lazyLoadSentinel` (head) and `#_lazyLoadSentinelBottom`
                    (tail): the lazy-window strips that stand in for messages
                    evicted above / below the rendered span.

   Furniture is NOT a message, but it lives in the same child list, so every
   positional DOM call has to step over it. That obligation was implicit, and
   it was violated at BOTH ends — the same bug twice:

     HEAD (2026-07-26, f1691021): the surgical reconcile in chat_render.js
       anchored its first insert on `inner.firstChild`, which is the HEAD
       sentinel when a lazy window is open. Every background repaint slid the
       sentinel down one slot until it reached the bottom; then
       `_loadOlderMessages` (`sentinel.after(frag)`) spliced the OLDEST
       messages below the newest one.

     TAIL (this file): `_ensureBottomSentinel` pins itself with
       `inner.appendChild(s)`, so it is the LAST child. `ConvView.apply` and
       `ConvView.startStreaming` used `insertAdjacentHTML('beforeend', …)`,
       which lands AFTER it:
           seed:                      a, b, SENT_BOT
           after apply(NEW):          a, b, SENT_BOT, NEW
           after startStreaming():    a, b, SENT_BOT, NEW, LIVE
       Then `_loadNewerMessages` (`sentinel.before(frag)`) splices the
       recovered tail ABOVE the message you just sent — the head inversion
       again, at the other end.

   The head fix did not prevent the tail bug because its anchor was a CLOSURE
   inside renderChat's surgical block: ConvView could not reach it, so the
   rule could not be shared. That is the actual lesson, and this module is the
   answer — ONE place that knows where furniture lives, reachable by every
   writer.

   THE RULE: no file other than this one may write to #chatInner with a raw
   positional API (`firstChild` as an anchor, `insertAdjacentHTML('beforeend')`,
   `appendChild`, `prepend`). Enforced by
   tests/test_frontend_lazy_sentinel_anchor.py.

   Leaf module: touches only `document` / `console`. Must load BEFORE every
   consumer (chat_render.js, conv_view.js) — order pinned by the same test.
   ══════════════════════════════════════════════════════════════════════════ */

/* The furniture ids. Kept here (not inlined at call sites) so adding a third
 * strip means editing ONE list instead of hunting every insertion point. */
const CHATINNER_FURNITURE_IDS = ['_lazyLoadSentinel', '_lazyLoadSentinelBottom'];

function _isFurniture(node) {
  return !!(node && node.nodeType === 1 && node.id &&
            CHATINNER_FURNITURE_IDS.indexOf(node.id) !== -1);
}

/** The node a HEAD insert must sit BEFORE — i.e. the first MESSAGE child,
 *  stepping over any leading furniture. `null` when there is no message yet
 *  (an insert before `null` appends, which is correct for an empty list).
 *
 *  NOT `inner.firstChild`: with a lazy window open that is the head sentinel,
 *  and anchoring on it pushes the sentinel down one slot per repaint. */
function chatInnerHeadAnchor(inner) {
  if (!inner) return null;
  let n = inner.firstChild;
  while (_isFurniture(n)) n = n.nextSibling;
  return n;
}

/** The node a TAIL insert must sit BEFORE — i.e. the trailing furniture run,
 *  so appended content lands above it. `null` when nothing trails (the common
 *  case: no bottom sentinel ⇒ plain append, byte-identical to `beforeend`).
 *
 *  NOT `appendChild` / `'beforeend'`: those land AFTER the bottom sentinel,
 *  and `_loadNewerMessages` then splices the recovered tail above the newly
 *  appended message. */
function chatInnerTailAnchor(inner) {
  if (!inner) return null;
  let anchor = null;
  let n = inner.lastChild;
  while (_isFurniture(n)) { anchor = n; n = n.previousSibling; }
  return anchor;
}

/** Insert `html` (a string) or `node` into #chatInner at a furniture-aware
 *  position. THE single write entry — every caller goes through here.
 *
 *  @param {Element} inner  — #chatInner
 *  @param {string|Node} content
 *  @param {Object} [opts]
 *    @param {'head'|'tail'} [opts.position='tail']
 *    @param {Node} [opts.before] - explicit anchor; overrides `position`.
 *                                  Callers that already computed a precise
 *                                  sibling (the surgical reconcile walking
 *                                  the list) pass it here.
 *    @param {Object} [opts.conv] - the conversation this DOM is projecting.
 *                                  Passed EXPLICITLY by every writer (the
 *                                  primitive never reaches for a global) so
 *                                  the order invariant below can run at the
 *                                  chokepoint. Omit ⇒ the check is skipped.
 *    @param {string} [opts.site] - caller name for the violation report
 *                                  (e.g. 'ConvView.apply'). Without it a
 *                                  report would only ever name renderChat,
 *                                  which is how the tail path stayed dark.
 *  @returns {Element|null} the inserted element, when resolvable.
 */
function chatInnerInsert(inner, content, opts) {
  if (!inner || content == null) return null;
  opts = opts || {};
  let anchor;
  if (Object.prototype.hasOwnProperty.call(opts, 'before')) {
    anchor = opts.before;
  } else if (opts.position === 'head') {
    anchor = chatInnerHeadAnchor(inner);
  } else {
    anchor = chatInnerTailAnchor(inner);
  }
  let node = /** @type {Element} */ (/** @type {unknown} */ (content));
  if (typeof content === 'string') {
    const wrapper = document.createElement('div');
    wrapper.innerHTML = content;
    node = wrapper.firstElementChild;
    if (!node) return null;
  }
  inner.insertBefore(node, anchor || null);
  /* ★ The invariant runs HERE — at the one chokepoint every writer passes
   * through. Putting it only on renderChat's exits (as it was first shipped)
   * meant it watched exactly the paths that were already fixed and already
   * covered by scenario tests, and was structurally blind to the tail bug
   * that came through ConvView. */
  if (opts.conv) assertChatInnerOrder(inner, opts.conv, opts.site || 'chatInnerInsert');
  return node;
}

/* ── Runtime order invariant (RENDER_CONTRACT Invariant 1) ─────────────────
 *
 * Both ordering bugs survived for months because NOTHING ever asserted the
 * thing the contract actually promises: the rendered DOM is a projection of
 * conv.messages, in order. Scenario tests only catch the scenario someone
 * thought of; this catches the shape.
 *
 * Two properties, checked together because they fail together:
 *   1. the message children's `data-msg-id` sequence is a SUBSEQUENCE of
 *      conv.messages (a lazy window renders a subset — that is legal; being
 *      out of order is not);
 *   2. no furniture sits BETWEEN two messages that are adjacent in the array
 *      (furniture stands in for ELIDED messages, so a strip between two
 *      neighbours is a misplaced sentinel — exactly the head bug, caught
 *      before it can migrate far enough to invert anything).
 *
 * ── PRODUCTION VISIBILITY: deliberate decision, not a flag default ────────
 * This check is NOT debug-gated. It was, and that made it inert on every real
 * deployment (`debug_mode` resolves to False by default in lib/__init__.py),
 * so the only people it could ever inform were developers who already knew.
 * BOTH ordering bugs reached real users and produced no signal — which is the
 * exact failure mode this exists to end.
 *
 * Cost is bounded and small: one pass over `inner.children`, and the rendered
 * span is capped by `_MAX_RENDER_WINDOW` (80). Inserts are per-turn events,
 * not per-token. A violation LATCHES, so a broken page reports ONCE — the
 * same discipline as core/identity_gate_tripwire.js, because a condition that
 * recurs on every repaint would otherwise bury its own signal.
 *
 * Delivery rides the EXISTING production beacon (`Api.clientError.report` →
 * POST /api/client-error → server log), the same channel the global
 * window.onerror handler already uses. No new endpoint, no new node.
 *
 * NEVER throws and NEVER mutates: reporting a broken projection must not also
 * break the render. */
let _chatInnerOrderViolated = false;
let _chatInnerOrderSite = '';

function assertChatInnerOrder(inner, conv, site) {
  if (_chatInnerOrderViolated) return true;        // latched — already reported
  if (!inner || !conv || !Array.isArray(conv.messages)) return true;
  try {
    const order = [];
    const kids = inner.children;
    for (let i = 0; i < kids.length; i++) {
      const el = kids[i];
      if (_isFurniture(el)) { order.push({ furniture: true, id: el.id }); continue; }
      const mid = el.getAttribute && el.getAttribute('data-msg-id');
      if (mid) order.push({ furniture: false, id: mid });
    }
    /* Index each rendered message against the array. */
    const pos = Object.create(null);
    for (let i = 0; i < conv.messages.length; i++) {
      const m = conv.messages[i];
      if (m && m._msgId) pos[m._msgId] = i;
    }
    let prevIdx = -1;
    let prevWasMsg = false;
    let pendingFurniture = false;
    let problem = '';
    for (const entry of order) {
      if (entry.furniture) { pendingFurniture = true; continue; }
      const idx = pos[entry.id];
      if (idx === undefined) { prevWasMsg = false; pendingFurniture = false; continue; }
      if (prevIdx >= 0 && idx <= prevIdx) {
        problem = 'OUT OF ORDER: ' + entry.id + ' (array idx ' + idx +
          ') renders after array idx ' + prevIdx;
        break;
      }
      if (pendingFurniture && prevWasMsg && prevIdx >= 0 && idx === prevIdx + 1) {
        problem = 'MISPLACED SENTINEL: lazy-window furniture sits between ' +
          'array-adjacent messages ' + prevIdx + ' and ' + idx +
          ' — furniture stands in for ELIDED messages, so it must never ' +
          'separate two neighbours';
        break;
      }
      prevIdx = idx;
      prevWasMsg = true;
      pendingFurniture = false;
    }
    if (!problem) return true;
    _chatInnerOrderViolated = true;
    _chatInnerOrderSite = String(site || 'unknown');
    const msg = '[chatInner] RENDER ORDER VIOLATION at ' + _chatInnerOrderSite +
      ' — ' + problem + '. The DOM is no longer a faithful projection of ' +
      'conv.messages (RENDER_CONTRACT Invariant 1). Every write to #chatInner ' +
      'must go through core/chatinner_dom.js::chatInnerInsert so lazy-window ' +
      'furniture is stepped over.';
    if (typeof console !== 'undefined' && console.warn) console.warn(msg);
    if (typeof debugLog === 'function') debugLog(msg, 'warn');
    _beaconChatInnerOrderViolation(msg, problem);
    return false;
  } catch (e) {
    /* A diagnostic must never break the render it is diagnosing. */
    return true;
  }
}

/* Ship the violation to the server over the EXISTING client-error beacon, so
 * a real user hitting a real inversion leaves a trace in the server log rather
 * than only in a console nobody is watching. Fire-and-forget; a transport
 * failure leaves the console line as the last resort. Never throws. */
function _beaconChatInnerOrderViolation(msg, problem) {
  try {
    if (typeof Api === 'undefined' || !Api.clientError ||
        typeof Api.clientError.report !== 'function') return false;
    Api.clientError.report({
      message: msg.slice(0, 2000),
      url: (typeof location !== 'undefined' && location.href) || '',
      extra: { site: _chatInnerOrderSite, problem: String(problem).slice(0, 300) },
    });
    return true;
  } catch (e) {
    return false;
  }
}

function chatInnerOrderViolated() { return _chatInnerOrderViolated; }
function chatInnerOrderViolationSite() { return _chatInnerOrderSite; }

/* Test seam only — never called by production code. */
function resetChatInnerOrderForTests() {
  _chatInnerOrderViolated = false;
  _chatInnerOrderSite = '';
}

if (typeof window !== 'undefined') {
  window.CHATINNER_FURNITURE_IDS = CHATINNER_FURNITURE_IDS;
  window.chatInnerHeadAnchor = chatInnerHeadAnchor;
  window.chatInnerTailAnchor = chatInnerTailAnchor;
  window.chatInnerInsert = chatInnerInsert;
  window.assertChatInnerOrder = assertChatInnerOrder;
  window.chatInnerOrderViolated = chatInnerOrderViolated;
  window.chatInnerOrderViolationSite = chatInnerOrderViolationSite;
  window.resetChatInnerOrderForTests = resetChatInnerOrderForTests;
}
