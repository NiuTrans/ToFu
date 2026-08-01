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
      /* The live #streaming-msg is NEVER a stray twin — its lifecycle is
       * owned by startStreaming/finalizeStreaming (Phase 3.5 step 3 ②).
       * startStreaming removes a stale live bubble explicitly, so exempting
       * it here cannot strand one. */
      if (el.id === 'streaming-msg') return;
      try { el.remove(); removed++; } catch (e) { /* already detached */ }
    });
    return removed;
  }

  /** Tail-insert through the shared ordered-insert primitive
   *  (core/chatinner_dom.js). Kept as a thin local wrapper so both call sites
   *  read the same and a missing primitive degrades LOUDLY once rather than
   *  silently reintroducing the raw `beforeend` bug.
   *
   *  The primitive is a leaf module loaded before conv_view.js by
   *  lib/js_bundler.py (order pinned by
   *  tests/test_frontend_lazy_sentinel_anchor.py), so the fallback is a
   *  build-order canary, not an expected path. */
  let _cvInsertWarned = false;
  function _cvInsert(inner, html, position, conv, site) {
    if (typeof chatInnerInsert === 'function') {
      return chatInnerInsert(inner, html, {
        position: position || 'tail',
        /* conv is passed EXPLICITLY (never resolved from a global inside the
         * primitive) so the order invariant can run at the chokepoint and
         * name THIS call site rather than only renderChat. */
        conv: conv || null,
        site: site || 'ConvView',
      });
    }
    if (!_cvInsertWarned) {
      _cvInsertWarned = true;
      console.warn('[ConvView] chatInnerInsert UNAVAILABLE — falling back to a ' +
        'raw beforeend append. core/chatinner_dom.js must load before ' +
        'conv_view.js in lib/js_bundler.py _BUNDLE_FILES; without it a bottom ' +
        'lazy-window sentinel will sort above newly sent messages.');
    }
    inner.insertAdjacentHTML('beforeend', html);
    return inner.lastElementChild;
  }

  /* Ship a ConvView forensic line to the server over the EXISTING
   * client-error beacon (Api.clientError.report → POST /api/client-error →
   * app.log) — the same channel chatinner_dom.js's order-violation report
   * rides. A bare console line is invisible to the user and lost on reload;
   * the live-task twin guard's whole point is that the NEXT recurrence
   * names its writer in the server log. Fire-and-forget; a transport
   * failure leaves the console line as the last resort. Never throws. */
  function _beaconConvView(msg, extra) {
    try {
      if (typeof Api === 'undefined' || !Api.clientError ||
          typeof Api.clientError.report !== 'function') return false;
      Api.clientError.report({
        message: String(msg).slice(0, 2000),
        url: (typeof location !== 'undefined' && location.href) || '',
        extra: extra || {},
      });
      return true;
    } catch (e) {
      return false;
    }
  }

  /* ── Public API ──────────────────────────────────────────────────── */

  const ConvView = {
    /** Resolve the DOM node for a message — IDENTITY FIRST.
     *
     *  The public face of the private `_findMsgEl`: `data-msg-id` (stable
     *  across index drift) wins, and the positional `msg-${idx}` handle is a
     *  LEGACY fallback for messages that carry no `_msgId` yet.
     *
     *  WHY THIS IS PUBLIC: callers outside this module used to hand-roll
     *  `document.getElementById('msg-' + idx)`, which silently resolves to a
     *  DIFFERENT message whenever `conv.messages` changed length without a
     *  repaint (a poll/merge/peer reconcile). `main_regen_continue.js`'s
     *  Continue shell did exactly that and converted a HISTORICAL answer into
     *  the live `Continuing…` bubble. The rule was already written down twice
     *  (here and in `chat_render.js::_reconcileFindEl`) but had no reusable
     *  seam — so exporting it is the root fix, not a third copy.
     *
     *  @param {Object} msg   - the message (its `_msgId` is the key).
     *  @param {number} [idx] - legacy positional fallback index.
     *  @returns {Element|null}
     */
    findMessageEl: function (msg, idx) {
      return _findMsgEl(document.getElementById('chatInner'), msg, idx);
    },

    /** THE single public DOM-apply entry (RENDER_CONTRACT Phase 3.5 §5).
     *
     *  Every CONTENT-DERIVED write to #chatInner routes through here so the
     *  rendered DOM is a pure projection of the message document:
     *  `renderMessage(msg, idx)` is the ONE projection; `_evictByMsgId`
     *  enforces the identity invariant (one _msgId ⇒ at most one DOM node);
     *  the fingerprint cache is refreshed so the next renderChat does not
     *  needlessly re-swap.
     *
     *  Semantics = upsert keyed on identity: replace the existing node
     *  IN PLACE when found (position preserved — the translate/edit path),
     *  append to the tail otherwise (the send/error-bubble path; suppress
     *  with opts.append === false). Callers whose target may be
     *  mid-index-drift MUST existence-check first and fall back to
     *  renderChat (see _renderMsgInPlace) — an append lands at the tail,
     *  which is wrong for a drifted mid-list message.
     *
     *  @param {string} convId
     *  @param {number} idx     — index into conv.messages (renderMessage key)
     *  @param {Object} msg     — the message object (must carry _msgId;
     *                            _ensureMsgId stamps one when missing).
     *  @param {Object} [opts]
     *    @param {boolean} [opts.append=true] - set false for replace-only
     *                            (legacy upsertMessage semantics).
     *  @returns {boolean} true if the DOM was mutated.
     */
    apply: function (convId, idx, msg, opts) {
      opts = opts || {};
      if (typeof activeConvId !== 'undefined' && activeConvId !== convId) return false;
      const conv = _findConv(convId);
      if (!conv || !msg) return false;
      if (typeof _ensureMsgId === 'function') _ensureMsgId(msg);
      const inner = document.getElementById('chatInner');
      if (!inner) return false;
      if (typeof idx !== 'number' || idx < 0) idx = _idxOf(conv, msg);
      if (idx < 0) return false;
      if (typeof renderMessage !== 'function') return false;
      const html = renderMessage(msg, idx);
      if (!html) return false;
      const existing = _findMsgEl(inner, msg, idx);
      /* ★ LIVE-BUBBLE GUARD (step 3 ②): the resolved target IS (or sits
       * inside) the live #streaming-msg — replacing it would wipe the live
       * zones (tailEl / tool panel) mid-stream. Per-round auto-translate
       * completes while the turn is STILL streaming, so this is not
       * hypothetical. The live lifecycle belongs to
       * startStreaming/finalizeStreaming — refuse loudly. */
      if (existing && (existing.id === 'streaming-msg' ||
          (typeof existing.closest === 'function' &&
           existing.closest('#streaming-msg')))) {
        console.warn('[ConvView] apply REFUSED — target is the live ' +
          'streaming bubble (msgId=' + (msg._msgId || '').slice(0, 12) +
          ' idx=' + idx + '); use startStreaming/finalizeStreaming for ' +
          'the live lifecycle.');
        return false;
      }
      if (existing) {
        existing.outerHTML = html;
      } else if (opts.append === false) {
        return false;
      } else {
        /* ★ LIVE-TASK TWIN GUARD (2026-07-31, conv ms8nqhwgur07jm): the msgId
         * keyed guard above only fires when `_findMsgEl` RESOLVES this
         * message's _msgId to the live #streaming-msg. If the id DRIFTED
         * (re-minted / server-adopted copy / bubble stamped before the id
         * existed), resolution misses and the append below lands a STATIC
         * renderMessage node — the exact "empty provenance-strip + model-pill
         * bubble alongside the live bubble" duplicate observed in production
         * on a zero-token retry-storm turn. A message OBJECT that the live
         * stream is actively accumulating into (object identity, not id
         * equality) must NEVER gain a static twin: its DOM is the live
         * bubble, full stop. Refuse loudly with a stack so the caller is
         * named — the next recurrence identifies its trigger instead of
         * silently twinning. Only the APPEND branch is gated (a resolved
         * replace-in-place above is always the correct write). */
        if (typeof activeStreams !== 'undefined') {
          const _stream = activeStreams.get(convId);
          if (_stream && _stream.assistantMsg
              && document.getElementById('streaming-msg')) {
            if (msg === _stream.assistantMsg) {
              const _refuseMsg = '[ConvView] apply REFUSED (live-task twin) — the ' +
                'message being statically appended IS the object the live ' +
                'stream is bound to (msgId=' + (msg._msgId || '').slice(0, 12) +
                ' idx=' + idx + ' conv=' + String(convId).slice(0, 8) + '). ' +
                'A static twin of the live bubble must never be appended; ' +
                'route live-turn updates through updateStreamingUI / ' +
                'startStreaming / finalizeStreaming.\n' +
                (new Error('live-task twin trace')).stack;
              console.error(_refuseMsg);
              _beaconConvView(_refuseMsg, {
                site: 'ConvView.apply', kind: 'live-task-twin-refused',
                convId: String(convId), taskId: String(_stream.taskId || ''),
                msgId: String(msg._msgId || ''), idx: idx,
              });
              return false;
            }
            if (msg._taskId && _stream.taskId && msg._taskId === _stream.taskId) {
              /* Same task, DIFFERENT object (server-adopted copy / splice
               * survivor): appending it alongside the live bubble twins the
               * turn. Not refused (endpoint/VU lanes legitimately re-apply
               * settled messages of the live task) — but logged with a stack
               * so the drift is diagnosable from the client-error report. */
              const _driftMsg = '[ConvView] apply appending a NON-bound object of ' +
                'the LIVE task (msgId=' + (msg._msgId || '').slice(0, 12) +
                ' taskId=' + String(msg._taskId).slice(0, 8) + ' conv=' +
                String(convId).slice(0, 8) + ') — twin risk; if a duplicate ' +
                'bubble appears, this trace names the writer.\n' +
                (new Error('same-task drift trace')).stack;
              console.warn(_driftMsg);
              _beaconConvView(_driftMsg, {
                site: 'ConvView.apply', kind: 'same-task-drift-warn',
                convId: String(convId), taskId: String(msg._taskId || ''),
                msgId: String(msg._msgId || ''), idx: idx,
              });
            }
          }
        }
        /* ★ ORDER-INVARIANT LOUD WARN (step 3 ③a): appending a message that
         * is NOT the tail means its DOM node could not be found at its
         * position — index drift. The append lands at the tail and the DOM
         * order silently diverges from conv.messages. Say so loudly; callers
         * that expect drift must existence-check first and fall back to
         * renderChat instead. */
        const _total = (conv.messages && conv.messages.length) || 0;
        if (idx < _total - 1) {
          console.warn('[ConvView] apply appending a MID-LIST message ' +
            '(idx=' + idx + ' of ' + _total + ', msgId=' +
            (msg._msgId || '').slice(0, 12) + ') — DOM order may drift ' +
            'from conv.messages; existence-check or renderChat instead.');
        }
        /* Tail insert via the ONE ordered-insert primitive: a raw
         * `beforeend` lands AFTER `#_lazyLoadSentinelBottom` when a lazy
         * window has evicted the tail, and `_loadNewerMessages` then splices
         * the recovered messages ABOVE this one. `chatInnerInsert` steps over
         * that furniture. With no bottom sentinel it is a plain append —
         * byte-identical to the old behaviour. */
        _cvInsert(inner, html, 'tail', conv, 'ConvView.apply');
      }
      /* Identity sweep (ALL paths — step 3 ①): the swap/insert is the SOLE
       * node for this _msgId — evict any stranded twin (drifted static
       * bubble, stale placeholder) so the invariant holds no matter which
       * path created it. The live #streaming-msg is exempt inside
       * _evictByMsgId. */
      if (msg._msgId) {
        const keep = document.getElementById('msg-' + idx);
        _evictByMsgId(inner, msg._msgId, keep);
      }
      if (typeof _lastRenderedFingerprint !== 'undefined' &&
          typeof _convRenderFingerprint === 'function') {
        try { _lastRenderedFingerprint = _convRenderFingerprint(conv); }
        catch (e) { /* best-effort cache update */ }
      }
      return true;
    },

    /** Legacy alias of apply() (step 3 ① — the collapse).
     *
     *  One implementation, two call shapes: this preserves the pre-collapse
     *  upsertMessage semantics — replace-in-place when the node exists,
     *  append ONLY when opts.append === true (default false). The identity
     *  sweep now runs on this path too (idempotent when no twin — pure
     *  gain). New code should call apply() directly.
     *
     *  @param {string} convId
     *  @param {Object} msg     — the message object (must have _msgId).
     *  @param {Object} [opts]
     *    @param {number} [opts.idx]      - explicit idx; otherwise computed
     *    @param {boolean} [opts.append=false] - append when no existing node
     *  @returns {boolean} true if the DOM was mutated.
     */
    upsertMessage: function (convId, msg, opts) {
      opts = opts || {};
      const conv = _findConv(convId);
      if (!conv || !msg) return false;
      const idx = (typeof opts.idx === 'number') ? opts.idx : _idxOf(conv, msg);
      if (idx < 0) return false;
      return ConvView.apply(convId, idx, msg, { append: !!opts.append });
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

    /** Wholesale re-render of the active conversation — THE public
     *  full-repaint entry (RENDER_CONTRACT Phase 3.5 §5 step 4, the SEAM-2
     *  fold). `renderChat` (chat_render.js) is this seam's reconcile ENGINE
     *  — its raw writes are the projection implementation, not a second
     *  public entry — so other modules' full repaints route through this
     *  method instead of calling renderChat directly. opts.forceScroll is
     *  forwarded to the engine with its scroll/anchor semantics unchanged. */
    replaceAll: function (convId, opts) {
      const conv = _findConv(convId);
      if (!conv) return false;
      if (typeof activeConvId !== 'undefined' && activeConvId !== convId) return false;
      if (typeof renderChat !== 'function') return false;
      renderChat(conv, opts && opts.forceScroll);
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
      /* Same furniture-aware tail insert as `apply` — the live bubble is the
       * newest node and must sit ABOVE any bottom sentinel. */
      _cvInsert(inner,
        _streamingBubbleHTML(opts.role || 'worker', opts.status || null,
                             opts.timeStr || null, opts.msgId || null),
        'tail', _findConv(convId), 'ConvView.startStreaming');
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
      /* ★ JUMP FIX: decide the target BEFORE the swap. A reader parked at the
       *   bottom (the common case at turn end) should stay pinned to the bottom;
       *   otherwise we hold their exact offset. Measured on the OLD DOM so the
       *   streaming-bubble geometry is what we compare against. */
      const _wasNearBottom = (ct && typeof isNearBottom === 'function')
        ? isNearBottom(80) : false;
      /* ★ Swap via replaceWith, NOT `sm.outerHTML = html`.
       *
       *   `outerHTML` ORPHANS `sm`, leaving no handle on the node just written
       *   — which is the ONLY reason the identity sweep below used to re-find it
       *   by `getElementById('msg-' + idx)`. That re-find is UNSAFE: straight
       *   after the swap the positional id can match TWO nodes (the fresh one
       *   plus a stale bubble still sitting at that slot after an index shift),
       *   and `getElementById` returns the FIRST — so the sweep kept the WRONG
       *   node and evicted the one it had just restored.
       *
       *   Observed (owner, 2026-07-29): a FAILED Continue rolled the shell back
       *   and then deleted it, so the interrupted turn VANISHED from the screen
       *   while the toast still offered a retry — nothing left to retry. Same
       *   root cause as the Continue shell's entry-side positional lookup: one
       *   end keyed on identity, the other on array position.
       *
       *   Parsing into a detached container and calling `replaceWith` keeps the
       *   node BY REFERENCE, so the keep-node is right no matter how stale
       *   `idx` is. */
      let _finalEl = null;
      try {
        const _holder = document.createElement('div');
        _holder.innerHTML = html;
        _finalEl = _holder.firstElementChild;
        if (!_finalEl) {
          console.error('[ConvView] finalizeStreaming: renderMessage produced no '
            + 'element for msgId=' + (msg._msgId || '').slice(0, 12));
          return false;
        }
        sm.replaceWith(_finalEl);
      } catch (e) {
        console.error('[ConvView] finalizeStreaming swap failed:', e && e.message);
        return false;
      }
      /* ★ Identity sweep (belt-and-suspenders). The swap just turned
       *   #streaming-msg into a static `msg-${idx}` node. If a STALE twin for
       *   the same _msgId was stranded in the DOM (a drifted static bubble the
       *   insert path failed to evict), it would now coexist with the
       *   finalized node — the exact "two identical bubbles, one data entry"
       *   render duplicate. Evict every OTHER node carrying this msgId, keeping
       *   the node we just inserted BY REFERENCE — never by positional id (see
       *   the swap note above for what that cost). Holds no matter which path
       *   inserted the twin, and no matter whether `idx` is fresh. */
      if (msg && msg._msgId) {
        const _inner = document.getElementById('chatInner');
        _evictByMsgId(_inner, msg._msgId, _finalEl);
      }
      /* ★ JUMP FIX — two parts:
       *   (1) The final `renderMessage` collapses the thinking block, drops the
       *       phase indicator and re-runs syntax highlighting, so the finalized
       *       node is a DIFFERENT height than the streaming bubble. Restoring the
       *       raw pre-swap scrollTop therefore visually shifts content. Instead:
       *       if the reader was at the bottom, re-pin to the bottom; else hold
       *       their offset. Write with smooth OFF (via _withInstantScroll) so the
       *       chat-container's `scroll-behavior:smooth` does not ANIMATE the snap
       *       — the animated slide is the "莫名跳动" the user sees.
       *   (2) hljs highlighting and (lazy) KaTeX typesetting change block heights
       *       AFTER this synchronous pass, so a scroll set now is stale the moment
       *       they land. Re-apply the same target on the next two frames (rAF²) so
       *       the final position is taken AFTER layout settles — killing the
       *       "定位完再变高" second jump. */
      const _repin = () => {
        if (!ct) return;
        const _apply = () => {
          if (_wasNearBottom) ct.scrollTop = ct.scrollHeight;
          else if (savedScroll >= 0) ct.scrollTop = savedScroll;
        };
        if (typeof _withInstantScroll === 'function') _withInstantScroll(ct, _apply);
        else _apply();
      };
      _repin();
      if (typeof requestAnimationFrame === 'function') {
        requestAnimationFrame(() => requestAnimationFrame(_repin));
      }
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
