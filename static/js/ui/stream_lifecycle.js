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
  const inner = document.getElementById("chatInner");
  const container = document.getElementById("chatContainer");
  /* ★ SCROLL FIX (parked-reader yank + flash): this function does a full
   *   `inner.innerHTML` wipe + rebuild of the whole message list and used to
   *   ALWAYS end in `_forceScrollToBottom`. During a live turn ANY renderChat()
   *   funnels here via Guard 1c (chat_render.js) — e.g. a cold-round
   *   `tool_compacted` fires `renderChat(conv,false)` once per tool round —
   *   so a reader scrolled UP to read history was repeatedly yanked to the
   *   bottom while the whole list flashed (the reported "keeps forcing me back
   *   to the bottom + the bottom block re-renders several times"). Mirror the
   *   fix already in renderChat's full-render path and `_bgRefreshChat`:
   *   capture the reader's viewport anchor from the OLD DOM when they are parked
   *   UP on the SAME conversation, then re-pin it after the rebuild instead of
   *   force-scrolling. A genuine conversation SWITCH (DOM still shows a
   *   different conv → `_sameConvDom` false), a first load, or a near-bottom
   *   reader still lands at the bottom via `_forceScrollToBottom`. The check
   *   MUST read `_lazyConvId` BEFORE the reassignment below. */
  const _sameConvDom = _lazyConvId === convId
    && inner && !!inner.querySelector('[id^="msg-"]');
  const _readerNearBottom = (typeof isNearBottom === 'function')
    ? isNearBottom(120) : true;
  const _preSwapAnchor = (_sameConvDom && !_readerNearBottom
      && typeof _captureScrollAnchor === 'function')
    ? _captureScrollAnchor(container, inner)
    : null;
  _destroyLazyObserver();
  _lazyConvId = convId;
  _lastRenderedFingerprint = "";

  /* ★ FIX (Root Cause 3): Only drop the trailing message when it actually
   * owns the streaming bubble (in-progress assistant, or in-progress critic).
   * If the last message is a user/optimizer/critic-done entry, buildTurnNav
   * will still create a dot for it, so we MUST render it statically — otherwise
   * msg-{last} is missing from the DOM and the turn-dot click is a no-op. */
  const _last = conv.messages[conv.messages.length - 1];
  /* ★ Bubble-owner IDENTITY binding (root fix for the mse9r2ir7ql0v4 incident,
   *   2026-08-05): the old positional heuristic treated ANY trailing assistant
   *   without an explicit `.done` as the live bubble's owner — but NO persisted
   *   message ever carries `.done` (it is a client-only streaming marker), so
   *   any transient that registered a stream while a SETTLED turn sat at the
   *   tail hid that whole turn behind the bubble (slice(0,-1) drops it): with
   *   the list at [m0, settled-m1] the screen showed exactly "first user turn
   *   glued to the streaming bubble" — the reported corruption, unreachable by
   *   every data-layer guard because it is a RENDER-layer verdict. Placement
   *   decisions must use server-assigned stable identity, never transient
   *   client state (RENDER_CONTRACT): the bubble owner is the message the live
   *   stream entry accumulates into — object identity with the bound
   *   assistantMsg, or the _taskId stamped at bind time. Endpoint planner /
   *   critic and the autopilot VU tail keep their explicit flags. */
  const _liveEntry = (typeof activeStreams !== 'undefined')
    ? activeStreams.get(convId) : null;
  const _lastIsStreamingBubble =
    !!_last && (
      (!!_liveEntry && (
        _last === _liveEntry.assistantMsg ||
        (_last.role === "assistant" && !!_last._taskId
          && _last._taskId === _liveEntry.taskId)
      )) ||
      (_last._isEndpointPlanner && !_last.done) ||
      (_last._swarmAutoContinue && !_last.done) ||
      (_last._isEndpointReview && !_last.done) ||
      /* ★ Autopilot VU tail: the streaming bubble is owned by a role=user
       *   VU placeholder (`_isVirtualUser` + `_streamingVu`), NOT an assistant.
       *   Without this arm a mid-stream renderChat (funneled here by Guard 1c
       *   once `#streaming-msg` exists) rebuilds the list STATICALLY, painting
       *   the frozen `vu-composing` "Autopilot starting…" pulse and never
       *   recreating the live `#streaming-msg` — so every later
       *   `autopilot_vu_event` frame hits `_twFlush` with no `#streaming-body`
       *   and is dropped. The bubble then sits frozen on the warm-up label
       *   while the elapsed timer keeps ticking. */
      (_last._isVirtualUser && _last._streamingVu)
    );
  const renderMsgs = _lastIsStreamingBubble
    ? conv.messages.slice(0, -1)
    : conv.messages;
  const total = renderMsgs.length;
  const startIdx = Math.max(0, total - _INITIAL_RENDER);
  _lazyRenderedFrom = startIdx;
  /* Tail is uncapped after this full rebuild (streaming bubble owns the very
   * bottom); reset the upper window bound. */
  _lazyRenderedTo = total;

  let html = "";
  if (startIdx > 0) {
    _ensureLazyObserver();
    html += `<div id="_lazyLoadSentinel" class="lazy-sentinel"><span class="lazy-sentinel-text">⬆ <span class="_lazy-count">${startIdx}</span> older messages</span></div>`;
  }
  for (let i = startIdx; i < total; i++) {
    html += renderMessage(renderMsgs[i], i);
  }

  const lastMsg = _last;
  /* ★ The live bubble is re-created whenever a stream is registered for this
   *   conv — INDEPENDENT of whether the array tail currently owns it. The old
   *   coupling re-created the bubble only when the tail was the bubble owner,
   *   so a stream whose bound message was not (or not yet) the tail lost its
   *   live UI on the next rebuild. Kind + data-msg-id come from the stream's
   *   BOUND message (identity), falling back to the tail's lane flags. */
  const _boundMsg = (_liveEntry && _liveEntry.assistantMsg) || null;
  const _smSrc = _boundMsg || lastMsg;
  const _smTime = formatClockTime(_smSrc && _smSrc.timestamp);
  if (_liveEntry || _lastIsStreamingBubble) {
    /* ★ Carry the message's stable _msgId onto the rebuilt bubble's
     *   data-msg-id. Without it, the live per-round translation preview
     *   (_renderStreamingTranslatePreview, routed by data-msg-id) can no
     *   longer target this bubble after any mid-stream full re-render, so
     *   the Chinese stops filling in until the task ends. */
    const _smMsgId = (_smSrc && _smSrc._msgId) || null;
    if (_smSrc.role === "assistant" && _smSrc._isEndpointPlanner) {
      html += _streamingBubbleHTML('planner', 'Planning…', _smTime, _smMsgId);
    } else if (_smSrc.role === "assistant" && _smSrc._swarmAutoContinue) {
      html += _streamingBubbleHTML('swarm', 'Continuing…', _smTime, _smMsgId);
    } else if (_smSrc.role === "assistant") {
      html += _streamingBubbleHTML('worker', 'Streaming…', _smTime, _smMsgId);
    } else if (_smSrc._isVirtualUser) {
      /* Autopilot VU (role=user, machine-authored) — stream in the USER lane
       * through the SAME substrate as the worker so its reply + tool rounds
       * render identically. `null` status → the `autopilot.warming` default,
       * which the first forwarded phase / delta immediately replaces. */
      html += _streamingBubbleHTML('autopilot', null, _smTime, _smMsgId);
    } else if (_smSrc._isEndpointReview) {
      html += _streamingBubbleHTML('critic', 'Reviewing…', _smTime, _smMsgId);
    }
  }
  inner.innerHTML = html;
  if (startIdx > 0) {
    const sentinel = document.getElementById("_lazyLoadSentinel");
    if (sentinel) _lazyObserver.observe(sentinel);
  }
  requestAnimationFrame(() => buildTurnNav(conv));
  /* ★ SCROLL FIX (see the _preSwapAnchor capture at the top): when the reader
   *   was parked UP on this same conversation, re-pin their viewport instead of
   *   yanking to the bottom. The innerHTML wipe above reset scrollTop→0, so
   *   under the cv-off guard (real heights, not the content-visibility:auto
   *   estimate) re-pin the anchor element to its prior offset. Otherwise
   *   (switch / first load / near-bottom reader) land at the bottom as before. */
  if (_preSwapAnchor && container && typeof _restoreScrollAnchor === 'function') {
    inner.classList.add('cv-off');
    void inner.scrollHeight;  // force real heights before re-pinning
    _restoreScrollAnchor(container, _preSwapAnchor);
    inner.classList.remove('cv-off');
  } else {
    _forceScrollToBottom(null, true);
  }
  updateSendButton();
  if ((_liveEntry || _lastIsStreamingBubble) && _smSrc) {
    /* §7: project straight from the message document; phase from the live
     * session slice. */
    const _sess = (typeof streamSessions !== 'undefined') ? streamSessions.get(convId) : null;
    updateStreamingUI({
      thinking: _smSrc.thinking || "",
      content: _smSrc.content || "",
      toolRounds: getToolRoundsFromMsg(_smSrc),
      phase: (_sess && _sess.phase) || null,
      _memoryPrefetch: _smSrc._memoryPrefetch,
      _mcpLoginHint: _smSrc._mcpLoginHint,
      fallbackModel: _smSrc.fallbackModel,
      fallbackFrom: _smSrc.fallbackFrom,
      fallbackReason: _smSrc.fallbackReason,
      fallbackKind: _smSrc.fallbackKind,
    });
    /* ★ Repaint the live translation preview immediately after the bubble is
     *   rebuilt. The body's innerHTML was just replaced, destroying any
     *   translatePreview zone, and the next server push frame may be 20-40s
     *   away (one per tool round). Re-render the last partial we stashed on
     *   the message so the Chinese-so-far survives the rebuild instead of
     *   blanking until the next round closes. No-op when nothing was
     *   translated yet or this isn't the streaming bubble. */
    if (_smSrc._translatePartial && _smSrc._msgId
        && typeof _renderStreamingTranslatePreview === 'function') {
      _renderStreamingTranslatePreview(convId, _smSrc._msgId, _smSrc._translatePartial, _smSrc._translatePartialByRound);
    }
    /* ★ FIX: After page refresh, SSE data may arrive AFTER this initial render.
     *   Schedule a deferred re-render (300ms) so that any SSE state event that
     *   arrives during the connection setup window gets rendered — without this,
     *   the user sees "Waiting…" until the NEXT SSE event triggers twUpdate. */
    const _deferConvId = convId;
    const _deferLastMsg = _smSrc;
    setTimeout(() => {
      if (activeConvId !== _deferConvId) return;           // user switched away
      if (!activeStreams.has(_deferConvId)) return;         // stream finished
      /* §7: project straight from the message document — the single live
       *   fact-source. Any SSE frame that landed during the connection-setup
       *   window has already been applied to the message by the pipeline, so
       *   re-reading it here paints that state without a second buffer. Phase
       *   still comes from the live session slice. */
      const _deferSess = (typeof streamSessions !== 'undefined')
        ? streamSessions.get(_deferConvId) : null;
      updateStreamingUI({
        thinking: _deferLastMsg.thinking || "",
        content: _deferLastMsg.content || "",
        toolRounds: getToolRoundsFromMsg(_deferLastMsg),
        phase: (_deferSess && _deferSess.phase) || null,
        _memoryPrefetch: _deferLastMsg._memoryPrefetch,
        _mcpLoginHint: _deferLastMsg._mcpLoginHint,
        fallbackModel: _deferLastMsg.fallbackModel,
        fallbackFrom: _deferLastMsg.fallbackFrom,
        fallbackReason: _deferLastMsg.fallbackReason,
        fallbackKind: _deferLastMsg.fallbackKind,
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
    /* ★ Empty-bubble root fix ③: IN-SESSION ghost-tail self-heal. Apply the
     *   SAME verdict the backend GET/startup reconcile applies
     *   (lib/conversations/reconcile.py::classify_ghost_tail) to the trailing
     *   assistant at turn end, so an empty/thinking-only husk is settled NOW —
     *   not left for the next warm reopen. This closes the residual window
     *   where a bare empty tail escaped fix ② (e.g. a swallowed done → poll
     *   fallback → finishStream with no `done` event to splice on). The verdict
     *   is byte-equivalent to the backend (pinned by the equivalence test):
     *     • 'delete'    → a bare empty husk (no content/thinking/finishReason/
     *                     usage/error/real-tool-round, not a special turn) →
     *                     splice it out so the in-memory list matches the
     *                     backend-reconciled DB (which drops it too);
     *     • 'interrupt' → a thinking-only husk → stamp finishReason='interrupted'
     *                     in place (preserve the reasoning), NOT delete;
     *     • null        → settled / special / keep — untouched. */
    if (lastMsg && lastMsg.role === 'assistant'
        && typeof _classifyGhostTailJS === 'function'
        && !_streamBoundToMsg(lastMsg)) {
      const _verdict = _classifyGhostTailJS(lastMsg);
      if (_verdict === 'delete') {
        conv.messages.pop();
        if (activeConvId === convId) {
          window.ConvView.removeMessage(convId, lastMsg._msgId || conv.messages.length);
        }
        if (activeConvId === convId) {
          const _sm = document.getElementById('streaming-msg');
          if (_sm) { try { _sm.remove(); } catch (e) { /* detached */ } }
        }
        console.info(`[finishStream] 🧹 Ghost-tail self-heal (delete) — conv=${convId.slice(0,8)} ` +
          `removed bare empty trailing assistant (matches backend reconcile).`);
      } else if (_verdict === 'interrupt') {
        lastMsg.finishReason = 'interrupted';
        console.info(`[finishStream] 🩹 Ghost-tail self-heal (interrupt) — conv=${convId.slice(0,8)} ` +
          `stamped finishReason=interrupted on thinking-only trailing assistant.`);
      }
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
        /* ★ Stop-during-VU cleanup: if the tail is a STILL-STREAMING VU
         *   placeholder (user clicked Stop mid-VU, so autopilot_vu_cancel was
         *   never read off the aborted stream), splice it out LOCALLY before
         *   finalizing the parent. Without this the ghost _streamingVu bubble
         *   survives to the next render. The helper preserves
         *   conv._apPendingBaton (a conv field, not a message). No-op when the
         *   VU already settled (autopilot_vu_done cleared _streamingVu). */
        if (_fsLast && _fsLast._isVirtualUser && _fsLast._streamingVu
            && typeof _removeStreamingVuBubbleIfTail === 'function') {
          _removeStreamingVuBubbleIfTail(conv, convId);
          const _smNow = document.getElementById("streaming-msg");
          if (_smNow) { try { _smNow.remove(); } catch (e) { /* detached */ } }
        }
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
        window.ConvView.replaceAll(convId);
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
  // ★ UNIFICATION (2026-07-10): decide with TWO resolvers, not one.
  //   • _frozen   = convAutoTranslate(conv)          — the send-time value the
  //                 BACKEND safety net resolves off (settings.autoTranslate).
  //   • _effective = convAutoTranslateEffective(conv) — the live intent (global
  //                 toggle wins when ON), the SAME resolver the on-open retro
  //                 path uses.
  //   Old behaviour gated ONLY on _frozen, so a conv sent with autoTranslate
  //   frozen-OFF that the user then toggles ON globally got NOTHING at
  //   finalize — the translation only appeared when the user switched AWAY and
  //   the retro (_resumePendingTranslations, effective-gated) path fired. That
  //   is exactly the reported "nothing happens while focused, a big bar appears
  //   the moment I switch conversations" bug. Fix: converge the finalize
  //   decision on the effective resolver too.
  const _frozen = convAutoTranslate(conv);
  const _effective = convAutoTranslateEffective(conv);
  if (!_effective) {
    console.info(`[finishStream] autoTranslate is OFF (effective) — skipping all ` +
      `translation scheduling for conv=${convId.slice(0,8)} ` +
      `(conv.autoTranslate=${conv?.autoTranslate}, global=${autoTranslate})`);
  }
  if (_effective && conv) {
    /* Resolve the trailing translatable assistant message once (shared by both
     * branches below). Walk back past a trailing VU / image-gen row. */
    let _atIdx = -1, _atMsg = null;
    for (let i = conv.messages.length - 1; i >= 0; i--) {
      const m = conv.messages[i];
      if (m.role !== 'assistant' || m._isVirtualUser) continue;
      if (!m.content) break;
      if (m.translatedContent || m._translateDone === true) break;
      if (m._igResult || m._isImageGen || m._igResults) break;
      _atIdx = i; _atMsg = m;
      break;
    }

    if (_frozen) {
      /* ★ Backend-owned path (frozen-ON): translation is driven by the
       *   server-side safety net in
       *   lib/tasks_pkg/manager.py::_maybe_auto_translate_assistant (which
       *   resolves off the SAME frozen settings.autoTranslate). Kicking off a
       *   parallel client task here would race it (the slower clobbering the
       *   faster). So we only ARM the DB-polling watchdog so a dropped push
       *   frame still surfaces live without a conversation switch — it
       *   short-circuits cheaply once translatedContent / a client task is
       *   present, so it's a no-op when the push path works. */
      console.info(`[finishStream] Auto-translate handled by backend safety net — ` +
        `skipping client-side scheduling for conv=${convId.slice(0,8)}`);
      if (activeConvId === convId && _atMsg
          && typeof _armAutoTranslateWatchdog === 'function') {
        _armAutoTranslateWatchdog(convId, _atIdx, _atMsg);
      }
    } else if (_atMsg && typeof _startAutoTranslateForMsg === 'function') {
      /* ★ Effective-ON but frozen-OFF (the reported gap): the BACKEND will NOT
       *   translate (it resolves off the frozen-OFF settings.autoTranslate), so
       *   nothing happens unless the user switches away and the retro path
       *   fires. Schedule the client-side unified pipeline NOW so it translates
       *   at finalize, in place, exactly like a frozen-ON conv. The pipeline's
       *   translateClaim + server-side claim_inflight guards prevent any
       *   double-fire against a manual click or a later retro pass. */
      console.info(`[finishStream] Auto-translate effective-ON but frozen-OFF — ` +
        `scheduling client-side pipeline for conv=${convId.slice(0,8)} msg=${_atIdx}`);
      _startAutoTranslateForMsg(conv, convId, _atIdx, _atMsg);
    }
  }
  // ── ★ Terminal continuation (autopilot baton + server-queue drain) ──
  //   Extracted into _runTerminalContinuation so EVERY terminal path — the
  //   normal finishStream, AND the self-heal reclaim in
  //   _healStuckPlaceholder — routes the follow-up/queue dispatch through
  //   ONE server-authoritative funnel. Before this, a self-heal that cleared
  //   the running predicate (activeTaskId/activeStreams) but skipped
  //   finishStream would leave a server-spawned autopilot follow-up or queued
  //   message invisible until a manual refresh (the "autonomous flow must
  //   self-heal" invariant, violated). See _runTerminalContinuation.
  _runTerminalContinuation(convId);
}

/**
 * Stamp the supersede successor from a terminal frame (done / LATE done)
 * onto the conv — the WRITER half of the latestLiveTaskId wire
 * (pt_8dc03017 cutover completion). The backend ships the conv→latest-task
 * index on terminal frames (lib/chat_dispatch.py LATE-done synthesis +
 * orchestrator/_finalize.py real done) so the attach reducer in
 * _runTerminalContinuation can hop to the autopilot VU / follow-up task
 * WITHOUT a hand-carried baton. Called from the SSE done branch
 * (sse_pipeline.js). A later frame simply overwrites — the reducer
 * consumes the stamp on attach, so it can never re-fire stale.
 */
function _stampLatestLiveTask(conv, ev) {
  if (!conv || !ev) return;
  const tid = ev.latestLiveTaskId;
  if (typeof tid === 'string' && tid) conv._latestLiveTaskId = tid;
  /* ★ VU-carrier flag rides the same terminal frame (2026-07-26 contract):
   *   when the successor is a VU sub-task the attach must delegate to the
   *   VU connector (detached dummy, no Agent placeholder) instead of the
   *   normal connectToTask path.  Stored alongside the id (single-use). */
  conv._latestLiveTaskIsVu = !!ev.latestLiveTaskIsVu;
}
if (typeof window !== 'undefined') window._stampLatestLiveTask = _stampLatestLiveTask;

/**
 * The terminal continuation funnel: after a conversation's running predicate
 * has been cleared (task done / aborted / reclaimed), resolve and attach any
 * follow-up work the backend may already have spawned — an autopilot next
 * turn or an auto-dispatched queued message.
 *
 * SERVER-AUTHORITATIVE by design (the root-cause requirement): the inline
 * `_apPendingBaton` is only a FAST-PATH optimisation available when a `done`
 * event actually arrived and stamped it. On a swallowed-done self-heal the
 * baton was NEVER stamped, so we MUST fall through to `_checkForQueuedTask`,
 * which probes `/api/chat/active` — the authority — and discovers the
 * follow-up/queued task the backend spawned regardless of whether any inline
 * baton survived. This is the ONLY way the empty-ghost / phantom-carrier case
 * (the task produced nothing, but a queued message or autopilot follow-up is
 * waiting behind it) self-heals without a manual refresh.
 *
 * MUST be called by every terminal path (finishStream, self-heal reclaim) so
 * the baton + queue-drain can never be dropped by one path diverging.
 *
 * @param {string} convId
 */
function _runTerminalContinuation(convId) {
  const conv = conversations.find((c) => c.id === convId);
  // ── ★ Supersede-index reducer (epic pt_8dc030176bad450b, build-order step 2)
  //    THE FUTURE PRIMARY PATH — a no-op TODAY (guarded so it cannot regress).
  //
  //    Target mechanism (design §4): an autopilot chain is a plain sequence of
  //    independent tasks (parent → VU → follow-up), each registered under the
  //    REAL convId. After ANY turn's done, if the conv's server-authoritative
  //    latest live task is a DIFFERENT pending/running task, attach to it — the
  //    SAME signal on SSE, poll, and cold reload, so no hand-carried baton is
  //    needed. This single rule (attach-to-newer-live-task) replaces the whole
  //    baton on cutover.
  //
  //    WHY IT IS A NO-OP TODAY: the field `conv._latestLiveTaskId` is only
  //    populated by the cutover backend (which stops the VU's `convId=''`
  //    opt-out and advances `_record_latest_task` to the VU BEFORE emitting the
  //    parent done — the HB-1 happens-before, design §4.1). Until then the VU
  //    is invisible to the conv→latest-task index, this field is absent, and
  //    the block short-circuits, leaving the existing baton fast-path below
  //    fully in charge. Shipping it now is safe and lets the cutover be a pure
  //    backend change.
  //
  //    IDEMPOTENT: we only attach when the target differs from the task we are
  //    already on (activeTaskId / an active stream), so a done observed on BOTH
  //    sse and a poll-fallback cannot double-attach (design §5, hazard 2).
  const _liveTaskId = conv && conv._latestLiveTaskId;
  if (_liveTaskId && conv
      && _liveTaskId !== conv.activeTaskId
      && !activeStreams.has(convId)
      && typeof connectToTask === 'function') {
    console.info(
      `%c[Autopilot] ▶ Supersede-index attach: conv=${convId.slice(0,8)} ` +
      `latestLiveTask=${_liveTaskId.slice(0,8)} (index-driven, no baton)`,
      'color:#a78bfa;font-weight:bold'
    );
    /* ★ Consume the stamp FIRST so a later continuation can never re-attach
     *   to this (now-aging) successor — the stamp is single-use. */
    conv._latestLiveTaskId = null;
    const _liveTaskIsVu = !!conv._latestLiveTaskIsVu;
    conv._latestLiveTaskIsVu = false;
    /* ★ Reload messages BEFORE opening the successor's stream: when the
     *   successor is an autopilot follow-up, the VU-synthesized user turn
     *   was already persisted by the backend hook — it must land in
     *   conv.messages before connectToTask appends its streaming bubble
     *   (mirrors _checkForQueuedTask's attach). On the VU-hop itself the
     *   reload is a cheap no-op (the VU runs on _inline_messages). */
    (async () => {
      try {
        conv._needsLoad = true;
        if (typeof loadConversationMessages === 'function') {
          await loadConversationMessages(convId);
        }
      } catch (e) {
        console.warn('[Autopilot] supersede reload failed (attaching anyway):', e);
      }
      /* Re-check after the await: a send/stream may have started meanwhile
       * (connectToTask re-guards on activeStreams internally too). */
      if (activeStreams.has(convId)) return;
      /* ★ VU successor → the VU connector (detached dummy assistant, no
       *   Agent placeholder); worker successor → the normal path. */
      connectToTask(convId, _liveTaskId, 0, _liveTaskIsVu ? { vuCarrier: true } : {});
    })();
    return;
  }
  // ── ★ Autopilot in-band follow-up (FAST PATH): when the done/poll event
  //    carried autopilotNextTaskId + autopilotVuMessage, the backend already
  //    appended the synthetic user message to conv DB and spawned the next
  //    task.  Attach to it directly instead of going through the queue-poll
  //    path.  This eliminates the race where the VU LLM call took longer than
  //    the polling retry budget (~15s) and the synthetic user msg + follow-up
  //    task stayed invisible until manual page refresh.
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

  // ── ★ Server-side queue (AUTHORITY): always check for an auto-dispatched
  // next task.  The backend's persist_task_result → _dispatch_queued_message
  // checks the message_queue table and auto-dispatches the next message; it
  // also spawns the autopilot follow-up task even when no inline baton
  // reached us (swallowed done).  _checkForQueuedTask probes /api/chat/active
  // and attaches to whatever is running — no frontend gate, the backend is the
  // single source of truth for queue/follow-up state.
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
        const _qHtml = _streamingBubbleHTML('worker', 'Dispatching queued message…', _qTime);
        /* Tail insert via the shared furniture-aware primitive — a raw
         * `beforeend` lands below a bottom lazy-window sentinel. */
        if (typeof chatInnerInsert === 'function') {
          chatInnerInsert(inner, _qHtml, {
            position: 'tail',
            conv: conversations.find((c) => c.id === convId) || null,
            site: '_runTerminalContinuation:queued-placeholder',
          });
        } else {
          inner.insertAdjacentHTML('beforeend', _qHtml);
        }
        if (isNearBottom(80)) scrollToBottom();
      }
    } catch (e) {
      console.warn('[_runTerminalContinuation] queued-dispatch placeholder insert failed:', e);
    }
  }
  const _queuedCheckDelay = _hasQueued ? 0 : 500;
  setTimeout(() => _checkForQueuedTask(convId), _queuedCheckDelay);
}
if (typeof window !== 'undefined') window._runTerminalContinuation = _runTerminalContinuation;

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

  // §7: no buffer to sync — the reactive pipeline (twUpdate →
  // updateStreamingUI → _syncToolRoundsDOM) reads the message document
  // directly, so translation flags (_hgTranslating, _translatedQuestion)
  // are visible the moment they are stamped on assistantMsg.toolRounds.
  function _syncHgToBuf() { /* retired mirror — kept as a no-op for the 3 call sites below */ }

  // Mark as translating (shows spinner in the card)
  round._hgTranslating = true;
  _syncHgToBuf();
  if (typeof twUpdate === 'function') twUpdate(convId);

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
    // §7: translated properties already live on the document (msg2.toolRounds)
    if (typeof twUpdate === 'function') twUpdate(convId);
  } catch (e) {
    console.warn(`[HG-Translate] Translation failed: ${e.message} — showing original`);
    // Clear translating flag, show original untranslated
    const conv2 = conversations.find(c => c.id === convId);
    if (conv2) {
      const msg2 = [...conv2.messages].reverse().find(m => m.role === 'assistant');
      const round2 = msg2?.toolRounds?.find(r => r.roundNum === roundNum);
      if (round2) {
        round2._hgTranslating = false;
        if (typeof twUpdate === 'function') twUpdate(convId);
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
