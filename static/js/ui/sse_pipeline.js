/* ═══════════════════════════════════════════════════════════════════
   sse pipeline — extracted from ui.js (split 2026-05-28)

   SSE chat-stream pipeline: connectToTask, _trySSE, _pollFallback, updateSendButton.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ── Stream connection ──
async function connectToTask(convId, taskId, retries = 0) {
  const conv = conversations.find((c) => c.id === convId);
  if (!conv) return;
  /* ★ CROSS-TALK DETECTION: log full stream context at connection time */
  console.info(
    `[connectToTask] 🔗 Connecting — conv=${convId.slice(0,8)} task=${taskId.slice(0,8)} ` +
    `activeConvId=${(typeof activeConvId !== 'undefined' ? activeConvId?.slice(0,8) : 'N/A')||'null'} ` +
    `msgs=${conv.messages.length} activeStreams=[${[...activeStreams.keys()].map(k=>k.slice(0,8)).join(',')}] ` +
    `retries=${retries}`
  );
  /* ★ Stable-id targeting: if a stream entry already exists for this conv
   *   (e.g. SSE reconnect after a network blip), prefer the assistantMsg
   *   it already targets so we keep accumulating into the SAME object the
   *   delta handler closure has been writing to.  Falls back to the array
   *   tail for fresh connections. */
  let assistantMsg = null;
  const _existingStream = activeStreams.get(convId);
  if (_existingStream && _existingStream.assistantMsg && _existingStream.assistantMsg._msgId) {
    const _live = _resolveAssistantById(conv, _existingStream.assistantMsg._msgId, null);
    if (_live) {
      assistantMsg = _live;
      console.info(
        `[connectToTask] 🎯 Re-targeting via stable id msgId=${_live._msgId.slice(0,12)} ` +
        `(reconnect for conv=${convId.slice(0,8)})`
      );
    }
  }
  if (!assistantMsg) assistantMsg = conv.messages[conv.messages.length - 1];

  /* ★ Stale-prior-turn guard — if the last assistant message belongs to a
   *   DIFFERENT, already-completed task (different `_taskId`, or has a
   *   `finishReason` set, or carries explicit `_doneAt`), the new task's
   *   SSE stream must NOT reuse it as the target.  Reusing it causes the
   *   previous turn's full content to be re-rendered into the new bubble
   *   (the bug the user reported: "上一轮对话又重新流式吐出").  Push a
   *   fresh placeholder instead.  Skip endpoint mode — the next block has
   *   its own logic that already handles critic→worker transitions. */
  if (assistantMsg && assistantMsg.role === 'assistant'
      && !conv.messages.some(m => m._epIteration)) {
    const _staleTaskId = assistantMsg._taskId && assistantMsg._taskId !== taskId;
    const _isCompletedTurn = !!assistantMsg.finishReason;
    if (_staleTaskId || _isCompletedTurn) {
      console.info(
        `[connectToTask] 🆕 Last assistant belongs to a prior turn ` +
        `(taskId=${assistantMsg._taskId?.slice(0,8) || 'none'} vs new=${taskId.slice(0,8)}, ` +
        `finishReason=${assistantMsg.finishReason || 'none'}) — pushing fresh placeholder ` +
        `for conv=${convId.slice(0,8)} so SSE doesn't replay old content into it`
      );
      assistantMsg = {
        role: 'assistant',
        content: '',
        thinking: '',
        timestamp: Date.now(),
        toolRounds: [],
        model: conv.model || (typeof serverModel !== 'undefined' ? serverModel : ''),
      };
      _ensureMsgId(assistantMsg);
      conv.messages.push(assistantMsg);
    }
  }

  /* ★ Endpoint mode reconnection: if the last message is a critic review
   *   (role=user, _isEndpointReview), we may need to create a fresh
   *   assistant message for the next worker turn that's about to start.
   *
   *   ROOT-CAUSE GUARD — the placeholder must ONLY be created when the
   *   backend is about to start a new worker turn.  If the critic has
   *   already approved ([VERDICT: STOP] / _epApproved) or chose
   *   CONTINUE_PLANNER, NO new worker turn is coming — creating a ghost
   *   placeholder here was the bug where the previous worker's content
   *   reappeared as a duplicate after Critic STOP (SSE replay / poll
   *   fallback writes the last worker's `td.content` into the ghost
   *   placeholder).  The authoritative SSE `state` event (endpointPhase)
   *   or live `endpoint_iteration(phase='working')` event will push a
   *   proper worker message when a new worker turn truly starts. */
  const hasEpTurns = conv.messages.some(m => m._epIteration);
  if (hasEpTurns && assistantMsg && assistantMsg.role !== "assistant") {
    const lastCriticApproved = !!(assistantMsg._isEndpointReview
      && (assistantMsg._epApproved
          || assistantMsg._epNextPhase === 'stop'
          || assistantMsg._epNextPhase === 'planner'));
    if (lastCriticApproved) {
      console.info(
        `[connectToTask] 🛡  Endpoint reconnect — last critic is ` +
        `${assistantMsg._epNextPhase || 'stop'} (approved=${!!assistantMsg._epApproved}); ` +
        `NOT creating a ghost worker placeholder for conv=${convId.slice(0,8)}`
      );
    } else {
      // The last message is a critic review awaiting a new worker turn —
      // create a placeholder assistant msg for the incoming worker.
      assistantMsg = {
        role: "assistant",
        content: "",
        thinking: "",
        toolRounds: [],
        timestamp: new Date().toISOString(),
        _epIteration: (assistantMsg._epIteration || 0) + 1,
      };
      _ensureMsgId(assistantMsg);
      conv.messages.push(assistantMsg);
    }
  }

  if (!assistantMsg || assistantMsg.role !== "assistant") {
    /* ★ FIX: Defensive recovery — if the last message is not assistant (e.g.
     *   loadConversationMessages Phase 2 overwrote conv.messages during a race
     *   with startAssistantResponse), push a fresh assistant message so the SSE
     *   stream has somewhere to accumulate content. Without this, connectToTask
     *   silently bails out → no streaming UI, but sidebar shows pulsing dot. */
    console.warn(
      `[connectToTask] ⚠️ Last msg is ${assistantMsg?.role || 'missing'}, not assistant — ` +
      `pushing recovery assistant msg for conv=${convId.slice(0,8)} task=${taskId.slice(0,8)}`
    );
    assistantMsg = {
      role: "assistant",
      content: "",
      thinking: "",
      timestamp: Date.now(),
      toolRounds: [],
      model: conv.model || (typeof serverModel !== 'undefined' ? serverModel : ''),
    };
    _ensureMsgId(assistantMsg);
    conv.messages.push(assistantMsg);
  }
  if (!activeStreams.has(convId)) {
    const controller = new AbortController();
    activeStreams.set(convId, { controller, taskId, assistantMsg });
    renderConversationList();
    updateSendButton();
    if (activeConvId === convId) {
      _lastRenderedFingerprint = "";
      const inner = document.getElementById("chatInner");
      const lastIdx = conv.messages.length - 1;
      const existing = document.getElementById(`msg-${lastIdx}`);
      if (existing) existing.remove();
      if (!document.getElementById("streaming-msg")) {
        /* ★ Detect endpoint planner phase so reconnection shows "Planner"
         *   instead of "Agent".  Check: the assistantMsg has _isEndpointPlanner,
         *   or the conv has endpointEnabled and no worker turns yet (iteration 0). */
        const _isEpPlanner = assistantMsg._isEndpointPlanner
          || (conv.endpointEnabled && !conv.messages.some(m => m._epIteration));
        const _reconRole = _isEpPlanner ? 'planner' : 'worker';
        /* ★ FIX (refresh-into-running-task UX):
         *   When we reconnect to an in-flight task, assistantMsg may
         *   ALREADY carry partial content/thinking/toolRounds that the
         *   server persisted before our refresh.  Previously we removed
         *   the rendered msg-N bubble and inserted an empty placeholder
         *   that just said "Connecting…", then waited for SSE replay
         *   to rebuild from event 0 — which made every refresh look
         *   like the task was regenerating from scratch (the bug the
         *   user just hit on conv movmck3a2x6jyk task 2f39395c).
         *   Now we pre-render the existing partial content so the
         *   bubble shows real progress immediately, and SSE deltas
         *   merely append on top.
         *   Status reflects task age: "Resuming…" if we have any prior
         *   content/thinking/toolRounds, plain "Connecting…" otherwise. */
        const _hasPartial = !!(
          (assistantMsg.content && assistantMsg.content.length) ||
          (assistantMsg.thinking && assistantMsg.thinking.length) ||
          (assistantMsg.toolRounds && assistantMsg.toolRounds.length)
        );
        const _reconStatus = _isEpPlanner
          ? 'Planning…'
          : (_hasPartial ? 'Resuming…' : 'Connecting…');
        const _reconTime = new Date(assistantMsg.timestamp || Date.now())
          .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        inner.insertAdjacentHTML('beforeend', _streamingBubbleHTML(_reconRole, _reconStatus, _reconTime));
        /* Pre-populate streaming-body with whatever content was already
         * persisted, so the user sees real progress before SSE arrives.
         * The first delta from _trySSE will replace .stream-status with
         * actual content via the normal append path. */
        if (_hasPartial) {
          try {
            const _body = document.getElementById('streaming-body');
            if (_body) {
              let _html = '';
              if (assistantMsg.thinking && assistantMsg.thinking.length) {
                _html += `<details class="thinking-block" open>` +
                  `<summary>Thinking</summary>` +
                  `<div class="thinking-body">${renderMarkdown(assistantMsg.thinking)}</div>` +
                  `</details>`;
              }
              if (assistantMsg.content && assistantMsg.content.length) {
                _html += `<div class="md-content">${renderMarkdown(assistantMsg.content)}</div>`;
              }
              _html += `<div class="stream-status"><div class="pulse"></div> Resuming…</div>`;
              _body.innerHTML = _html;
              console.info(
                `[connectToTask] 🔁 Pre-populated bubble — content=${(assistantMsg.content||'').length}c ` +
                `thinking=${(assistantMsg.thinking||'').length}c ` +
                `toolRounds=${(assistantMsg.toolRounds||[]).length} for conv=${convId.slice(0,8)}`
              );
            }
          } catch (_e) {
            console.warn(`[connectToTask] pre-populate failed (non-fatal): ${_e.message}`);
          }
        }
        scrollToBottom();
      }
    }
    twStart(convId);
    const buf = streamBufs.get(convId);
    if (assistantMsg.toolRounds)
      buf.toolRounds = [...assistantMsg.toolRounds];
    else if (assistantMsg.searchResults)
      buf.toolRounds = [
        {
          roundNum: 1,
          query: assistantMsg.searchQuery || "search",
          results: assistantMsg.searchResults,
          status: "done",
        },
      ];
  }
  const stream = activeStreams.get(convId);
  let sseWorked = false;
  try {
    sseWorked = await _trySSE(convId, taskId, stream, assistantMsg);
  } catch (e) {
    if (e.name === "AbortError") {
      // ★ User clicked Stop — set finishReason BEFORE finishStream
      const _abortConv = conversations.find(c => c.id === convId);
      if (_abortConv) {
        const _abortMsg = _abortConv.messages[_abortConv.messages.length - 1];
        if (_abortMsg && _abortMsg.role === 'assistant') {
          _abortMsg.finishReason = 'aborted';
          console.log(`[connectToTask] User abort — set finishReason='aborted' for conv=${convId.slice(0,8)}`);
        }
      }
      twStop(convId);
      finishStream(convId);
      return;
    }
    debugLog(`SSE failed: ${e.message}`, "warn");
  }
  if (!sseWorked && !stream.controller.signal.aborted) {
    debugLog(`Falling back to polling for ${taskId.slice(0, 8)}`, "warn");
    await _pollFallback(convId, taskId, stream, assistantMsg);
  }
}

async function _trySSE(convId, taskId, stream, assistantMsg) {
  let lastSave = Date.now(),
    gotData = false;
  let sseTimeout = setTimeout(() => {
    if (!gotData) stream.controller.abort();
  }, 30000);
  let buf = streamBufs.get(convId);
  /* ── Stable identity capture ──
   * Pin the assistantMsg reference by its `_msgId` (minted by
   * `_ensureMsgId` / server's `_assign_message_ids`).  Phase-2
   * reconciliation or any concurrent push can replace
   * `conv.messages[length-1]` mid-stream — without this re-resolve we
   * accumulate into a detached object and the renderer never sees the
   * data (the "Autopilot content invisible until stop+refresh" bug).
   * If the message has no id yet, stamp one so subsequent rebinds work. */
  if (assistantMsg && !assistantMsg._msgId && typeof _ensureMsgId === 'function') {
    _ensureMsgId(assistantMsg);
  }
  const _pinnedMsgId = assistantMsg && assistantMsg._msgId;
  /** Re-bind `assistantMsg` to whatever object currently lives in
   *  conv.messages for `_pinnedMsgId`.  Returns the resolved object so
   *  callers can use it locally even if the outer closure already cached
   *  a stale ref.  Logs a `[StableId]` warning when a recovery happens. */
  function _rebindAssistant() {
    if (!_pinnedMsgId) return assistantMsg;
    const _conv = conversations.find(c => c.id === convId);
    if (!_conv) return assistantMsg;
    const live = _resolveAssistantById(_conv, _pinnedMsgId, null);
    if (live && live !== assistantMsg) {
      const _autopilotOn = !!_conv.autopilotEnabled;
      console.warn(
        `[StableId]${_autopilotOn ? '[Autopilot]' : ''} 🔁 Rebinding dangling assistantMsg ` +
        `for conv=${convId.slice(0,8)} msgId=${_pinnedMsgId.slice(0,12)} — ` +
        `prev contentLen=${(assistantMsg?.content||'').length} live contentLen=${(live.content||'').length}`
      );
      assistantMsg = live;
    } else if (!live) {
      const _autopilotOn = !!_conv.autopilotEnabled;
      console.warn(
        `[StableId]${_autopilotOn ? '[Autopilot]' : ''} ⚠ msgId=${_pinnedMsgId.slice(0,12)} ` +
        `not found in conv.messages (${_conv.messages.length} msgs) — keeping closure ref`
      );
    }
    return assistantMsg;
  }
  /* ── Endpoint critic-phase guard ──
   * When the critic is running, it streams delta/tool events through the same
   * SSE pipe.  We must NOT accumulate those into the worker's assistantMsg.
   * Instead, we accumulate into a separate criticBuf and show a dedicated
   * streaming bubble for the critic's review.  */
  let _epCriticPhase = false;
  let _epCriticMsg = null;   // the critic message object in conv.messages
  let _epCriticBuf = null;   // {content, thinking, toolRounds}
  let _roundThinkingLen = 0; // thinking chars accumulated in current LLM call (reset on phase events)
  let _lastEventId = null; // ★ Item 6: track SSE event ID for reconnection
  function _processSSELine(line) {
    // ★ Capture id: field for Last-Event-ID reconnection
    if (line.startsWith("id: ")) {
      _lastEventId = line.slice(4).trim();
      return false;
    }
    if (!line.startsWith("data: ")) return false;
    /* ★ Re-resolve assistantMsg by stable id BEFORE every event.  This
     * is the single point that makes the streaming pipeline immune to
     * Phase-2 array overwrites / autopilot pushes that move the live
     * message object off `conv.messages[length-1]`. */
    _rebindAssistant();
    const ds = line.slice(6).trim();
    if (!ds) return false;
    let ev;
    try {
      ev = JSON.parse(ds);
    } catch {
      return false;
    }
    /* ★ Continue checkpoint: toolRounds to merge with newly streamed ones */
    if (ev.type === "state") {
      /* ★ SyncFix: discard stale state snapshots from an aborted/superseded
       *   task. Without this, a delayed SSE state event from the OLD task
       *   (after the user interrupted and hit Edit/Regen) can resurrect the
       *   truncated endpoint turns into conv.messages — manifesting as the
       *   old conversation reappearing without a page refresh. */
      const _stateConv = conversations.find(c => c.id === convId);
      if (_stateConv) {
        const _aborted = stream && stream.controller && stream.controller.signal.aborted;
        const _supersededByNewTask = _stateConv.activeTaskId && _stateConv.activeTaskId !== taskId;
        const _isLastAborted = _stateConv._lastAbortedTaskId === taskId;
        if (_aborted || _supersededByNewTask || _isLastAborted) {
          console.info(`[SyncFix][SSE state] discarding stale state taskId=${taskId.slice(0,8)} activeTaskId=${_stateConv.activeTaskId?.slice(0,8)||'null'} aborted=${!!_aborted} superseded=${!!_supersededByNewTask} isLastAborted=${!!_isLastAborted}`);
          return false;
        }
      }
      /* ★ Endpoint mode reconnection: rebuild conv.messages from endpointTurns
       *   and set the correct phase (working/reviewing) so streaming goes to
       *   the right target (assistantMsg vs _epCriticMsg). */
      if (ev.endpointMode && (ev.endpointPhase === 'planning' || (ev.endpointTurns && ev.endpointTurns.length > 0))) {
        /* ★ Fresh first connection in planning phase with no turns yet:
         *   startAssistantResponse already created the planner bubble — skip
         *   the full reconnection handler (renderChat + re-create streaming-msg)
         *   to avoid a visual flash. Just update the planner assistantMsg data. */
        const _hasTurns = ev.endpointTurns && ev.endpointTurns.length > 0;
        if (ev.endpointPhase === 'planning' && document.getElementById('streaming-msg')) {
          const conv = conversations.find(c => c.id === convId);
          if (conv) {
            _epCriticPhase = false;
            let plannerMsg = [...conv.messages].reverse().find(m => m._isEndpointPlanner);
            if (!plannerMsg) {
              // assistantMsg from startAssistantResponse or connectToTask should already be the planner
              if (assistantMsg && assistantMsg.role === 'assistant') {
                assistantMsg._isEndpointPlanner = true;
                plannerMsg = assistantMsg;
              }
            }
            if (plannerMsg) {
              plannerMsg.content = ev.content || plannerMsg.content || "";
              plannerMsg.thinking = ev.thinking || plannerMsg.thinking || "";
              if (ev.toolRounds) plannerMsg.toolRounds = ev.toolRounds;
              assistantMsg = plannerMsg;
              if (buf) {
                buf.thinking = assistantMsg.thinking;
                buf.content = assistantMsg.content;
                if (ev.toolRounds) buf.toolRounds = ev.toolRounds;
              }
            }
            /* ★ FIX: Update the streaming-msg DOM to show Planner role/avatar
             *   in case connectToTask created it with Agent styling */
            const sm = document.getElementById('streaming-msg');
            if (sm && activeConvId === convId) {
              if (!sm.classList.contains('ep-planner-msg')) {
                sm.classList.remove('ep-worker-msg');
                sm.classList.add('ep-planner-msg');
              }
              const roleEl = sm.querySelector('.message-role');
              if (roleEl && roleEl.textContent !== 'Planner') roleEl.textContent = 'Planner';
              const avatarEl = sm.querySelector('.message-avatar');
              if (avatarEl && typeof _TOFU_PLANNER_SVG !== 'undefined') {
                avatarEl.innerHTML = _TOFU_PLANNER_SVG;
              }
            }
            console.debug(`[SSE state] Endpoint planning — skipping full reconnect (turns=${(ev.endpointTurns||[]).length})`);
          }
        } else {
        const conv = conversations.find(c => c.id === convId);
        if (conv) {
          // Rebuild: keep base messages, replace endpoint turns with server copy
          let baseEnd = 0;
          for (let i = 0; i < conv.messages.length; i++) {
            if (!conv.messages[i]._epIteration && !conv.messages[i]._isEndpointReview && !conv.messages[i]._isEndpointPlanner) {
              baseEnd = i + 1;
            }
          }
          const baseMsgs = conv.messages.slice(0, baseEnd);
          conv.messages = baseMsgs.concat(ev.endpointTurns || []);

          if (ev.endpointPhase === 'planning') {
            // Planner is in progress — create a planner assistant msg
            _epCriticPhase = false;
            let plannerMsg = [...conv.messages].reverse().find(m => m._isEndpointPlanner);
            if (!plannerMsg) {
              plannerMsg = {
                role: "assistant", content: ev.content || "", thinking: ev.thinking || "",
                toolRounds: ev.toolRounds || [],
                timestamp: new Date().toISOString(),
                _isEndpointPlanner: true,
              };
              _ensureMsgId(plannerMsg);
              conv.messages.push(plannerMsg);
            }
            plannerMsg.content = ev.content || "";
            plannerMsg.thinking = ev.thinking || "";
            if (ev.toolRounds) plannerMsg.toolRounds = ev.toolRounds;
            assistantMsg = plannerMsg;
            if (buf) {
              buf.thinking = assistantMsg.thinking;
              buf.content = assistantMsg.content;
              if (ev.toolRounds) buf.toolRounds = ev.toolRounds;
            }
          } else if (ev.endpointPhase === 'reviewing') {
            // Critic is in progress — create a critic msg and set phase
            _epCriticPhase = true;
            _epCriticMsg = {
              role: "user", content: ev.content || "", thinking: ev.thinking || "",
              toolRounds: ev.toolRounds || [],
              timestamp: new Date().toISOString(),
              _isEndpointReview: true, _epIteration: ev.endpointIteration || 1,
              _epApproved: false, _isStuck: false,
            };
            _ensureMsgId(_epCriticMsg);
            conv.messages.push(_epCriticMsg);
            _epCriticBuf = {
              content: (_epCriticMsg.content || "").replace(/\[VERDICT:\s*(?:STOP|CONTINUE)\s*\]\s*$/i, "").trimEnd(),
              thinking: _epCriticMsg.thinking, toolRounds: [],
            };
            streamBufs.set(convId, _epCriticBuf);
            buf = _epCriticBuf;
            // Point assistantMsg to the last completed worker turn
            const lastWorker = [...conv.messages].reverse().find(m => m.role === "assistant");
            if (lastWorker) assistantMsg = lastWorker;
          } else if (ev.endpointPhase === 'done' || ev.endpointStopReason) {
            /* ★ Task already finalized by the backend (Critic STOP /
             *   max-iterations / aborted).  DO NOT create any new
             *   worker/critic/planner message — the authoritative
             *   endpoint turns have already been appended to
             *   conv.messages above.  Point assistantMsg at the last
             *   assistant turn so metadata (done event, finishStream)
             *   attaches to the correct bubble. */
            _epCriticPhase = false;
            const lastAssist = [...conv.messages].reverse().find(m => m.role === "assistant");
            if (lastAssist) assistantMsg = lastAssist;
            console.info(`[SSE state] Endpoint already finalized — ` +
              `phase=${ev.endpointPhase} stopReason=${ev.endpointStopReason || 'n/a'} ` +
              `conv=${convId.slice(0,8)} — skipping in-progress bubble creation`);
          } else {
            // Worker is in progress — find or create the current worker msg.
            // Guard against ghost-worker creation after Critic STOP: if the
            // last message is an approved critic, the task is finished even
            // if endpointPhase hasn't been updated yet — don't blindly push
            // a new worker turn (that was Bug 1).
            _epCriticPhase = false;
            const _lastMsg = conv.messages[conv.messages.length - 1];
            const _lastCriticApproved = _lastMsg && _lastMsg._isEndpointReview
              && (_lastMsg._epApproved || _lastMsg._epNextPhase === 'stop'
                  || _lastMsg._epNextPhase === 'planner');
            if (_lastCriticApproved) {
              const lastAssist = [...conv.messages].reverse().find(m => m.role === "assistant");
              if (lastAssist) assistantMsg = lastAssist;
              console.info(`[SSE state] Endpoint reconnect — last critic is ` +
                `${_lastMsg._epNextPhase || 'stop'}; skipping ghost worker creation ` +
                `conv=${convId.slice(0,8)}`);
            } else {
              const iterNum = ev.endpointIteration || 1;
              let workerMsg = conv.messages.find(m =>
                m.role === "assistant" && m._epIteration === iterNum);
              if (!workerMsg) {
                workerMsg = {
                  role: "assistant", content: "", thinking: "",
                  toolRounds: [], timestamp: new Date().toISOString(),
                  _epIteration: iterNum,
                };
                _ensureMsgId(workerMsg);
                conv.messages.push(workerMsg);
              }
              workerMsg.content = ev.content || "";
              workerMsg.thinking = ev.thinking || "";
              if (ev.toolRounds) workerMsg.toolRounds = ev.toolRounds;
              assistantMsg = workerMsg;
              if (buf) {
                buf.thinking = assistantMsg.thinking;
                buf.content = assistantMsg.content;
                if (ev.toolRounds) buf.toolRounds = ev.toolRounds;
              }
            }
          }

          console.info(`[SSE state] Endpoint reconnect — phase=${ev.endpointPhase} ` +
            `iter=${ev.endpointIteration} epTurns=${(ev.endpointTurns || []).length} ` +
            `totalMsgs=${conv.messages.length}`);

          // Re-render if active
          if (activeConvId === convId) {
            renderChat(conv);
            /* ★ FIX: renderChat rendered ALL messages including the in-progress
             *   one.  We need to remove that last static element before creating
             *   the streaming-msg, otherwise the in-progress message shows twice:
             *   once as a "dead" static element and once as the live streaming bubble. */
            const lastMsgIdx = conv.messages.length - 1;
            const staleRenderedEl = document.getElementById(`msg-${lastMsgIdx}`);
            if (staleRenderedEl) staleRenderedEl.remove();

            // Re-create streaming-msg for the in-progress turn
            const inner = document.getElementById("chatInner");
            const _reconRole = _epCriticPhase ? 'critic'
              : (ev.endpointPhase === 'planning' ? 'planner' : 'worker');
            const _reconStatus = _epCriticPhase ? 'Reviewing…'
              : (ev.endpointPhase === 'planning' ? 'Planning…' : 'Thinking…');
            if (inner) inner.insertAdjacentHTML("beforeend", _streamingBubbleHTML(_reconRole, _reconStatus));
            buildTurnNav(conv);
          }
        }
        } /* close else (full reconnection) */
      } else if (_epCriticPhase && _epCriticMsg) {
        /* State snapshot during critic phase → update critic msg */
        _epCriticMsg.content = ev.content || "";
        _epCriticMsg.thinking = ev.thinking || "";
        if (_epCriticBuf) {
          _epCriticBuf.content = (_epCriticMsg.content || "").replace(/\[VERDICT:\s*(?:STOP|CONTINUE)\s*\]\s*$/i, "").trimEnd();
          _epCriticBuf.thinking = _epCriticMsg.thinking;
        }
      } else {
        assistantMsg.content = ev.content || "";
        assistantMsg.thinking = ev.thinking || "";
        if (ev.error) assistantMsg.error = ev.error;
        if (ev.toolRounds) {
          /* Merge: keep checkpoint rounds + new ones from state snapshot */
          const existing = assistantMsg._continueToolRounds || [];
          assistantMsg.toolRounds = existing.concat(ev.toolRounds || []);
          if (buf)
            buf.toolRounds = assistantMsg.toolRounds;
        }
        if (ev.finishReason) assistantMsg.finishReason = ev.finishReason;
        if (ev.usage) assistantMsg.usage = ev.usage;
        if (ev.model) assistantMsg.model = ev.model;
        else if (ev.preset) assistantMsg.model = ev.preset;
        else if (ev.effort) assistantMsg.model = ev.effort;
        if (ev.thinkingDepth) assistantMsg.thinkingDepth = ev.thinkingDepth;
        if (buf) {
          buf.thinking = assistantMsg.thinking;
          buf.content = assistantMsg.content;
        }
        if (ev.usage && typeof updateContextBar === 'function') updateContextBar();
      }
      // ★ Restore memory prefetch state from snapshot (WS/SSE connect mid-prefetch)
      if (ev.memoryPrefetch) {
        assistantMsg._memoryPrefetch = ev.memoryPrefetch;
        if (buf) buf._memoryPrefetch = ev.memoryPrefetch;
        const _mpConv = conversations.find(c => c.id === convId);
        if (_mpConv) {
          const _mpPhase = ev.memoryPrefetch.phase;
          const _RUNNING = new Set(['started', 'bm25_done', 'rerank_started']);
          _mpConv._memoryPrefetching = _RUNNING.has(_mpPhase);
        }
      }
      twUpdate(convId);
      // ★ Re-trigger HG translations on state snapshot (handles page refresh / SSE reconnect)
      if (ev.toolRounds) _retriggerHgTranslations(convId);
    } else if (ev.type === "autopilot_vu_event"
            || ev.type === "autopilot_vu_done"
            || ev.type === "autopilot_vu_cancel") {
      /* ★ Autopilot virtual-user STREAMING events ──────────────────────
       * The VU bubble is created LAZILY — the first inner event with
       * real activity (delta with text, or tool_start) triggers
       * insertion.  We deliberately do NOT have an `autopilot_vu_start`
       * pre-creation event, because it caused empty bubbles to flash
       * on-screen and persist to DB even when autopilot bailed out.
       *
       *   autopilot_vu_event  — wraps a VU sub-task event (delta /
       *     tool_start / tool_result / tool_progress / tool_complete /
       *     tool_compacted / stdin_* / write_approval_request /
       *     human_guidance_*).  Routed by `vuMsgId` into the VU msg's
       *     content/thinking/toolRounds.  Lazily creates the bubble.
       *   autopilot_vu_done   — VU reply finalized; replace fields
       *     with the authoritative copy from DB and persist locally.
       *   autopilot_vu_cancel — VU bailed out (TASK_DONE / aborted /
       *     real-user-message-arrived); remove any in-memory bubble. */
      _handleAutopilotVuEvent(convId, ev);
      return false;
    } else if (ev.type === "delta") {
      if (_epCriticPhase) {
        /* Accumulate into critic bubble instead of worker */
        if (_epCriticMsg) {
          if (ev.thinking) _epCriticMsg.thinking = (_epCriticMsg.thinking || "") + ev.thinking;
          if (ev.content)  _epCriticMsg.content  = (_epCriticMsg.content  || "") + ev.content;
          if (_epCriticBuf) {
            _epCriticBuf.thinking = _epCriticMsg.thinking || "";
            /* Strip [VERDICT: STOP/CONTINUE] tag during live streaming so
               the user never sees the raw structured marker.  The backend
               sends the fully-stripped content in endpoint_critic_msg later,
               but stripping here avoids a flash of the raw tag. */
            const _rawCritic = _epCriticMsg.content || "";
            _epCriticBuf.content = _rawCritic.replace(/\[VERDICT:\s*(?:STOP|CONTINUE)\s*\]\s*$/i, "").trimEnd();
          }
        }
        twUpdate(convId);
      } else {
        if (ev.thinking) {
          assistantMsg.thinking = (assistantMsg.thinking || "") + ev.thinking;
          if (buf) buf.thinking = assistantMsg.thinking;
          _roundThinkingLen += ev.thinking.length;
        }
        if (ev.content) {
          assistantMsg.content = (assistantMsg.content || "") + ev.content;
          if (buf) buf.content = assistantMsg.content;
        }
        /* ★ Phase management during deltas:
         *   - Content delta arrived → model is producing visible output, clear phase
         *   - Thinking-only delta → model is reasoning, show thinking indicator
         *     This works on ALL rounds (even when msg.content is already non-empty
         *     from previous tool rounds) */
        if (buf) {
          if (ev.content) {
            buf.phase = null;
          } else if (ev.thinking && !ev.content) {
            buf.phase = { phase: "thinking_active", _thinkingLen: _roundThinkingLen };
          }
        }
        twUpdate(convId);
      }
    } else if (ev.type === "phase") {
      _roundThinkingLen = 0; // reset thinking counter on new phase
      /* Each new LLM round starts with a 'phase' event whose phase is
       * 'llm_thinking' (see lib/tasks_pkg/orchestrator.py:_emit_tool_round_phase).
       * Tick the gauge here so the % visibly catches up at the moment the
       * model is actually called, not just when usage from the *previous*
       * round arrives. The token math itself doesn't change — but a fresh
       * read forces zone re-evaluation against the now-current model and
       * picks up any usage write that may have raced this branch. */
      if (ev.phase === 'llm_thinking' && typeof updateContextBar === 'function') {
        updateContextBar();
      }
      if (_epCriticPhase) {
        /* Phase events during critic review — update critic buf instead */
        if (_epCriticBuf)
          _epCriticBuf.phase = { phase: ev.phase, detail: ev.detail || "",
            tools: ev.tools || [], toolContext: ev.toolContext || "", round: ev.round || 0 };
      } else if (buf) {
        buf.phase = {
          phase: ev.phase,
          detail: ev.detail || "",
          tools: ev.tools || [],
          toolContext: ev.toolContext || "",
          round: ev.round || 0,
        };
      }
      twUpdate(convId);
    } else if (ev.type === "tool_start") {
      if (_epCriticPhase) {
        /* Critic's tool usage → accumulate into critic message */
        if (_epCriticMsg) {
          const r = {
            roundNum: ev.roundNum, query: ev.query, results: null,
            status: "searching", toolName: ev.toolName || null,
            toolCallId: ev.toolCallId || null, toolArgs: ev.toolArgs || null,
            llmRound: ev.llmRound ?? null, _swarm: false,
          };
          if (!_epCriticMsg.toolRounds) _epCriticMsg.toolRounds = [];
          _epCriticMsg.toolRounds.push(r);
          if (_epCriticBuf) _epCriticBuf.toolRounds = _epCriticMsg.toolRounds;
        }
        twUpdate(convId);
      } else {
        const r = {
          roundNum: ev.roundNum,
          query: ev.query,
          results: null,
          status: "searching",
          toolName: ev.toolName || null,
          toolCallId: ev.toolCallId || null,
          toolArgs: ev.toolArgs || null,
          llmRound: ev.llmRound ?? null,
          _swarm: ev._swarm || false,
        };
        // ★ Preserve per-round assistantContent for Continue replay
        if (ev.assistantContent) r.assistantContent = ev.assistantContent;
        // ★ Harness self-repair descriptor — the backend auto-corrected this
        //   call's malformed arguments; surfaced as an "auto-fixed" badge.
        if (ev._repaired) r._repaired = ev._repaired;
        if (!assistantMsg.toolRounds) assistantMsg.toolRounds = [];
        assistantMsg.toolRounds.push(r);
        /* ★ MCP login-hint: surface a prominent "Check your phone for the
         *   approval push" banner whenever a login-style MCP call starts.
         *   Meituan's `hope login` blocks the subprocess for up to ~5 min
         *   waiting for the user to tap Approve on their mobile-office app
         *   — without this banner the user has no idea the tool is
         *   waiting on them and the task appears frozen.
         *   Matches:
         *     - mcp__hope__hope_login
         *     - mcp__hope__hope_check_login (auto-login triggered when
         *       HOPE_USERNAME is configured and no cached creds)
         *     - generic *_login / *_check_login MCP tools
         */
        try {
          const _toolN = String(ev.toolName || '');
          if (/^mcp__/.test(_toolN) && /(hope_login|hope_check_login|_login$)/.test(_toolN)) {
            let _un = '';
            try {
              const a = ev.toolArgs && (typeof ev.toolArgs === 'string' ? JSON.parse(ev.toolArgs) : ev.toolArgs);
              _un = (a && (a.username || a.user)) || '';
            } catch (_e) { /* best-effort username extraction */ }
            assistantMsg._mcpLoginHint = {
              phase: 'awaiting_approval',
              toolName: _toolN,
              roundNum: ev.roundNum,
              username: _un,
              updatedAt: Date.now(),
            };
            if (buf) buf._mcpLoginHint = assistantMsg._mcpLoginHint;
          }
        } catch (_e) { /* best-effort */ }
        /* Track swarm round number so swarm_phase events can find it */
        if (r._swarm) assistantMsg._swarmRoundNum = r.roundNum;
        if (buf)
          buf.toolRounds = assistantMsg.toolRounds;
        twUpdate(convId);
      }
    } else if (ev.type === "human_guidance_request") {
      /* ── Human Guidance: LLM is asking the user a question ── */
      if (assistantMsg.toolRounds) {
        const r = assistantMsg.toolRounds.find(
          (r) => r.roundNum === ev.roundNum,
        );
        if (r) {
          r.status = "awaiting_human";
          r.guidanceId = ev.guidanceId;
          r.guidanceQuestion = ev.question;
          r.guidanceType = ev.responseType;
          /* ★ Defensive: ev.options may arrive as a JSON string or object
           *   from an upstream model that serialised it oddly. Normalise to
           *   an array before assigning so _renderHumanGuidanceCard can map. */
          let _ev_opts = ev.options;
          if (typeof _ev_opts === 'string') {
            try { _ev_opts = JSON.parse(_ev_opts); }
            catch (_e) { _ev_opts = []; }
          }
          if (!Array.isArray(_ev_opts)) _ev_opts = [];
          r.guidanceOptions = _ev_opts.map(o => ({...(o || {})}));
        }
      }
      if (buf)
        buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);
      // ★ Update sidebar to show amber blinking dot for awaiting-human state
      renderConversationList();
      // ★ Auto-translate question & options (EN→CN) when autoTranslate is ON.
      //   This mirrors the finishStream auto-translate flow for assistant messages.
      //   Fire-and-forget: translates asynchronously, re-renders card when done.
      const _hgConv = conversations.find(c => c.id === convId);
      const _hgAutoTrans = _hgConv ? (_hgConv.autoTranslate !== undefined ? !!_hgConv.autoTranslate : true) : !!autoTranslate;
      if (_hgAutoTrans && ev.question) {
        _autoTranslateHumanGuidance(convId, ev.roundNum, ev.question, ev.responseType, ev.options || []);
      }
    } else if (ev.type === "tool_progress") {
      /* ── Streaming run_command output: append chunk to the round's
       *    partial output buffer and re-render so the user sees it live. */
      const _trMsg = _epCriticPhase ? _epCriticMsg : assistantMsg;
      if (_trMsg && _trMsg.toolRounds) {
        const r = _trMsg.toolRounds.find(rr => rr.roundNum === ev.roundNum);
        if (r) {
          // _partialOutput is the live, growing terminal buffer.
          // It's replaced wholesale by meta.output once tool_result arrives.
          if (typeof r._partialOutput !== "string") r._partialOutput = "";
          r._partialOutput += (ev.chunk || "");
        }
      }
      if (!_epCriticPhase && buf) {
        buf.toolRounds = assistantMsg.toolRounds || [];
      } else if (_epCriticPhase && _epCriticBuf && _epCriticMsg) {
        _epCriticBuf.toolRounds = _epCriticMsg.toolRounds || [];
      }
      twUpdate(convId);
      // Auto-scroll the live terminal box(es) to the bottom so the newest
      // output is always visible — DOM was just rerendered above.
      try {
        const _liveOut = document.querySelectorAll('.ptool-cmd-output-live');
        if (_liveOut.length) {
          /* Batch into one rAF: read all scrollHeights, then write all
           * scrollTops, so we don't interleave layout reads with writes
           * (forced synchronous reflow) once per element per SSE chunk. */
          requestAnimationFrame(() => {
            const _heights = [];
            for (let i = 0; i < _liveOut.length; i++) _heights.push(_liveOut[i].scrollHeight);
            for (let i = 0; i < _liveOut.length; i++) _liveOut[i].scrollTop = _heights[i];
          });
        }
      } catch (_e) { /* best-effort */ }
    } else if (ev.type === "stdin_request") {
      /* ── Stdin Request: subprocess is waiting for user keyboard input ── */
      if (assistantMsg.toolRounds) {
        const r = assistantMsg.toolRounds.find(
          (r) => r.roundNum === ev.roundNum,
        );
        if (r) {
          r.status = "awaiting_stdin";
          r.stdinId = ev.stdinId;
          r.stdinPrompt = ev.prompt;
          r.stdinCommand = ev.command;
        }
      }
      if (buf)
        buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);
    } else if (ev.type === "stdin_resolved") {
      /* ── Stdin Resolved: user input was sent, command continues ── */
      if (assistantMsg.toolRounds) {
        const r = assistantMsg.toolRounds.find(
          (r) => r.roundNum === ev.roundNum,
        );
        if (r) {
          r.status = "searching";
          r.stdinId = null;
          r.stdinPrompt = null;
        }
      }
      if (buf)
        buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);
    } else if (ev.type === "write_approval_request") {
      if (_epCriticPhase) { /* skip approval during critic phase */ }
      else if (assistantMsg.toolRounds) {
        const r = assistantMsg.toolRounds.find(
          (r) => r.roundNum === ev.roundNum,
        );
        if (r) {
          r.status = "pending_approval";
          r.approvalId = ev.approvalId;
          r.approvalMeta = ev.meta;
        }
      }
      if (!_epCriticPhase && buf)
        buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);
    } else if (ev.type === "tool_result") {
      if (_epCriticPhase && _epCriticMsg) {
        /* Critic's tool result → accumulate into critic message */
        if (_epCriticMsg.toolRounds) {
          const r = _epCriticMsg.toolRounds.find(r => r.roundNum === ev.roundNum);
          if (r) { r.results = ev.results; r.status = "done"; if (ev.searchDiag) r.searchDiag = ev.searchDiag; if (ev.engineBreakdown) r.engineBreakdown = ev.engineBreakdown; }
        }
        if (_epCriticBuf) _epCriticBuf.toolRounds = _epCriticMsg.toolRounds || [];
        twUpdate(convId);
      } else if (assistantMsg.toolRounds) {
        const r = assistantMsg.toolRounds.find(
          (r) => r.roundNum === ev.roundNum,
        );
        if (r) {
          r.results = ev.results;
          r.status = "done";
          r.approvalId = null;
          r.approvalMeta = null;
          r.guidanceId = null;
          if (ev.searchDiag) r.searchDiag = ev.searchDiag;
          if (ev.engineBreakdown) r.engineBreakdown = ev.engineBreakdown;
          /* ★ Harness self-repair: when the backend auto-corrected this
           *   call's args AFTER the (garbled) tool_start was already shown,
           *   the tool_result carries the corrected display + descriptor.
           *   Apply them so the live view stops showing the garbled line. */
          if (ev._repaired) {
            r._repaired = ev._repaired;
            if (ev.query) r.query = ev.query;
          }
        }
        /* ★ Clear the MCP login-hint banner once the login call returns.
         *   Classification priority (each test uses STRUCTURED fields
         *   first, text matching second, and always with word-boundaries
         *   to avoid matching e.g. "denied": false inside a JSON dump):
         *     1. Parse snippet as JSON → read `approved`/`denied`/`approval_timed_out`
         *     2. Fallback regex on rendered text WITH word boundaries
         *   Without this, the chip showed "Login denied" whenever the
         *   result JSON contained the literal token `"denied"`, even
         *   when approval actually succeeded. */
        const _lh = assistantMsg._mcpLoginHint;
        if (_lh && _lh.roundNum === ev.roundNum) {
          const _res = Array.isArray(ev.results) ? ev.results[0] : null;
          const _snippet = (_res && (_res.snippet || _res.title || '')) || '';
          const _resultOk = !!(_res && _res.ok);
          // Try to parse the structured result — MCP tools typically
          // embed the tool's JSON response in snippet.
          let _parsed = null;
          if (_snippet) {
            try {
              // The snippet may be "{...}" or wrapped in markdown fences
              const _trim = _snippet.trim().replace(/^```(?:json)?\s*/, '').replace(/\s*```$/, '');
              _parsed = JSON.parse(_trim);
            } catch (_e) { /* non-JSON snippet — fall through */ }
          }
          let _phase;
          if (_parsed && typeof _parsed === 'object') {
            /* Trust the tool's own structured verdict. hope_login returns
             * {approved, denied, approval_timed_out, token_verified}. */
            if (_parsed.approved === true) _phase = 'approved';
            else if (_parsed.denied === true) _phase = 'denied';
            else if (_parsed.approval_timed_out === true) _phase = 'timeout';
            else if (_parsed.token_verified === false) _phase = 'denied'; // token missing → treat as failure
            else _phase = _resultOk ? 'approved' : 'done';
          } else {
            /* Word-boundary regex fallback for non-JSON replies.
             * \b prevents matching "denied" inside "\"denied\":false". */
            const _deniedText = /\bdenied\b\s*$|\brejected\b|\bcancell?ed\b/i.test(_snippet);
            const _timeoutText = /\btimed?\s*out\b|\bapproval\s+timeout\b/i.test(_snippet);
            _phase = _resultOk ? 'approved'
                  : _deniedText ? 'denied'
                  : _timeoutText ? 'timeout'
                  : 'done';
          }
          assistantMsg._mcpLoginHint = {
            ..._lh,
            phase: _phase,
            /* Keep the snippet in full — the user wants to see the
             * complete response, not a 200-char slice with ellipsis.
             * The chip CSS is also updated to wrap instead of clip. */
            snippet: _snippet,
            updatedAt: Date.now(),
          };
          if (buf) buf._mcpLoginHint = assistantMsg._mcpLoginHint;
          /* Auto-dismiss on success after 4s so the chip doesn't linger
           * forever once the session is live. */
          if (_phase === 'approved') {
            setTimeout(() => {
              if (assistantMsg._mcpLoginHint === buf?._mcpLoginHint) {
                assistantMsg._mcpLoginHint = null;
                if (buf) buf._mcpLoginHint = null;
                twUpdate(convId);
              }
            }, 4000);
          }
        }
      }
      /* ★ After create_project: refresh project status so the new extra
       * root appears in the sidebar AND gets persisted to conv.projectPaths.
       * Without this the backend has the root registered but the frontend
       * will overwrite it on the next set_project call (e.g. page refresh,
       * conv switch), causing any subsequent 'name:path' writes to land
       * under the primary root — see create_project frontend-sync bug. */
      if (ev.results && ev.results.some(r => r.toolName === 'create_project')) {
        try {
          Api.project.status()
            .then(data => {
              if (!data) return;
              if (typeof _applyProjectData === 'function') _applyProjectData(data);
              const c = typeof getActiveConv === 'function' ? getActiveConv() : null;
              if (c && data.path) {
                const paths = [data.path];
                if (Array.isArray(data.extraRoots)) {
                  for (const r of data.extraRoots) {
                    const pp = typeof r === 'string' ? r : r.path;
                    if (pp && !paths.includes(pp)) paths.push(pp);
                  }
                }
                c.projectPath = data.path;
                c.projectPaths = paths;
                if (typeof saveConversations === 'function') saveConversations(c.id);
                if (typeof syncConversationToServer === 'function') syncConversationToServer(c);
              }
              if (typeof showToast === 'function') {
                const cp = ev.results.find(r => r.toolName === 'create_project');
                showToast('', 'New workspace root',
                  (cp && (cp.snippet || cp.title)) || 'Registered an additional project root',
                  4000);
              }
            })
            .catch(() => {});
        } catch (_) {}
      }
      /* ★ Toast for create_memory */
      if (ev.results && ev.results.some(r => r.toolName === 'create_memory')) {
        const sk = ev.results.find(r => r.toolName === 'create_memory');
        const ok = sk.memoryOk === true || (sk.badge && sk.badge.includes('saved'));
        if (typeof showToast === 'function') {
          const sName = sk.memoryName || 'Memory';
          const sScope = sk.memoryScope || 'project';
          const title = ok ? `${sName}` : 'Memory Failed';
          const body = ok
            ? `Saved to ${sScope} scope — available in future sessions`
            : (sk.snippet || sk.title || 'Unknown error');
          showToast('', title, body, ok ? 5000 : 8000);
        }
      }
      if (buf)
        buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);
      // ★ If this was an ask_human tool_result, refresh sidebar to clear amber dot
      if (ev.results && ev.results.some(r2 => r2.toolName === 'ask_human')) {
        renderConversationList();
      }
    } else if (ev.type === "tool_complete") {
      // ★ Store raw tool content for continue context restoration
      const _applyToolComplete = (r) => {
        if (!r) return;
        r.toolContent = ev.toolContent || null;
        if (ev.toolTokens != null) r.toolTokens = ev.toolTokens;
        // L0 may already be stamped server-side at emit time.
        if (ev.compactionLayer) {
          r.compactionLayer = ev.compactionLayer;
          r.compactedFromChars = ev.compactedFromChars;
          r.compactedToChars = ev.compactedToChars;
        }
      };
      if (_epCriticPhase && _epCriticMsg) {
        if (_epCriticMsg.toolRounds) {
          _applyToolComplete(_epCriticMsg.toolRounds.find(r => r.roundNum === ev.roundNum && r.toolCallId === ev.toolCallId));
        }
        if (_epCriticBuf)
          _epCriticBuf.toolRounds = _epCriticMsg.toolRounds || [];
      } else if (assistantMsg.toolRounds) {
        _applyToolComplete(assistantMsg.toolRounds.find(
          (r) => r.roundNum === ev.roundNum && r.toolCallId === ev.toolCallId,
        ));
      }
      // ★ Sync to buf and let the reactive pipeline (twUpdate → _syncToolRoundsDOM)
      //   handle preview button rendering — no fragile direct DOM injection needed.
      if (buf)
        buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);

    } else if (ev.type === "tool_compacted") {
      /* ★ Per-tool compaction event — emitted by lib/tasks_pkg/compaction.py
       * micro_compact() (L1) and the aggregate-budget pass in
       * tool_dispatch.py (L0). Tags the matching round so its chip can
       * render the COMPACTED label in real time, even on already-
       * completed tool rounds that compaction just rewrote.
       *
       * IMPORTANT: L1 compacts COLD rounds — i.e. tool calls from
       * EARLIER assistant messages, not the in-flight one. Searching
       * only `assistantMsg.toolRounds` (the current bubble) misses
       * those entirely and the pill never renders. We have to walk
       * every assistant message in the conversation and stamp the
       * matching round wherever it lives.
       *
       * Toolcall IDs are conversation-unique (UUID-style), so a single
       * find across the whole conv is safe and unambiguous. */
      const _applyCompacted = (r) => {
        if (!r) return false;
        r.compactionLayer = ev.compactionLayer || r.compactionLayer || "L1";
        if (ev.compactedFromChars != null) r.compactedFromChars = ev.compactedFromChars;
        if (ev.compactedToChars != null) r.compactedToChars = ev.compactedToChars;
        if (ev.toolTokens != null) r.toolTokens = ev.toolTokens;
        return true;
      };
      let _stampedMsg = null;
      // 1. Try the active critic bubble first (endpoint mode).
      if (_epCriticPhase && _epCriticMsg && _epCriticMsg.toolRounds
          && _applyCompacted(_epCriticMsg.toolRounds.find(r => r.toolCallId === ev.toolCallId))) {
        _stampedMsg = _epCriticMsg;
        if (_epCriticBuf) _epCriticBuf.toolRounds = _epCriticMsg.toolRounds || [];
      }
      // 2. Fall through to every assistant message in this conversation.
      //    Most events match in the in-flight assistantMsg; cold-round
      //    compactions match in older messages.
      if (!_stampedMsg) {
        const _conv = (typeof conversations !== 'undefined')
          ? conversations.find(c => c && c.id === convId)
          : null;
        if (_conv && Array.isArray(_conv.messages)) {
          for (let i = _conv.messages.length - 1; i >= 0; i--) {
            const m = _conv.messages[i];
            if (!m || m.role !== 'assistant' || !Array.isArray(m.toolRounds)) continue;
            const r = m.toolRounds.find(rr => rr.toolCallId === ev.toolCallId);
            if (_applyCompacted(r)) { _stampedMsg = m; break; }
          }
        }
      }
      if (buf && assistantMsg && Array.isArray(assistantMsg.toolRounds))
        buf.toolRounds = assistantMsg.toolRounds;
      twUpdate(convId);
      /* If we stamped a round in an OLDER message (not the in-flight
       * bubble), twUpdate alone won't re-render that message — it
       * only refreshes the streaming bubble.  Trigger a full conv
       * re-render so the COMPACTED pill on the older row materializes
       * immediately.  Cheap: renderChat is fingerprint-guarded and
       * the new compactedCount in _msgFingerprint forces re-render
       * of just the message that changed. */
      if (_stampedMsg && _stampedMsg !== assistantMsg
          && convId === activeConvId
          && typeof renderChat === 'function') {
        const _conv = (typeof conversations !== 'undefined')
          ? conversations.find(c => c && c.id === convId) : null;
        if (_conv) renderChat(_conv, false);
      }
      /* ── Debug-panel alignment ──
       * The debug panel renders the api-form messages snapshot the model
       * just received. Compaction mutates a tool message's content
       * mid-round; without patching the cached snapshot here, the panel
       * keeps showing the pre-compaction blob (e.g. 100 KB grep dump)
       * until the next ``messages_snapshot`` lands — which never arrives
       * if the task ends or pauses. Patch the cached entry by toolCallId
       * and re-render so the JSON tree matches what the model now sees. */
      if (typeof _debugCache !== 'undefined'
          && _debugCache[convId]
          && Array.isArray(_debugCache[convId].messages)
          && ev.compactedContent != null) {
        const _cached = _debugCache[convId].messages;
        for (let i = 0; i < _cached.length; i++) {
          const _m = _cached[i];
          if (_m && _m.role === 'tool' && _m.tool_call_id === ev.toolCallId) {
            _m.content = ev.compactedContent;
            _m._compactionLayer = ev.compactionLayer || 'L1';
            _m._compactedFromChars = ev.compactedFromChars;
            _m._compactedToChars = ev.compactedToChars;
            _m._toolTokens = ev.toolTokens;
            break;
          }
        }
        if (convId === activeConvId
            && typeof showMessagesInDebug === 'function') {
          const _c = _debugCache[convId];
          showMessagesInDebug(_c.messages, _c.label, true, convId, _c.tools);
        }
      }

    } else if (ev.type === "round_usage") {
      /* ── Per-round usage tick ──────────────────────────────────────────
       * The orchestrator emits this immediately after EACH LLM round
       * lands, carrying the raw usage dict + a pre-computed `tokensIn`
       * (input tokens including cache, Anthropic/OpenAI conventions
       * normalized server-side in lib/tasks_pkg/llm_fallback.py
       * :_emit_round_usage).
       *
       * Stash the latest reading on the in-flight assistant msg as
       * `_liveLastRoundUsage` so the context-health gauge reflects the
       * size of the prompt JUST sent to the model — without waiting
       * for the final `done` event to populate `apiRounds`.  This is
       * what makes the bar move on every tool round, not just at the
       * end of the user-visible turn.
       *
       * The reader is `static/js/context-bar.js:_lastUsageTokens`,
       * which prefers `_liveLastRoundUsage` over `apiRounds[-1]` and
       * falls back to `msg.usage / n` for older conversations. */
      if (assistantMsg) {
        assistantMsg._liveLastRoundUsage = {
          round: ev.round,
          model: ev.model,
          tag: ev.tag,
          tokensIn: ev.tokensIn,
          tokensOut: ev.tokensOut,
          usage: ev.usage,
        };
      }
      if (typeof updateContextBar === 'function') updateContextBar();
      return false;

    } else if (ev.type === "artifact") {
      /* ── Renderable artifact (md/html/svg) — see lib/artifacts/ ───────
       * Producer A (write_file post-hook in lib/tasks_pkg/handlers/project.py)
       * persists the bytes server-side and emits this metadata-only event.
       * The actual content is fetched lazily via /api/artifacts/<id>/raw
       * when the user clicks the chip.
       *
       * We stash the meta on assistantMsg._artifacts so the chip survives
       * re-renders, and also into the global Artifacts cache so a click
       * after compaction (which strips toolRounds) still finds it. */
      if (typeof window.Artifacts !== "undefined" && window.Artifacts.attachToMessage) {
        try {
          window.Artifacts.attachToMessage(assistantMsg, {
            id:             ev.id,
            conv_id:        ev.conv_id || convId,
            task_id:        ev.task_id || taskId,
            msg_id:         ev.msg_id || (assistantMsg && assistantMsg._msgId) || "",
            source:         ev.source || "",
            source_ref:     ev.source_ref || {},
            format:         ev.format || "",
            title:          ev.title || "",
            size_bytes:     ev.size_bytes || 0,
            version:        ev.version || 1,
            parent_id:      ev.parent_id || "",
            pinned:         !!ev.pinned,
            created_at:     ev.created_at || 0,
            url:            ev.url || ("/api/v1/artifacts/" + (ev.id || "")),
          });
        } catch (e) {
          console.debug("[Artifacts] attachToMessage failed:", e);
        }
      }
      if (buf) {
        buf._artifacts = (assistantMsg && assistantMsg._artifacts) || buf._artifacts || [];
      }
      twUpdate(convId);

    } else if (ev.type === "compaction" || ev.type === "compaction_done") {
      /* ── Compaction marker ────────────────────────────────────────────
       * Emitted by lib/tasks_pkg/compaction.py when an archive row is
       * inserted (transcript_archive). Each marker becomes an inline
       * chip inside the assistant bubble; clicking it opens the right-
       * side Compaction Viewer drawer (see static/js/compaction-viewer.js)
       * which lazy-loads the pre-compaction message list.
       *
       * We store markers on the LIVE assistant message so they reappear
       * after re-render without a DB round-trip. On reload, the drawer
       * also pulls the authoritative list from
       * GET /api/conversations/<id>/compactions. */
      assistantMsg._compactions = assistantMsg._compactions || [];
      if (ev.type === "compaction") {
        const existing = assistantMsg._compactions.find(c => c.archiveId === ev.archiveId);
        if (!existing) {
          assistantMsg._compactions.push({
            archiveId:     ev.archiveId,
            convId:        ev.convId || convId,
            trigger:       ev.trigger || 'force',
            roundNum:      ev.roundNum || 0,
            tokensBefore:  ev.tokensBefore || 0,
            tokensAfter:   ev.tokensAfter || 0,
            msgsBefore:    ev.msgsBefore || 0,
            msgsAfter:     ev.msgsAfter || 0,
            model:         ev.model || '',
            reason:        ev.reason || '',
            ts:            ev.ts || Math.floor(Date.now() / 1000),
            status:        'in_progress',
          });
        }
      } else {
        // compaction_done — upgrade the matching marker with final numbers
        const marker = assistantMsg._compactions.find(c => c.archiveId === ev.archiveId);
        if (marker) {
          marker.tokensAfter = ev.tokensAfter || marker.tokensAfter;
          marker.msgsAfter   = ev.msgsAfter   || marker.msgsAfter;
          marker.reductionPct = ev.reductionPct;
          marker.status = 'done';
        }
      }
      if (buf) buf._compactions = assistantMsg._compactions;
      /* Bind the gauge to the compaction event the moment it fires.
       * 'compaction' arrives before the LLM summary call and carries
       * tokensBefore — the new tick on the donut materializes here.
       * 'compaction_done' lands the final tokensAfter and we flash the
       * matching tick so the eye is drawn from chip → gauge in one beat. */
      if (typeof updateContextBar === 'function') updateContextBar();
      if (ev.type === 'compaction_done' && typeof window.flashGaugeForArchive === 'function') {
        window.flashGaugeForArchive(ev.archiveId);
      }
      twUpdate(convId);

    } else if (ev.type === "memory_prefetch") {
      /* ── Memory Prefetch indicator ────────────────────────────────────
       * Phases emitted by lib/memory/prefetch.py:
       *   started       — BM25 scoring about to run
       *   bm25_done     — coarse stage complete; cheap-LLM next
       *   rerank_started — cheap-model filter running
       *   done          — memories injected (or none picked)
       *   skipped       — no memories / empty query / bm25 empty
       *   failed        — unexpected error
       * We show a small chip inside the assistant bubble (above the tool panel)
       * so the user can see that a cheap model is filtering memories in the
       * background — otherwise the ~1-3s latency before the main model starts
       * producing tokens would feel unexplained.
       *
       * In ADDITION, we mirror the translate-pattern: while the cheap-model
       * filter is running we set conv._memoryPrefetching so the sidebar
       * shows a status dot + tag (parallel to conv._translating). */
      const prev = assistantMsg._memoryPrefetch || {};
      assistantMsg._memoryPrefetch = {
        ...prev,
        phase: ev.phase,
        totalMemories: ev.total_memories ?? prev.totalMemories,
        candidates: ev.candidates ?? prev.candidates,
        bm25Ms: ev.bm25_ms ?? prev.bm25Ms,
        rerankMs: ev.rerank_ms ?? prev.rerankMs,
        totalMs: ev.total_ms ?? prev.totalMs,
        selected: ev.selected ?? prev.selected,
        memories: ev.memories ?? prev.memories,
        reason: ev.reason ?? prev.reason,
        fellBack: ev.fell_back ?? prev.fellBack,
        startedAt: prev.startedAt || Date.now(),
      };
      if (buf) buf._memoryPrefetch = assistantMsg._memoryPrefetch;

      // Sidebar status mirror — only the cheap-LLM running phases mark the
      // conversation as "filtering memories"; terminal phases clear it.
      const _conv = (typeof conversations !== 'undefined')
        ? conversations.find(c => c.id === convId) : null;
      if (_conv) {
        const RUNNING = new Set(['started', 'bm25_done', 'rerank_started']);
        const TERMINAL = new Set(['done', 'skipped', 'failed']);
        if (RUNNING.has(ev.phase)) {
          _conv._memoryPrefetching = true;
        } else if (TERMINAL.has(ev.phase)) {
          _conv._memoryPrefetching = false;
        }
        // Re-render sidebar so the dot/tag updates immediately.
        if (typeof renderConversationList === 'function') {
          renderConversationList();
        }
      }
      twUpdate(convId);

    } else if (ev.type === "project_external_edit") {
      // ★ Git-shim: external edits captured outside Tofu round boundary.
      //   Show a brief toast so the user knows we auto-committed their changes.
      const files = ev.files || [];
      const sha = (ev.sha || '').slice(0, 7);
      try {
        if (typeof showToast === 'function') {
          const preview = files.slice(0, 3).join(', ') + (files.length > 3 ? ` +${files.length - 3} more` : '');
          showToast(`📝 Captured ${files.length} external edit(s) — ${preview}${sha ? ' · ' + sha : ''}`, 'info');
        }
      } catch (e) { console.warn('[project_external_edit] toast failed', e); }
      console.log('[project_external_edit]', { sha, files });

    } else if (ev.type === "timer_poll_check") {
      /* ═══ Timer Watcher inline poll progress ═══
         Each poll emits a sub-event attached to the timer_create tool round.
         We store polls as _timerPolls[] on the round for collapsible rendering.
         ★ decision='skipped' is a lightweight heartbeat for polls where
           the check_command output was unchanged — we don't push it into
           _timerPolls[] (would spam), just bump skip metadata so the UI
           can render a subdued "N skipped — output unchanged" trailer. */
      if (assistantMsg.toolRounds) {
        const r = assistantMsg.toolRounds.find(r => r.roundNum === ev.roundNum);
        if (r) {
          r._timerTimerId = ev.timerId;
          if (ev.decision === "skipped") {
            r._timerSkipCount = (r._timerSkipCount || 0) + 1;
            r._timerLastSkipTs = Date.now();
            r._timerLastSkipPollNum = ev.pollNum;
            // Keep the round in "searching" state while timer is polling
            r.status = "searching";
          } else {
            if (!r._timerPolls) r._timerPolls = [];
            // ★ Dedup: skip if this pollNum already exists (from state snapshot)
            const _alreadyHas = r._timerPolls.some(p => p.pollNum === ev.pollNum && p.decision === ev.decision);
            if (!_alreadyHas) {
              r._timerPolls.push({
                pollNum: ev.pollNum,
                decision: ev.decision,
                reason: ev.reason || "",
                tokensUsed: ev.tokensUsed || 0,
                timerId: ev.timerId || "",
                ts: Date.now(),
              });
            }
            // Keep the round in "searching" state while timer is polling
            if (ev.decision === "ready") {
              r.status = "done";
              r._timerTriggered = true;
            } else {
              r.status = "searching";
            }
          }
        }
      }
      if (buf)
        buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);

    /* ═══ Swarm mode events ═══ */
    } else if (ev.type === "swarm_phase") {
      /* Master-level swarm lifecycle: planning → spawning → wave_start → complete */
      if (!assistantMsg.toolRounds) assistantMsg.toolRounds = [];
      /* Async swarm: ``complete`` may fire AFTER assistantMsg has rotated
         to a different turn (the user got intermediate messages while
         agents kept running in the background).  Walk all assistant
         messages in the active conversation as a fallback so the
         original swarm panel still gets its terminal status update. */
      const _findSwarmRound = () => {
        const rn = assistantMsg._swarmRoundNum;
        const inCurrent = (assistantMsg.toolRounds || []).find(
          r => r._swarm && (rn ? r.roundNum === rn : true));
        if (inCurrent) return inCurrent;
        if (ev.phase !== "complete") return null;
        /* Only complete events trigger the cross-message walk — spawning
           events should always create or upgrade in the current turn. */
        const conv = conversations.find(c => c && c.id === convId);
        if (!conv) return null;
        for (let i = conv.messages.length - 1; i >= 0; i--) {
          const m = conv.messages[i];
          if (!m || m.role !== "assistant" || !m.toolRounds) continue;
          const sr = m.toolRounds.find(r => r._swarm && (r._swarmActive || r._asyncRunning));
          if (sr) return sr;
        }
        return null;
      };
      if (ev.phase === "spawning" || ev.phase === "planning" || ev.phase === "spawn_more") {
        /* Upgrade the existing tool_start round into a swarm panel */
        let sr = _findSwarmRound();
        const agentData = (ev.agents || []).map((a, i) => ({
          id: a.agentId || a.id || `agent-${i}`,
          role: a.role || "general",
          model: a.model || "",
          objective: a.objective || "",
          context: a.context || "",
          dependsOn: a.depends_on || a.dependsOn || [],
          status: "pending",
          phase: "waiting",
          preview: "",
          tools: [],
        }));
        if (sr) {
          sr.query = "Agent Swarm";
          sr._swarmActive = true;
          sr._swarmStartTime = sr._swarmStartTime || Date.now();
          if (ev.phase === "spawn_more" && agentData.length) {
            /* Append new agents from spawn_more — don't replace existing ones */
            if (!sr._swarmAgents) sr._swarmAgents = [];
            const existingIds = new Set(sr._swarmAgents.map(a => a.id));
            for (const ad of agentData) {
              if (!existingIds.has(ad.id)) sr._swarmAgents.push(ad);
            }
          } else if (agentData.length) {
            sr._swarmAgents = agentData;
          }
        } else {
          sr = {
            roundNum: (assistantMsg.toolRounds.length + 1),
            query: "Agent Swarm",
            results: null,
            status: "searching",
            toolName: "spawn_agents",
            _swarm: true,
            _swarmActive: true,
            _swarmStartTime: Date.now(),
            _swarmAgents: agentData,
          };
          assistantMsg.toolRounds.push(sr);
          assistantMsg._swarmRoundNum = sr.roundNum;
        }
      } else if (ev.phase === "complete") {
        /* Swarm finished — every agent terminated. Drop the async-
           running badge so the panel reads as truly complete.       */
        const sr = _findSwarmRound();
        if (sr) {
          sr.status = "done";
          sr._swarmActive = false;
          sr._asyncRunning = false;
          // Freeze the wall-clock end so _buildSwarmPanelHTML doesn't keep
          // sliding the header timer forward via Date.now() on later re-renders.
          sr._swarmEndTime = Date.now();
          const elapsed = sr._swarmStartTime ? ((sr._swarmEndTime - sr._swarmStartTime) / 1000).toFixed(1) + "s" : "";
          sr._elapsed = elapsed;
          sr._swarmStats = {
            totalTokens: ev.totalTokens || 0,
            totalCostUsd: ev.totalCost || 0,
            agentCount: ev.agentCount || 0,
            failedCount: ev.failedCount || 0,
          };
          /* Update agent data from final results */
          if (ev.agents && sr._swarmAgents) {
            for (const ea of ev.agents) {
              const agent = sr._swarmAgents.find(a => a.id === ea.agentId || a.id === ea.id);
              if (agent) {
                agent.status = ea.status === "completed" ? "done" : (ea.status || "done");
                if (ea.model) agent.model = ea.model;
                if (ea.preview || ea.summary) agent.preview = ea.preview || ea.summary;
                if (ea.elapsed) agent.elapsed = ea.elapsed;
                if (ea.tokens) agent.tokens = ea.tokens;
              }
            }
          }
          for (const a of (sr._swarmAgents || [])) {
            if (a.status === "pending" || a.status === "running") a.status = "done";
          }
        }
      }
      if (buf) buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);

    } else if (ev.type === "swarm_agent_phase" || ev.type === "swarm_agent_progress" ||
               ev.type === "swarm_agent_complete" || ev.type === "swarm_agent_error" ||
               ev.type === "swarm_agent_tool_call") {
      /* Per-agent updates may arrive AFTER the spawning assistantMsg has
         rotated to a different turn (async swarm).  Look up the swarm
         panel in the current msg first, then walk back through the
         conv's assistant messages until we find the one that owns this
         agent_id (or a still-active panel). */
      const _findOwningSwarmRound = () => {
        /* Strict ownership match: the panel must contain this agent_id.
           Returning a panel that does NOT own the agent would silently
           graft the agent onto the wrong panel (B11). */
        const inCurrent = (assistantMsg.toolRounds || []).find(r => r._swarmActive || r._swarm);
        if (inCurrent && (inCurrent._swarmAgents || []).some(a => a.id === ev.agentId)) {
          return inCurrent;
        }
        const conv = conversations.find(c => c && c.id === convId);
        if (conv) {
          for (let i = conv.messages.length - 1; i >= 0; i--) {
            const m = conv.messages[i];
            if (!m || m.role !== "assistant" || !m.toolRounds) continue;
            for (const r of m.toolRounds) {
              if (!r._swarm) continue;
              if ((r._swarmAgents || []).some(a => a.id === ev.agentId)) return r;
            }
          }
        }
        /* Genuinely new agent (e.g. spawn_more arrived before its phase
           event) — only the swarm_agent_phase handler creates new agent
           cards, and only on the CURRENT panel.  Return inCurrent for
           that one branch so the create-on-the-fly path still works;
           progress / complete / error events return null and become
           no-ops, preventing accidental cross-panel writes. */
        if (_swarm_evtype === "swarm_agent_phase") return inCurrent || null;
        return null;
      };
      const _swarm_evtype = ev.type;

      if (_swarm_evtype === "swarm_agent_phase") {
      /* An individual agent changed phase (starting, thinking, tool_use, done, error) */
      const sr = _findOwningSwarmRound();
      if (sr) {
        if (!sr._swarmAgents) sr._swarmAgents = [];
        let agent = sr._swarmAgents.find(a => a.id === ev.agentId);
        if (!agent && ev.agentId) {
          /* ID not found — check if there's an existing agent with the same
             objective that hasn't been matched yet (stale from spawning event).
             This happens when the spawning event uses placeholder IDs that
             differ from the actual agent IDs assigned by the scheduler. */
          if (ev.objective) {
            const objNorm = ev.objective.trim().toLowerCase();
            agent = sr._swarmAgents.find(a =>
              a.id !== ev.agentId &&
              !a._idConfirmed &&
              (a.status === "pending" || a.status === "running" || a.phase === "starting" || a.phase === "Queued" || !a.phase) &&
              a.objective && (a.objective.trim().toLowerCase().startsWith(objNorm) || objNorm.startsWith(a.objective.trim().toLowerCase()))
            );
          }
          if (agent) {
            /* Re-map: update the stale placeholder ID to the real agent ID */
            agent.id = ev.agentId;
            agent._idConfirmed = true;
          } else {
            /* Genuinely new agent (e.g. from spawn_more) — add dynamically */
            agent = { id: ev.agentId, role: ev.role || "agent", model: ev.model || "", objective: ev.objective || "",
                      status: "running", phase: "starting", preview: "", tools: [], _idConfirmed: true };
            sr._swarmAgents.push(agent);
          }
        }
        if (agent) agent._idConfirmed = true;
        if (agent) {
          agent.status = ev.status || agent.status;
          agent.phase = ev.phase || agent.phase;
          if (ev.model) agent.model = ev.model;
          if (ev.preview || ev.summary) agent.preview = ev.preview || ev.summary;
          if (ev.objective) agent.objective = ev.objective;
          if (ev.error) agent.preview = errorEnvelopeMessage(ev.error) || (typeof ev.error === 'string' ? ev.error : '');
          if (ev.elapsed) agent.elapsed = ev.elapsed;
          if (ev.tokens) agent.tokens = ev.tokens;
          /* Stamp a frontend-side start time on first transition to a
           * running phase so the agent card can show a live ticking
           * timer (the backend only sends `elapsed` on completion). */
          if (!agent._startedAt && (agent.status === "running" || agent.status === "thinking")) {
            agent._startedAt = Date.now();
          }
        }
      }
      if (buf) buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);

      } else if (_swarm_evtype === "swarm_agent_progress") {
      /* Agent progress: tool usage, partial results, etc. */
      const sr = _findOwningSwarmRound();
      if (sr && sr._swarmAgents) {
        const agent = sr._swarmAgents.find(a => a.id === ev.agentId);
        if (agent) {
          agent.status = ev.status || "running";
          agent.phase = ev.phase || agent.phase;
          if (ev.preview) agent.preview = ev.preview;
          if (ev.toolNames) {
            agent.phase = "tool_use";
            if (!agent.tools) agent.tools = [];
            for (const tn of ev.toolNames) {
              if (!agent.tools.includes(tn)) agent.tools.push(tn);
            }
            agent.preview = `Using ${ev.toolNames.join(", ")}`;
          }
        }
      }
      if (buf) buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);

      } else if (_swarm_evtype === "swarm_agent_complete") {
      /* Individual agent finished */
      const sr = _findOwningSwarmRound();
      if (sr && sr._swarmAgents) {
        let agent = sr._swarmAgents.find(a => a.id === ev.agentId);
        /* Fallback: match by objective if ID doesn't match (ID remap) */
        if (!agent && ev.objective) {
          const objNorm = ev.objective.trim().toLowerCase();
          agent = sr._swarmAgents.find(a =>
            a.objective && (a.objective.trim().toLowerCase().startsWith(objNorm) || objNorm.startsWith(a.objective.trim().toLowerCase())) &&
            a.status !== "done" && a.status !== "failed"
          );
          if (agent) agent.id = ev.agentId;
        }
        if (agent) {
          agent.status = ev.status === "failed" ? "failed" : "done";
          agent.phase = ev.status === "failed" ? "error" : "done";
          if (ev.preview || ev.summary) agent.preview = ev.preview || ev.summary;
          if (ev.elapsed) agent.elapsed = ev.elapsed;
          if (ev.tokens) agent.tokens = ev.tokens;
          if (typeof ev.modifiedFiles === "number") agent.modifiedFiles = ev.modifiedFiles;
          if (ev.error) agent.preview = errorEnvelopeMessage(ev.error) || (typeof ev.error === 'string' ? ev.error : '');
        }
      }
      if (buf) buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);

      } else if (_swarm_evtype === "swarm_agent_error") {
      const sr = _findOwningSwarmRound();
      if (sr && sr._swarmAgents) {
        const agent = sr._swarmAgents.find(a => a.id === ev.agentId);
        if (agent) {
          agent.status = "failed";
          agent.phase = "error";
          agent.preview = errorEnvelopeMessage(ev.error) || (typeof ev.error === 'string' ? ev.error : '') || ev.content || "Agent failed";
        }
      }
      if (buf) buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);

      } else if (_swarm_evtype === "swarm_agent_tool_call") {
      /* Per-tool-call timeline entry from a sub-agent.
       * callStatus: 'running' (start) | 'done' | 'failed'
       * Keyed by callId so the start event creates the row and the
       * finish event updates the same row in place. */
      const sr = _findOwningSwarmRound();
      if (sr && sr._swarmAgents && ev.callId) {
        const agent = sr._swarmAgents.find(a => a.id === ev.agentId);
        if (agent) {
          if (!agent._toolCalls) agent._toolCalls = [];
          let entry = agent._toolCalls.find(c => c.callId === ev.callId);
          if (!entry) {
            entry = { callId: ev.callId, toolName: ev.toolName || "?",
                      argsBrief: ev.argsBrief || "", status: "running",
                      startedAt: Date.now() };
            agent._toolCalls.push(entry);
            /* Keep only the last 30 calls per agent to bound memory
             * for long-running agents — older calls drop off the
             * timeline but the agent's own history is unaffected. */
            if (agent._toolCalls.length > 30) {
              agent._toolCalls.splice(0, agent._toolCalls.length - 30);
            }
          }
          if (ev.callStatus) entry.status = ev.callStatus;
          if (typeof ev.callElapsed === "number") entry.elapsed = ev.callElapsed;
          if (ev.preview) entry.preview = ev.preview;
          if (ev.error) entry.error = ev.error;
          if (ev.toolName) entry.toolName = ev.toolName;
          if (ev.argsBrief) entry.argsBrief = ev.argsBrief;
        }
      }
      if (buf) buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);
      }  /* end inner _swarm_evtype dispatch */

    } else if (ev.type === "swarm_inbox_inject") {
      /* ── Async swarm: <swarm-update> messages were just drained from the
       *    inbox and prepended to the model's next round as user messages.
       *    Stamp a chip on the assistant bubble so the human user sees
       *    "the model just received N async updates from sub-agents on
       *    this round" — same instant as the model itself sees them.    */
      if (!assistantMsg._inboxInjects) assistantMsg._inboxInjects = [];
      assistantMsg._inboxInjects.push({
        round:    ev.round,
        count:    ev.count || 0,
        agentIds: Array.isArray(ev.agentIds) ? ev.agentIds.filter(Boolean) : [],
        ts:       Date.now(),
      });
      /* ★ Also surface the injection as a chronological row inside the
       *   ptool-panel — not just an ephemeral streaming-zone chip. This
       *   makes "the model received these sub-agent results, here, in
       *   order" visible in the unified tool timeline AND persisted to
       *   DB (survives reload), honoring the frontend-visibility
       *   principle. The row is a synthetic toolRound flagged
       *   `_inboxInject`; _renderUnifiedToolLine renders it specially.
       *   Dedup by round so SSE replay / poll fallback doesn't double it. */
      if (!assistantMsg.toolRounds) assistantMsg.toolRounds = [];
      const _injKey = "inbox:" + (ev.round || 0);
      if (!assistantMsg.toolRounds.some(r => r._inboxInject && r._inboxKey === _injKey)) {
        assistantMsg.toolRounds.push({
          /* Collision-proof synthetic roundNum for the data-prn slot —
             real tool rounds use small sequential numbers; 9e6+ never clashes. */
          roundNum:   9000000 + assistantMsg.toolRounds.length,
          status:     "done",
          _inboxInject:  true,
          _inboxKey:     _injKey,
          inboxRound:    ev.round || 0,
          inboxCount:    ev.count || 0,
          inboxAgentIds: Array.isArray(ev.agentIds) ? ev.agentIds.filter(Boolean) : [],
          inboxPreviews: Array.isArray(ev.previews) ? ev.previews : [],
        });
        if (buf) buf.toolRounds = assistantMsg.toolRounds;
      }
      /* Mark the most recent swarm panel (across the whole conv) as
         still-active-async — the panel may have been flipped to
         "complete" by a previous swarm_phase event, but if updates are
         still landing we want the "N running async" badge to show until
         the inbox actually stops.                                       */
      const _markAsyncRunning = (sr) => {
        if (sr && !sr._asyncRunning) sr._asyncRunning = true;
      };
      const _curLive = (assistantMsg.toolRounds || []).filter(r => r._swarm);
      if (_curLive.length) {
        _markAsyncRunning(_curLive[_curLive.length - 1]);
      } else {
        const conv = conversations.find(c => c && c.id === convId);
        if (conv) {
          for (let i = conv.messages.length - 1; i >= 0; i--) {
            const m = conv.messages[i];
            if (!m || m.role !== "assistant" || !m.toolRounds) continue;
            const sr = m.toolRounds.find(r => r._swarm);
            if (sr) { _markAsyncRunning(sr); break; }
          }
        }
      }
      if (buf) buf._inboxInjects = assistantMsg._inboxInjects;
      twUpdate(convId);

    } else if (ev.type === "messages_snapshot") {
      if (typeof showMessagesInDebug === "function")
        showMessagesInDebug(
          ev.messages,
          ev.label || `Round ${ev.round} · ${ev.messageCount}条`,
          true,
          convId,
          ev.tools || undefined,
        );

    /* ═══ Endpoint mode events ═══ */
    } else if (ev.type === "endpoint_iteration") {
      const isPlanning = ev.phase === "planning";
      const isReview = ev.phase === "reviewing";
      const phase = isPlanning ? "Planning" : (isReview ? "Reviewing" : "Working");
      if (!assistantMsg._epIter) assistantMsg._epIter = 0;
      assistantMsg._epIter = ev.iteration;
      const _isActiveConv = (activeConvId === convId);

      if (isPlanning) {
        /* ── Entering planner phase ── */
        _epCriticPhase = false;
        const conv = conversations.find(c => c.id === convId);
        // The planner streams into the initial assistantMsg (created by sendMessage)
        // Mark it as a planner message
        assistantMsg._isEndpointPlanner = true;
        assistantMsg.content = "";
        assistantMsg.thinking = "";

        if (_isActiveConv) {
          // Update the streaming bubble to show planner role
          const sm = document.getElementById("streaming-msg");
          if (sm) {
            sm.classList.add("ep-planner-msg");
            const roleEl = sm.querySelector(".message-role");
            if (roleEl) roleEl.textContent = "Planner";
            const avatarEl = sm.querySelector(".message-avatar");
            if (avatarEl) avatarEl.innerHTML = (typeof _TOFU_PLANNER_SVG !== 'undefined') ? _TOFU_PLANNER_SVG : '✦';
            const bodyEl = sm.querySelector(".message-body");
            if (bodyEl) bodyEl.innerHTML = '<div class="stream-status"><div class="pulse"></div> Planning…</div>';
          }
        }

      } else if (isReview) {
        /* ── Entering critic phase ── */
        _epCriticPhase = true;

        // 1. Finalize the worker's streaming bubble (DOM — only if active)
        const conv = conversations.find(c => c.id === convId);
        assistantMsg.content = assistantMsg.content || "";
        assistantMsg.done = true;
        assistantMsg._epIteration = ev.iteration;
        if (_isActiveConv && conv) {
          /* ConvView funnels scroll preservation + identity lookup; if
           * the assistant message was truncated away, the controller
           * just removes the bubble. */
          window.ConvView.finalizeStreaming(convId, assistantMsg);
        }

        // 2. Create a critic message object in conv.messages for live streaming
        if (conv) {
          /* ★ Dedup: remove any stale DB-loaded critic for this iteration */
          const staleCriticIdx = conv.messages.findIndex(m =>
            m._isEndpointReview && m._epIteration === ev.iteration);
          if (staleCriticIdx >= 0) {
            conv.messages.splice(staleCriticIdx, 1);
            console.info(`[endpoint_iteration] Dedup — removed stale critic at idx=${staleCriticIdx} ` +
              `for iteration=${ev.iteration}`);
          }

          _epCriticMsg = {
            role: "user",
            content: "",
            thinking: "",
            toolRounds: [],
            timestamp: new Date().toISOString(),
            _isEndpointReview: true,
            _epIteration: ev.iteration,
            _epApproved: false,
            _isStuck: false,
          };
          _ensureMsgId(_epCriticMsg);
          conv.messages.push(_epCriticMsg);

          // 3. Create a streaming element for the critic (DOM — only if active)
          if (_isActiveConv) {
            const inner = document.getElementById("chatInner");
            if (inner) inner.insertAdjacentHTML("beforeend", _streamingBubbleHTML('critic'));
          }

          // 4. Create a separate stream buffer for the critic
          _epCriticBuf = { content: "", thinking: "", toolRounds: [] };
          streamBufs.set(convId, _epCriticBuf);
          buf = _epCriticBuf;

          if (_isActiveConv) {
            buildTurnNav(conv);
            _forceScrollToBottom();
          }
        }
      } else if (!isPlanning) {
        /* ── Working phase ── */
        _epCriticPhase = false;

        /* After the planner finishes, the first worker turn (iteration 1) needs
         * a new assistant message + streaming bubble since the planner's bubble
         * was finalized by endpoint_planner_done.  For subsequent iterations,
         * endpoint_new_turn handles this.
         *
         * ★ FIX: Also handle the case where streaming-msg STILL EXISTS but
         *   belongs to the planner (endpoint_planner_done didn't finalize it,
         *   e.g. because activeConvId !== convId at that moment, or plannerIdx
         *   was -1).  In this case, finalize the planner element first, then
         *   create a fresh worker streaming bubble. */
        const conv = conversations.find(c => c.id === convId);
        if (conv) {
          let existingSm = document.getElementById("streaming-msg");

          /* ★ Detect stale planner streaming-msg: if the existing streaming-msg
           *   has ep-planner-msg class, the planner's finalization was missed.
           *   Finalize it now before creating the worker bubble. */
          if (existingSm && existingSm.classList.contains('ep-planner-msg')) {
            console.warn(`[endpoint_iteration] ⚠️ Stale planner streaming-msg detected — ` +
              `finalizing planner before starting worker phase (iter=${ev.iteration})`);
            const plannerMsg = [...conv.messages].reverse().find(m => m._isEndpointPlanner);
            if (plannerMsg && _isActiveConv) {
              plannerMsg.done = true;
              /* finalizeStreaming auto-removes the bubble when the
               * planner message is no longer in conv.messages, matching
               * the previous explicit `existingSm.remove()` fallback. */
              window.ConvView.finalizeStreaming(convId, plannerMsg);
            } else if (_isActiveConv) {
              existingSm.remove();
            }
            existingSm = null;  // force creation of a new worker streaming-msg
          }

          if (!existingSm) {
            /* ★ Dedup: remove any stale DB-loaded worker for this iteration */
            const staleIdx = conv.messages.findIndex(m =>
              m.role === "assistant" && m._epIteration === ev.iteration);
            if (staleIdx >= 0) {
              conv.messages.splice(staleIdx);
            }

            const newAssistant = {
              role: "assistant",
              content: "",
              thinking: "",
              toolRounds: [],
              timestamp: new Date().toISOString(),
              _epIteration: ev.iteration,
            };
            _ensureMsgId(newAssistant);
            conv.messages.push(newAssistant);
            assistantMsg = newAssistant;

            // Reset stream buffer for the new worker turn
            const newBuf = { content: "", thinking: "", toolRounds: [] };
            streamBufs.set(convId, newBuf);
            buf = newBuf;

            // Create streaming element — only if this conv is active
            if (_isActiveConv) {
              const inner = document.getElementById("chatInner");
              if (inner) inner.insertAdjacentHTML("beforeend", _streamingBubbleHTML('worker', 'Thinking…'));
              buildTurnNav(conv);
              _forceScrollToBottom();
            }
          }
        }
      }

      if (_isActiveConv) {
        const bannerText = isPlanning
          ? `Endpoint Planning`
          : `Endpoint ${phase} — Iteration ${ev.iteration}`;
        const banner = document.getElementById("ep-iter-banner");
        if (banner) {
          banner.textContent = bannerText;
        } else {
          const sm = document.getElementById("streaming-msg");
          if (sm) {
            const b = document.createElement("div");
            b.id = "ep-iter-banner";
            b.className = "ep-iter-banner" + (isPlanning ? " ep-iter-banner-planner" : "");
            b.textContent = bannerText;
            const content = sm.querySelector(".message-content");
            if (content) content.prepend(b);
          }
        }
      }

    } else if (ev.type === "endpoint_planner_done") {
      /* ── Planner finished — finalize the planner streaming bubble, prepare for worker ── */
      const conv = conversations.find(c => c.id === convId);
      if (conv) {
        // Update the planner message with final content
        assistantMsg.content = ev.content || assistantMsg.content;
        assistantMsg.thinking = ev.thinking || assistantMsg.thinking;
        assistantMsg._isEndpointPlanner = true;
        assistantMsg.done = true;
        if (ev.usage) {
          assistantMsg.usage = ev.usage;
          if (typeof updateContextBar === 'function') updateContextBar();
        }

        // Re-render the streaming element as a static planner bubble (DOM — only if active)
        if (activeConvId === convId) {
          const sm = document.getElementById("streaming-msg");
          const plannerIdx = conv.messages.indexOf(assistantMsg);
          if (sm && plannerIdx >= 0) {
            window.ConvView.finalizeStreaming(convId, assistantMsg);
          } else if (sm) {
            /* ★ FIX: assistantMsg is a dangling ref (conv.messages was replaced,
             * e.g. by loadConversationMessages Phase 2).  The planner message
             * exists in conv.messages under a different object.  Re-add assistantMsg
             * to conv.messages if missing, or at minimum remove the streaming-msg
             * so the working phase handler creates a fresh assistant message. */
            const existingPlanner = [...conv.messages].reverse().find(m => m._isEndpointPlanner);
            if (existingPlanner) {
              /* Planner exists from backend sync — just remove the streaming bubble */
              sm.outerHTML = renderMessage(existingPlanner, conv.messages.indexOf(existingPlanner));
            } else {
              /* No planner in conv.messages — re-insert our flagged copy */
              const userIdx = conv.messages.findIndex(m => m.role === "user");
              const insertAt = userIdx >= 0 ? userIdx + 1 : conv.messages.length;
              conv.messages.splice(insertAt, 0, assistantMsg);
              sm.outerHTML = renderMessage(assistantMsg, insertAt);
            }
            console.warn(`[endpoint_planner_done] ⚠️ Dangling assistantMsg ref — ` +
              `recovered by ${existingPlanner ? 'using existing planner' : 're-inserting'} ` +
              `conv=${convId.slice(0,8)}`);
          }
        }

        // Save & update nav
        saveConversations(convId);
        if (activeConvId === convId) {
          buildTurnNav(conv);
          _forceScrollToBottom();
        }
      }

    } else if (ev.type === "endpoint_critic_msg") {
      /* ── Critic finished — finalize the critic streaming bubble ── */
      _epCriticPhase = false;
      const conv = conversations.find(c => c.id === convId);
      if (conv) {
        // Derive next_phase with graceful legacy fallback: if the backend
        // only sends the old should_stop boolean, treat as 'stop'/'worker'.
        const nextPhase = ev.next_phase
          || (ev.should_stop ? 'stop' : 'worker');

        // Update the critic message with final content from event
        // (the event content has the verdict tag stripped by the backend)
        if (_epCriticMsg) {
          _epCriticMsg.content = ev.content;
          _epCriticMsg._epApproved = nextPhase === 'stop';
          _epCriticMsg._epNextPhase = nextPhase;
          _epCriticMsg._isStuck = ev.is_stuck || false;
          _epCriticMsg.done = true;
        }

        // Re-render the streaming element as a static critic bubble (DOM — only if active)
        if (activeConvId === convId && _epCriticMsg) {
          window.ConvView.finalizeStreaming(convId, _epCriticMsg);
        }

        // Clean up critic state
        _epCriticMsg = null;
        _epCriticBuf = null;

        /* ── Replan branch: Critic requested CONTINUE_PLANNER ──
         * Create a new planner placeholder message so the subsequent
         * endpoint_planner_done event finalizes into it.  This mirrors
         * the initial-plan placeholder created in startAssistantResponse
         * (main.js L1337) and lets users see a second Plan bubble in-line
         * with the iteration history. */
        if (nextPhase === 'planner') {
          const plannerPlaceholder = {
            role: "assistant",
            content: "",
            thinking: "",
            toolRounds: [],
            timestamp: new Date().toISOString(),
            _isEndpointPlanner: true,
          };
          _ensureMsgId(plannerPlaceholder);
          conv.messages.push(plannerPlaceholder);
          assistantMsg = plannerPlaceholder;

          // Reset stream buffer for the planner turn
          const newBuf = { content: "", thinking: "", toolRounds: [] };
          streamBufs.set(convId, newBuf);
          buf = newBuf;

          if (activeConvId === convId) {
            const inner = document.getElementById("chatInner");
            if (inner) inner.insertAdjacentHTML("beforeend", _streamingBubbleHTML('planner', 'Replanning…'));
            const banner = document.getElementById("ep-iter-banner");
            if (banner) banner.textContent = `Replanning…`;
          }
        }

        // Save & update nav
        saveConversations(convId);
        if (activeConvId === convId) {
          buildTurnNav(conv);
          _forceScrollToBottom();
        }
      }

    } else if (ev.type === "endpoint_new_turn") {
      /* ── Worker starts a new revision turn — renders as normal assistant reply ── */
      _epCriticPhase = false;
      const conv = conversations.find(c => c.id === convId);
      if (conv) {
        /* ★ Dedup: if this iteration already exists from a DB-loaded endpoint turn
         *   (page reload reconnection), remove the stale DB version first and
         *   re-use a fresh streaming assistant message.  Also remove any stale
         *   critic messages for this iteration and beyond. */
        const staleIdx = conv.messages.findIndex(m =>
          m.role === "assistant" && m._epIteration === ev.iteration);
        if (staleIdx >= 0) {
          // Remove this iteration's worker turn and everything after it
          // (subsequent critic + worker turns will be re-streamed)
          conv.messages.splice(staleIdx);
          console.info(`[endpoint_new_turn] Dedup — removed stale turns from idx=${staleIdx} ` +
            `for iteration=${ev.iteration}, conv=${convId.slice(0,8)}`);
        }

        const newAssistant = {
          role: "assistant",
          content: "",
          thinking: "",
          toolRounds: [],
          timestamp: new Date().toISOString(),
          _epIteration: ev.iteration,
        };
        _ensureMsgId(newAssistant);
        conv.messages.push(newAssistant);
        assistantMsg = newAssistant;

        // Reset stream buffer for the new worker turn
        const newBuf = { content: "", thinking: "", toolRounds: [] };
        streamBufs.set(convId, newBuf);
        buf = newBuf;

        // DOM operations — only if this conv is currently viewed
        if (activeConvId === convId) {
          const inner = document.getElementById("chatInner");
          if (inner) inner.insertAdjacentHTML("beforeend", _streamingBubbleHTML('worker', 'Thinking…'));

          // Update banner & turn-nav
          const banner = document.getElementById("ep-iter-banner");
          if (banner) banner.textContent = `Endpoint Iteration ${ev.iteration}`;
          buildTurnNav(conv);

          _forceScrollToBottom();
        }
      }

    } else if (ev.type === "endpoint_complete") {
      _epCriticPhase = false;
      assistantMsg.endpointResult = {
        totalIterations: ev.totalIterations,
        reason: ev.reason,
      };
      const reasonLabel = { approved: "Approved", stuck: "Stuck", max_iterations: "Max Iterations", error: "Error", aborted: "Aborted" }[ev.reason] || ev.reason;

      if (activeConvId === convId) {
        const banner = document.getElementById("ep-iter-banner");
        if (banner) banner.textContent = `Done — ${reasonLabel} (${ev.totalIterations} iterations)`;

        /* Clean up any dangling streaming-msg element (e.g. if endpoint_new_turn
           was emitted but no worker phase ran before max_iterations break). */
        const danglingSm = document.getElementById("streaming-msg");
        if (danglingSm && !danglingSm.querySelector(".md-content")) {
          /* Only remove if still showing placeholder ("Thinking…" / "Reviewing…"),
             not if it has real content that hasn't been finalized yet. */
          danglingSm.remove();
        }
      }
      /* Remove empty assistant message from data regardless of active view */
      const conv = conversations.find(c => c.id === convId);
      if (conv && assistantMsg && !assistantMsg.content) {
        const idx = conv.messages.indexOf(assistantMsg);
        if (idx >= 0) conv.messages.splice(idx, 1);
      }

      /* ★ FIX: Clean up ghost assistant messages — unmarked assistants left by
       * startAssistantResponse() that weren't properly absorbed into the endpoint
       * planner turn.  Only scan AFTER the last base user message to avoid
       * accidentally removing legitimate historical assistant messages from
       * previous non-endpoint conversation turns. */
      if (conv) {
        const hasEpTurns = conv.messages.some(m => m._isEndpointPlanner || m._epIteration);
        if (hasEpTurns) {
          /* Find the last "base" user message (not an endpoint review) —
           * ghost assistants can only appear between this user message
           * and the first endpoint-marked message (planner). */
          let lastBaseUserIdx = -1;
          for (let i = conv.messages.length - 1; i >= 0; i--) {
            if (conv.messages[i].role === 'user' && !conv.messages[i]._isEndpointReview) {
              lastBaseUserIdx = i;
              break;
            }
          }
          let cleaned = 0;
          /* Only scan messages AFTER the last base user message */
          for (let i = conv.messages.length - 1; i > lastBaseUserIdx && i >= 0; i--) {
            const m = conv.messages[i];
            if (m.role === "assistant"
                && !m._isEndpointPlanner
                && !m._epIteration
                && !m._isEndpointReview) {
              /* This is a ghost — an assistant message without endpoint markers
               * sitting after the last user message (created by startAssistantResponse) */
              console.warn(`[endpoint_complete] 🧹 Removing ghost assistant at idx=${i} ` +
                `contentLen=${(m.content||'').length} conv=${convId.slice(0,8)}`);
              conv.messages.splice(i, 1);
              cleaned++;
            }
          }
          if (cleaned > 0) {
            console.info(`[endpoint_complete] Cleaned ${cleaned} ghost assistant(s) from conv=${convId.slice(0,8)}`);
          }
        }
      }

    } else if (ev.type === "sse_timeout") {
      /* SSE connection hit max duration — backend task is STILL RUNNING.
         Show a toast and return false (not done). The stream will close,
         _trySSE will detect !streamDone and return false, triggering _pollFallback. */
      if (typeof showToast === 'function') {
        showToast('', 'Connection Switched',
          'Long-running task: SSE stream reached max duration. Switching to polling — your task is still running in the background.',
          10000);
      }
      console.warn(
        `[_trySSE] SSE timeout notice received — taskId=${taskId.slice(0,8)} conv=${convId.slice(0,8)} ` +
        `contentSoFar=${assistantMsg.content?.length || 0}chars thinkingSoFar=${assistantMsg.thinking?.length || 0}chars ` +
        `toolRounds=${assistantMsg.toolRounds?.length || 0} — backend continues, switching to poll fallback`
      );
      // Return false — NOT a done event. Task is still running.
      return false;


    } else if (ev.type === "round_committed") {
      /* ★ Async shadow-git commit landed AFTER the done event was emitted.
         Backend moved commit_round out of the critical path (2026-05-07)
         so queue dispatch isn't blocked by slow FUSE git.  Wire the sha
         (and any git-derived modifiedFileList additions) onto the
         assistant message so undo/diff UI still works. */
      if (ev.gitSha) assistantMsg._gitSha = ev.gitSha;
      if (ev.modifiedFileList) assistantMsg.modifiedFileList = ev.modifiedFileList;
      if (typeof ev.modifiedFiles === 'number') assistantMsg.modifiedFiles = ev.modifiedFiles;
      /* Re-render the message so its footer reflects the new file list /
         undo button availability. */
      try { if (typeof renderChat === 'function') renderChat(); } catch (_) {}
      return false;  // not a done event

    } else if (ev.type === "done") {
      /* ★ DIAGNOSTIC: log task completion details for debugging silent completions */
      const _dContentLen = assistantMsg.content?.length || 0;
      const _dThinkLen = assistantMsg.thinking?.length || 0;
      const _dToolRounds = assistantMsg.toolRounds?.length || 0;
      /* ★ CROSS-TALK DETECTION: verify the conv we're writing to still matches */
      const _dConv = conversations.find(c => c.id === convId);
      const _dMsgCount = _dConv?.messages?.length || 0;
      const _dIsActive = activeConvId === convId;
      const _dErrSummary = ev.error
        ? (typeof ev.error === 'object' ? (ev.error.kind || 'unknown') : String(ev.error).slice(0, 100))
        : 'none';
      console.log(
        `[connectToTask] DONE event received — task=${taskId.slice(0,8)} conv=${convId.slice(0,8)} ` +
        `finishReason=${ev.finishReason || 'none'} ` +
        `contentLen=${_dContentLen} thinkingLen=${_dThinkLen} ` +
        `toolRounds=${_dToolRounds} error=${_dErrSummary} ` +
        `model=${ev.model || 'unknown'} msgCount=${_dMsgCount} ` +
        `isActiveConv=${_dIsActive} activeConvId=${activeConvId?.slice(0,8)||'null'}`
      );
      if (_dContentLen === 0 && _dThinkLen === 0 && !ev.error) {
        console.error(
          `[connectToTask] ⚠ SUSPICIOUS DONE: task=${taskId.slice(0,8)} completed with ` +
          `ZERO content and ZERO thinking but no error flag. ` +
          `finishReason=${ev.finishReason} — possible silent completion bug!`
        );
      }
      if (ev._diagnostics) {
        console.warn(
          `[connectToTask] 🔍 SERVER DIAGNOSTICS for task=${taskId.slice(0,8)}:`,
          ev._diagnostics
        );
      }
      if (ev.error) assistantMsg.error = ev.error;
      if (ev.finishReason) assistantMsg.finishReason = ev.finishReason;
      if (ev.model) assistantMsg.model = ev.model;
      else if (ev.preset) assistantMsg.model = ev.preset;
      else if (ev.effort) assistantMsg.model = ev.effort;
      if (ev.thinkingDepth) assistantMsg.thinkingDepth = ev.thinkingDepth;
      if (ev.toolSummary) assistantMsg.toolSummary = ev.toolSummary;
      if (ev.fallbackModel) assistantMsg.fallbackModel = ev.fallbackModel;
      if (ev.fallbackFrom) assistantMsg.fallbackFrom = ev.fallbackFrom;
      /* ★ Continue: merge modifiedFiles & modifiedFileList with existing */
      if (ev.modifiedFiles != null) {
        if (assistantMsg._continueModifiedFiles) {
          assistantMsg.modifiedFiles = assistantMsg._continueModifiedFiles + ev.modifiedFiles;
          delete assistantMsg._continueModifiedFiles;
        } else {
          assistantMsg.modifiedFiles = ev.modifiedFiles;
        }
      }
      if (ev.modifiedFileList) {
        if (assistantMsg._continueModifiedFileList) {
          // Merge: old files + new files, dedup by path (new action wins)
          const merged = new Map();
          for (const f of assistantMsg._continueModifiedFileList) merged.set(f.path, f);
          for (const f of ev.modifiedFileList) merged.set(f.path, f);
          assistantMsg.modifiedFileList = Array.from(merged.values());
          delete assistantMsg._continueModifiedFileList;
        } else {
          assistantMsg.modifiedFileList = ev.modifiedFileList;
        }
      }
      if (ev.taskId) assistantMsg._taskId = ev.taskId;
      /* ★ git-shim: round commit sha for redo/diff references */
      if (ev.gitSha) assistantMsg._gitSha = ev.gitSha;
      /* ★ Autopilot follow-up arrives in-band on the done event so the
       *   frontend can attach to the next task immediately, without
       *   polling /api/chat/active after the SSE stream has closed.
       *   finishStream() reads these to dispatch the follow-up. */
      if (ev.autopilotNextTaskId && ev.autopilotVuMessage) {
        assistantMsg._autopilotPending = {
          nextTaskId: ev.autopilotNextTaskId,
          vuMessage: ev.autopilotVuMessage,
        };
        console.info(
          `[connectToTask] 🤖 Autopilot follow-up attached to done — ` +
          `next task=${ev.autopilotNextTaskId.slice(0,8)} ` +
          `vu="${(ev.autopilotVuMessage.content||'').slice(0,80)}${(ev.autopilotVuMessage.content||'').length>80?'…':''}"`
        );
      }
      /* ★ Continue: merge usage & apiRounds with existing */ if (ev.usage) {
        if (typeof updateContextBar === 'function') updateContextBar();
        if (assistantMsg._continueUsage) {
          const cu = assistantMsg._continueUsage;
          const nu = ev.usage;
          assistantMsg.usage = {};
          for (const k of new Set([...Object.keys(cu), ...Object.keys(nu)])) {
            const cv = cu[k],
              nv = nu[k];
            assistantMsg.usage[k] =
              typeof cv === "number" && typeof nv === "number"
                ? cv + nv
                : (nv ?? cv);
          }
          delete assistantMsg._continueUsage;
        } else {
          assistantMsg.usage = ev.usage;
        }
      }
      if (ev.apiRounds) {
        if (assistantMsg._continueApiRounds) {
          assistantMsg.apiRounds = assistantMsg._continueApiRounds.concat(
            ev.apiRounds,
          );
          delete assistantMsg._continueApiRounds;
        } else {
          assistantMsg.apiRounds = ev.apiRounds;
        }
      }
      /* ★ Persisted-cost snapshot (stamped server-side at orchestrator
       *   finalisation). When present, finish_info.js reads msg.cost
       *   directly and skips the lazy /api/v1/messages/cost fetch. */
      if (ev.cost) assistantMsg.cost = ev.cost;
      /* ★ Clean up continue checkpoint markers */
      delete assistantMsg._continueToolRounds;
      delete assistantMsg._continueContentPrefix;
      delete assistantMsg._continueApiRounds;
      delete assistantMsg._continueUsage;
      delete assistantMsg._continueModifiedFiles;
      delete assistantMsg._continueModifiedFileList;
      return true;
    }
    return false;
  }
  try {
    // ★ Last-Event-ID resume: if we have a previous event id (from a
    //   prior connection attempt for this task), send it so the server
    //   resumes from that cursor instead of replaying the full state
    //   snapshot.
    const _sseHeaders = {};
    if (stream._lastEventId) {
      _sseHeaders['Last-Event-ID'] = stream._lastEventId;
      console.info(`[_trySSE] Reconnecting with Last-Event-ID=${stream._lastEventId} for task=${taskId.slice(0,8)}`);
    }
    const resp = await Api.chat.streamResponse(taskId, {
      signal: stream.controller.signal,
      headers: _sseHeaders,
    });
    if (!resp || !resp.ok) {
      clearTimeout(sseTimeout);
      if (resp && resp.status === 404) return false;
      throw new Error(`HTTP ${resp ? resp.status : 'no response'}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "",
      streamDone = false;
    while (!streamDone) {
      const { done: rd, value } = await reader.read();
      if (rd) {
        /* ★ Process any remaining data in buffer after stream closes */ if (
          buffer.trim()
        ) {
          const remaining = buffer.split("\n");
          for (const line of remaining) {
            if (_processSSELine(line)) {
              streamDone = true;
            }
          }
        }
        break;
      }
      gotData = true;
      clearTimeout(sseTimeout);
      _streamTimerTouch(convId); // ★ Any bytes (including keepalives) prove server is alive
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        const isDone = _processSSELine(line);
        if (isDone) {
          streamDone = true;
          break;
        }
      }
      const now = Date.now();
      if (now - lastSave > 3000) {
        /* ★ CROSS-TALK DETECTION: verify the conv we're about to save still has
         *   the right message count and the assistantMsg ref is valid */
        const _saveConv = conversations.find(c => c.id === convId);
        if (_saveConv) {
          const _saveLast = _saveConv.messages[_saveConv.messages.length - 1];
          if (_saveLast !== assistantMsg) {
            console.error(
              `[_trySSE] ⛔ PERIODIC SAVE: assistantMsg ref DETACHED from conv=${convId.slice(0,8)}! ` +
              `conv.messages[-1].role=${_saveLast?.role||'none'} ≠ assistantMsg. ` +
              `Streaming data is accumulating into a ghost object!`
            );
          }
          if (activeStreams.size > 1) {
            console.info(
              `[_trySSE] 📊 Periodic save: conv=${convId.slice(0,8)} msgs=${_saveConv.messages.length} ` +
              `contentLen=${(assistantMsg.content||'').length} ` +
              `concurrentStreams=${activeStreams.size} ` +
              `otherConvs=[${[...activeStreams.keys()].filter(k=>k!==convId).map(k=>k.slice(0,8)).join(',')}]`
            );
          }
        }
        saveConversations(convId);
        lastSave = now;
      }
      /* ★ No IndexedDB cache write during streaming — the server checkpoints
       *   to PostgreSQL every 5s (checkpoint_task_partial), which is always fresher.
       *   Cache is only updated in finishStream() when the stream completes. */
    }
    if (!streamDone) {
      /* ★ SSE stream closed prematurely without receiving 'done' event
         (e.g. proxy/TCP timeout on long-running tasks). Reset controller
         and return false so connectToTask falls back to polling. */
      const _accContent = assistantMsg.content?.length || 0;
      const _accThinking = assistantMsg.thinking?.length || 0;
      const _accRounds = assistantMsg.toolRounds?.length || 0;
      console.error(
        `[_trySSE] ⚠ SSE PREMATURE CLOSE — taskId=${taskId.slice(0,8)} ` +
        `contentAccumulated=${_accContent}chars thinkingAccumulated=${_accThinking}chars ` +
        `toolRounds=${_accRounds} lastEventId=${_lastEventId || 'none'} — falling back to poll. ` +
        `If poll returns empty content, this accumulated data will be OVERWRITTEN!\n` +
        `Possible causes: proxy timeout, TCP reset, server crash, nginx buffering.\n` +
        `Check server logs for matching task ID: ${taskId}`
      );
      /* ★ Emergency save: persist whatever we accumulated via SSE before poll overwrites it */
      saveConversations(convId);
      /* ★ No emergency cache write — server DB has 5s-fresh checkpoint data.
       *   Writing partial SSE-accumulated data to cache would create a stale
       *   snapshot that's WORSE than what the server already has. */
      // ★ Item 6: Save last event ID so reconnection can resume from cursor
      if (_lastEventId) stream._lastEventId = _lastEventId;
      stream.controller = new AbortController();
      return false;
    }
    twStop(convId);
    finishStream(convId);
    return true;
  } catch (e) {
    clearTimeout(sseTimeout);
    if (e.name === "AbortError") {
      /* ★ Check if this was a timer-probe abort (task already done on server,
       *   SSE pipe is stale) vs a user-initiated stop.
       *   Timer probe sets stream._probeAbort = true before aborting. */
      if (stream._probeAbort) {
        delete stream._probeAbort;
        console.warn(`[_trySSE] ★ Timer probe abort — task done on server, SSE was stale. lastEventId=${_lastEventId || 'none'}`);
        if (_lastEventId) stream._lastEventId = _lastEventId;
        stream.controller = new AbortController();
        return false;  // → triggers _pollFallback to retrieve completed result
      }
      if (!gotData && !stream._userAbort) {
        stream.controller = new AbortController();
        return false;
      }
      throw e;  // re-throw user abort → connectToTask handles it
    }
    throw e;
  }
}

async function _pollFallback(convId, taskId, stream, assistantMsg) {
  let lastSave = Date.now();
  const buf = streamBufs.get(convId);
  const _preExistingContent = assistantMsg.content?.length || 0;
  const _preExistingThinking = assistantMsg.thinking?.length || 0;
  console.warn(`[_pollFallback] START — conv=${convId.slice(0,8)} taskId=${taskId.slice(0,8)} preExistingContent=${_preExistingContent}chars preExistingThinking=${_preExistingThinking}chars`);
  // Poll until the task finishes, the user aborts, or server is confirmed dead.
  let _pollIter = 0;
  let _consecutiveErrors = 0;     // ★ Circuit breaker: track consecutive network failures
  const _MAX_CONSECUTIVE_ERRORS = 10; // ★ After 10 failures (~5s), do health check
  let _rttEma = 300; // ★ Item 8: exponential moving average of poll RTT (ms), seed 300ms
  while (true) {
    if (stream.controller.signal.aborted) {
      console.warn(`[_pollFallback] ABORTED at iteration ${_pollIter} — conv=${convId.slice(0,8)}`);
      twStop(convId);
      finishStream(convId);
      return;
    }
    const _pollStart = Date.now();
    try {
      const resp = await Api.chat.poll(taskId);
      if (!resp || !resp.ok) {
        if (resp && resp.status === 404) {
          console.error(`[_pollFallback] 404 NOT FOUND — taskId=${taskId.slice(0,8)} conv=${convId.slice(0,8)} ` +
            `existingContent=${assistantMsg.content?.length||0}chars existingThinking=${assistantMsg.thinking?.length||0}chars — ` +
            `${(assistantMsg.content || assistantMsg.thinking) ? 'PRESERVING existing accumulated data' : 'NO DATA to preserve, marking error'}`);
          if (!assistantMsg.content && !assistantMsg.thinking)
            assistantMsg.error = "Task not found";
          twStop(convId);
          finishStream(convId);
          return;
        }
        throw new Error(`Poll HTTP ${resp.status}`);
      }
      _consecutiveErrors = 0; // ★ Reset on any successful response
      const data = await resp.json();

      /* ★ SyncFix: discard poll responses for a superseded/aborted task so we
       *   don't resurrect old endpoint turns into conv.messages after the user
       *   interrupted + edited. */
      {
        const _pollConv = conversations.find(c => c.id === convId);
        if (_pollConv) {
          const _aborted = stream && stream.controller && stream.controller.signal.aborted;
          const _superseded = _pollConv.activeTaskId && _pollConv.activeTaskId !== taskId;
          const _isLastAborted = _pollConv._lastAbortedTaskId === taskId;
          if (_aborted || _superseded || _isLastAborted) {
            console.info(`[SyncFix][_pollFallback] discarding stale poll taskId=${taskId.slice(0,8)} activeTaskId=${_pollConv.activeTaskId?.slice(0,8)||'null'} aborted=${!!_aborted} superseded=${!!_superseded} isLastAborted=${!!_isLastAborted}`);
            twStop(convId);
            finishStream(convId);
            return;
          }
        }
      }

      /* ★ Endpoint mode: poll returns endpointTurns with the full multi-turn
       *   structure.  Rebuild conv.messages from it instead of overwriting
       *   a single assistantMsg with the current turn's content. */
      if (data.endpointMode && data.endpointTurns && data.endpointTurns.length > 0) {
        const conv = conversations.find(c => c.id === convId);
        if (conv) {
          // Find where original messages end (non-endpoint messages)
          let baseEnd = 0;
          for (let i = 0; i < conv.messages.length; i++) {
            if (!conv.messages[i]._epIteration && !conv.messages[i]._isEndpointReview && !conv.messages[i]._isEndpointPlanner) {
              baseEnd = i + 1;
            }
          }
          const baseMsgs = conv.messages.slice(0, baseEnd);
          const prevEpCount = conv._epPollTurnCount || 0;
          const newEpCount = data.endpointTurns.length;

          // Replace endpoint turns with the server's authoritative copy
          conv.messages = baseMsgs.concat(data.endpointTurns);
          conv._epPollTurnCount = newEpCount;

          // Point assistantMsg to the last assistant message for metadata/finishStream
          const lastAssist = [...conv.messages].reverse().find(m => m.role === "assistant");
          if (lastAssist) {
            assistantMsg = lastAssist;
          }

          // ★ DO NOT overwrite completed turn content with data.content!
          // data.content is the IN-PROGRESS turn (not yet in endpointTurns).
          // Completed turns in endpointTurns already have their full content.

          console.info(`[_pollFallback] Endpoint sync — conv=${convId.slice(0,8)} ` +
            `baseMsgs=${baseMsgs.length} endpointTurns=${newEpCount} ` +
            `totalMsgs=${conv.messages.length} prevTurns=${prevEpCount}`);

          // ★ Re-render the full conversation when new completed turns arrive
          if (newEpCount !== prevEpCount && activeConvId === convId) {
            renderChat(conv);
          }
        }
      } else {
        /* ★ Normal (non-endpoint) mode: update single assistantMsg.
         *
         * Regression-safe overwrite: a poll snapshot can briefly lag the
         * delta-applied client state because the server appends to
         * task['content'] under content_lock independently from append_event
         * — a poll observed between those lock cycles can see fewer chars
         * than the client already accumulated. Previously we logged the
         * regression then OVERWROTE anyway, silently losing what the user
         * already saw. Now we keep whichever side is longer and only log
         * suspicious shrinkage. */
        if (data.content != null) {
          const oldLen = assistantMsg.content?.length || 0;
          const newLen = data.content.length;
          if (newLen >= oldLen) {
            assistantMsg.content = data.content;
            if (buf) buf.content = assistantMsg.content;
          } else if (oldLen > 0 && newLen < oldLen * 0.5) {
            console.warn(`[_pollFallback] CONTENT REGRESSION ignored — conv=${convId.slice(0,8)} ` +
              `oldContentLen=${oldLen} newContentLen=${newLen} — keeping longer accumulated content (delta cycle vs poll cycle race).`);
          }
        }
        if (data.thinking != null) {
          const oldThinkLen = assistantMsg.thinking?.length || 0;
          const newThinkLen = data.thinking.length;
          if (newThinkLen >= oldThinkLen) {
            assistantMsg.thinking = data.thinking;
            if (buf) buf.thinking = assistantMsg.thinking;
          } else if (oldThinkLen > 0 && newThinkLen < oldThinkLen * 0.5) {
            console.warn(`[_pollFallback] THINKING REGRESSION ignored — conv=${convId.slice(0,8)} ` +
              `oldThinkingLen=${oldThinkLen} newThinkingLen=${newThinkLen} — keeping longer accumulated thinking.`);
          }
        }
      }
      if (data.error) assistantMsg.error = data.error;
      if (data.finishReason) assistantMsg.finishReason = data.finishReason;
      if (data.usage) {
        if (assistantMsg._continueUsage) {
          // Merge usage: sum numeric fields
          const cu = assistantMsg._continueUsage;
          for (const k of Object.keys(data.usage)) {
            const cv = cu[k], nv = data.usage[k];
            data.usage[k] = typeof cv === 'number' && typeof nv === 'number' ? cv + nv : (nv ?? cv);
          }
        }
        assistantMsg.usage = data.usage;
        if (typeof updateContextBar === 'function') updateContextBar();
      }
      if (data.preset) assistantMsg.preset = data.preset;
      else if (data.effort) assistantMsg.preset = data.effort;
      if (data.model) assistantMsg.model = data.model;
      if (data.thinkingDepth) assistantMsg.thinkingDepth = data.thinkingDepth;
      if (data.toolSummary) assistantMsg.toolSummary = data.toolSummary;
      if (data.fallbackModel) assistantMsg.fallbackModel = data.fallbackModel;
      if (data.fallbackFrom) assistantMsg.fallbackFrom = data.fallbackFrom;
      /* ★ Continue: merge modifiedFiles & modifiedFileList with checkpoint */
      if (data.modifiedFiles != null) {
        if (assistantMsg._continueModifiedFiles) {
          assistantMsg.modifiedFiles = assistantMsg._continueModifiedFiles + data.modifiedFiles;
          delete assistantMsg._continueModifiedFiles;
        } else {
          assistantMsg.modifiedFiles = data.modifiedFiles;
        }
      }
      if (data.modifiedFileList) {
        if (assistantMsg._continueModifiedFileList) {
          const merged = new Map();
          for (const f of assistantMsg._continueModifiedFileList) merged.set(f.path, f);
          for (const f of data.modifiedFileList) merged.set(f.path, f);
          assistantMsg.modifiedFileList = Array.from(merged.values());
          delete assistantMsg._continueModifiedFileList;
        } else {
          assistantMsg.modifiedFileList = data.modifiedFileList;
        }
      }
      if (data.taskId) assistantMsg._taskId = data.taskId;
      /* ★ memory prefetch: recover indicator state from poll response */
      if (data.memoryPrefetch) assistantMsg._memoryPrefetch = data.memoryPrefetch;
      /* ★ git-shim: round commit sha for redo/diff references */
      if (data.gitSha) assistantMsg._gitSha = data.gitSha;
      /* ★ Persisted cost snapshot (server-side stamp). */
      if (data.cost) assistantMsg.cost = data.cost;
      if (data.apiRounds) {
        const existingApiRounds = assistantMsg._continueApiRounds || [];
        assistantMsg.apiRounds = existingApiRounds.concat(data.apiRounds);
        // Carrier handed over — clear the checkpoint so the cleanup at
        // task-finalization (delete _continueApiRounds below) is not the
        // only cleanup site. SSE done path already does this in-place at
        // the equivalent merge step (~line 2246).
        delete assistantMsg._continueApiRounds;
      }
      if (data.toolRounds) {
        const existingRounds = assistantMsg._continueToolRounds || [];
        assistantMsg.toolRounds = existingRounds.concat(data.toolRounds);
        if (buf) buf.toolRounds = assistantMsg.toolRounds;
      }
      if (buf) buf.phase = data.phase || null;
      twUpdate(convId);
      const now = Date.now();
      if (now - lastSave > 3000) {
        saveConversations(convId);
        lastSave = now;
      }
      /* ★ No cache write during polling — server DB is always fresher */
      if (data.status !== "running") {
        /* ★ If status is 'interrupted', the server crashed mid-generation.
           Mark finishReason so the UI shows the recovery indicator. */
        if (data.status === 'interrupted' && !assistantMsg.finishReason) {
          assistantMsg.finishReason = 'interrupted';
          console.warn(`[_pollFallback] Task ${taskId.slice(0,8)} was interrupted (server crash recovery) — ` +
            `recovered content=${assistantMsg.content?.length||0}chars thinking=${assistantMsg.thinking?.length||0}chars`);
        }
        /* ★ Clean up continue checkpoint markers (poll fallback) */
        delete assistantMsg._continueToolRounds;
        delete assistantMsg._continueContentPrefix;
        delete assistantMsg._continueApiRounds;
        delete assistantMsg._continueUsage;
        delete assistantMsg._continueModifiedFiles;
        delete assistantMsg._continueModifiedFileList;
        twStop(convId);
        finishStream(convId);
        return;
      }
    } catch (e) {
      if (e.name === "AbortError") {
        twStop(convId);
        finishStream(convId);
        return;
      }
      _consecutiveErrors++;
      debugLog(`Poll error (${_consecutiveErrors}/${_MAX_CONSECUTIVE_ERRORS}): ${e.message}`, "warn");
      if (typeof _reportClientError === 'function') _reportClientError(`[poll] ${e.message}`);

      // ★ Circuit breaker: after N consecutive failures, check server health.
      //   For VSCode port forwarding drops, the outage may last 10-60s while
      //   the tunnel re-establishes. We enter a "network recovery wait" mode
      //   that waits up to 2 minutes before truly giving up.
      if (_consecutiveErrors >= _MAX_CONSECUTIVE_ERRORS) {
        console.error(`[_pollFallback] ⚠️ CIRCUIT BREAKER — ${_consecutiveErrors} consecutive poll failures for conv=${convId.slice(0,8)}`);
        const alive = await _checkServerHealth();
        if (!alive) {
          // ★ Network Recovery Wait: instead of immediately giving up, wait
          //   up to 2 minutes for the server to come back (VSCode reconnect).
          //   During this wait, check health every 5 seconds.
          const _RECOVERY_WAIT_MS = 120000; // 2 minutes
          const _RECOVERY_POLL_MS = 5000;   // check every 5s
          const _recoveryStart = Date.now();
          let _recovered = false;
          console.warn(`[_pollFallback] 🔄 Entering network recovery wait (up to ${_RECOVERY_WAIT_MS/1000}s) for conv=${convId.slice(0,8)}`);
          showToast('🔄', 'Connection Lost',
            'Server unreachable — waiting for reconnection… Task is still running on the server.', 8000);
          while (Date.now() - _recoveryStart < _RECOVERY_WAIT_MS) {
            if (stream.controller.signal.aborted) {
              twStop(convId);
              finishStream(convId);
              return;
            }
            await new Promise(r => setTimeout(r, _RECOVERY_POLL_MS));
            // Force a fresh health check (bypass cache)
            _lastHealthCheck = 0;
            const nowAlive = await _checkServerHealth();
            if (nowAlive) {
              console.warn(`[_pollFallback] ✅ Server is BACK after ${Math.round((Date.now() - _recoveryStart)/1000)}s — resuming poll for conv=${convId.slice(0,8)}`);
              _recovered = true;
              _consecutiveErrors = 0;
              showToast('✅', 'Reconnected', 'Server connection restored — resuming…', 4000);
              break;
            }
            console.debug(`[_pollFallback] Still waiting for server… ${Math.round((Date.now() - _recoveryStart)/1000)}s elapsed`);
          }
          if (!_recovered) {
            console.error(`[_pollFallback] 💀 SERVER STILL DEAD after ${_RECOVERY_WAIT_MS/1000}s recovery wait — force-finishing for conv=${convId.slice(0,8)} ` +
              `content=${assistantMsg.content?.length||0}chars thinking=${assistantMsg.thinking?.length||0}chars`);
            assistantMsg.finishReason = 'server_offline';
            assistantMsg.error = '⚠️ Server offline — response may be incomplete. This notice will clear automatically when the server comes back.';
            saveConversations(convId);
            twStop(convId);
            finishStream(convId);
            showToast('⚠️', 'Server Offline',
              'Backend server did not reconnect within 2 minutes. Your partial response has been saved. It will recover automatically when the server comes back.',
              12000);
            // ★ Start periodic recovery polling so the result is auto-recovered later
            _startOfflineRecoveryPolling();
            return;
          }
          // If recovered, fall through and continue the poll loop
        } else {
          // Server is alive but poll failed (maybe task was cleaned up) — continue trying a bit more
          _consecutiveErrors = Math.floor(_MAX_CONSECUTIVE_ERRORS / 2); // partial reset
        }
      }
    }
    // ★ Item 8: Measure RTT for adaptive delay
    const _pollRtt = Date.now() - _pollStart;
    _rttEma = Math.round(_rttEma * 0.7 + _pollRtt * 0.3); // EMA with α=0.3
    _pollIter++;
    // ★ RTT-adaptive poll interval: when the tunnel is fast (RTT < 100ms),
    //   poll more aggressively (min 300ms sleep). When slow (RTT > 500ms),
    //   back off to avoid wasting bandwidth. After the initial burst (first
    //   4 polls), gradually ramp the base interval.
    //   Effective interval = sleep + RTT ≈ target responsiveness.
    const _baseDelay = _pollIter < 4 ? 300 : Math.min(300 + _pollIter * 100, 1500);
    // Scale by RTT: fast tunnel → shorter sleep; slow tunnel → longer sleep
    const _rttFactor = Math.max(0.5, Math.min(2.0, _rttEma / 200));
    const _pollDelay = Math.round(Math.min(_baseDelay * _rttFactor, 2000));
    await new Promise((r) => setTimeout(r, _pollDelay));
  }
  // Loop only exits via return (task done, abort, server dead, or 404) — no infinite hang.
}

function updateSendButton() {
  const btn = document.getElementById("sendBtn");
  const conv = getActiveConv();

  // ── Detect branch streaming: if in branch mode, check branch-specific stream ──
  let branchStreaming = false;
  let branchStreamKey = null;
  if (_activeBranch && conv) {
    const bk = _branchKey(conv.id, _activeBranch.msgIdx, _activeBranch.branchIdx);
    if (_branchStreams.has(bk)) {
      branchStreaming = true;
      branchStreamKey = bk;
    }
  }

  // Also detect any branch stream for this conversation (even if not in branch mode)
  let anyBranchStreaming = false;
  if (conv && !branchStreaming) {
    const prefix = conv.id + ":";
    for (const k of _branchStreams.keys()) {
      if (k.startsWith(prefix)) { anyBranchStreaming = true; break; }
    }
  }

  const mainStreaming =
    activeStreams.has(activeConvId) || (conv && conv.activeTaskId);
  const translating = conv && conv._translating;
  const streaming = branchStreaming || mainStreaming || anyBranchStreaming || translating;

  if (streaming) {
    const queueCount = (conv && pendingMessageQueue.has(conv.id)) ? pendingMessageQueue.get(conv.id).length : 0;
    btn.className = "send-btn stop-btn";
    btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>`
      + (queueCount > 0 ? `<span class="queue-badge">${queueCount}</span>` : '');
    btn.onclick = () => {
      // ── Priority 0: stop translation ──
      if (translating && conv) {
        console.log(`[stopBtn] Aborting translation — conv=${conv.id.slice(0,8)}`);
        conv._translateAborted = true;
        conv._translating = false;
        // Abort the in-flight sync fetch if present
        if (conv._translateAbortCtrl) {
          conv._translateAbortCtrl.abort();
          conv._translateAbortCtrl = null;
        }
        updateSendButton();
        renderConversationList();
        return;
      }
      // ── Priority 1: stop active branch stream ──
      if (branchStreaming && branchStreamKey) {
        const bs = _branchStreams.get(branchStreamKey);
        if (bs) {
          // ★ Pre-set finishReason before abort kills the SSE reader
          const _bMsg = conv.messages[_activeBranch.msgIdx];
          const _bBranch = _bMsg?.branches?.[_activeBranch.branchIdx];
          if (_bBranch?.messages) {
            const _bLast = _bBranch.messages[_bBranch.messages.length - 1];
            if (_bLast?.role === 'assistant') _bLast.finishReason = 'aborted';
          }
          bs.controller.abort();
          Api.chat.abortTask(bs.taskId);
          // Clean up branch state
          const msg = conv.messages[_activeBranch.msgIdx];
          const branch = msg?.branches?.[_activeBranch.branchIdx];
          if (branch) branch.activeTaskId = null;
          _finishBranchStream(conv, _activeBranch.msgIdx, _activeBranch.branchIdx, branch, branchStreamKey);
        }
        return;
      }
      // ── Priority 2: stop any branch stream for this conv ──
      if (anyBranchStreaming && conv) {
        const prefix = conv.id + ":";
        for (const [k, bs] of _branchStreams.entries()) {
          if (k.startsWith(prefix)) {
            // ★ Pre-set finishReason before abort kills the SSE reader
            const _p2parts = k.split(":");
            const _p2mi = parseInt(_p2parts[1]);
            const _p2bi = parseInt(_p2parts[2]);
            const _p2msg = conv.messages[_p2mi];
            const _p2branch = _p2msg?.branches?.[_p2bi];
            if (_p2branch?.messages) {
              const _p2last = _p2branch.messages[_p2branch.messages.length - 1];
              if (_p2last?.role === 'assistant') _p2last.finishReason = 'aborted';
            }
            bs.controller.abort();
            Api.chat.abortTask(bs.taskId);
            // Parse key to get msgIdx, branchIdx
            const parts = k.split(":");
            const mi = parseInt(parts[1]);
            const bi = parseInt(parts[2]);
            const msg = conv.messages[mi];
            const branch = msg?.branches?.[bi];
            if (branch) branch.activeTaskId = null;
            _finishBranchStream(conv, mi, bi, branch, k);
          }
        }
        return;
      }
      // ── Priority 3: stop main stream ──
      const s = activeStreams.get(activeConvId);
      if (s) {
        console.log(`[stopBtn] Aborting main stream — conv=${activeConvId.slice(0,8)} task=${s.taskId?.slice(0,8)}`);
        // ★ Pre-set finishReason before abort kills the SSE reader
        if (conv) {
          const _stopMsg = conv.messages[conv.messages.length - 1];
          if (_stopMsg && _stopMsg.role === 'assistant') {
            _stopMsg.finishReason = 'aborted';
          }
        }
        // ★ FIX: Mark as user-initiated abort so _trySSE doesn't fall back
        //   to polling when gotData=false (indistinguishable from SSE timeout).
        s._userAbort = true;
        s.controller.abort();
        // ★ Record aborted task ID so sendMessage can inform the backend
        //   even if the abort API call hasn't completed yet.
        if (conv) conv._lastAbortedTaskId = s.taskId;
        Api.chat.abortTask(s.taskId);
        // ★ SyncFix: don't wait for finishStream to tear down the stream
        //   buffer — a late delta event arriving between abort() and the
        //   AbortError propagation would otherwise accumulate into a dead
        //   buffer. twStop is idempotent and safe to call twice.
        try { twStop(activeConvId); }
        catch (_e) { console.warn('[stopBtn] twStop threw:', _e); }
      } else if (conv && conv.activeTaskId) {
        // ★ Record aborted task ID
        const _abortingTaskId = conv.activeTaskId;
        if (conv) conv._lastAbortedTaskId = _abortingTaskId;
        Api.chat.abortTask(_abortingTaskId);
        // ★ Pre-set finishReason for the no-stream abort path too
        const _noStreamMsg = conv.messages[conv.messages.length - 1];
        if (_noStreamMsg && _noStreamMsg.role === 'assistant') {
          _noStreamMsg.finishReason = 'aborted';
        }
        conv.activeTaskId = null;
        conv._activeTaskClearedAt = Date.now();
        finishStream(activeConvId);
      }
    };
  } else {
    btn.className = "send-btn";
    btn.innerHTML = `<span style="font-size:13px;font-weight:600;letter-spacing:.5px">⏎</span>`;
    btn.onclick = sendMessage;
  }
}
