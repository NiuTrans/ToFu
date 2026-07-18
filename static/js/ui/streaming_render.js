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
 * Remove a still-streaming VU placeholder bubble LOCALLY (client-side).
 *
 * WHY this exists separate from the `autopilot_vu_cancel` handler: when the
 * user clicks Stop WHILE the VU is streaming, the SSE reader is torn down by
 * `controller.abort()` BEFORE the backend's `autopilot_vu_cancel` frame can be
 * read — so the splice in `_handleAutopilotVuEvent` never runs and a dangling
 * `_streamingVu:true` bubble is left rendering the frozen "Autopilot…" pulse
 * forever (only a page reload cleared it, via the idb-cache `_streamingVu`
 * filter). The Stop handler + `finishStream`'s autopilot branch call THIS to do
 * the removal locally instead of waiting for an event that can't arrive.
 *
 * Mirrors the `autopilot_vu_cancel` teardown (splice + `#streaming-msg`
 * removal + `twStop` + `buildTurnNav` + persist), and is a no-op unless the
 * conversation's TAIL message is a streaming VU bubble (so it can never remove
 * a settled VU turn or a real message).
 *
 * ★ BATON PRESERVED: this removes only the message object. `conv._apPendingBaton`
 * (the authoritative conv-level autopilot follow-up baton) is a conv FIELD, not
 * a message — the splice cannot touch it, and this function deliberately never
 * clears it. So `_findAutopilotPendingCarrier` still resolves any pending
 * follow-up after the ghost bubble is gone (the
 * test_frontend_autopilot_baton_survives_splice.py contract).
 *
 * @returns {boolean} true if a streaming VU bubble was removed.
 */
function _removeStreamingVuBubbleIfTail(conv, convId) {
  if (!conv || !Array.isArray(conv.messages) || !conv.messages.length) return false;
  const last = conv.messages[conv.messages.length - 1];
  if (!last || !last._isVirtualUser || !last._streamingVu) return false;
  conv.messages.pop();
  console.info(
    `[Autopilot VU] ✂ local splice on stop — removed streaming placeholder ` +
    `vuMsgId=${(last._msgId || '').slice(0,12)} for conv=${(convId || conv.id || '').slice(0,8)}`
  );
  if (activeConvId === convId) {
    const sm = document.getElementById("streaming-msg");
    if (sm) { try { sm.remove(); } catch (e) { /* already detached */ } }
    if (typeof twStop === "function") { try { twStop(convId); } catch (e) { /* idempotent */ } }
    if (typeof buildTurnNav === "function") buildTurnNav(conv);
  }
  if (typeof saveConversations === "function") saveConversations(convId);
  try { if (typeof ConvCache !== "undefined") ConvCache.put(conv); }
  catch (e) { /* non-fatal */ }
  return true;
}
if (typeof window !== 'undefined') window._removeStreamingVuBubbleIfTail = _removeStreamingVuBubbleIfTail;

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
 * Push the empty VU message into conv.messages and stand up the SHARED
 * streaming substrate for it — the SAME `#streaming-msg` + `streamBufs` +
 * elapsed-timer machinery the worker turn uses (mirrors the worker→critic
 * handoff in dispatchSSEEvent).  This is what makes the autopilot reply
 * render *identically to the agent*: incremental markdown, thinking block,
 * tool rounds, and the live elapsed-time bar.
 *
 * The parent worker turn's `#streaming-msg` is finalized to a static bubble
 * first (its finish bar is refreshed later when the parent `done` event
 * lands — see the done handler's autopilot branch in sse_pipeline.js).
 *
 * @param {Object} [parentMessage] — the SETTLED parent worker assistant dict
 *   (== the parent `done` event's `committedMessage`), delivered early on
 *   `autopilot_vu_start`. When present, projected onto the parent assistant
 *   BEFORE it is finalized so its finish bar (model / usage / cost /
 *   finishReason) is complete at handoff — not incomplete until the parent
 *   `done` event fires tens of seconds later (that event is withheld until the
 *   whole VU stream completes). Absent on backend skip paths → the parent keeps
 *   its transient buffer, and the later `done` re-render still fills the bar.
 * @returns {{msg:Object, idx:number}} the VU message entry.
 */
function _beginVuStreaming(convId, conv, vuMsgId, parentMessage) {
  const vuMsg = {
    role: "user",
    content: "",
    thinking: "",
    _msgId: vuMsgId,
    _isVirtualUser: true,
    _streamingVu: true,
    timestamp: Date.now(),
    toolRounds: [],
  };

  if (activeConvId === convId) {
    /* Finalize the parent worker's streaming bubble so the VU can own the
     * single `#streaming-body` element.  Walk back to the nearest non-VU
     * assistant (the worker turn that just stopped). */
    const sm = document.getElementById("streaming-msg");
    if (sm) {
      let parentAssistant = null;
      for (let i = conv.messages.length - 1; i >= 0; i--) {
        const m = conv.messages[i];
        if (m && m.role === "assistant" && !m._isVirtualUser) { parentAssistant = m; break; }
      }
      if (parentAssistant && window.ConvView) {
        /* ★ Project the parent worker's SETTLED finish metadata NOW (delivered
         * on vu_start as `parentMessage`) so its finish bar renders COMPLETE at
         * handoff — model + tokens + cost + finishReason ✓. Without this the
         * early finalize below stamps a bar carrying ONLY the model tag (the
         * reported "incomplete finish bar") for the whole VU turn, because the
         * usage/cost/finishReason-bearing parent `done` event is withheld until
         * the VU stream ends. This mirrors the `done` handler's committedMessage
         * projection — same authoritative dict, delivered early. VERBATIM
         * projection: copy the settled fields onto the existing object (not
         * replace it) so frontend-local fields (_translate*, _msgId, branches)
         * survive. */
        if (parentMessage && typeof parentMessage === 'object') {
          const _pm = parentMessage;
          if (_pm.content != null) parentAssistant.content = _pm.content;
          if (_pm.thinking != null) parentAssistant.thinking = _pm.thinking;
          if (Array.isArray(_pm.toolRounds)) parentAssistant.toolRounds = _pm.toolRounds;
          if (Array.isArray(_pm.segments)) parentAssistant.segments = _pm.segments;
          for (const _k of ['finishReason', 'usage', 'preset', 'toolSummary',
                            'model', 'provider_id', 'apiRounds', 'modifiedFiles',
                            'modifiedFileList', 'cost', '_taskId',
                            'fallbackModel', 'fallbackFrom', 'fallbackReason',
                            'fallbackKind', 'error', 'thinkingDepth', '_gitSha']) {
            if (_pm[_k] != null) parentAssistant[_k] = _pm[_k];
          }
        }
        /* Mark the parent so the worker turn's `done` handler still re-renders
         * its finish bar. `done` remains AUTHORITATIVE (it ships the same
         * committedMessage verbatim); this early projection just prevents the
         * incomplete-bar window on the skip-free path, and is a harmless no-op
         * repaint on `done`. When vu_start carried NO parentMessage (backend
         * skip path), the `done` re-render is the sole fill — preserved. */
        parentAssistant._vuTookOverBubble = true;
        window.ConvView.finalizeStreaming(convId, parentAssistant);
      } else {
        try { sm.remove(); } catch (e) { /* already detached */ }
      }
    }
  }

  conv.messages.push(vuMsg);
  const idx = conv.messages.length - 1;

  /* Fresh streaming buffer + elapsed timer for the VU turn. */
  if (typeof twStart === "function") twStart(convId);

  if (activeConvId === convId) {
    const inner = document.getElementById("chatInner");
    if (inner) {
      inner.insertAdjacentHTML(
        "beforeend",
        _streamingBubbleHTML("autopilot", null, null, vuMsgId)
      );
      if (typeof buildTurnNav === "function") buildTurnNav(conv);
      if (typeof scrollToBottom === "function"
          && (typeof isNearBottom !== "function" || isNearBottom(80))) {
        scrollToBottom();
      }
    }
  }
  return { msg: vuMsg, idx };
}

/**
 * Push the accumulated VU buffer to the shared streaming UI (worker
 * substrate).  Reads from `streamBufs.get(convId)` exactly like the
 * worker delta path so the VU bubble renders with identical layout.
 */
function _flushVuStreaming(convId) {
  if (activeConvId !== convId) return;
  if (typeof twUpdate === "function") { twUpdate(convId); return; }
}

/**
 * Auto-translate a finalized VU message to the UI language (Chinese) so the
 * user reads the simulated-user reply in their language even though the VU
 * composed it in the assistant's language.  Mirrors the assistant turn's
 * auto-translate path; gated on the per-conv autoTranslate setting and the
 * shared `_isAlreadyChinese` skip (so a Chinese VU reply isn't re-translated).
 * Fire-and-forget — failure leaves the original VU text shown.
 */
function _maybeAutoTranslateVu(convId, conv, entry) {
  try {
    if (!conv || !entry || !entry.msg) return;
    const msg = entry.msg;
    if (!msg.content || msg.translatedContent || msg._translateDone) return;
    if (!convAutoTranslate(conv)) return;
    if (typeof _startAutoTranslateForMsg !== 'function') return;
    /* idx is the message's current position; _findVuMsgById gives a stable
     * lookup but the pipeline needs the array index for surgical re-render. */
    const idx = conv.messages.indexOf(msg);
    if (idx < 0) return;
    msg._translateDone = false;  // show the "translating…" indicator
    _startAutoTranslateForMsg(conv, convId, idx, msg);
  } catch (e) {
    console.warn('[Autopilot VU] auto-translate trigger failed:', e && e.message);
  }
}

/**
 * Handle the four autopilot_vu_* SSE event types.  See `_processSSELine`
 * for the contract.  The VU streams through the SAME substrate as the
 * worker (`#streaming-msg` + `streamBufs` + `twUpdate`), so its reply is
 * presented exactly like an agent turn (incremental markdown, thinking,
 * tool rounds, elapsed-time bar) — just in the user lane with the
 * Autopilot avatar/label.
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

  if (ev.type === "autopilot_vu_start") {
    /* Stand up the VU streaming bubble the moment autopilot kicks in, so
     * the user sees an "Autopilot · composing…" bubble in the USER lane
     * that then streams the reply live — identical to a worker turn.
     * In-memory ONLY: nothing is persisted until autopilot_vu_done.  If
     * the bubble already exists (reconnect / duplicate start) reuse it. */
    if (_findVuMsgById(conv, vuMsgId)) return;
    const entry = _beginVuStreaming(convId, conv, vuMsgId, ev.parentMessage);
    console.info(
      `[Autopilot VU] ▶ began VU streaming bubble vuMsgId=${vuMsgId.slice(0,12)} ` +
      `at idx=${entry.idx} for conv=${convId.slice(0,8)}`
    );
    try { if (typeof ConvCache !== "undefined") ConvCache.put(conv); }
    catch (e) { /* non-fatal */ }
    return;
  }

  if (ev.type === "autopilot_vu_cancel") {
    /* The VU bailed out (TASK_DONE / aborted / queued real-user msg).
     * Remove the placeholder and tear down the streaming substrate. */
    const entry = _findVuMsgById(conv, vuMsgId);
    if (!entry) return;
    conv.messages.splice(entry.idx, 1);
    console.info(
      `[Autopilot VU] ⛔ cancel — removed placeholder vuMsgId=${vuMsgId.slice(0,12)} ` +
      `for conv=${convId.slice(0,8)}`
    );
    if (activeConvId === convId) {
      const sm = document.getElementById("streaming-msg");
      if (sm) { try { sm.remove(); } catch (e) { /* detached */ } }
      if (typeof twStop === "function") twStop(convId);
      if (typeof buildTurnNav === "function") buildTurnNav(conv);
    }
    if (typeof saveConversations === "function") saveConversations(convId);
    try { if (typeof ConvCache !== "undefined") ConvCache.put(conv); }
    catch (e) { /* non-fatal */ }
    return;
  }

  if (ev.type === "autopilot_vu_done") {
    /* Replace the placeholder's fields with the authoritative final copy
     * from the backend (content + toolRounds), clear the streaming
     * decoration, and finalize the bubble (convert `#streaming-msg` →
     * static) through the same path the worker uses. */
    let entry = _findVuMsgById(conv, vuMsgId);
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
      entry = { msg: fresh, idx: conv.messages.length - 1 };
    } else {
      /* SETTLED state = pure projection of the ONE backend-authoritative
       * record (ev.vuMessage — the same dict _append_vu_message_to_conv
       * wrote to the DB). Rendered VERBATIM: no `|| buf.content` fallback,
       * because a local-buffer fallback makes the frontend a SECOND source
       * of truth and a stuck bubble un-diagnosable from one place. An empty
       * `content` here is a legitimate "keep going" VU reply (the backend
       * bails to VU_CANCEL, not DONE, when the VU produced nothing) — render
       * it as-is; if it's ever wrongly empty that's a backend bug to fix at
       * the source. See .tofu/skills/separation-of-concerns-directive.md. */
      const finalMsg = ev.vuMessage || {};
      entry.msg.content = finalMsg.content || "";
      entry.msg.toolRounds = Array.isArray(finalMsg.toolRounds) ? finalMsg.toolRounds : [];
      /* ★ Segments (epic pt_cb8f98b0cb9b47fb): project the backend's typed
       * timeline VERBATIM so the settled VU bubble renders the IDENTICAL agent
       * inline per-tool timeline (widened gate in chat_render.js). Without this
       * the live stream shows the timeline but the settle repaint drops it →
       * the grouped-panel snap-back. Same verbatim contract as content /
       * toolRounds above: NO local-buffer fallback. Absent (legacy backend /
       * assembly failure) → grouped render, graceful. */
      entry.msg.segments = Array.isArray(finalMsg.segments) ? finalMsg.segments : [];
      delete entry.msg._streamingVu;
      console.info(
        `[Autopilot VU] ✓ done — finalized vuMsgId=${vuMsgId.slice(0,12)} ` +
        `(${(entry.msg.content||'').length} chars, ${(entry.msg.toolRounds||[]).length} rounds) ` +
        `for conv=${convId.slice(0,8)}`
      );
    }
    if (activeConvId === convId) {
      /* Convert the live `#streaming-msg` into the settled static bubble,
       * then tear down the buffer + elapsed timer.  finalizeStreaming now
       * accepts _isVirtualUser (see conv_view.js). */
      if (window.ConvView) window.ConvView.finalizeStreaming(convId, entry.msg);
      if (typeof twStop === "function") twStop(convId);
    }
    if (typeof saveConversations === "function") saveConversations(convId);
    try { if (typeof ConvCache !== "undefined") ConvCache.put(conv); }
    catch (e) { /* non-fatal */ }
    /* ★ Display-language parity: the VU composes in the assistant's language,
     * but the user should SEE it in their UI language.  Reuse the same
     * assistant→Chinese auto-translate pipeline as agent turns when
     * autoTranslate is on (the _isAlreadyChinese guard skips a no-op when the
     * VU already replied in Chinese).  Fire-and-forget; renders bilingually
     * via the VU branch in chat_render.js. */
    _maybeAutoTranslateVu(convId, conv, entry);
    return;
  }

  /* autopilot_vu_event — route the inner event into the VU bubble's shared
   * stream buffer.  The bubble is normally created EAGERLY by
   * autopilot_vu_start, but we keep a LAZY-CREATION fallback here for
   * resilience: if a content-bearing event arrives before / without a
   * start event (dropped frame, cold replay), stand up the bubble now. */
  const inner = ev.inner || {};
  const itype = inner.type || "";

  let entry = _findVuMsgById(conv, vuMsgId);
  if (!entry) {
    /* Lazy-create the bubble when a content-bearing frame arrives without a
     * preceding autopilot_vu_start (reconnect / late-connect / dropped start).
     * `phase` counts as content-bearing: a rate-limited first-token stall
     * emits phase-only frames (waiting_model / retrying/限流中) for tens of
     * seconds BEFORE any delta — if a replay cursor lands inside that window
     * and we dropped them, the bubble would never materialize and the user
     * would stare at a dead warm-up state during precisely the window where
     * the phase chip is the ONLY liveness signal. The `phase` branch below
     * sets buf.phase + flushes once `entry` exists. Non-rendered interactive
     * types (stdin_request / write_approval_request / human_guidance_*) still
     * do NOT create the bubble — the VU IS the user, it hosts no widgets. */
    const _isContentBearing =
      (itype === "tool_start") ||
      (itype === "phase") ||
      (itype === "delta" && (inner.content || inner.thinking));
    if (!_isContentBearing) {
      return; // silently skip — nothing to show yet
    }
    entry = _beginVuStreaming(convId, conv, vuMsgId);
    console.info(
      `[Autopilot VU] ▶ lazy-began VU streaming bubble vuMsgId=${vuMsgId.slice(0,12)} ` +
      `at idx=${entry.idx} on inner=${itype} for conv=${convId.slice(0,8)}`
    );
  }
  const vuMsg = entry.msg;
  const buf = (typeof streamBufs !== "undefined") ? streamBufs.get(convId) : null;
  if (!Array.isArray(vuMsg.toolRounds)) vuMsg.toolRounds = [];

  if (itype === "delta") {
    if (inner.content) vuMsg.content = (vuMsg.content || "") + inner.content;
    if (inner.thinking) vuMsg.thinking = (vuMsg.thinking || "") + inner.thinking;
    if (buf) {
      buf.content = vuMsg.content || "";
      buf.thinking = vuMsg.thinking || "";
      /* Mirror the worker's phase handling: content delta clears the
       * phase; thinking-only delta shows the reasoning indicator. */
      if (inner.content) buf.phase = null;
      else if (inner.thinking) buf.phase = { phase: "thinking_active" };
    }
  } else if (itype === "tool_start") {
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
    if (buf) {
      buf.toolRounds = vuMsg.toolRounds;
      buf.phase = { phase: "tool_exec", detail: inner.query || inner.toolName || "" };
    }
  } else if (itype === "tool_result") {
    const r = vuMsg.toolRounds.find(rr => rr.roundNum === inner.roundNum);
    if (r) {
      r.results = inner.results;
      r.status = "done";
      if (inner.searchDiag) r.searchDiag = inner.searchDiag;
      if (inner.engineBreakdown) r.engineBreakdown = inner.engineBreakdown;
    }
    if (buf) buf.toolRounds = vuMsg.toolRounds;
  } else if (itype === "tool_progress") {
    const r = vuMsg.toolRounds.find(rr => rr.roundNum === inner.roundNum);
    if (r) {
      if (typeof r._partialOutput !== "string") r._partialOutput = "";
      r._partialOutput += (inner.chunk || "");
    }
    if (buf) buf.toolRounds = vuMsg.toolRounds;
  } else if (itype === "tool_complete") {
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
    if (buf) buf.toolRounds = vuMsg.toolRounds;
  } else if (itype === "tool_compacted") {
    const r = vuMsg.toolRounds.find(rr => rr.toolCallId === inner.toolCallId);
    if (r) {
      r.compactionLayer = inner.compactionLayer || r.compactionLayer || "L1";
      if (inner.compactedFromChars != null) r.compactedFromChars = inner.compactedFromChars;
      if (inner.compactedToChars != null) r.compactedToChars = inner.compactedToChars;
      if (inner.toolTokens != null) r.toolTokens = inner.toolTokens;
    }
    if (buf) buf.toolRounds = vuMsg.toolRounds;
  } else if (itype === "phase") {
    /* Drive the same phase indicator the worker uses (tool_exec /
     * llm_thinking / retrying / working show in the elapsed bar). */
    if (buf) {
      buf.phase = {
        phase: inner.phase,
        detail: inner.detail || "",
        tools: inner.tools || [],
        toolContext: inner.toolContext || "",
        round: inner.round || 0,
      };
    }
  } else {
    /* stdin_request / write_approval_request / human_guidance_request etc.
     * — the VU bubble doesn't host interactive widgets (the VU IS the
     * user), so log and ignore. */
    console.debug(
      `[Autopilot VU] inner type=${itype} not surfaced in VU bubble for ` +
      `conv=${convId.slice(0,8)} (vuMsgId=${vuMsgId.slice(0,12)})`
    );
    return;
  }

  /* Render through the shared streaming substrate — identical to the
   * worker delta path. */
  _flushVuStreaming(convId);
}

/**
 * Apply a BACKEND-AUTHORITATIVE autopilot run-concluded record onto a conv.
 *
 * ONE record per run (`{runId, status:'concluded', reason:'task_done'|
 * 'stopped', content?, translatedContent?, ts, _summaryId}`) carries BOTH the
 * terminal fold-fact AND the optional close-out report (a manual stop has no
 * `content`). It is human-only: stored backend-side under
 * `settings.autopilotSummaries[runId]`, mirrored here onto
 * `conv.autopilotSummaries[runId]`, and NEVER entered into `conv.messages`
 * (transcript) nor the LLM context. Idempotent + monotonic: a re-delivery
 * (reconnect / cold-replay / settings round-trip) overwrites the same runId
 * entry, but a bare `stopped` record NEVER clobbers an existing `task_done`
 * record that already carries a report.
 *
 * Shared by the SSE `autopilot_run_concluded` handler AND the disarm response
 * (which returns the same record so an idle disarm — no live stream — folds
 * instantly without a reload). Returns true iff a record was applied.
 */
function _applyAutopilotRunConcluded(conv, rec, runId) {
  if (!conv || !rec || typeof rec !== 'object') return false;
  runId = runId || rec.runId;
  if (!runId) return false;
  if (!conv.autopilotSummaries || typeof conv.autopilotSummaries !== 'object') {
    conv.autopilotSummaries = {};
  }
  const prior = conv.autopilotSummaries[runId];
  /* Monotonic merge: a manual `stopped` record must not erase an earlier
   * clean `task_done` record's report/verdict (they can race on close-out). */
  const priorIsCleanReport = !!(prior && prior.reason === 'task_done' && prior.content);
  const incomingIsBareStop = (rec.reason === 'stopped') && !rec.content;
  if (priorIsCleanReport && incomingIsBareStop) return false;
  const _reason = rec.reason || (prior && prior.reason) || 'task_done';
  conv.autopilotSummaries[runId] = {
    runId,
    status: rec.status || 'concluded',
    reason: _reason,
    content: rec.content || (prior && prior.content) || '',
    translatedContent: rec.translatedContent || (prior && prior.translatedContent) || '',
    ts: rec.ts || Date.now(),
    _summaryId: rec._summaryId || (prior && prior._summaryId) || '',
    /* Preserve the "stopped early — needs review" flag. A clean task_done
     * supersedes an incomplete stop (reason no-downgrade), so drop the flag
     * when the merged reason is task_done. */
    incomplete: (_reason !== 'task_done')
      && !!(rec.incomplete || (prior && prior.incomplete)),
  };
  return true;
}

/**
 * Handle the `autopilot_run_concluded` SSE event: the single BACKEND fact that
 * an autopilot run reached its terminal boundary — a clean [VU: TASK_DONE]
 * (reason=task_done, with a report) OR a manual stop (reason=stopped, no
 * report). Receiving it is what lets `_applyAutopilotRunFolds` fold the run
 * (the gate keys on `conv.autopilotSummaries[runId].status==='concluded'` —
 * see `_apRunConcluded`); the report, when present, renders as the fold's
 * read-only PANEL. The record is human-only — never a chat message.
 *
 * Tolerates the legacy shape (`ev.summary`/`ev.summaryMessage`) during rollout.
 */
/**
 * Apply a disarm response's ``runConcluded`` record to the conv and re-render.
 *
 * The disarm endpoint (toggle-OFF / queue-cancel) is the manual-stop arm of
 * the conclude contract: it returns the SAME backend-authoritative record the
 * SSE ``autopilot_run_concluded`` event carries. Because a disarm can happen
 * when there is NO live SSE stream (the reply already finished — the idle case)
 * the client would otherwise never receive the concluded fact until a reload;
 * applying the response body here makes the run fold instantly. No-op when the
 * response carried no record (nothing was an autopilot run to conclude).
 */
function _applyDisarmResponse(convId, resp) {
  try {
    const rec = resp && resp.runConcluded;
    if (!rec) return;
    const conv = conversations.find(c => c.id === convId);
    if (!conv) return;
    if (!_applyAutopilotRunConcluded(conv, rec, rec.runId)) return;
    if (typeof saveConversations === 'function') saveConversations(convId);
    try { if (typeof ConvCache !== 'undefined') ConvCache.put(conv); }
    catch (e) { /* non-fatal */ }
    if (activeConvId === convId && typeof renderChat === 'function') {
      renderChat(conv, true);
    }
  } catch (e) {
    console.warn('[Autopilot] apply disarm response failed:', e && e.message);
  }
}

function _handleAutopilotRunConcluded(convId, ev) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) {
    console.debug(`[Autopilot run] conv=${convId.slice(0,8)} not found — dropping`);
    return;
  }
  /* New shape: `record`. Legacy rollout shapes: `summary` / `summaryMessage`. */
  const rec = ev.record || ev.summary || ev.summaryMessage;
  const runId = ev.runId || (rec && rec.runId);
  if (!rec || !runId) {
    console.warn('[Autopilot run] missing concluded record / runId', ev);
    return;
  }
  if (!_applyAutopilotRunConcluded(conv, rec, runId)) return;
  const _stored = conv.autopilotSummaries[runId] || {};
  console.info(
    `[Autopilot run] ✓ run=${(runId||'').slice(0,12)} concluded ` +
    `(reason=${_stored.reason}, ${(_stored.content||'').length} report chars, ` +
    `NOT a message) for conv=${convId.slice(0,8)}`
  );
  if (typeof saveConversations === "function") saveConversations(convId);
  try { if (typeof ConvCache !== "undefined") ConvCache.put(conv); }
  catch (e) { /* non-fatal */ }
  if (activeConvId === convId && typeof renderChat === "function") {
    renderChat(conv, true);
  }
}

/**
 * Build the HTML string for a streaming bubble (#streaming-msg).
 * @param {'worker'|'planner'|'critic'|'autopilot'|'swarm'} role  - which phase / avatar
 * @param {string} [status]   - status text shown inside the pulse
 * @param {string} [timeStr]  - formatted time string (defaults to now)
 * @param {string} [msgId]    - optional data-msg-id to stamp on the bubble
 * @returns {string} HTML string
 */
function _streamingBubbleHTML(role, status, timeStr, msgId) {
  const _cfg = {
    worker:  { avatar: (typeof _TOFU_WORKER_SVG  !== 'undefined') ? _TOFU_WORKER_SVG  : '✦', label: 'Agent',   cls: 'ep-worker-msg',  defaultStatus: 'Preparing...' },
    planner: { avatar: (typeof _TOFU_PLANNER_SVG !== 'undefined') ? _TOFU_PLANNER_SVG : '✦', label: 'Planner', cls: 'ep-planner-msg', defaultStatus: 'Planning…' },
    critic:  { avatar: (typeof _TOFU_CRITIC_SVG  !== 'undefined') ? _TOFU_CRITIC_SVG  : '✦', label: 'Critic',  cls: 'ep-critic-msg',  defaultStatus: 'Reviewing…' },
    /* Autopilot virtual-user: streams in the USER lane with the same
     * substrate as the worker so its reply renders identically to the
     * agent (incremental markdown, thinking, tool rounds, elapsed bar). */
    autopilot: { avatar: (typeof _TOFU_CRITIC_SVG !== 'undefined') ? _TOFU_CRITIC_SVG : '✦', label: 'Autopilot', cls: 'vu-user-msg', defaultStatus: (typeof t === 'function' ? t('autopilot.warming') : 'Autopilot…') },
  };
  /* Swarm auto-continuation: a backend-started assistant turn draining
   * sub-agent updates. Sourced from the shared INITIATOR_REGISTRY so its
   * streaming avatar/label matches the settled bubble (chat_render.js). */
  if (typeof INITIATOR_REGISTRY !== 'undefined' && INITIATOR_REGISTRY.swarm) {
    const _sr = INITIATOR_REGISTRY.swarm;
    const _sl = (typeof t === 'function' && t(_sr.labelKey) !== _sr.labelKey) ? t(_sr.labelKey) : _sr.fallback;
    _cfg.swarm = { avatar: _sr.avatar, label: _sl, cls: 'ep-worker-msg ' + _sr.cls, defaultStatus: 'Continuing…' };
  }
  const c = _cfg[role] || _cfg.worker;
  const st = status || c.defaultStatus;
  const tm = timeStr || formatClockTime();
  const extraCls = (role === 'critic' || role === 'autopilot') ? ' user-msg' : '';
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
 * @param {Object} [cfg] - sendConfig / regenConfig with endpointMode flag
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
/* Exclusive upper bound of the currently-rendered message range. Finite only
 * AFTER a downward eviction has capped the window (tail bubbles removed while
 * the reader scrolled up to read history); otherwise it tracks `total` (the
 * tail is rendered). Mirror of `_lazyRenderedFrom`. */
let _lazyRenderedTo = Infinity;
/* Hard upper bound on how many `.message` nodes live in the DOM at once. Upward
 * lazy-load (`_loadOlderMessages`) and downward re-render (`_loadNewerMessages`)
 * each evict from the FAR side once the rendered span exceeds this, so scroll /
 * re-render / hit-test cost is O(window) not O(history). This is the structural
 * fix for "the longer the conversation, the laggier scrolling gets": without a
 * cap, scrolling up in a 500-message conversation accretes hundreds of heavy
 * bubbles that are never reclaimed. MUST exceed _INITIAL_RENDER + one BATCH so a
 * first upward load never immediately evicts what it just added. */
const _MAX_RENDER_WINDOW = 80;
let _lazyBottomObserver = null;
let _loadingNewer = false;
/* Which conv's OPEN has already had its view positioned (see the open-scroll
 * coalescing block in renderChat). Set by the force-scroll branch during an
 * initial switch load; reset by loadConversation on a genuine switch so the
 * next open force-scrolls exactly once. */
let _openScrollConvId = null;

function _destroyLazyObserver() {
  if (_lazyObserver) {
    _lazyObserver.disconnect();
    _lazyObserver = null;
  }
  _loadingOlder = false;
  /* The full-render / show-streaming paths that call this wipe innerHTML and
   * rebuild the UNCAPPED tail window, so the downward-load machinery must reset
   * in lockstep — otherwise the bottom observer keeps a handle on a removed
   * sentinel and the stale cap survives the rebuild. */
  _destroyBottomObserver();
}
function _destroyBottomObserver() {
  if (_lazyBottomObserver) {
    _lazyBottomObserver.disconnect();
    _lazyBottomObserver = null;
  }
  _loadingNewer = false;
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

/* Build (or update) a bottom sentinel mirroring the top `_lazyLoadSentinel`.
 * It marks the `hiddenBelow` count of tail messages evicted downward and is the
 * IntersectionObserver trigger that re-renders them when the reader scrolls
 * back down. Absent (removed) when the window reaches the true tail. */
function _ensureBottomSentinel(inner, hiddenBelow) {
  let s = document.getElementById("_lazyLoadSentinelBottom");
  if (hiddenBelow <= 0) {
    if (s) s.remove();
    return null;
  }
  if (!s) {
    s = document.createElement("div");
    s.id = "_lazyLoadSentinelBottom";
    s.className = "lazy-sentinel";
    s.innerHTML = '<span class="lazy-sentinel-text">⬇ <span class="_lazy-count-bottom">0</span> newer messages</span>';
    inner.appendChild(s);
  } else if (s !== inner.lastElementChild) {
    inner.appendChild(s);  // keep it pinned at the very bottom after appends
  }
  const c = s.querySelector("._lazy-count-bottom");
  if (c) c.textContent = String(hiddenBelow);
  return s;
}

/* Evict the topmost rendered messages so the rendered span never exceeds
 * `_MAX_RENDER_WINDOW`. Called AFTER a downward re-render appended tail
 * bubbles. Mirror of the tail eviction in `_loadOlderMessages`. Advances
 * `_lazyRenderedFrom` and refreshes the top sentinel; preserves the scroll
 * anchor via the caller's height-delta compensation. Returns px height removed
 * above the fold so the caller can keep the viewport pinned. */
function _evictAboveWindow(inner, container) {
  let removedPx = 0;
  while ((_lazyRenderedTo - _lazyRenderedFrom) > _MAX_RENDER_WINDOW && _lazyRenderedFrom < _lazyRenderedTo) {
    const el = document.getElementById("msg-" + _lazyRenderedFrom);
    if (!el) { _lazyRenderedFrom++; continue; }
    removedPx += el.getBoundingClientRect().height;
    el.remove();
    _lazyRenderedFrom++;
  }
  if (_lazyRenderedFrom > 0) {
    _ensureLazyObserver();
    let top = document.getElementById("_lazyLoadSentinel");
    if (!top) {
      top = document.createElement("div");
      top.id = "_lazyLoadSentinel";
      top.className = "lazy-sentinel";
      top.innerHTML = '<span class="lazy-sentinel-text">⬆ <span class="_lazy-count">0</span> older messages</span>';
      inner.insertBefore(top, inner.firstChild);
    }
    const c = top.querySelector("._lazy-count");
    if (c) c.textContent = String(_lazyRenderedFrom);
    _lazyObserver.observe(top);
  }
  return removedPx;
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

  /* Seed the upper bound on first upward load: before any eviction the tail
   * (through the last rendered message) is on screen, so the current window
   * ends at the last rendered msg index + 1. */
  if (!Number.isFinite(_lazyRenderedTo)) {
    _lazyRenderedTo = _highestRenderedMsgIdx(inner) + 1;
  }

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

  /* Update or remove the TOP sentinel */
  if (startIdx <= 0) {
    sentinel.remove();
  } else {
    sentinel.querySelector("._lazy-count").textContent = startIdx;
    _lazyObserver.observe(sentinel);
  }

  /* ★ BOUNDED WINDOW: having grown the head, evict the TAIL so the DOM never
   * accretes without bound. This is the structural fix — without it, scrolling
   * up through a long conversation keeps every bubble alive forever, and every
   * scroll frame / re-render / hit-test pays for all of them. The reader is
   * near the top here, so removing far-below bubbles is invisible and does NOT
   * move the viewport (content below the fold shrinking never shifts what's
   * above it). A bottom sentinel is (re)placed so scrolling back down
   * re-renders them. */
  _evictBelowWindow(inner, container, /*anchorFromTop=*/true);
  _lazyObserver.observe(document.getElementById("_lazyLoadSentinel") || sentinel);
  _loadingOlder = false;
}

/* Highest msg-N index currently in the DOM (or -1 when none). */
function _highestRenderedMsgIdx(inner) {
  const els = inner.querySelectorAll('[id^="msg-"]');
  let hi = -1;
  els.forEach((el) => {
    const m = el.id.match(/^msg-(\d+)$/);
    if (m) { const i = parseInt(m[1], 10); if (i > hi) hi = i; }
  });
  return hi;
}

/* Evict the bottom-most rendered messages so the rendered span never exceeds
 * `_MAX_RENDER_WINDOW`, and (re)place the bottom sentinel for the hidden tail.
 * `anchorFromTop` true means the reader is anchored near the head (upward-load
 * case) so removing tail bubbles is invisible and needs no scroll compensation.
 * Retracts `_lazyRenderedTo`. */
function _evictBelowWindow(inner, container, anchorFromTop) {
  const conv = conversations.find((c) => c.id === _lazyConvId);
  const total = conv ? conv.messages.length : _lazyRenderedTo;
  if (!Number.isFinite(_lazyRenderedTo)) _lazyRenderedTo = _highestRenderedMsgIdx(inner) + 1;
  /* Never evict the live streaming bubble's message or anything a stream owns —
   * the bounded window is a settled-history optimisation only. */
  if (conv && activeStreams.has(conv.id)) return;
  while ((_lazyRenderedTo - _lazyRenderedFrom) > _MAX_RENDER_WINDOW && _lazyRenderedTo > _lazyRenderedFrom + 1) {
    const idx = _lazyRenderedTo - 1;
    const el = document.getElementById("msg-" + idx);
    if (el) el.remove();
    _lazyRenderedTo = idx;
  }
  const hiddenBelow = Math.max(0, total - _lazyRenderedTo);
  const s = _ensureBottomSentinel(inner, hiddenBelow);
  if (s) {
    _ensureBottomObserver();
    _lazyBottomObserver.observe(s);
  } else {
    _destroyBottomObserver();
  }
}

function _ensureBottomObserver() {
  if (_lazyBottomObserver) return;
  _lazyBottomObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        _lazyBottomObserver.unobserve(e.target);
        _loadNewerMessages();
      });
    },
    {
      root: document.getElementById("chatContainer"),
      rootMargin: "0px 0px 600px 0px",
    },
  );
}

/* Symmetric downward loader: when the reader scrolls back down to the bottom
 * sentinel, re-render the next BATCH of tail messages that were evicted, then
 * evict from the HEAD to keep the window bounded. Mirror of
 * `_loadOlderMessages`; preserves the scroll anchor via the same
 * `prevScrollTop + (newHeight - prevHeight)` head-compensation used on upward
 * load. */
function _loadNewerMessages() {
  if (_loadingNewer) return;
  const conv = conversations.find((c) => c.id === _lazyConvId);
  if (!conv) return;
  if (activeStreams.has(conv.id)) return;  // stream owns the tail — don't fight it
  const total = conv.messages.length;
  const BATCH = 20;
  if (!Number.isFinite(_lazyRenderedTo) || _lazyRenderedTo >= total) return;
  const inner = document.getElementById("chatInner");
  const sentinel = document.getElementById("_lazyLoadSentinelBottom");
  if (!inner || !sentinel) return;
  const container = document.getElementById("chatContainer");
  _loadingNewer = true;

  const startIdx = _lazyRenderedTo;
  const endIdx = Math.min(total, startIdx + BATCH);
  let html = "";
  for (let i = startIdx; i < endIdx; i++) html += renderMessage(conv.messages[i], i);

  const prevScrollTop = container.scrollTop;
  const prevScrollHeight = container.scrollHeight;

  const wrapper = document.createElement("div");
  wrapper.innerHTML = html;
  const frag = document.createDocumentFragment();
  while (wrapper.firstChild) frag.appendChild(wrapper.firstChild);
  sentinel.before(frag);  // insert the newer bubbles ABOVE the bottom sentinel
  _lazyRenderedTo = endIdx;

  /* Growing content BELOW the fold does not move what the reader currently
   * sees, so the head stays put — no scroll compensation needed for the append
   * itself. (prevScrollTop/prevScrollHeight are captured only for symmetry with
   * the head-eviction compensation below.) */
  void prevScrollTop; void prevScrollHeight;

  /* Now evict from the HEAD to keep the window bounded. The reader is anchored
   * near the bottom, so compensate the viewport for removed head height. */
  const beforeH = container.scrollHeight;
  const removedTop = _evictAboveWindow(inner, container);
  void beforeH;
  if (removedTop > 0) container.scrollTop = Math.max(0, container.scrollTop - removedTop);

  /* Refresh / remove the bottom sentinel for the remaining hidden tail. */
  const hiddenBelow = Math.max(0, total - _lazyRenderedTo);
  const s = _ensureBottomSentinel(inner, hiddenBelow);
  if (s) { _ensureBottomObserver(); _lazyBottomObserver.observe(s); }
  else { _destroyBottomObserver(); }
  _loadingNewer = false;
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

