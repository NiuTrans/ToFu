/* ═══════════════════════════════════════════════════════════════════
   streaming render — extracted from ui.js (split 2026-05-28)

   Streaming-message rendering: autopilot VU events, surgical DOM updates, lazy-load older messages.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Locate an existing VU streaming message by its stable `_msgId`.
 * Used by `_handleAutopilotVuEvent` to route streaming updates from the
 * parent task's SSE pipe into the right user-message object on the
 * conversation.
 */
function _findVuMsgById(conv, vuMsgId) {
  if (!conv || !Array.isArray(conv.messages) || !vuMsgId) return null;
  for (let i = conv.messages.length - 1; i >= 0; i--) {
    const m = conv.messages[i];
    if (m && m._msgId === vuMsgId && m._isVirtualUser) {
      return { msg: m, idx: i };
    }
  }
  return null;
}

/**
 * Surgically re-render a single message by index.  Used by the
 * autopilot VU streaming pipeline so each delta / tool_start /
 * tool_result update repaints just that bubble — far cheaper than a
 * full `renderChat()` and preserves the parent task's
 * `#streaming-msg` element + scroll position.
 */
function _surgicalRerenderMsg(convId, idx) {
  if (activeConvId !== convId) return;
  const conv = conversations.find(c => c.id === convId);
  if (!conv || !conv.messages || idx < 0 || idx >= conv.messages.length) return;
  const inner = document.getElementById("chatInner");
  if (!inner) return;
  const el = document.getElementById("msg-" + idx);
  const html = renderMessage(conv.messages[idx], idx);
  if (el) {
    el.outerHTML = html;
  } else {
    /* Insert AFTER the parent's #streaming-msg bubble so the natural
     * turn order is preserved: parent_user → parent_assistant
     * (#streaming-msg, still streaming) → VU_user (new) → next
     * assistant placeholder.  When there's no live streaming-msg
     * (e.g. SSE cold replay), append at the end of the chat. */
    const streaming = document.getElementById("streaming-msg");
    if (streaming && streaming.parentNode === inner) {
      streaming.insertAdjacentHTML("afterend", html);
    } else {
      inner.insertAdjacentHTML("beforeend", html);
    }
  }
  if (typeof isNearBottom === "function" && isNearBottom(80)) scrollToBottom();
}

/**
 * Handle the four autopilot_vu_* SSE event types.  See `_processSSELine`
 * for the contract.  Mutates the conversation's local message state and
 * triggers a surgical re-render so the user sees streaming updates.
 */
function _handleAutopilotVuEvent(convId, ev) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) {
    console.debug(`[Autopilot VU] conv=${convId.slice(0,8)} not found — dropping ${ev.type}`);
    return;
  }
  const vuMsgId = ev.vuMsgId;
  if (!vuMsgId) {
    console.warn(`[Autopilot VU] missing vuMsgId on ${ev.type}`, ev);
    return;
  }

  if (ev.type === "autopilot_vu_cancel") {
    /* Remove the placeholder if it has no useful content yet — the VU
     * bailed out (TASK_DONE / aborted / queued real-user msg). */
    const entry = _findVuMsgById(conv, vuMsgId);
    if (!entry) return;
    conv.messages.splice(entry.idx, 1);
    console.info(
      `[Autopilot VU] ⛔ cancel — removed placeholder vuMsgId=${vuMsgId.slice(0,12)} ` +
      `for conv=${convId.slice(0,8)}`
    );
    if (typeof saveConversations === "function") saveConversations(convId);
    try { if (typeof ConvCache !== "undefined") ConvCache.put(conv); }
    catch (e) { /* non-fatal */ }
    if (activeConvId === convId) {
      /* Remove just the VU bubble's DOM node, leave the rest alone
       * (especially the parent's #streaming-msg). */
      const el = document.getElementById("msg-" + entry.idx);
      if (el) el.remove();
      buildTurnNav(conv);
    }
    return;
  }

  if (ev.type === "autopilot_vu_done") {
    /* Replace the placeholder's fields with the authoritative final
     * copy from the backend (content + toolRounds), and clear the
     * streaming decoration so the bubble looks "settled". */
    const entry = _findVuMsgById(conv, vuMsgId);
    if (!entry) {
      /* Shouldn't happen — the start event runs first.  Log + push as
       * a fresh tail message so the user still sees the VU reply. */
      console.warn(
        `[Autopilot VU] ✓ done but no placeholder for vuMsgId=${vuMsgId.slice(0,12)} — ` +
        `appending fresh for conv=${convId.slice(0,8)}`
      );
      const fresh = Object.assign({}, ev.vuMessage || {}, {
        _msgId: vuMsgId, _isVirtualUser: true,
      });
      delete fresh._streamingVu;
      conv.messages.push(fresh);
    } else {
      const finalMsg = ev.vuMessage || {};
      entry.msg.content = finalMsg.content || entry.msg.content || "";
      if (Array.isArray(finalMsg.toolRounds) && finalMsg.toolRounds.length) {
        entry.msg.toolRounds = finalMsg.toolRounds;
      }
      delete entry.msg._streamingVu;
      console.info(
        `[Autopilot VU] ✓ done — finalized vuMsgId=${vuMsgId.slice(0,12)} ` +
        `(${(entry.msg.content||'').length} chars, ${(entry.msg.toolRounds||[]).length} rounds) ` +
        `for conv=${convId.slice(0,8)}`
      );
    }
    if (typeof saveConversations === "function") saveConversations(convId);
    try { if (typeof ConvCache !== "undefined") ConvCache.put(conv); }
    catch (e) { /* non-fatal */ }
    if (activeConvId === convId) {
      const idx = conv.messages.findIndex(m => m && m._msgId === vuMsgId);
      if (idx >= 0) _surgicalRerenderMsg(convId, idx);
    }
    return;
  }

  /* autopilot_vu_event — route the inner event into the VU bubble.
   * LAZY CREATION: the VU bubble is NOT pre-created by the backend.
   * We create it in-memory the first time a content-bearing event
   * arrives (delta with text, or tool_start).  Phase-only events
   * do NOT trigger creation so the user doesn't see an empty bubble
   * while the VU is still warming up.  DB persistence only happens
   * when the backend sends autopilot_vu_done (success path). */
  const inner = ev.inner || {};
  const itype = inner.type || "";

  let entry = _findVuMsgById(conv, vuMsgId);
  if (!entry) {
    /* Decide whether this event is "content-bearing" enough to warrant
     * creating the VU bubble.  Only delta-with-text and tool_start
     * qualify.  Everything else (phase, tool_result for a round we
     * don't have yet, etc.) is silently dropped — these are either
     * redundant with the parent-stream chip or will arrive again after
     * creation. */
    const _isContentBearing =
      (itype === "tool_start") ||
      (itype === "delta" && (inner.content || inner.thinking));
    if (!_isContentBearing) {
      return; // silently skip — nothing to show yet
    }
    /* Create the VU bubble in-memory. */
    const vuNew = {
      role: "user",
      content: "",
      _msgId: vuMsgId,
      _isVirtualUser: true,
      _streamingVu: true,
      toolRounds: [],
    };
    conv.messages.push(vuNew);
    entry = { msg: vuNew, idx: conv.messages.length - 1 };
    console.info(
      `[Autopilot VU] ▶ lazy-created VU bubble vuMsgId=${vuMsgId.slice(0,12)} ` +
      `at idx=${entry.idx} on inner=${itype} for conv=${convId.slice(0,8)}`
    );
  }
  const vuMsg = entry.msg;

  if (itype === "delta") {
    if (inner.content) vuMsg.content = (vuMsg.content || "") + inner.content;
    if (inner.thinking) vuMsg.thinking = (vuMsg.thinking || "") + inner.thinking;
  } else if (itype === "tool_start") {
    if (!Array.isArray(vuMsg.toolRounds)) vuMsg.toolRounds = [];
    vuMsg.toolRounds.push({
      roundNum: inner.roundNum,
      query: inner.query,
      results: null,
      status: "searching",
      toolName: inner.toolName || null,
      toolCallId: inner.toolCallId || null,
      toolArgs: inner.toolArgs || null,
      llmRound: inner.llmRound != null ? inner.llmRound : null,
      _swarm: false,
    });
  } else if (itype === "tool_result") {
    if (Array.isArray(vuMsg.toolRounds)) {
      const r = vuMsg.toolRounds.find(rr => rr.roundNum === inner.roundNum);
      if (r) {
        r.results = inner.results;
        r.status = "done";
        if (inner.searchDiag) r.searchDiag = inner.searchDiag;
        if (inner.engineBreakdown) r.engineBreakdown = inner.engineBreakdown;
      }
    }
  } else if (itype === "tool_progress") {
    if (Array.isArray(vuMsg.toolRounds)) {
      const r = vuMsg.toolRounds.find(rr => rr.roundNum === inner.roundNum);
      if (r) {
        if (typeof r._partialOutput !== "string") r._partialOutput = "";
        r._partialOutput += (inner.chunk || "");
      }
    }
  } else if (itype === "tool_complete") {
    if (Array.isArray(vuMsg.toolRounds)) {
      const r = vuMsg.toolRounds.find(rr =>
        rr.roundNum === inner.roundNum && rr.toolCallId === inner.toolCallId);
      if (r) {
        r.toolContent = inner.toolContent || null;
        if (inner.toolTokens != null) r.toolTokens = inner.toolTokens;
        if (inner.compactionLayer) {
          r.compactionLayer = inner.compactionLayer;
          r.compactedFromChars = inner.compactedFromChars;
          r.compactedToChars = inner.compactedToChars;
        }
      }
    }
  } else if (itype === "tool_compacted") {
    if (Array.isArray(vuMsg.toolRounds)) {
      const r = vuMsg.toolRounds.find(rr => rr.toolCallId === inner.toolCallId);
      if (r) {
        r.compactionLayer = inner.compactionLayer || r.compactionLayer || "L1";
        if (inner.compactedFromChars != null) r.compactedFromChars = inner.compactedFromChars;
        if (inner.compactedToChars != null) r.compactedToChars = inner.compactedToChars;
        if (inner.toolTokens != null) r.toolTokens = inner.toolTokens;
      }
    }
  } else if (itype === "phase") {
    /* Phase events on the VU sub-task are not surfaced in the VU
     * bubble itself — the parent already shows an
     * `autopilot_thinking` chip on its worker bubble.  Skip the
     * single-message re-render so we don't repaint the VU bubble for
     * every phase tick. */
    return;
  } else {
    /* stdin_request / stdin_resolved / write_approval_request /
     * human_guidance_request / human_guidance_response — the VU
     * bubble doesn't host interactive widgets (the VU IS the user),
     * so we just log and ignore. */
    console.debug(
      `[Autopilot VU] inner type=${itype} not surfaced in VU bubble for ` +
      `conv=${convId.slice(0,8)} (vuMsgId=${vuMsgId.slice(0,12)})`
    );
    return;
  }

  /* ★ Surgical single-message re-render — much cheaper than full
   * renderChat() and avoids touching the parent's `#streaming-msg`. */
  if (activeConvId === convId) {
    _surgicalRerenderMsg(convId, entry.idx);
  }
}

/**
 * Build the HTML string for a streaming bubble (#streaming-msg).
 * @param {'worker'|'planner'|'critic'} role  — which phase / avatar
 * @param {string} [status]   — status text shown inside the pulse
 * @param {string} [timeStr]  — formatted time string (defaults to now)
 * @returns {string} HTML string
 */
function _streamingBubbleHTML(role, status, timeStr, msgId) {
  const _cfg = {
    worker:  { avatar: (typeof _TOFU_WORKER_SVG  !== 'undefined') ? _TOFU_WORKER_SVG  : '✦', label: 'Agent',   cls: 'ep-worker-msg',  defaultStatus: 'Preparing...' },
    planner: { avatar: (typeof _TOFU_PLANNER_SVG !== 'undefined') ? _TOFU_PLANNER_SVG : '✦', label: 'Planner', cls: 'ep-planner-msg', defaultStatus: 'Planning…' },
    critic:  { avatar: (typeof _TOFU_CRITIC_SVG  !== 'undefined') ? _TOFU_CRITIC_SVG  : '✦', label: 'Critic',  cls: 'ep-critic-msg',  defaultStatus: 'Reviewing…' },
  };
  const c = _cfg[role] || _cfg.worker;
  const st = status || c.defaultStatus;
  const tm = timeStr || new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const extraCls = role === 'critic' ? ' user-msg' : '';
  /* safeHtml auto-escapes every interpolation. The avatar is trusted
   * hardcoded SVG/img markup (settings/branding.js) so it is wrapped in
   * raw(); msgId (caller-supplied) is escaped automatically. Returns a
   * _SafeHtmlRaw whose toString() yields the HTML string the
   * insertAdjacentHTML call sites expect. */
  const dataMsgId = msgId ? raw(` data-msg-id="${escapeHtml(msgId)}"`) : '';
  return String(safeHtml`<div class="message${extraCls} ${raw(c.cls)}" id="streaming-msg"${dataMsgId}><div class="message-avatar">${raw(c.avatar)}</div><div class="message-content"><div class="message-header"><span class="message-role">${c.label}</span><span class="message-time">${tm}</span><span id="stream-elapsed-timer" class="stream-elapsed-timer"></span></div><div class="message-body" id="streaming-body"><div class="stream-status"><div class="pulse"></div> ${st}</div></div></div></div>`);
}

/**
 * Determine streaming bubble role from config / conversation state.
 * @param {Object} conv
 * @param {Object} [cfg] — sendConfig / regenConfig with endpointMode flag
 * @returns {'worker'|'planner'}
 */
function _streamingBubbleRole(conv, cfg) {
  if (cfg && cfg.endpointMode) return 'planner';
  if (conv && conv.endpointEnabled && !conv.messages.some(m => m._epIteration)) return 'planner';
  return 'worker';
}

/**
 * Surgically remove DOM elements for messages with index > cutoffIdx,
 * plus any leftover #streaming-msg / #translating-msg / stale endpoint bubbles.
 * Updates fingerprint and turn nav.
 *
 * Also wipes the conv's streamBufs entry when the streaming bubble is removed,
 * so a stale SSE callback can't keep accumulating into a now-detached buffer.
 *
 * @param {Object} conv
 * @param {number} cutoffIdx — keep messages 0..cutoffIdx, remove cutoffIdx+1..
 * @returns {boolean} true if surgical path was used
 */
function _surgicalTruncateDOM(conv, cutoffIdx) {
  if (activeConvId !== conv.id) return false;
  const inner = document.getElementById("chatInner");
  if (!inner) return false;
  const toRemove = [];
  inner.querySelectorAll('.message[id^="msg-"]').forEach(el => {
    const m = el.id.match(/^msg-(\d+)$/);
    if (m && parseInt(m[1], 10) > cutoffIdx) toRemove.push(el);
  });
  const oldStreaming = document.getElementById("streaming-msg");
  if (oldStreaming) toRemove.push(oldStreaming);
  // ★ SyncFix: also evict translating bubble and any orphan endpoint bubbles
  //   that may have been inserted without a msg-N id (critic/planner/worker
  //   rendered directly by SSE reconnection paths).
  const translating = document.getElementById("translating-msg");
  if (translating) toRemove.push(translating);
  inner.querySelectorAll('.message.ep-critic-msg, .message.ep-worker-msg, .message.ep-planner-msg').forEach(el => {
    if (!el.id || !el.id.startsWith('msg-')) {
      // Orphan role-styled message without msg-N id — leftover from a
      // prior streaming/reconnect render. Safe to remove.
      toRemove.push(el);
    }
  });
  if (toRemove.length > 0 || inner.querySelector('.message[id^="msg-"]')) {
    const removedStreaming = toRemove.includes(oldStreaming) || toRemove.some(el => el === translating);
    for (const el of toRemove) el.remove();
    // ★ SyncFix: wipe stream buffer so a still-alive SSE closure (for the
    //   now-aborted task) stops accumulating into a detached object. twStop
    //   also cancels any pending rAF/timeout render and clears _pendingStreamMsg.
    if (removedStreaming && typeof twStop === 'function') {
      try { twStop(conv.id); }
      catch (e) { console.warn('[SyncFix] twStop during truncate failed:', e); }
    } else if (typeof streamBufs !== 'undefined' && streamBufs.has(conv.id)) {
      streamBufs.delete(conv.id);
    }
    _lastRenderedFingerprint = _convRenderFingerprint(conv);
    buildTurnNav(conv);
    console.info(`[SyncFix] _surgicalTruncateDOM conv=${conv.id.slice(0,8)} cutoffIdx=${cutoffIdx} removed=${toRemove.length} streamingCleared=${!!removedStreaming}`);
    return true;
  }
  return false;
}

/**
 * Synchronously hard-cancel any in-flight stream/task for a conv, so that
 * a subsequent edit/regen flow can truncate and restart without colliding
 * with the old task's late SSE deliveries or polling responses.
 *
 * Fire-and-forget: the server-side abort POST is dispatched but not awaited.
 * Local in-memory state is cleaned synchronously so the caller can proceed
 * immediately to truncation + new task start.
 *
 * Safe to call when no stream is active — becomes a no-op.
 *
 * @param {Object} conv
 * @returns {boolean} true if something was actually cancelled
 */
function _hardCancelActiveStream(conv) {
  if (!conv) return false;
  const convId = conv.id;
  let cancelled = false;
  const s = (typeof activeStreams !== 'undefined') ? activeStreams.get(convId) : null;
  const oldTaskId = conv.activeTaskId || (s && s.taskId) || null;
  if (s) {
    try {
      s._userAbort = true;
      if (s.controller && !s.controller.signal.aborted) s.controller.abort();
    } catch (e) {
      console.warn(`[SyncFix] _hardCancelActiveStream: controller.abort failed for conv=${convId.slice(0,8)}:`, e);
    }
    cancelled = true;
  }
  if (oldTaskId) {
    // Record last-aborted-task id so polling/SSE stragglers can be discarded
    conv._lastAbortedTaskId = oldTaskId;
    cancelled = true;
  }
  // Clear local in-memory state synchronously
  if (typeof twStop === 'function') {
    try { twStop(convId); }
    catch (e) { console.warn('[SyncFix] _hardCancelActiveStream: twStop failed:', e); }
  } else if (typeof streamBufs !== 'undefined') {
    streamBufs.delete(convId);
  }
  conv.activeTaskId = null;
  conv._activeTaskClearedAt = Date.now();
  // Fire-and-forget abort-by-conv (covers any racing tasks server-side)
  if (cancelled) {
    try {
      Api.chat.abortConv(convId).catch(err => {
        console.warn(`[SyncFix] abort-conv POST failed for conv=${convId.slice(0,8)}:`, err);
      });
    } catch (e) {
      console.warn('[SyncFix] abort-conv fetch threw:', e);
    }
    console.info(`[SyncFix] _hardCancelActiveStream conv=${convId.slice(0,8)} oldTask=${oldTaskId?.slice(0,8)||'null'} hadStream=${!!s}`);
  }
  return cancelled;
}

// ── Chat rendering ──
/* ── Lazy chat rendering with IntersectionObserver ── */
const _INITIAL_RENDER = 20;
let _lazyObserver = null;
let _lazyConvId = null;
let _lazyRenderedFrom = Infinity;

function _destroyLazyObserver() {
  if (_lazyObserver) {
    _lazyObserver.disconnect();
    _lazyObserver = null;
  }
  _loadingOlder = false;
}

function _ensureLazyObserver() {
  if (_lazyObserver) return;
  _lazyObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        const sentinel = e.target;
        _lazyObserver.unobserve(sentinel);
        _loadOlderMessages();
      });
    },
    {
      root: document.getElementById("chatContainer"),
      rootMargin: "600px 0px 0px 0px",
    },
  );
}

let _loadingOlder = false;
function _loadOlderMessages() {
  if (_loadingOlder) return;
  const conv = conversations.find((c) => c.id === _lazyConvId);
  if (!conv) return;
  const BATCH = 20;
  const endIdx = _lazyRenderedFrom;
  if (endIdx <= 0) return;
  _loadingOlder = true;
  const startIdx = Math.max(0, endIdx - BATCH);
  const inner = document.getElementById("chatInner");
  const sentinel = document.getElementById("_lazyLoadSentinel");
  if (!sentinel || !inner) {
    _loadingOlder = false;
    return;
  }

  const container = document.getElementById("chatContainer");

  /* Build all HTML strings first (cheaper than individual DOM creates) */
  let html = "";
  for (let i = startIdx; i < endIdx; i++) {
    html += renderMessage(conv.messages[i], i);
  }

  /* Single DOM mutation: measure → mutate → fix scroll — no intermediate frame */
  const prevScrollTop = container.scrollTop;
  const prevScrollHeight = container.scrollHeight;

  const wrapper = document.createElement("div");
  wrapper.innerHTML = html;
  const frag = document.createDocumentFragment();
  while (wrapper.firstChild) frag.appendChild(wrapper.firstChild);
  sentinel.after(frag);

  _lazyRenderedFrom = startIdx;

  /* Fix scroll synchronously BEFORE the browser paints */
  container.scrollTop =
    prevScrollTop + (container.scrollHeight - prevScrollHeight);

  /* Update or remove sentinel */
  if (startIdx <= 0) {
    sentinel.remove();
  } else {
    sentinel.querySelector("._lazy-count").textContent = startIdx;
    _lazyObserver.observe(sentinel);
  }
  _loadingOlder = false;
}

/**
 * Reliably scroll a container to the very bottom.
 * Uses double-rAF to wait for layout, then a fallback timer
 * to handle async content (images, KaTeX, code highlights).
 */
function _forceScrollToBottom(container, forceActualHeights) {
  if (!container) container = document.getElementById("chatContainer");
  if (!container) return;
  const inner = document.getElementById("chatInner");
  // Override CSS scroll-behavior:smooth so programmatic scrolls are instant.
  container.style.scrollBehavior = 'auto';

  if (forceActualHeights && inner) {
    // Disable content-visibility:auto so the browser computes REAL heights
    // synchronously instead of using the 120px estimate.  This makes
    // scrollHeight accurate on the very first read — no flash.
    inner.classList.add('cv-off');
    // Force sync reflow so heights are computed NOW.
    void container.scrollHeight;
  }

  container.scrollTop = container.scrollHeight;

  // Safety net for async content (images, KaTeX, code highlights).
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      container.scrollTop = container.scrollHeight;
    });
  });
  setTimeout(() => {
    container.scrollTop = container.scrollHeight;
    container.style.scrollBehavior = '';
    if (forceActualHeights && inner) {
      // Re-enable content-visibility:auto only AFTER scroll has fully settled.
      // Keeping cv-off across the rAF + 150ms passes means every tool-round slot
      // ([data-prn]) stays rendered during the switch instead of flashing empty
      // (collapsing to its 32px contain-intrinsic-size placeholder) as the browser
      // re-evaluates off-screen slots against a still-moving scroll position.
      // The browser cached real heights (via "auto" contain-intrinsic-size) while
      // cv-off was on, so scrollHeight stays correct when auto-skip resumes here.
      inner.classList.remove('cv-off');
    }
  }, 150);
}

