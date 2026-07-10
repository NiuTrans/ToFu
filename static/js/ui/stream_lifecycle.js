/* ═══════════════════════════════════════════════════════════════
   ui/stream_lifecycle.js — extracted from ui/streaming_ui.js (split 2026-06-27)

   Stream lifecycle + finalize: showStreamingUIForConv (initial render +
   lazy-load), finishStream (terminal cleanup: orphaned-HG rounds, queue-race
   guard, autopilot detection, sync, auto-translate dispatch), and the
   Human-Guidance auto-translate helpers.

   These are DOWNSTREAM callers of the streaming render path: they call
   updateStreamingUI / renderMessage / _streamingBubbleHTML /
   ConvView.finalizeStreaming / _runTranslationPipeline etc. — all via shared
   window scope, all at runtime (inside function bodies), so load order beyond
   "after ui/streaming_ui.js" is free.

   Concatenated by lib/js_bundler.py AFTER ui/streaming_ui.js — symbols share
   the same window scope as every other static/js/*.js file. No exports.
   ═══════════════════════════════════════════════════════════════ */

function showStreamingUIForConv(convId) {
  const conv = conversations.find((c) => c.id === convId);
  if (!conv || conv.messages.length === 0) return;
  _destroyLazyObserver();
  _lazyConvId = convId;
  _lastRenderedFingerprint = "";
  const inner = document.getElementById("chatInner");

  /* ★ FIX (Root Cause 3): Only drop the trailing message when it actually
   * owns the streaming bubble (in-progress assistant, or in-progress critic).
   * If the last message is a user/optimizer/critic-done entry, buildTurnNav
   * will still create a dot for it, so we MUST render it statically — otherwise
   * msg-{last} is missing from the DOM and the turn-dot click is a no-op. */
  const _last = conv.messages[conv.messages.length - 1];
  const _lastIsStreamingBubble =
    !!_last && (
      (_last.role === "assistant" && !_last.done) ||
      (_last._isEndpointReview && !_last.done)
    );
  const renderMsgs = _lastIsStreamingBubble
    ? conv.messages.slice(0, -1)
    : conv.messages;
  const total = renderMsgs.length;
  const startIdx = Math.max(0, total - _INITIAL_RENDER);
  _lazyRenderedFrom = startIdx;

  let html = "";
  if (startIdx > 0) {
    _ensureLazyObserver();
    html += `<div id="_lazyLoadSentinel" class="lazy-sentinel"><span class="lazy-sentinel-text">⬆ <span class="_lazy-count">${startIdx}</span> older messages</span></div>`;
  }
  for (let i = startIdx; i < total; i++) {
    html += renderMessage(renderMsgs[i], i);
  }

  const lastMsg = _last;
  const _smTime = formatClockTime(lastMsg?.timestamp);
  if (_lastIsStreamingBubble) {
    /* ★ Carry the message's stable _msgId onto the rebuilt bubble's
     *   data-msg-id. Without it, the live per-round translation preview
     *   (_renderStreamingTranslatePreview, routed by data-msg-id) can no
     *   longer target this bubble after any mid-stream full re-render, so
     *   the Chinese stops filling in until the task ends. */
    const _smMsgId = lastMsg._msgId || null;
    if (lastMsg.role === "assistant" && lastMsg._isEndpointPlanner) {
      html += _streamingBubbleHTML('planner', 'Planning…', _smTime, _smMsgId);
    } else if (lastMsg.role === "assistant" && lastMsg._swarmAutoContinue) {
      html += _streamingBubbleHTML('swarm', 'Continuing…', _smTime, _smMsgId);
    } else if (lastMsg.role === "assistant") {
      html += _streamingBubbleHTML('worker', 'Streaming…', _smTime, _smMsgId);
    } else if (lastMsg._isEndpointReview) {
      html += _streamingBubbleHTML('critic', 'Reviewing…', _smTime, _smMsgId);
    }
  }
  inner.innerHTML = html;
  if (startIdx > 0) {
    const sentinel = document.getElementById("_lazyLoadSentinel");
    if (sentinel) _lazyObserver.observe(sentinel);
  }
  requestAnimationFrame(() => buildTurnNav(conv));
  _forceScrollToBottom(null, true);
  updateSendButton();
  if (_lastIsStreamingBubble) {
    const buf = streamBufs.get(convId);
    /* ★ FIX: buf?.toolRounds is [] (truthy) even when empty, preventing
     *   fallback to getToolRoundsFromMsg(lastMsg).  Use .length check. */
    const rounds = (buf?.toolRounds?.length ? buf.toolRounds : null)
                   || getToolRoundsFromMsg(lastMsg);
    updateStreamingUI({
      thinking: buf?.thinking || lastMsg.thinking || "",
      content: buf?.content || lastMsg.content || "",
      toolRounds: rounds,
      phase: buf?.phase || null,
      _memoryPrefetch: buf?._memoryPrefetch || lastMsg._memoryPrefetch,
      _mcpLoginHint: buf?._mcpLoginHint,
    });
    /* ★ Repaint the live translation preview immediately after the bubble is
     *   rebuilt. The body's innerHTML was just replaced, destroying any
     *   translatePreview zone, and the next server push frame may be 20-40s
     *   away (one per tool round). Re-render the last partial we stashed on
     *   the message so the Chinese-so-far survives the rebuild instead of
     *   blanking until the next round closes. No-op when nothing was
     *   translated yet or this isn't the streaming bubble. */
    if (lastMsg._translatePartial && lastMsg._msgId
        && typeof _renderStreamingTranslatePreview === 'function') {
      _renderStreamingTranslatePreview(convId, lastMsg._msgId, lastMsg._translatePartial, lastMsg._translatePartialByRound);
    }
    /* ★ FIX: After page refresh, SSE data may arrive AFTER this initial render.
     *   Schedule a deferred re-render (300ms) so that any SSE state event that
     *   arrives during the connection setup window gets rendered — without this,
     *   the user sees "Waiting…" until the NEXT SSE event triggers twUpdate. */
    const _deferConvId = convId;
    const _deferLastMsg = lastMsg;
    setTimeout(() => {
      if (activeConvId !== _deferConvId) return;           // user switched away
      if (!activeStreams.has(_deferConvId)) return;         // stream finished
      const dBuf = streamBufs.get(_deferConvId);
      if (!dBuf) return;
      /* ★ FIX (stuck "等待中…" wipe): fall back to the persisted message when
       *   the buffer field is empty, exactly like the initial render above.
       *   A raw `dBuf.content` here re-paints updateStreamingUI({content:''})
       *   on a freshly-seeded/empty buffer, snapping the bubble from the real
       *   (checkpointed) English back to the "wait" branch 300ms after load.
       *   The buffer is authoritative only once it has data; until then the
       *   message's checkpoint content is the truth. */
      updateStreamingUI({
        thinking: dBuf.thinking || _deferLastMsg.thinking || "",
        content: dBuf.content || _deferLastMsg.content || "",
        toolRounds: (dBuf.toolRounds?.length ? dBuf.toolRounds : null)
                    || getToolRoundsFromMsg(_deferLastMsg),
        phase: dBuf.phase,
        _memoryPrefetch: dBuf._memoryPrefetch || _deferLastMsg._memoryPrefetch,
        _mcpLoginHint: dBuf._mcpLoginHint,
      });
    }, 300);
  }
}

function finishStream(convId) {
  activeStreams.delete(convId);
  const conv = conversations.find((c) => c.id === convId);
  if (conv) {
    const lastMsg = conv.messages[conv.messages.length - 1];
    const contentLen = lastMsg?.content?.length || 0;
    const thinkingLen = lastMsg?.thinking?.length || 0;
    const hasError = !!lastMsg?.error;
    /* ★ CROSS-TALK DETECTION: count how many active streams exist at finish time.
     *   If >1 stream was active, there's elevated risk of cross-talk injection.
     *   Also check if the conv's message count changed unexpectedly. */
    const _fsActiveCount = activeStreams.size;  // checked AFTER delete above
    const _fsOtherStreams = [...activeStreams.keys()].filter(k => k !== convId).map(k => k.slice(0,8));
    console.warn(
      `[finishStream] conv=${convId.slice(0,8)} msgs=${conv.messages.length} ` +
      `lastRole=${lastMsg?.role} contentLen=${contentLen} thinkingLen=${thinkingLen} ` +
      `hasError=${hasError} taskId=${conv.activeTaskId?.slice(0,8)||'null'} ` +
      `otherActiveStreams=[${_fsOtherStreams.join(',')}] ` +
      `isActiveConv=${activeConvId === convId} activeConvId=${activeConvId?.slice(0,8)||'null'}`
    );
    if (_fsOtherStreams.length > 0) {
      console.warn(
        `[finishStream] ⚠️ CONCURRENT STREAMS: ${_fsOtherStreams.length} other stream(s) still active ` +
        `while finishing conv=${convId.slice(0,8)} — elevated cross-talk risk! ` +
        `Other convs: [${_fsOtherStreams.join(', ')}]`
      );
    }
    if (lastMsg?.role === 'assistant' && contentLen === 0 && thinkingLen === 0 && !hasError) {
      console.error(`[finishStream] ⚠️ EMPTY ASSISTANT MESSAGE DETECTED — conv=${convId.slice(0,8)} — this is likely the data loss bug!`, {
        message: JSON.parse(JSON.stringify(lastMsg)),
        convTitle: conv.title,
        messageCount: conv.messages.length,
      });
    }
    /* ★ FIX: Clean up any lingering awaiting_human / submitted rounds.
     *   When the task finishes (normally or via abort/timeout), any HG round
     *   that was never answered is now orphaned — the backend won't accept a
     *   response anymore.  Mark them as "done" so the sidebar amber dot clears
     *   and the card collapses to a "no response" line. */
    let _hgCleaned = 0;
    for (const m of conv.messages) {
      if (m.toolRounds) {
        for (const r of m.toolRounds) {
          if (r.status === 'awaiting_human' || r.status === 'submitted') {
            r.status = 'done';
            r.guidanceId = null;
            r._hgSkipped = true;  // marker: user never answered
            _hgCleaned++;
          }
        }
      }
    }
    if (_hgCleaned > 0) {
      console.info(`[finishStream] 🧹 Cleaned ${_hgCleaned} orphaned HG round(s) — conv=${convId.slice(0,8)}`);
    }
    /* ★ FIX: clear a lingering "filtering memories" state.  If the task was
     *   stopped (or died) while the cheap-LLM memory prefetch was still
     *   running, no terminal memory_prefetch event ever arrives to reset
     *   conv._memoryPrefetching — the sidebar dot/tag would stay stuck.
     *   finishStream is the universal terminal point, so reset it here. */
    if (conv._memoryPrefetching) {
      conv._memoryPrefetching = false;
      console.info(`[finishStream] 🧹 Cleared stuck memory-prefetch state — conv=${convId.slice(0,8)}`);
    }
    conv.activeTaskId = null;
    conv._activeTaskClearedAt = Date.now();
    saveConversations(convId);
    /* ★ Phase 2 (completion-workflow consolidation): NO full-conv PUT here.
     *   The backend's _sync_result_to_conversation is the SOLE authoritative
     *   writer of the settled turn into conversations.messages — it commits
     *   the assistant message BEFORE the terminal `done` event, clears
     *   settings.activeTaskId, and updates lastMsgRole/lastMsgTimestamp
     *   (manager.py). The done event ships that exact dict as
     *   `committedMessage`, which the SSE done handler projects verbatim, so
     *   the client's in-memory copy already matches the DB. A PUT here only
     *   RE-uploaded what the backend just wrote — and, worse, RACED it: three
     *   skip-guards (queue-race, autopilot-inbound, server_offline) existed
     *   ONLY to suppress this PUT in the exact windows where it would clobber
     *   a backend write (the queued user_msg from dispatch_next_queued, the
     *   autopilot VU user_msg, or the complete offline content with a
     *   truncated snapshot). Removing the PUT makes all three moot — that
     *   whole race class is gone. Config/toggle changes (autoTranslate, pin,
     *   folder, tool flags) are persisted by their OWN explicit call sites
     *   (syncConversationToServerDebounced / PATCH /settings), and the
     *   autopilotSummaries sidecar is durably written server-side
     *   (autopilot.py) — none of that state ORIGINATES at turn-end, so
     *   dropping the turn-end PUT loses nothing. */
    /* ★ Eagerly update the IndexedDB cache with the (already backend-matched)
     *   local state for instant reload. This is a LOCAL cache write, not the
     *   server PUT — it stays. */
    ConvCache.put(conv);
    /* ★ Auto-generate a descriptive title once the first turn completes.
     *   The helper guards itself (skips if user-edited, already attempted, or
     *   the conversation lacks a user+assistant pair), so this is a safe
     *   fire-and-forget call on every stream finish. */
    if (typeof _maybeAutoGenerateTitle === 'function' && !hasError) {
      _maybeAutoGenerateTitle(convId);
    }
  } else {
    console.error(`[finishStream] conv not found for id=${convId.slice(0,8)} — cannot save!`);
  }
  // ── UI updates (wrapped in try/catch so auto-translate always runs) ──
  try {
    if (activeConvId === convId) {
      const sm = document.getElementById("streaming-msg");
      const hasEndpointTurns = conv && conv.messages.some(m => m._epIteration);
      // ★ SyncFix: if the aborted stream's trailing assistant was already
      //   truncated away (user clicked Edit/Regen mid-abort), don't re-render
      //   a ghost msg-N for a message that no longer exists. The last message
      //   after truncation should be a user message; if so, just remove the
      //   stale streaming-msg without replacing it.
      const _fsLast = conv ? conv.messages[conv.messages.length - 1] : null;
      /* ★ Autopilot detection: when autopilot fires, _handleAutopilotVuEvent
       *   has pushed a VU user message at conv.messages[length-1] BEFORE
       *   finishStream runs.  The real streaming assistant lives at
       *   length-2 (or earlier).  Without this branch, the trailing-VU
       *   path would either remove #streaming-msg without finalizing the
       *   parent assistant (visual data loss) or pass the VU into
       *   ConvView.finalizeStreaming and stamp VU's HTML onto the
       *   streaming bubble's slot — both manifest as "VU user message
       *   invisible until force-refresh".  Walk back to the nearest
       *   non-VU assistant and finalize that one instead. */
      let _fsAutopilotAssistant = null;
      if (sm && conv && _fsLast && _fsLast.role !== 'assistant') {
        for (let i = conv.messages.length - 1; i >= 0; i--) {
          const m = conv.messages[i];
          if (m && m.role === 'assistant' && !m._isVirtualUser) {
            _fsAutopilotAssistant = m;
            break;
          }
        }
      }
      const _truncatedAway = sm && _fsLast && _fsLast.role !== 'assistant'
        && !_fsAutopilotAssistant;
      if (_truncatedAway) {
        console.info(`[SyncFix] finishStream skipping render — trailing assistant was truncated (conv=${convId.slice(0,8)}, lastRole=${_fsLast.role})`);
        try { sm.remove(); } catch (e) { /* already detached */ }
      } else if (sm && _fsAutopilotAssistant) {
        /* Autopilot path — finalize the parent assistant, NOT the VU
         * user message that was pushed at the tail. */
        console.info(`[finishStream] 🤖 Autopilot tail detected — finalizing parent assistant ` +
          `(idx=${conv.messages.indexOf(_fsAutopilotAssistant)}, lastRole=${_fsLast.role}, ` +
          `lastIsVU=${!!_fsLast._isVirtualUser}) for conv=${convId.slice(0,8)}`);
        window.ConvView.finalizeStreaming(convId, _fsAutopilotAssistant);
      } else if (sm && conv) {
        /* Normal streaming finish — funnel through ConvView so scroll
         * preservation (the "thinking-block collapse" jump fix) and the
         * truncation-aware fallback live in one place.  See conv_view.js
         * `finalizeStreaming` for the full rationale. */
        const idx = conv.messages.length - 1;
        const msg = conv.messages[idx];
        if (msg) window.ConvView.finalizeStreaming(convId, msg);
      } else if (hasEndpointTurns) {
        // ★ Endpoint mode after poll fallback — no streaming-msg element exists
        // (SSE timed out, poll was used). Do a full re-render to show all turns.
        console.info(`[finishStream] Endpoint mode full re-render — ` +
          `conv=${convId.slice(0,8)} msgs=${conv.messages.length}`);
        renderChat(conv);
      }
      /* ★ FIX: Don't force-scroll-to-bottom after stream finishes.
       *   The user is already reading the content at their current scroll position.
       *   Forcing to bottom after the streaming→final DOM swap causes a visible jump
       *   because the final message may be shorter (collapsed thinking, no phase indicator).
       *   Only scroll if the user was already near the bottom (within 80px). */
      if (isNearBottom(80)) scrollToBottom();
      if (conv) {
        buildTurnNav(conv);
        _lastRenderedFingerprint = _convRenderFingerprint(conv);
      }
    }
    renderConversationList();
    updateSendButton();
  } catch (uiErr) {
    console.error('[finishStream] UI update error (non-fatal, translate will still run):', uiErr.message);
  }
  // ── Auto-translate assistant response ──
  // ★ Use per-conversation setting, NOT the global (which reflects current viewed conv)
  const _convAutoTranslate = convAutoTranslate(conv);
  if (!_convAutoTranslate) {
    console.info(`[finishStream] autoTranslate is OFF — skipping all ` +
      `translation scheduling for conv=${convId.slice(0,8)} ` +
      `(conv.autoTranslate=${conv?.autoTranslate}, global=${autoTranslate})`);
  }
  if (_convAutoTranslate && conv) {
    /* ★ Auto-translate is now driven by the server-side safety net in
     *   lib/tasks_pkg/manager.py::_maybe_auto_translate_assistant (and the
     *   endpoint-mode equivalent _trigger_endpoint_auto_translate).  The
     *   backend fires translation right after persisting assistant content
     *   to the DB, so kicking off a parallel client-driven task here just
     *   races the server task — and since /api/translate/start has no
     *   conv+msgIdx-level dedup, both writes can land and the slower one
     *   silently clobbers the faster (real) translation.  Skip in normal
     *   stream-completion; manual click (translateMessage) and page-load
     *   resume (_resumePendingTranslations) still go through their own
     *   call sites and are unaffected. */
    console.info(`[finishStream] Auto-translate handled by backend safety net — ` +
      `skipping client-side scheduling for conv=${convId.slice(0,8)}`);
    /* ★ Reliability: the backend delivers the translation to the live view
     *   via a single fire-and-forget push frame, which the hub drops when no
     *   client is subscribed at emit time. If BOTH the 'running' and 'done'
     *   frames are lost, the active view shows the bare original until a
     *   conversation switch re-reads the DB. Arm the DB-polling watchdog on
     *   the trailing assistant so the translation surfaces live regardless of
     *   push delivery. The watchdog short-circuits cheaply once
     *   translatedContent / a client task is present, so it's a no-op when
     *   the push path works. */
    if (activeConvId === convId && typeof _armAutoTranslateWatchdog === 'function') {
      for (let i = conv.messages.length - 1; i >= 0; i--) {
        const m = conv.messages[i];
        if (m.role !== 'assistant' || m._isVirtualUser) continue;
        if (!m.content) break;
        if (m.translatedContent || m._translateDone === true) break;
        if (m._igResult || m._isImageGen || m._igResults) break;
        _armAutoTranslateWatchdog(convId, i, m);
        break;
      }
    }
  }
  // ── ★ Autopilot in-band follow-up: when the done event carried
  //    autopilotNextTaskId + autopilotVuMessage, the backend already
  //    appended the synthetic user message to conv DB and spawned the
  //    next task before closing this SSE stream.  Attach to it directly
  //    instead of going through the queue-poll path.  This eliminates
  //    the race where the VU LLM call took longer than the polling
  //    retry budget (~15s) and the synthetic user msg + follow-up task
  //    stayed invisible until manual page refresh.
  /* Locate the autopilot carrier by flag, not by tail position — the
   * carrier is whichever message the SSE done handler stamped, which
   * may have been moved off the tail by Phase-2 reconciliation. */
  const _apCarrier = conv ? _findAutopilotPendingCarrier(conv) : null;
  if (_apCarrier) {
    const _autopilotPending = _apCarrier.msg._autopilotPending;
    /* Consume BOTH the conv-level baton and any positional stamp so a
     * later finishStream doesn't re-dispatch the same follow-up. */
    if (conv) delete conv._apPendingBaton;
    if (!_apCarrier._convLevel) delete _apCarrier.msg._autopilotPending;
    if (typeof _attachAutopilotFollowup === 'function') {
      _attachAutopilotFollowup(convId, _autopilotPending);
      return;
    }
    /* \u2605 Self-heal: the attach fn isn't loaded (bundle-timing miss).
     *   Do NOT silently drop the baton \u2014 the backend already spawned the
     *   follow-up task, so fall through to the queue-poll path below
     *   (/api/chat/active) which will discover and attach to it. */
    console.warn(
      `[Autopilot] _attachAutopilotFollowup unavailable \u2014 falling back to ` +
      `queue-poll for follow-up task=${(_autopilotPending && _autopilotPending.nextTaskId || '').slice(0,8)}`
    );
  }

  // ── ★ Server-side queue: always check for auto-dispatched next task ──
  // The backend's persist_task_result → _dispatch_queued_message checks the
  // message_queue table and auto-dispatches the next message. We poll for
  // the new task to connect to its SSE stream. No frontend gate — the
  // backend is the single source of truth for queue state.
  //
  // ★ Optimistic UI (when queue has items): insert a placeholder streaming
  //   bubble immediately so the user has visual feedback that their queued
  //   message is about to be dispatched, instead of dead air between stop and
  //   the new SSE connection.  _checkForQueuedTask → loadConversationMessages
  //   → renderChat will replace this bubble with the real streaming one.
  // ★ Timing: skip the 500ms delay when there's a queued item — we want the
  //   dispatch poll to fire ASAP so the user sees the new task start without
  //   lag.  When there's no queue, keep the 500ms debounce to avoid hammering
  //   /api/chat/active on every normal stream end.
  const _hasQueued = (typeof _dispatchableQueueCount === 'function')
    && _dispatchableQueueCount(convId) > 0;
  if (_hasQueued && activeConvId === convId) {
    try {
      const inner = document.getElementById('chatInner');
      if (inner && !document.getElementById('streaming-msg')) {
        const _qTime = formatClockTime();
        inner.insertAdjacentHTML('beforeend',
          _streamingBubbleHTML('worker', 'Dispatching queued message…', _qTime));
        if (isNearBottom(80)) scrollToBottom();
      }
    } catch (e) {
      console.warn('[finishStream] queued-dispatch placeholder insert failed:', e);
    }
  }
  const _queuedCheckDelay = _hasQueued ? 0 : 500;
  setTimeout(() => _checkForQueuedTask(convId), _queuedCheckDelay);
}

/**
 * Re-trigger EN→CN translation for any awaiting_human rounds that haven't
 * been translated yet.  Called after SSE state snapshot / page-load reconnection
 * so translations survive page refreshes.
 */
function _retriggerHgTranslations(convId) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return;
  const _hgAutoTrans = convAutoTranslate(conv);
  if (!_hgAutoTrans) return;
  const assistantMsg = [...conv.messages].reverse().find(m => m.role === 'assistant');
  if (!assistantMsg || !assistantMsg.toolRounds) return;
  for (const r of assistantMsg.toolRounds) {
    if (r.status === 'awaiting_human' && r.guidanceQuestion && !r._translatedQuestion && !r._hgTranslating) {
      console.log(`[HG-Translate] Re-triggering translation for guidance=${r.guidanceId} after reconnect`);
      _autoTranslateHumanGuidance(convId, r.roundNum, r.guidanceQuestion, r.guidanceType || 'free_text', r.guidanceOptions || []);
    }
  }
}

/**
 * Auto-translate Human Guidance question & options (EN→CN).
 * Called when a `human_guidance_request` SSE event arrives and conv.autoTranslate is ON.
 * Translates asynchronously; re-renders the HG card when translation completes.
 */
async function _autoTranslateHumanGuidance(convId, roundNum, question, responseType, options) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return;
  const assistantMsg = [...conv.messages].reverse().find(m => m.role === 'assistant');
  if (!assistantMsg || !assistantMsg.toolRounds) return;
  const round = assistantMsg.toolRounds.find(r => r.roundNum === roundNum);
  if (!round || round.status !== 'awaiting_human') return;

  // ★ Helper: sync assistantMsg.toolRounds → buf.toolRounds so that the
  //   reactive rendering pipeline (twUpdate → updateStreamingUI → _syncToolRoundsDOM)
  //   sees translation-related flags (_hgTranslating, _translatedQuestion, etc.).
  //   Without this, buf.toolRounds is a stale shallow copy from the
  //   human_guidance_request handler and never gets updated.
  function _syncHgToBuf() {
    const buf = streamBufs.get(convId);
    if (buf && assistantMsg.toolRounds) {
      buf.toolRounds = assistantMsg.toolRounds;
    }
  }

  // Mark as translating (shows spinner in the card)
  round._hgTranslating = true;
  _syncHgToBuf();
  twUpdate(convId);

  // ── Build a single translation batch: question + all option labels + descriptions ──
  // Concatenate all texts with a separator to make a single API call (cheaper & faster)
  const SEP = '\n‖‖‖\n'; // unique separator unlikely to appear in content
  const parts = [question];
  /* ★ Defensive: ensure `options` is an array before iterating. Some
   *   upstream callers (e.g. legacy persisted rounds) can pass null,
   *   a JSON string, or an object. */
  let _optsArr = options;
  if (typeof _optsArr === 'string') {
    try { _optsArr = JSON.parse(_optsArr); }
    catch (_e) { _optsArr = []; }
  }
  if (!Array.isArray(_optsArr)) _optsArr = [];
  if (responseType === 'choice' && _optsArr.length > 0) {
    for (const opt of _optsArr) {
      parts.push((opt && opt.label) || '');
      parts.push((opt && opt.description) || '');
    }
  }
  const batchText = parts.join(SEP);

  try {
    console.log(`[HG-Translate] Starting EN→CN translation for guidance=${round.guidanceId}, parts=${parts.length}`);
    const translated = await _callTranslateAPI(batchText, 'Chinese', 'English');
    // Split back by separator
    const translatedParts = translated.split(/\n?‖‖‖\n?/);

    // Re-find the round (may have changed during async)
    const conv2 = conversations.find(c => c.id === convId);
    if (!conv2) return;
    const msg2 = [...conv2.messages].reverse().find(m => m.role === 'assistant');
    if (!msg2 || !msg2.toolRounds) return;
    const round2 = msg2.toolRounds.find(r => r.roundNum === roundNum);
    if (!round2 || round2.status !== 'awaiting_human') return;

    // Apply translated question
    round2._translatedQuestion = translatedParts[0] || question;
    round2._hgTranslating = false;

    // Apply translated option labels & descriptions
    // ★ Defensive: round2.guidanceOptions may not be an array (see above).
    if (responseType === 'choice' && Array.isArray(round2.guidanceOptions)
        && translatedParts.length > 1) {
      for (let i = 0; i < round2.guidanceOptions.length; i++) {
        const labelIdx = 1 + i * 2;
        const descIdx = 2 + i * 2;
        if (translatedParts[labelIdx]) {
          round2.guidanceOptions[i]._translatedLabel = translatedParts[labelIdx];
        }
        if (translatedParts[descIdx] && round2.guidanceOptions[i].description) {
          round2.guidanceOptions[i]._translatedDescription = translatedParts[descIdx];
        }
      }
    }

    console.log(`[HG-Translate] ✓ Translation done for guidance=${round2.guidanceId}, ` +
      `question: ${question.length}→${round2._translatedQuestion.length} chars`);
    // ★ Sync translated properties to buf before re-render
    const buf2 = streamBufs.get(convId);
    if (buf2 && msg2.toolRounds) {
      buf2.toolRounds = msg2.toolRounds;
    }
    twUpdate(convId);
  } catch (e) {
    console.warn(`[HG-Translate] Translation failed: ${e.message} — showing original`);
    // Clear translating flag, show original untranslated
    const conv2 = conversations.find(c => c.id === convId);
    if (conv2) {
      const msg2 = [...conv2.messages].reverse().find(m => m.role === 'assistant');
      const round2 = msg2?.toolRounds?.find(r => r.roundNum === roundNum);
      if (round2) {
        round2._hgTranslating = false;
        // ★ Sync cleared flag to buf before re-render
        const buf2 = streamBufs.get(convId);
        if (buf2 && msg2.toolRounds) {
          buf2.toolRounds = msg2.toolRounds;
        }
        twUpdate(convId);
      }
    }
  }
}

/**
 * Start auto-translate for an assistant message. Thin wrapper around the
 * unified _runTranslationPipeline (defined in translation.js).
 */
async function _startAutoTranslateForMsg(conv, convId, idx, msg) {
  return _runTranslationPipeline(conv, idx, msg, {
    sourceLang: 'English',
    targetLang: 'Chinese',
    field: 'translatedContent',
    mode: 'auto',
  });
}
