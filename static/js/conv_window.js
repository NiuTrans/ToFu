/* ═══════════════════════════════════════════════════════════════════
   conv_window.js — client half of windowed conversation reads

   Backend (routes/conversations.py get_conv, gated on TOFU_MESSAGES_ROWS_READD
   + a `window` query param) can serve only the TAIL N messages of a long
   conversation from the normalized conversation_messages row store, so
   first-open cost is O(window) not O(history). The response then carries a
   pagination envelope:

       { windowed:true, totalCount, firstLoadedSeq, lastLoadedSeq, hasMore,
         messages:[tail N] }

   This module owns the client side of that contract WITHOUT touching the
   render core:
     • windowParam()        — the ?window=N to request (0/absent = full load).
     • recordWindowState()  — stamp pagination state from a windowed response
                              onto the conv (no-op for a legacy full response).
     • loadEarlier()        — fetch the previous window (before_seq cursor) and
                              PREPEND it to conv.messages, preserving the scroll
                              anchor; re-renders the open conv.
     • wireScrollUpLoader() — attach a scroll-to-top sentinel handler.

   HARD INVARIANT (scale-out rollout): when the backend does NOT return
   windowed:true (flag off, or no window param sent), EVERYTHING here is inert —
   conv.messages is the full array exactly as today, no pagination state is set,
   and the scroll handler short-circuits. Single-box default is byte-identical.
   ═══════════════════════════════════════════════════════════════════ */

/* Requested tail size. Defaults to _DEFAULT_CONV_WINDOW so windowed first-open
 *   is ON by default — the backend blob tail-slice path (safe for every conv,
 *   no migration flag) bounds the RESPONSE BODY to this many messages, which is
 *   what cuts the slow first-open of long conversations over the tunnel.
 *   A deployment may override the size via window.TOFU_CONV_WINDOW, or set it
 *   to 0 to explicitly DISABLE windowing (full-blob load, legacy behavior). */
const _DEFAULT_CONV_WINDOW = 60;
function _convWindowSize() {
  const raw = (typeof window !== 'undefined') ? /** @type {any} */ (window).TOFU_CONV_WINDOW : undefined;
  /* Explicit 0 / '0' → disabled (full load). Unset/undefined/null → default. */
  if (raw === 0 || raw === '0') return 0;
  const v = parseInt(raw, 10);
  if (Number.isFinite(v) && v > 0) return v;
  return _DEFAULT_CONV_WINDOW;
}

/* The ?window= value to attach to a first-open GET, or '' to request the full
 *   array (legacy behavior). Exposed so loadConversationMessages can build its
 *   query without knowing the windowing policy. */
function convWindowParam() {
  const n = _convWindowSize();
  return n > 0 ? String(n) : '';
}

/* Stamp pagination state from a GET response onto the conv. Returns true iff
 *   the response was windowed (so the caller knows earlier messages exist and
 *   must NOT treat conv.messages as the complete history — e.g. skip a
 *   "trim to server length" step). A legacy full response → false, no-op. */
function recordWindowState(conv, data) {
  if (!conv || !data || data.windowed !== true) {
    if (conv) conv._windowed = false;
    return false;
  }
  conv._windowed = true;
  conv._totalCount = data.totalCount;
  conv._firstLoadedSeq = data.firstLoadedSeq;
  conv._lastLoadedSeq = data.lastLoadedSeq;
  conv._hasMoreEarlier = !!data.hasMore;
  /* Heavy per-message fields (toolRounds/segments/apiRounds/...) were stripped
   *   for transport (data.trimmed). Note it so the renderer shows a "load tool
   *   activity" affordance and hydrateFullConversation() can refill on demand. */
  conv._trimmed = data.trimmed === true;
  return true;
}

/* Lazy-hydrate a windowed conv's TRIMMED heavy fields (toolRounds / segments /
 *   apiRounds / _continue*) by fetching the FULL, un-windowed conversation once
 *   and merging the heavy fields back into the in-memory messages by stable
 *   _msgId (fallback index). Called on demand when the user expands a trimmed
 *   message's tool timeline — first-paint stayed tiny, the heavy payload is
 *   pulled only if actually needed. Re-entrant-guarded; repaints on success. */
async function hydrateFullConversation(convId) {
  const conv = (typeof conversations !== 'undefined')
    ? conversations.find((c) => c.id === convId) : null;
  if (!conv || conv._hydratingFull) return false;
  if (!conv._trimmed) return false;  // nothing trimmed → nothing to hydrate
  conv._hydratingFull = true;
  try {
    /* Explicit window=0 → the server serves the FULL untrimmed array. */
    const data = await Api.conversations.get(convId, { query: { window: '0' } });
    if (!data || !Array.isArray(data.messages)) return false;
    const _HEAVY = ['segments', 'toolRounds', 'apiRounds',
                    '_continueToolRounds', '_continueApiRounds', 'toolSummary'];
    const bySrcId = new Map();
    data.messages.forEach((m, i) => {
      if (m && m._msgId) bySrcId.set(m._msgId, m);
    });
    (conv.messages || []).forEach((dst, i) => {
      if (!dst) return;
      const src = (dst._msgId && bySrcId.get(dst._msgId)) || data.messages[i];
      if (!src) return;
      for (const f of _HEAVY) { if (src[f] !== undefined) dst[f] = src[f]; }
      delete dst._trimmed;
      delete dst._trimmedToolRoundCount;
    });
    conv._trimmed = false;
    if (typeof activeConvId !== 'undefined' && activeConvId === convId
        && typeof renderChat === 'function') renderChat(conv, false);
    return true;
  } catch (e) {
    console.warn('[conv-window] hydrateFull failed for %s: %s',
                 convId.slice(0, 8), e && e.message);
    return false;
  } finally {
    conv._hydratingFull = false;
  }
}

/* Whether there are older messages above the loaded window. */
function convHasMoreEarlier(conv) {
  return !!(conv && conv._windowed && conv._hasMoreEarlier);
}

/* Fetch the previous window and PREPEND it. Preserves the scroll anchor so the
 *   viewport does not jump when older content is inserted above. Idempotent /
 *   re-entrant-guarded via conv._loadingEarlier. Returns count prepended. */
async function loadEarlierMessages(convId) {
  const conv = (typeof conversations !== 'undefined')
    ? conversations.find((c) => c.id === convId) : null;
  if (!conv || !convHasMoreEarlier(conv) || conv._loadingEarlier) return 0;
  if (typeof conv._firstLoadedSeq !== 'number') return 0;
  conv._loadingEarlier = true;

  /* Measure on the ACTUAL scroll box. #chatContainer is the overflow-y:auto
   *   element (styles.css: .chat-container{overflow-y:auto}); #chatInner is its
   *   non-scrolling content child (scrollTop always 0, scrollHeight==client).
   *   Measuring/re-pinning on #chatInner was a no-op → viewport jumped to top
   *   on every earlier-page prepend. Match _getChatContainer/isNearBottom. */
  const container = document.getElementById('chatContainer');
  const prevHeight = container ? container.scrollHeight : 0;
  const prevTop = container ? container.scrollTop : 0;

  try {
    const n = _convWindowSize() || 60;
    const data = await Api.conversations.get(convId, {
      query: { window: String(n), before_seq: String(conv._firstLoadedSeq) },
    });
    if (!data || !Array.isArray(data.messages) || data.messages.length === 0) {
      conv._hasMoreEarlier = false;
      return 0;
    }
    const earlier = data.messages;
    conv.messages = earlier.concat(conv.messages || []);
    // advance the cursor + hasMore from this page's envelope
    if (typeof data.firstLoadedSeq === 'number') conv._firstLoadedSeq = data.firstLoadedSeq;
    conv._hasMoreEarlier = !!data.hasMore;
    /* Earlier pages are heavy-field-trimmed too (server before_seq branch). If
     *   any scrolled-in message is trimmed, (re-)arm conv._trimmed so expanding
     *   its tool timeline can still hydrate — even AFTER an earlier
     *   hydrateFullConversation() cleared the flag for the initial tail. */
    if (data.trimmed === true || earlier.some((m) => m && m._trimmed)) {
      conv._trimmed = true;
    }

    if (typeof activeConvId !== 'undefined' && activeConvId === convId
        && typeof renderChat === 'function') {
      renderChat(conv, false);
      // re-pin the scroll anchor: keep the previously-top message in place by
      // restoring scrollTop + (newHeight - oldHeight).
      if (container) {
        const newHeight = container.scrollHeight;
        container.scrollTop = prevTop + (newHeight - prevHeight);
      }
    }
    return earlier.length;
  } catch (e) {
    console.warn('[conv-window] loadEarlier failed for %s: %s', convId.slice(0, 8), e && e.message);
    return 0;
  } finally {
    conv._loadingEarlier = false;
  }
}

/* Attach a scroll-to-top loader on the chat container. When the user scrolls
 *   near the top AND the active conv has earlier messages, fetch+prepend them.
 *   Idempotent; safe to call on every conv open. No-op if not windowed. */
let _scrollUpWired = false;
function wireConvWindowScrollLoader() {
  if (_scrollUpWired) return;
  /* Listen on the actual scroll box (#chatContainer), not its non-scrolling
   *   #chatInner child — the latter never fires a scroll event, so the
   *   scroll-to-top prefetch trigger was dead. */
  const container = document.getElementById('chatContainer');
  if (!container) return;
  _scrollUpWired = true;
  const THRESHOLD = 120;  // px from the top that triggers a prefetch
  container.addEventListener('scroll', () => {
    if (container.scrollTop > THRESHOLD) return;
    const cid = (typeof activeConvId !== 'undefined') ? activeConvId : null;
    if (!cid) return;
    const conv = (typeof conversations !== 'undefined')
      ? conversations.find((c) => c.id === cid) : null;
    if (convHasMoreEarlier(conv) && !conv._loadingEarlier) {
      loadEarlierMessages(cid);
    }
  }, { passive: true });
}

if (typeof window !== 'undefined') {
  window.convWindowParam = convWindowParam;
  window.recordWindowState = recordWindowState;
  window.convHasMoreEarlier = convHasMoreEarlier;
  window.loadEarlierMessages = loadEarlierMessages;
  window.wireConvWindowScrollLoader = wireConvWindowScrollLoader;
  window.hydrateFullConversation = hydrateFullConversation;
}
