/* ConvView — unified controller for the #chatInner DOM.
 *
 * Step 3 of the unified chatInner-rendering refactor.
 *
 * Goal: be the only thing in the codebase allowed to mutate
 * #chatInner.  Every other module publishes intent through this
 * surface — `upsertMessage`, `removeMessage`, `replaceAll`,
 * `removeAfter`, `finalizeStreaming` — so the rules about identity
 * (msg._msgId vs msg-${idx}), streaming bubble lifecycle, and
 * fingerprint cache live in one place.
 *
 * In this initial pass the controller is intentionally thin: it
 * delegates rendering to existing helpers (`renderMessage`,
 * `_surgicalTruncateDOM`, `renderChat`) and exposes a stable surface
 * that callers can migrate to incrementally.  Later iterations will
 * move the streaming-merge logic, the `data-mfp` diff, and the
 * sequence-number reconciliation into here.
 *
 * Identity rules
 *   • Persisted messages: `msg._msgId` is a UUID minted by
 *     `lib/tasks_pkg/manager.py:_assign_message_ids`.
 *   • Client-only messages: `msg._msgId` is `tmp_<uuid>` minted by
 *     `_ensureMsgId(msg)` in core.js.
 *   • DOM mirrors both: `id="msg-${idx}" data-msg-id="${msg._msgId}"`.
 *   • Lookup priority: data-msg-id → msg-${idx} (legacy fallback).
 */
(function () {
  'use strict';

  /* ── Internal helpers ────────────────────────────────────────────── */

  /** Find a conversation by id, tolerating the `conversations` global
   *  not being defined yet (extremely early init). */
  function _findConv(convId) {
    if (typeof conversations === 'undefined' || !Array.isArray(conversations)) return null;
    return conversations.find(c => c && c.id === convId) || null;
  }

  /** Locate a message DOM element inside #chatInner.  Prefers
   *  `data-msg-id` (stable across truncations); falls back to
   *  `msg-${idx}` for legacy messages without an id. */
  function _findMsgEl(inner, msg, idx) {
    if (!inner) return null;
    if (msg && msg._msgId && typeof CSS !== 'undefined' && CSS.escape) {
      const byId = inner.querySelector('[data-msg-id="' + CSS.escape(msg._msgId) + '"]');
      if (byId) return byId;
    } else if (msg && msg._msgId) {
      /* CSS.escape unavailable — use attribute selector with quoted
       * literal.  _msgId is a UUID or `tmp_<uuid>`, characters are
       * already CSS-safe, but we still wrap in quotes. */
      const byId = inner.querySelector('[data-msg-id="' + msg._msgId + '"]');
      if (byId) return byId;
    }
    if (typeof idx === 'number') {
      return document.getElementById('msg-' + idx);
    }
    return null;
  }

  /** Compute the idx for a message in conv.messages (linear scan;
   *  fine for the few-hundred-msg max we see in chatInner). */
  function _idxOf(conv, msg) {
    if (!conv || !msg) return -1;
    if (msg._msgId) {
      for (let i = 0; i < conv.messages.length; i++) {
        if (conv.messages[i] && conv.messages[i]._msgId === msg._msgId) return i;
      }
    }
    return conv.messages.indexOf(msg);
  }

  /** Remove every node inside `inner` whose data-msg-id === msgId, EXCEPT
   *  `exceptEl` (the one node we intend to keep). Returns the count removed.
   *
   *  This is the enforcement primitive for the render invariant
   *  "one msg._msgId ⇒ at most one DOM node in #chatInner". A settled bubble
   *  is keyed `id="msg-${idx}"` (mutable array position); the live turn is the
   *  `#streaming-msg` singleton. When the tail's array index DRIFTS (a
   *  placeholder push / splice / lazy-window offset), an index-based eviction
   *  (`getElementById('msg-'+lastIdx)`) misses the real static bubble, and a
   *  fresh streaming bubble is inserted alongside it — later finalized into a
   *  second `msg-M` node. Two identical bubbles then render for a SINGLE
   *  conv.messages entry. Keying eviction on the stable `data-msg-id` closes
   *  that vector regardless of index drift. */
  function _evictByMsgId(inner, msgId, exceptEl) {
    if (!inner || !msgId) return 0;
    var sel;
    if (typeof CSS !== 'undefined' && CSS.escape) {
      sel = '[data-msg-id="' + CSS.escape(msgId) + '"]';
    } else {
      sel = '[data-msg-id="' + msgId + '"]';
    }
    var removed = 0;
    inner.querySelectorAll(sel).forEach(function (el) {
      if (el === exceptEl) return;
      try { el.remove(); removed++; } catch (e) { /* already detached */ }
    });
    return removed;
  }

  /* ── Public API ──────────────────────────────────────────────────── */

  const ConvView = {
    /** Replace or insert a single message's DOM element.
     *
     *  @param {string} convId
     *  @param {Object} msg     — the message object (must have _msgId
     *                            for stable identity; _ensureMsgId
     *                            stamps one if missing).
     *  @param {Object} [opts]
     *    @param {number} [opts.idx]      - explicit idx; otherwise computed
     *    @param {boolean} [opts.append]  - if true and no existing element
     *                                       is found, append to chatInner
     *  @returns {boolean} true if the DOM was mutated.
     */
    upsertMessage: function (convId, msg, opts) {
      opts = opts || {};
      if (typeof activeConvId !== 'undefined' && activeConvId !== convId) return false;
      const conv = _findConv(convId);
      if (!conv || !msg) return false;
      if (typeof _ensureMsgId === 'function') _ensureMsgId(msg);
      const inner = document.getElementById('chatInner');
      if (!inner) return false;
      const idx = (typeof opts.idx === 'number') ? opts.idx : _idxOf(conv, msg);
      if (idx < 0) return false;
      if (typeof renderMessage !== 'function') return false;
      const html = renderMessage(msg, idx);
      if (!html) return false;
      const existing = _findMsgEl(inner, msg, idx);
      if (existing) {
        existing.outerHTML = html;
      } else if (opts.append) {
        inner.insertAdjacentHTML('beforeend', html);
      } else {
        return false;
      }
      if (typeof _lastRenderedFingerprint !== 'undefined' &&
          typeof _convRenderFingerprint === 'function') {
        try { _lastRenderedFingerprint = _convRenderFingerprint(conv); }
        catch (e) { /* ignore — best-effort cache update */ }
      }
      return true;
    },

    /** Remove a single message's DOM element by msg id or index.
     *  Does NOT touch conv.messages — caller is responsible for
     *  the model-side mutation. */
    removeMessage: function (convId, msgOrIdOrIdx) {
      if (typeof activeConvId !== 'undefined' && activeConvId !== convId) return false;
      const inner = document.getElementById('chatInner');
      if (!inner) return false;
      let el = null;
      if (typeof msgOrIdOrIdx === 'number') {
        el = document.getElementById('msg-' + msgOrIdOrIdx);
      } else if (typeof msgOrIdOrIdx === 'string') {
        if (typeof CSS !== 'undefined' && CSS.escape) {
          el = inner.querySelector('[data-msg-id="' + CSS.escape(msgOrIdOrIdx) + '"]');
        } else {
          el = inner.querySelector('[data-msg-id="' + msgOrIdOrIdx + '"]');
        }
      } else if (msgOrIdOrIdx && typeof msgOrIdOrIdx === 'object') {
        const conv = _findConv(convId);
        const idx = _idxOf(conv, msgOrIdOrIdx);
        el = _findMsgEl(inner, msgOrIdOrIdx, idx);
      }
      if (!el) return false;
      el.remove();
      return true;
    },

    /** Drop every DOM element after the given message index.
     *  Delegates to the existing surgical primitive in ui.js so the
     *  stream-buffer + turn-nav + fingerprint cleanup stay
     *  authoritative.  Falls back to a full renderChat() when the
     *  surgical primitive is unavailable.
     *
     *  @param {string} convId
     *  @param {number} cutoffIdx — keep messages 0..cutoffIdx
     *  @returns {boolean} true if any DOM was removed.
     */
    removeAfter: function (convId, cutoffIdx) {
      const conv = _findConv(convId);
      if (!conv) return false;
      if (typeof activeConvId !== 'undefined' && activeConvId !== convId) {
        /* Inactive conv: nothing to mutate. Caller already updated
         * conv.messages; the next renderChat will pick it up. */
        return false;
      }
      if (typeof _surgicalTruncateDOM === 'function') {
        const ok = _surgicalTruncateDOM(conv, cutoffIdx);
        if (ok) return true;
      }
      if (typeof renderChat === 'function') {
        renderChat(conv);
        return true;
      }
      return false;
    },

    /** Wholesale re-render of the active conversation.  Equivalent
     *  to the existing renderChat() but funnelled through this
     *  controller so future keyed-diff implementations can be
     *  swapped in without touching every caller. */
    replaceAll: function (convId) {
      const conv = _findConv(convId);
      if (!conv) return false;
      if (typeof activeConvId !== 'undefined' && activeConvId !== convId) return false;
      if (typeof renderChat !== 'function') return false;
      renderChat(conv);
      return true;
    },

    /** Stand up the live `#streaming-msg` bubble as an IDENTITY-KEYED insert.
     *
     *  The single seam for creating the streaming bubble (connectToTask
     *  reconnect + showStreamingUIForConv). Enforces the
     *  "one _msgId ⇒ at most one DOM node" invariant at the INSERT boundary:
     *  before inserting, `_evictByMsgId` removes any node bound to this
     *  `msgId` — a static `msg-N` stranded at a DRIFTED index (which the old
     *  `getElementById('msg-'+lastIdx)` eviction missed) OR a stale
     *  `#streaming-msg`. This is the structural fix for the render duplicate
     *  ("one entry in the data, two identical bubbles on screen").
     *
     *  @param {string} convId
     *  @param {Object} [opts]
     *    @param {'worker'|'planner'|'critic'|'autopilot'} [opts.role] - default 'worker'
     *    @param {string} [opts.status]  - status text shown inside the pulse
     *    @param {string} [opts.timeStr] - formatted time string
     *    @param {string} [opts.msgId]   - data-msg-id to stamp + dedupe on
     *  @returns {boolean} true if a bubble was inserted.
     */
    startStreaming: function (convId, opts) {
      opts = opts || {};
      if (typeof activeConvId !== 'undefined' && activeConvId !== convId) return false;
      const inner = document.getElementById('chatInner');
      if (!inner) return false;
      if (typeof _streamingBubbleHTML !== 'function') return false;
      /* Identity eviction: any node already bound to this msgId (drifted
       * static bubble or a stale streaming bubble) is removed so the fresh
       * insert is the SOLE node for it. */
      if (opts.msgId) _evictByMsgId(inner, opts.msgId, null);
      /* There is only ever one live bubble — drop any leftover #streaming-msg
       * singleton even when it carries no / a different msgId. */
      const oldSm = document.getElementById('streaming-msg');
      if (oldSm) { try { oldSm.remove(); } catch (e) { /* detached */ } }
      inner.insertAdjacentHTML('beforeend',
        _streamingBubbleHTML(opts.role || 'worker', opts.status || null,
                             opts.timeStr || null, opts.msgId || null));
      return true;
    },

    /** Replace the live `#streaming-msg` bubble with a final-rendered
     *  message.  Used at three high-bug-density sites:
     *    • main.js startAssistantResponse error fallback
     *    • ui.js finishStream main path
     *    • ui.js endpoint_iteration "entering critic" finalize-worker
     *
     *  Preserves chatContainer scroll position (the streaming bubble
     *  shows expanded thinking; the final renderMessage usually
     *  collapses it, so a naive outerHTML swap visibly jumps).
     *
     *  @param {string} convId
     *  @param {Object} msg          — the assistant message that was streaming
     *  @param {Object} [opts]
     *    @param {boolean} [opts.removeIfTruncated=true] - if msg is no
     *                       longer in conv.messages (truncated by Edit/Regen
     *                       during finishStream), just remove the bubble.
     *  @returns {boolean} true if a swap or removal happened.
     */
    finalizeStreaming: function (convId, msg, opts) {
      opts = opts || {};
      if (typeof activeConvId !== 'undefined' && activeConvId !== convId) return false;
      const conv = _findConv(convId);
      if (!conv) return false;
      const sm = document.getElementById('streaming-msg');
      if (!sm) return false;
      /* ★ Defensive guard: #streaming-msg always belongs to an
       * assistant turn (worker / planner / critic).  If callers pass
       * a non-assistant message (e.g. an autopilot VU user msg pushed
       * at the tail by _handleAutopilotVuEvent before finishStream
       * fires), stamping its HTML onto the streaming bubble's slot
       * silently corrupts the DOM and hides the real assistant.
       * Refuse the swap and let the caller decide what to do. */
      if (msg && msg.role && msg.role !== 'assistant'
          && !msg._isEndpointReview && !msg._isVirtualUser) {
        console.warn(
          '[ConvView] finalizeStreaming refused — msg.role=%s ' +
          '(_isVirtualUser=%s _msgId=%s) is not an assistant; ' +
          'caller should walk back to the parent assistant turn.',
          msg.role, !!msg._isVirtualUser,
          (msg._msgId || '').slice(0, 12));
        return false;
      }
      const idx = msg ? _idxOf(conv, msg) : -1;
      const removeIfMissing = opts.removeIfTruncated !== false;
      if (idx < 0) {
        if (removeIfMissing) {
          try { sm.remove(); } catch (e) { /* already detached */ }
          return true;
        }
        return false;
      }
      if (typeof renderMessage !== 'function') return false;
      const html = renderMessage(msg, idx);
      if (!html) return false;
      const ct = document.getElementById('chatContainer');
      const savedScroll = ct ? ct.scrollTop : -1;
      try {
        sm.outerHTML = html;
      } catch (e) {
        console.error('[ConvView] finalizeStreaming outerHTML failed:', e && e.message);
        return false;
      }
      /* ★ Identity sweep (belt-and-suspenders). The swap just turned
       *   #streaming-msg into a static `msg-${idx}` node. If a STALE twin for
       *   the same _msgId was stranded in the DOM (a drifted static bubble the
       *   insert path failed to evict), it would now coexist with the
       *   finalized node — the exact "two identical bubbles, one data entry"
       *   render duplicate. Evict every OTHER node carrying this msgId, keeping
       *   only the just-finalized one (found by its stable msg-${idx} id). This
       *   makes the invariant hold no matter which path inserted the twin. */
      if (msg && msg._msgId) {
        const _inner = document.getElementById('chatInner');
        const _keep = document.getElementById('msg-' + idx);
        _evictByMsgId(_inner, msg._msgId, _keep);
      }
      if (savedScroll >= 0 && ct) ct.scrollTop = savedScroll;
      if (typeof _lastRenderedFingerprint !== 'undefined' &&
          typeof _convRenderFingerprint === 'function') {
        try { _lastRenderedFingerprint = _convRenderFingerprint(conv); }
        catch (e) { /* ignore */ }
      }
      return true;
    },
  };

  /* Expose globally — concatenated bundle has no module system. */
  window.ConvView = ConvView;
})();
