/* ═══════════════════════════════════════════════════════════════════
   sse pipeline — extracted from ui.js (split 2026-05-28)

   SSE chat-stream pipeline: connectToTask, _trySSE, _pollFallback, updateSendButton.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* ── SSE cursor persistence across page reload (#4) ──────────────────────
 * `stream._lastEventId` is per-connection in-memory state, so a page reload
 * destroys it → the reload reconnect sends NO Last-Event-ID and the server
 * replays a full state snapshot. That's lossless for FINAL state, but loses
 * the intermediate tool/phase event granularity the user had already seen.
 * We persist the cursor in `sessionStorage` (survives reload, auto-scoped
 * per-tab, cleared on tab close) keyed by taskId, so the reload reconnect can
 * offset-resume via the SAME Last-Event-ID path the in-memory reconnect uses.
 * The server's warm/cold replay logic is unchanged — this only feeds it the
 * cursor it already knows how to honor. sessionStorage can throw (private
 * mode / quota) so every access is guarded; failure silently degrades to the
 * existing full-snapshot reload.
 */
function _sseCursorKey(taskId) { return 'tofu_sse_cursor_' + taskId; }

function _saveSseCursor(taskId, eventId) {
  if (!taskId || eventId == null) return;
  try { sessionStorage.setItem(_sseCursorKey(taskId), String(eventId)); }
  catch (e) { /* private mode / quota — degrade to full-snapshot reload */ }
}

function _loadSseCursor(taskId) {
  if (!taskId) return null;
  try { return sessionStorage.getItem(_sseCursorKey(taskId)); }
  catch (e) { return null; }
}

function _clearSseCursor(taskId) {
  if (!taskId) return;
  try { sessionStorage.removeItem(_sseCursorKey(taskId)); }
  catch (e) { /* no-op */ }
}


/* ── State-snapshot regression guard — TWO-TIER (2026-07-11) ──────────────
 * A reconnect `state` snapshot replays the server's record of the turn. Its
 * TEXT (content/thinking) is now BACKEND-AUTHORITATIVE and applied VERBATIM at
 * the 5 state sites: the server folds the lossless per-delta task_events log on
 * every cold path (lib/tasks_pkg/event_fold.py::fold_cold_state_text) AND
 * persists each delta BEFORE pushing it to the client (durable-before-visible
 * ordering, lib/tasks_pkg/manager.py::append_event) — so a state snapshot's
 * text is never SHORTER than the client buffer. The old `_snapshotLonger` text
 * keep-longer belt was therefore RETIRED here (the "sent and generating, later
 * found GONE" cold-replay race is closed at the source).
 *
 * toolRounds is the REMAINING residual: on every cold path it is still sourced
 * from the 5s task_results.tool_rounds checkpoint / the conversation (NOT the
 * delta fold — reconstructing rounds needs the tool_start/tool_done
 * choreography, owned by the segment-timeline epic). So a cold mid-round
 * reconnect can still deliver a SHORTER rounds array, and the keep-longer guard
 * below is still load-bearing FOR ROUNDS ONLY.
 *
 * Invariant (rounds): a snapshot may only GROW the rounds, never shrink them.
 * Returns the incoming array only when it is at least as long as the current one.
 */
function _snapshotLongerRounds(current, incoming) {
  const cur = Array.isArray(current) ? current : [];
  const inc = Array.isArray(incoming) ? incoming : [];
  return inc.length >= cur.length ? inc : cur;
}


/**
 * Connect to an autopilot KICK carrier task (push-a-finished-conv-forward).
/**
 * Connect to an autopilot KICK carrier task (push-a-finished-conv-forward).
 *
 * The carrier emits no worker content — only the `autopilot_vu_*` stream and
 * a terminal `done` carrying the follow-up baton.  So unlike `connectToTask`
 * we deliberately do NOT push an assistant placeholder into `conv.messages`:
 *   • A ghost empty "Agent" bubble before the VU bubble would be confusing.
 *   • The carrier's empty `done` event, if it targeted a real message, would
 *     blank out the prior agent reply.
 * Instead we hand the SSE pipeline a DETACHED dummy assistantMsg (not in
 * `conv.messages`).  The VU bubble is created by `autopilot_vu_start` →
 * `_beginVuStreaming`, and the `done` handler detects the detached dummy
 * (indexOf === -1) and stamps the autopilot baton on the finalized VU user
 * message at the tail instead.
 *
 * @param {string} convId
 * @param {string} taskId
 */
async function _connectAutopilotKick(convId, taskId) {
  const conv = conversations.find((c) => c.id === convId);
  if (!conv) return;
  console.info(
    `[connectToTask] 🤖 Autopilot KICK connect — conv=${convId.slice(0,8)} task=${taskId.slice(0,8)}`
  );
  if (activeStreams.has(convId)) {
    console.info(`[connectToTask] kick: stream already active for conv=${convId.slice(0,8)} — skipping`);
    return;
  }
  /* Detached dummy — NOT pushed into conv.messages. The SSE pipeline needs a
   * non-null assistantMsg to accumulate into, but for a kick carrier nothing
   * worker-side is emitted, so this stays empty and unreferenced. */
  const dummyAssistant = {
    role: 'assistant', content: '', thinking: '', toolRounds: [],
    timestamp: Date.now(),
  };
  if (typeof _ensureMsgId === 'function') _ensureMsgId(dummyAssistant);

  const controller = new AbortController();
  activeStreams.set(convId, { controller, taskId, assistantMsg: dummyAssistant });
  conv.activeTaskId = taskId;
  renderConversationList();
  updateSendButton();
  if (typeof twStart === 'function') twStart(convId);

  const stream = activeStreams.get(convId);
  let sseWorked = false;
  try {
    sseWorked = await _trySSE(convId, taskId, stream, dummyAssistant);
  } catch (e) {
    if (e.name === 'AbortError') {
      if (typeof twStop === 'function') twStop(convId);
      finishStream(convId);
      return;
    }
    debugLog(`Autopilot kick SSE failed: ${e.message}`, 'warn');
  }
  /* ★ Tunnel-stutter fix (#2): bounded SSE resume before poll surrender — same
   *   as connectToTask. The autopilot-kick carrier is short-lived but still
   *   drops on a flaky tunnel; a resume keeps it on SSE. */
  if (!sseWorked && !stream.controller.signal.aborted) {
    try {
      sseWorked = await _resumeSSEWithRetry(convId, taskId, stream, dummyAssistant);
    } catch (e) {
      if (e.name === 'AbortError') { if (typeof twStop === 'function') twStop(convId); finishStream(convId); return; }
      debugLog(`Autopilot kick SSE resume-retry failed: ${e.message}`, 'warn');
    }
  }
  if (!sseWorked && !stream.controller.signal.aborted) {
    debugLog(`Autopilot kick falling back to polling for ${taskId.slice(0, 8)}`, 'warn');
    await _pollFallback(convId, taskId, stream, dummyAssistant);
  }
}

/* ★ Bounded SSE resume-retry — the tunnel-stutter root-cause fix (#2).
 *
 *   On a buffering / flaky tunnel (VS Code port-forward, nginx, corporate
 *   proxy) the SSE connection is dropped MID-TURN. `_trySSE` returns false on
 *   that premature close, and the OLD path dropped STRAIGHT to `_pollFallback`
 *   and stayed there for the entire rest of the turn → lumpy 300–2000ms
 *   RTT-adaptive full-repaints (the reported stutter). But `_trySSE` already
 *   stashed `stream._lastEventId` (the last received `id:` cursor) and re-armed
 *   a fresh `stream.controller` right before returning false — everything
 *   needed to RESUME a live SSE. The server replays post-cursor events via the
 *   `Last-Event-ID` header (routes/chat.py) and live streaming continues.
 *
 *   So: re-open SSE while it keeps making PROGRESS instead of surrendering to
 *   poll on the first drop. "Progress" = the cursor advanced since the previous
 *   attempt; a reconnect that stalls (cursor unchanged → no new events) bails to
 *   poll immediately. Only genuinely-blocked SSE (never delivered a tagged event
 *   → NO cursor, or a stalled/exhausted resume) falls to poll — the correct
 *   last resort for a proxy that strips event-stream entirely.
 *
 *   This never amplifies dead air: no cursor (zero real events ever) → return
 *   false at once, no retry. Returns true iff a resume ran the stream to `done`
 *   (SSE owns finishStream); false → caller falls through to poll. Lets a
 *   user-stop AbortError propagate so connectToTask's single catch handles it.
 */
const _MAX_SSE_RESUME_ATTEMPTS = 6;
async function _resumeSSEWithRetry(convId, taskId, stream, assistantMsg) {
  let attempts = 0;
  while (attempts < _MAX_SSE_RESUME_ATTEMPTS) {
    if (stream.controller.signal.aborted) return false;
    /* No cursor → SSE never delivered a real (id:-tagged) event this turn, so
     * a resume would just replay the same snapshot and drop again. Surrender to
     * poll — the right call when the proxy strips event-stream wholesale. */
    const cursor = stream._lastEventId;
    if (!cursor) return false;
    attempts++;
    debugLog(
      `[connectToTask] ↻ SSE resume attempt ${attempts}/${_MAX_SSE_RESUME_ATTEMPTS} ` +
      `via Last-Event-ID=${cursor} for task=${taskId.slice(0,8)} (tunnel premature-close recovery)`,
      'warn'
    );
    const _ok = await _trySSE(convId, taskId, stream, assistantMsg);
    if (_ok) return true;                       // resumed and ran to done
    if (stream.controller.signal.aborted) return false;
    /* The cursor did NOT advance vs the attempt we just made → the resume is
     * stalled (reconnecting but no new events land). Stop retrying and let poll
     * take over rather than spin. A cursor that DID advance = genuine choppy
     * progress → loop again (still bounded by _MAX_SSE_RESUME_ATTEMPTS). */
    if (stream._lastEventId === cursor) {
      debugLog(
        `[connectToTask] SSE resume stalled at cursor=${cursor} — surrendering to poll ` +
        `for task=${taskId.slice(0,8)}`, 'warn'
      );
      return false;
    }
  }
  debugLog(
    `[connectToTask] SSE resume exhausted (${_MAX_SSE_RESUME_ATTEMPTS} attempts) ` +
    `for task=${taskId.slice(0,8)} — surrendering to poll`, 'warn'
  );
  return false;
}

// ── Stream connection ──
async function connectToTask(convId, taskId, retries = 0, opts = {}) {
  const conv = conversations.find((c) => c.id === convId);
  if (!conv) return;
  /* ★ Autopilot kick-from-idle: the carrier task has NO worker turn, so we
   *   must NOT push the usual empty assistant placeholder (it would render a
   *   ghost "Agent" bubble before the VU bubble, and the carrier's empty
   *   `done` event could overwrite the prior real reply).  Delegate to a
   *   dedicated connector that uses a DETACHED dummy assistantMsg; the VU
   *   bubble itself is created by the `autopilot_vu_start` event.
   * ★ vuCarrier (2026-07-26, conv ms1rrjchpa5pqw): the SAME constraint
   *   applies when hopping from the parent's closed stream onto the VU
   *   sub-task's own stream (`latestLiveTaskId` + `latestLiveTaskIsVu`) —
   *   the carrier emits ONLY the autopilot_vu_* contract, so binding the
   *   parent assistant (or a fresh placeholder) as the stream target would
   *   render the VU's frames as a second "Agent" bubble. Same connector,
   *   same detached-dummy discipline. */
  if (opts && (opts.autopilotKick || opts.vuCarrier)) {
    return _connectAutopilotKick(convId, taskId);
  }
  /* ★ Duplicate-connect guard (pt_44e985ec): a second connectToTask for the
   *   SAME conv+task must not open a second SSE reader — the server supersedes
   *   the first, whose connect then surrenders to the poll fallback, leaving
   *   the poll lane and the surviving SSE lane racing the same bubble (the
   *   等待中…↔推理中 N字符 flip-flop). Reported server-side: the caller that
   *   double-fired is exactly the forensic signal for this class. */
  const _dupStream = activeStreams.get(convId);
  if (_dupStream && _dupStream.taskId === taskId
      && _dupStream.controller && !_dupStream.controller.signal.aborted) {
    console.info(
      `[connectToTask] ⏭ stream already active for conv=${convId.slice(0,8)} ` +
      `task=${taskId.slice(0,8)} — skipping duplicate connect`
    );
    if (typeof _reportClientError === 'function') {
      _reportClientError(
        `[connectToTask] duplicate connect suppressed (stream already active) ` +
        `conv=${convId.slice(0,8)} task=${taskId.slice(0,8)}`);
    }
    return;
  }
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
  /* ★ Identity-first target resolution — if an assistant slot is ALREADY
   *   bound to THIS taskId (a reconnect after finishStream cleared the stream
   *   entry, a poll→SSE takeover, or a reconnect to a just-finished task),
   *   re-target that exact slot instead of the array tail.  This is the
   *   root-cause guard for the "one user → two assistants" duplicate bubble:
   *   the positional fallback below, combined with the stale-turn guard,
   *   used to APPEND a fresh empty placeholder when the tail carried a
   *   finishReason — even when that completed tail WAS this task's own reply.
   *   Matching by identity (never by position) makes a second bubble for the
   *   same task structurally impossible. `_taskId` is stamped at bind time
   *   (below) and by the done/state/poll handlers, so any slot that ever
   *   streamed for this task resolves here. */
  if (!assistantMsg) {
    const _byTask = _resolveAssistantByTaskId(conv, taskId);
    if (_byTask) {
      assistantMsg = _byTask;
      console.info(
        `[connectToTask] 🎯 Re-targeting via taskId=${taskId.slice(0,8)} ` +
        `(existing bound slot; conv=${convId.slice(0,8)}) — no new bubble`
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
    /* ★ Single-source-of-truth: the "is this tail a prior turn?" decision is
     *   the shared reducer in core/conversations.js — a pure reducer over the
     *   backend-issued facts (`_taskId` bind + `finishReason`), NOT a re-inlined
     *   client-state predicate that can drift from the init_tasks Case-A call
     *   site. A tail owned by a different task, OR any completed tail
     *   (finishReason set), is a prior turn a fresh placeholder must precede —
     *   reusing it would replay the old turn's content into the new bubble
     *   ("上一轮对话又重新流式吐出"). Endpoint mode is gated out above. */
    if (assistantTailIsPriorTurn(assistantMsg, taskId)) {
      console.info(
        `[connectToTask] 🆕 Last assistant belongs to a prior turn ` +
        `(taskId=${assistantMsg._taskId?.slice(0,8) || 'none'} vs new=${taskId.slice(0,8)}, ` +
        `finishReason=${assistantMsg.finishReason || 'none'}) — pushing fresh placeholder ` +
        `for conv=${convId.slice(0,8)} so SSE doesn't replay old content into it`
      );
      if (typeof _reportClientError === 'function') {
        _reportClientError(
          `[connectToTask] pushed placeholder (prior-turn guard) conv=${convId.slice(0,8)} ` +
          `task=${taskId.slice(0,8)} tailTask=${assistantMsg._taskId?.slice(0,8) || 'none'} ` +
          `finishReason=${assistantMsg.finishReason || 'none'}`);
      }
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
    if (typeof _reportClientError === 'function') {
      _reportClientError(
        `[connectToTask] pushed recovery placeholder (tail=${assistantMsg?.role || 'missing'}) ` +
        `conv=${convId.slice(0,8)} task=${taskId.slice(0,8)}`);
    }
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
    /* ★ Bind the slot to this task immediately (before any token arrives) so
     *   a reconnect landing after finishStream cleared the stream entry can
     *   re-target it via _resolveAssistantByTaskId instead of appending a
     *   duplicate placeholder.  The done/state/poll handlers also stamp
     *   _taskId; doing it here closes the gap for a task that finishes (or
     *   drops) before those handlers run. */
    if (assistantMsg && !assistantMsg._taskId) assistantMsg._taskId = taskId;
    activeStreams.set(convId, { controller, taskId, assistantMsg });
    renderConversationList();
    updateSendButton();
    if (activeConvId === convId) {
      _lastRenderedFingerprint = "";
      const inner = document.getElementById("chatInner");
      /* ★ Identity-keyed streaming insert via ConvView (the single seam).
       *   The old path evicted `msg-${lastIdx}` by ARRAY INDEX, which MISSED
       *   the real static bubble whenever the tail's index had DRIFTED (a
       *   placeholder push / splice / lazy-window offset). The stranded static
       *   bubble then coexisted with the finalized streaming bubble as TWO
       *   identical DOM nodes for ONE conv.messages entry — the render
       *   duplicate (self-heals on refresh because the full innerHTML rebuild
       *   is a faithful projection). ConvView.startStreaming evicts by the
       *   stable data-msg-id instead, so index drift can no longer strand a
       *   twin. */
      if (!document.getElementById("streaming-msg")) {
        /* ★ Detect endpoint planner phase so reconnection shows "Planner"
         *   instead of "Agent".  Check: the assistantMsg has _isEndpointPlanner,
         *   or the conv has endpointEnabled and no worker turns yet (iteration 0). */
        const _isEpPlanner = assistantMsg._isEndpointPlanner
          || (conv.endpointEnabled && !conv.messages.some(m => m._epIteration));
        const _reconRole = _isEpPlanner ? 'planner'
          : (assistantMsg._swarmAutoContinue ? 'swarm' : 'worker');
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
        const _reconTime = formatClockTime(assistantMsg.timestamp);
        /* No raw fallback — the boot-time ConvView hard check (main.js)
         * turns a missing seam into a loud startup failure. */
        window.ConvView.startStreaming(convId, {
          role: _reconRole, status: _reconStatus,
          timeStr: _reconTime, msgId: assistantMsg._msgId || null,
        });
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
                const _resumeThinkLbl = (typeof t === 'function') ? t('stream.thinking.done') : 'Thinking Process';
                _html += `<details class="thinking-block" open>` +
                  `<summary>${escapeHtml(_resumeThinkLbl)}</summary>` +
                  `<div class="thinking-body">${renderMarkdown(assistantMsg.thinking)}</div>` +
                  `</details>`;
              }
              if (assistantMsg.content && assistantMsg.content.length) {
                _html += `<div class="md-content">${renderMarkdown(assistantMsg.content)}</div>`;
              }
              _html += `<div class="stream-status"><div class="pulse"></div> Resuming…</div>`;
              _body.innerHTML = _html;
              /* ★ Repaint the live translation preview after this reconnect
               *   rebuild, same as showStreamingUIForConv. The innerHTML
               *   assignment above wiped any translatePreview zone; without
               *   this the Chinese-so-far stays blank until the next server
               *   push frame (one per tool round, 20-40s away). The bubble's
               *   data-msg-id was stamped above, so the repaint targets it. */
              if (assistantMsg._translatePartial && assistantMsg._msgId
                  && typeof _renderStreamingTranslatePreview === 'function') {
                _renderStreamingTranslatePreview(convId, assistantMsg._msgId, assistantMsg._translatePartial, assistantMsg._translatePartialByRound);
              }
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
    if (typeof twStart === 'function') twStart(convId);
    /* §7 streamBufs fully RETIRED (the Map no longer exists): NO buffer
     * seeding — content/thinking/rounds
     * project straight from the message document (assistantMsg), which the
     * checkpoint already carries; phase lives in streamSessions. */
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
          /* ★ Defensive mirror of the backend dangling-round sweep
           *   (orchestrator._finalize_dangling_tool_rounds): flip any tool
           *   round still 'searching' to 'aborted' so the live DOM doesn't
           *   keep showing "Running…" until the backend's persisted snapshot
           *   lands. The authoritative state is still written server-side. */
          if (Array.isArray(_abortMsg.toolRounds)) {
            for (const _r of _abortMsg.toolRounds) {
              if (_r && _r.status === 'searching' && !(_r.results && _r.results.length)) {
                _r.status = 'aborted';
              }
            }
          }
          console.log(`[connectToTask] User abort — set finishReason='aborted' for conv=${convId.slice(0,8)}`);
        }
      }
      if (typeof twStop === 'function') twStop(convId);
      finishStream(convId);
      return;
    }
    debugLog(`SSE failed: ${e.message}`, "warn");
  }
  /* ★ Tunnel-stutter fix (#2): before surrendering to the lumpy poll fallback,
   *   try a bounded SSE resume via Last-Event-ID. On a buffering tunnel the SSE
   *   drops mid-turn but can re-attach and keep live-streaming; only genuinely
   *   blocked SSE falls through to poll. Idempotent + abort-safe (see helper). */
  if (!sseWorked && !stream.controller.signal.aborted) {
    try {
      sseWorked = await _resumeSSEWithRetry(convId, taskId, stream, assistantMsg);
    } catch (e) {
      if (e.name === "AbortError") { if (typeof twStop === 'function') twStop(convId); finishStream(convId); return; }
      debugLog(`SSE resume-retry failed: ${e.message}`, "warn");
    }
  }
  if (!sseWorked && !stream.controller.signal.aborted) {
    debugLog(`Falling back to polling for ${taskId.slice(0, 8)}`, "warn");
    await _pollFallback(convId, taskId, stream, assistantMsg);
  }
}

/* ════════════════════════════════════════════════════════════════════
   SSE event dispatcher — extracted from the _processSSELine closure
   inside _trySSE (refactor 2026-06). The former closure-locals now live
   on a mutable `ctx` object so this is a plain module-level function that
   can be unit-tested (see tests/test_frontend_sse_dispatch.py via the
   window.__sse_test__ seam). Body is byte-for-byte the original dispatch
   logic; only the 7 reassigned locals were lifted to `ctx` (destructured
   on entry, written back in `finally` so every early `return` propagates).

   ctx = { convId, taskId, stream, assistantMsg, epCriticPhase,
           epCriticMsg, roundThinkingLen, lastEventId,
           pinnedMsgId }
   Returns truthy only for the `done` event (signals stream end). */
function dispatchSSEEvent(line, ctx) {
  let { assistantMsg, pinnedMsgId: _pinnedMsgId } = ctx;
  let _epCriticPhase = ctx.epCriticPhase;
  let _epCriticMsg = ctx.epCriticMsg;
  let _roundThinkingLen = ctx.roundThinkingLen;
  let _lastEventId = ctx.lastEventId;
  let _pendingEventId = ctx.pendingEventId;
  const convId = ctx.convId, taskId = ctx.taskId, stream = ctx.stream;
  try {
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
  /* Snapshot of the live dispatch state passed to the extracted
   * property-only handlers (ui/sse_handlers_tool.js / _swarm.js). They
   * mutate object PROPERTIES of assistantMsg/_epCriticMsg (same refs we
   * hold here) and never reassign the locals, so no write-back is needed. */
  function _hctx() {
    return { convId, taskId, stream, assistantMsg,
             epCriticPhase: _epCriticPhase, epCriticMsg: _epCriticMsg };
  }
    // ★ Capture id: field for Last-Event-ID reconnection.
    //   The id line PRECEDES its paired data line on the wire, so we only
    //   STASH it here as pending — the cursor is committed once the paired
    //   data event has parsed and is about to be applied (see below). This
    //   guarantees Last-Event-ID never names an event that was not applied:
    //   a drop between the id and data lines would otherwise advance the
    //   cursor past an unapplied delta, which the resume (event_id > cursor)
    //   then silently skips.
    if (line.startsWith("id: ")) {
      _pendingEventId = line.slice(4).trim();
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
    /* ★ receivedAt (pt_67ffc2b7) — stamped HERE, at stream INGRESS, and only
     * for tool frames. This is the client end of the timing contract:
     *   execution = tEnd - tStart          (both backend clocks)
     *   transport = receivedAt - emittedAt (backend → this line)
     *   render    = painted  - receivedAt  (this line → pixels)
     * Without it "the tool is slow" cannot be told apart from "the frame was
     * slow to arrive" or "the browser dropped the paint" (health_stream_timer
     * really does drop renders — see its [twFlush-skip] diagnostic).
     *
     * It is stamped at ingress rather than inside the reducer on purpose: the
     * reducer is a PURE function whose live fold must stay byte-identical to
     * the cold projection of the same turn, and a Date.now() in there would
     * break that contract (and its purity guard). Scoped to tool frames so the
     * delta hot path is untouched.
     *
     * NOTE the shape of the type check. Doing the typeof test directly against
     * `ev.type` trips tests/test_event_registry.py's frontend scanner: it finds
     * every event type this file handles by matching a strict-equality compare
     * against the `.type` field, does NOT strip comments, and would read that
     * form as a declaration that we handle an event type literally named after
     * the typeof result — failing the registry drift guard. Reading the field
     * into a local first keeps the guard honest without inventing a phantom
     * event type. */
    const _evType = ev && ev.type;
    if (typeof _evType === 'string' && _evType.indexOf('tool_') === 0
        && ev.receivedAt == null) {
      ev.receivedAt = Date.now();
    }
    /* ★ Stall watch feed (pt_e0ea29f2): EVERY dispatched event — live or
     * Last-Event-ID replay — flows through this ONE seam, so the "no
     * unannounced freeze" detector in ui/stall_watch.js sees the same
     * evidence the user does. Self-tick heartbeat frames are graded there
     * (they prove the dispatcher, not the tool). Best-effort: a watch
     * failure must never take the stream down. */
    if (typeof stallWatchFeed === 'function') {
      try { stallWatchFeed(convId, taskId, ev); }
      catch (_swErr) { console.debug('[stall-watch] feed failed: %s', _swErr); }
    }
    // ★ Commit the resume cursor now: the paired data event has parsed and
    //   is applied SYNCHRONOUSLY below (dispatchSSEEvent has no await between
    //   here and its return), so once we advance _lastEventId the named event
    //   is guaranteed to have been applied. Persist it too so a page RELOAD
    //   (which wipes the in-memory stream._lastEventId) can offset-resume
    //   instead of replaying a full snapshot; cleared on terminal done.
    if (_pendingEventId != null) {
      _lastEventId = _pendingEventId;
      _saveSseCursor(ctx.taskId, _lastEventId);
      _pendingEventId = null;
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
      /* ★ Seed the elapsed timer from the SERVER-AUTHORITATIVE task start so a
       *   refresh / reconnect continues from the real elapsed instead of
       *   restarting from 0 (twStart used the connect instant). min-guarded in
       *   _seedStreamTimerStart — can only move startTime earlier. */
      if (ev.createdAt && typeof _seedStreamTimerStart === 'function') {
        _seedStreamTimerStart(convId, ev.createdAt);
      }
      /* ★ Model-fallback sidecar adoption: the backend stamps these AT THE
       *   DECISION MOMENT (llm_fallback/_call.py) and folds them into every
       *   state snapshot — a cold reload DURING the fallback generation must
       *   re-stamp them so the banner repaints instead of waiting for done. */
      if (ev.fallbackModel && assistantMsg) {
        assistantMsg.fallbackModel = ev.fallbackModel;
        assistantMsg.fallbackFrom = ev.fallbackFrom || '';
        assistantMsg.fallbackReason = ev.fallbackReason || '';
        assistantMsg.fallbackKind = ev.fallbackKind || '';
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
              // Verbatim text projection: the backend folds the lossless
              // task_events log into content/thinking on every cold path
              // (event_fold.fold_cold_state_text) AND persists each delta
              // BEFORE pushing it, so a state snapshot's text is never behind
              // the client buffer. toolRounds is NOT yet foldable (still 5s-
              // checkpoint-sourced), so it keeps the keep-longer guard.
              plannerMsg.content = ev.content || "";
              plannerMsg.thinking = ev.thinking || "";
              plannerMsg.toolRounds = _snapshotLongerRounds(plannerMsg.toolRounds, ev.toolRounds);
              assistantMsg = plannerMsg;
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
            plannerMsg.content = ev.content || "";  // verbatim (server fold authoritative for text)
            plannerMsg.thinking = ev.thinking || "";
            plannerMsg.toolRounds = _snapshotLongerRounds(plannerMsg.toolRounds, ev.toolRounds);
            assistantMsg = plannerMsg;
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
              workerMsg.content = ev.content || "";  // verbatim (server fold authoritative for text)
              workerMsg.thinking = ev.thinking || "";
              workerMsg.toolRounds = _snapshotLongerRounds(workerMsg.toolRounds, ev.toolRounds);
              assistantMsg = workerMsg;
            }
          }

          console.info(`[SSE state] Endpoint reconnect — phase=${ev.endpointPhase} ` +
            `iter=${ev.endpointIteration} epTurns=${(ev.endpointTurns || []).length} ` +
            `totalMsgs=${conv.messages.length}`);

          // Re-render if active
          if (activeConvId === convId) {
            window.ConvView.replaceAll(convId);
            /* ★ FIX: renderChat rendered ALL messages including the in-progress
             *   one.  We need to remove that last static element before creating
             *   the streaming-msg, otherwise the in-progress message shows twice:
             *   once as a "dead" static element and once as the live streaming bubble. */
            const lastMsgIdx = conv.messages.length - 1;
            const staleRenderedEl = document.getElementById(`msg-${lastMsgIdx}`);
            if (staleRenderedEl) staleRenderedEl.remove();

            // Re-create streaming-msg for the in-progress turn
            const _reconRole = _epCriticPhase ? 'critic'
              : (ev.endpointPhase === 'planning' ? 'planner' : 'worker');
            const _reconStatus = _epCriticPhase ? 'Reviewing…'
              : (ev.endpointPhase === 'planning' ? 'Planning…' : 'Thinking…');
            /* ★ Dedup at the boundary (ConvView.startStreaming → _evictByMsgId). */
            window.ConvView.startStreaming(convId, { role: _reconRole, status: _reconStatus, msgId: assistantMsg._msgId || null });
            buildTurnNav(conv);
          }
        }
        } /* close else (full reconnection) */
      } else if (_epCriticPhase && _epCriticMsg) {
        /* State snapshot during critic phase → update critic msg */
        _epCriticMsg.content = ev.content || "";  // verbatim (server fold authoritative for text)
        _epCriticMsg.thinking = ev.thinking || "";
      } else {
        /* ★ RENDER_CONTRACT Phase 3: route the COLD state snapshot through the
         *   ONE pure reducer (projectColdSnapshot). Only the reducer-OWNED
         *   projection {content,thinking,toolRounds} is applied; every other
         *   field on assistantMsg — crucially the translation projection
         *   (translatedContent / _showingTranslation / segments[].translatedText
         *   / originalContent) — is left untouched, so a cold reconnect never
         *   drops the translated bubble. The keep-longer merge still feeds the
         *   snapshot (a cold checkpoint may lag SHORTER than the live panel), so
         *   the reducer sees the widened round set — proven byte-identical to
         *   the live fold by tests/test_frontend_reducer_parity.py F3. */
        const _existing = assistantMsg._continueToolRounds || [];
        const _mergedRounds = ev.toolRounds
          ? _snapshotLongerRounds(assistantMsg.toolRounds, _existing.concat(ev.toolRounds || []))
          : assistantMsg.toolRounds;
        const _proj = projectColdSnapshot({
          content: ev.content, thinking: ev.thinking, toolRounds: _mergedRounds,
        });
        assistantMsg.content = _proj.content;
        assistantMsg.thinking = _proj.thinking;
        if (ev.error) assistantMsg.error = ev.error;
        if (ev.toolRounds) {
          assistantMsg.toolRounds = _proj.toolRounds;
        }
        if (ev.finishReason) assistantMsg.finishReason = ev.finishReason;
        if (ev.usage) assistantMsg.usage = ev.usage;
        if (ev.model) assistantMsg.model = ev.model;
        else if (ev.preset) assistantMsg.model = ev.preset;
        else if (ev.effort) assistantMsg.model = ev.effort;
        if (ev.thinkingDepth) assistantMsg.thinkingDepth = ev.thinkingDepth;
        if (ev.usage && typeof updateContextBar === 'function') updateContextBar();
      }
      // ★ Restore memory prefetch state from snapshot (WS/SSE connect mid-prefetch)
      if (ev.memoryPrefetch) {
        assistantMsg._memoryPrefetch = ev.memoryPrefetch;
        const _mpConv = conversations.find(c => c.id === convId);
        if (_mpConv) {
          const _mpPhase = ev.memoryPrefetch.phase;
          const _RUNNING = new Set(['started', 'bm25_done', 'rerank_started']);
          _mpConv._memoryPrefetching = _RUNNING.has(_mpPhase);
        }
      }
      // ★ Restore preferences-applied chip from snapshot (mid-stream reconnect)
      if (ev.preferencesApplied) {
        assistantMsg._preferencesApplied = ev.preferencesApplied;
      }
      if (ev.relatedConversations) {
        assistantMsg._relatedConversations = ev.relatedConversations;
      }
      if (ev.preferencesLearned) {
        assistantMsg._preferencesLearned = ev.preferencesLearned;
      }
      /* Inbox-inject sidecars (swarm/peer/user-steer) — restore from the
       * reconnect snapshot so the in-timeline inject chips repaint. Display
       * only; getToolRoundsFromMsg rebuilds the synthetic rows from these. */
      if (ev.inboxInjects) {
        assistantMsg._inboxInjects = ev.inboxInjects;
      }
      if (ev.peerInjects) {
        assistantMsg._peerInjects = ev.peerInjects;
      }
      if (ev.userSteerInjects) {
        assistantMsg._userSteerInjects = ev.userSteerInjects;
      }
      if (typeof twUpdate === 'function') twUpdate(convId);
      // ★ Re-trigger HG translations on state snapshot (handles page refresh / SSE reconnect)
      if (ev.toolRounds) _retriggerHgTranslations(convId);
    } else if (ev.type === "autopilot_vu_start"
            || ev.type === "autopilot_vu_event"
            || ev.type === "autopilot_vu_done"
            || ev.type === "autopilot_vu_cancel") {
      /* ★ Autopilot virtual-user STREAMING events ──────────────────────
       * The VU bubble is created EAGERLY by `autopilot_vu_start` the
       * moment the worker stops, so the user sees an "Autopilot ·
       * composing…" bubble in the USER lane (NOT a phase chip on the
       * worker bubble).  The bubble is in-memory ONLY until success —
       * `autopilot_vu_cancel` removes it with no DB trace, so a VU that
       * bails out (TASK_DONE / abort / real-user msg) leaves nothing
       * behind (preserves the old "no ghost empty VU" guarantee).
       *
       *   autopilot_vu_start  — create the VU bubble (empty, streaming).
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
    } else if (ev.type === "autopilot_run_concluded") {
      /* ★ The single BACKEND-AUTHORITATIVE "this autopilot run is over" fact —
       * a clean [VU: TASK_DONE] (with a report) OR a manual stop (no report).
       * Stores the human-only sidecar record on conv.autopilotSummaries[runId]
       * (NEVER conv.messages); the fold gate keys on its concluded status.
       * (The handler still tolerates the legacy `ev.summary`/`ev.summaryMessage`
       * FIELD shapes for a record delivered by an older backend.) */
      _handleAutopilotRunConcluded(convId, ev);
      return false;
    } else if (ev.type === "delta") {
      if (_epCriticPhase) {
        /* Accumulate into critic bubble instead of worker */
        if (_epCriticMsg) {
          if (ev.thinking) _epCriticMsg.thinking = (_epCriticMsg.thinking || "") + ev.thinking;
          if (ev.content)  _epCriticMsg.content  = (_epCriticMsg.content  || "") + ev.content;
        }
        if (typeof twUpdate === 'function') twUpdate(convId);
      } else {
        /* ★ RENDER_CONTRACT Phase 3: route the LIVE content/thinking append
         *   through the ONE pure reducer (reduceStreamState 'delta' action)
         *   instead of an inline `+=`, so the live fold uses the identical
         *   append discipline the cold/poll snapshot projection is proven
         *   byte-equal to (golden F1/F2/F3). The reducer mutates+returns the
         *   SAME assistantMsg (its {content,thinking,toolRounds} are the state),
         *   so buf mirror + _roundThinkingLen + phase management below are
         *   unchanged render-buffer concerns. */
        reduceStreamState(assistantMsg, { type: 'delta', content: ev.content, thinking: ev.thinking });
        if (ev.thinking) {
          _roundThinkingLen += ev.thinking.length;
        }
        /* ★ Phase management during deltas:
         *   - Content delta arrived → model is producing visible output, clear phase
         *   - Thinking-only delta → model is reasoning, show thinking indicator
         *     This works on ALL rounds (even when msg.content is already non-empty
         *     from previous tool rounds). §7: phase lives in the live session
         *     slice (streamSessions), never a render buffer. */
        if (ev.content) {
          setStreamPhase(convId, null);
        } else if (ev.thinking && !ev.content) {
          setStreamPhase(convId, { phase: "thinking_active", _thinkingLen: _roundThinkingLen });
        }
        if (typeof twUpdate === 'function') twUpdate(convId);
      }
    } else if (ev.type === "retry_reset") {
      /* Turn-level auto-retry: the backend is re-running the whole turn after
       * a transient error. Deltas append client-side (see the "delta" branch
       * below: assistantMsg.content = (assistantMsg.content||"") + ev.content),
       * and the JS model value is NEVER reset mid-stream except here — so
       * without this the about-to-be-re-streamed output would STACK on top of
       * the failed attempt's partial bubble. Clear the accumulated
       * content/thinking/toolRounds (and error) so the re-run renders clean.
       * Non-terminal: the task stays running; a phase:retrying frame follows. */
      _roundThinkingLen = 0;
      const _rrTarget = (_epCriticPhase && _epCriticMsg) ? _epCriticMsg : assistantMsg;
      if (_rrTarget) {
        _rrTarget.content = "";
        _rrTarget.thinking = "";
        delete _rrTarget.error;
        /* Keep any pre-turn checkpoint rounds (Continue), drop this attempt's. */
        _rrTarget.toolRounds = _rrTarget._continueToolRounds
          ? _rrTarget._continueToolRounds.slice() : [];
      }
      setStreamPhase(convId, null);
      if (typeof twUpdate === 'function') twUpdate(convId);
    } else if (ev.type === "delta_reset") {
      /* The just-ended LLM round issued TOOL CALLS, so the prose it streamed
       * before those calls was inter-round narration ("Now let me check the
       * utility functions."), NOT the final answer. Those content deltas were
       * already appended to the live bubble (see the "delta" branch:
       * assistantMsg.content += ev.content). Clear the accumulated
       * content/thinking so the narration isn't concatenated in front of the
       * terminal round's real answer. UNLIKE retry_reset, KEEP toolRounds —
       * the tool calls from this turn are legitimate and keep rendering.
       * With ev.discard === true (the canned-greeting retry — a discarded
       * round that issued NO tool calls) the reducer clears unconditionally:
       * there is no batch to stamp the prose onto. */
      _roundThinkingLen = 0;
      const _drTarget = (_epCriticPhase && _epCriticMsg) ? _epCriticMsg : assistantMsg;
      if (_drTarget) {
        /* ★ RENDER_CONTRACT Phase 3: the prose-capture + freeze-guarded clear is
         * now owned by the ONE pure reducer's delta_reset case
         * (_stampDeltaReset) — it stamps this round's pre-tool narration onto
         * the FIRST tool round of the llmRound batch (exactly where the backend
         * assemble_segments puts it) with the same append/non-containment guard,
         * and clears content/thinking ONLY once the prose is GUARANTEED
         * preserved on a round (the "frozen at a half word" freeze guard: if the
         * batch's tool_start hasn't applied yet, the reducer KEEPS the prose and
         * a later frame carries it). _drTarget's {content,thinking,toolRounds}
         * ARE the reducer state, so it mutates in place. This is the last LIVE
         * projection write folded onto the reducer, closing the LIVE side of the
         * four-path convergence. */
        const _hadProse = !!(_drTarget.content || _drTarget.thinking);
        reduceStreamState(_drTarget, ev);
        /* _stamped = the reducer actually captured+cleared this round's prose.
         * Derived from the accumulators being cleared, so the
         * atomic clear-and-repaint below (DOM concerns the pure reducer must NOT
         * own) fires exactly when a stamp happened. When there was no prose to
         * begin with, there is nothing to wipe — the atomic repaint's whole job
         * is to wipe the content zone AS the prose moves into the panel — so
         * skipping it is correct; the trailing twUpdate still refreshes. */
        const _stamped = _hadProse && !_drTarget.content && !_drTarget.thinking;
        if (_stamped) {
          /* §7: no buffer mirror — the reducer already cleared the document
           * (_drTarget), and the reactive render reads the document's
           * prose-stamped toolRounds directly. */
          /* ★ ATOMIC CLEAR + REPAINT (root cause of the mid-stream content
           * freeze). twUpdate is COALESCED — the empty-content frame this reset
           * produces is routinely dropped when a long-running tool (or the next
           * content delta) beats the rAF, so the pre-tool prose that was just
           * moved into the tool panel stays FROZEN in the content zone for the
           * rest of the turn (the "Compile gate green. T…" the model would
           * never emit as a final answer). The narration is now safely stamped
           * on its tool round, so force a SYNCHRONOUS render in the SAME step:
           * it wipes the content zone as the prose moves into the panel
           * (atomic), so the deliverable text can never linger mid-stream.
           * Guarded to the visible conv (updateStreamingUI drives the singleton
           * #streaming-body); background convs still coalesce via twUpdate. */
          if (typeof activeConvId !== 'undefined' && activeConvId === convId
              && typeof updateStreamingUI === 'function') {
            const _drSess = (typeof streamSessions !== 'undefined')
              ? streamSessions.get(convId) : null;
            updateStreamingUI({
              content: '', thinking: '',
              toolRounds: (_drTarget && _drTarget.toolRounds) || [],
              phase: (_drSess && _drSess.phase) || null,
              _memoryPrefetch: (_drTarget && _drTarget._memoryPrefetch),
            });
          }
        }
      }
      if (typeof twUpdate === 'function') twUpdate(convId);
    } else if (ev.type === "round_start" || ev.type === "round_end") {
      /* ★ RENDER_CONTRACT Phase 3: explicit round boundaries. Route through the
       *   ONE pure reducer so the open-round index is tracked off a REAL
       *   boundary (round_start.roundNum) instead of inferred from the first
       *   tool_start — a prose-only round now has a signal too. The reducer
       *   records/clears state._currentRound (dropped by the finalizer, never
       *   part of the committed shape); the critic bubble uses the same target.
       *   Display-only bookkeeping — no content mutation — so a light twUpdate
       *   suffices. Backend `done`/`committedMessage` stays authoritative. */
      const _rbTarget = (_epCriticPhase && _epCriticMsg) ? _epCriticMsg : assistantMsg;
      if (_rbTarget) reduceStreamState(_rbTarget, ev);
      if (typeof twUpdate === 'function') twUpdate(convId);
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
      /* §7: phase lives in the live session slice — one home for worker and
       * critic phases alike (critic phase used to swap the whole buf object;
       * the session slice just carries the current phase). Warm reconnects
       * re-seed it via this same handler from the replayed event log. */
      setStreamPhase(convId, {
        phase: ev.phase,
        detail: ev.detail || "",
        /* ★ i18n plumb: renderers prefer detailKey→t() over `detail`, which
         * remains the English fallback for headless / non-i18n clients. */
        detailKey: ev.detailKey || "",
        detailArgs: ev.detailArgs || null,
        /* ★ LOAD-BEARING, not telemetry (pt_1e7f6539b41c4d8f). The retry
         * banner's repaint key is `"retry:" + (phase.attempt || 0)`
         * (streaming_ui.js), and the status zone only swaps innerHTML when
         * `data-phase-key` CHANGES. Drop this field and every retry beat
         * collapses to `retry:0`, so the banner FREEZES on the first
         * attempt's text — "attempt 2 / 3 / 4…" can never appear even though
         * the backend emits a fresh PHASE event for each one (measured:
         * 3 beats → 3 distinct keys with the field, 1 key without).
         * The backend sends it TOP-LEVEL on every retry / first-byte
         * heartbeat beat (lib/tasks_pkg/manager/_stream.py::_on_waiting,
         * lib/tasks_pkg/stream_handler/_analyse.py). `detailArgs.attempt`
         * carries the same number for the i18n TEXT, but the repaint key
         * reads the top-level field — the two are not interchangeable. */
        attempt: ev.attempt || 0,
        tools: ev.tools || [],
        toolContext: ev.toolContext || "",
        /* Structured raw tool names behind toolContext — the renderer
         * composes the localized suffix from THESE when present (the
         * English toolContext string stays the headless fallback). */
        toolContextTools: ev.toolContextTools || null,
        round: ev.roundNum || 0,
      });
      if (typeof twUpdate === 'function') twUpdate(convId);
    } else if (ev.type === "model_fallback") {
      _handleModelFallback(ev, _hctx());
    } else if (ev.type === "tool_start") {
      _handleToolStart(ev, _hctx());
    } else if (ev.type === "human_guidance_request") {
      _handleHumanGuidance(ev, _hctx());
    } else if (ev.type === "tool_progress") {
      _handleToolProgress(ev, _hctx());
    } else if (ev.type === "stdin_request") {
      _handleStdinRequest(ev, _hctx());
    } else if (ev.type === "stdin_resolved") {
      _handleStdinResolved(ev, _hctx());
    } else if (ev.type === "write_approval_request") {
      _handleWriteApproval(ev, _hctx());
    } else if (ev.type === "tool_result") {
      _handleToolResult(ev, _hctx());
    } else if (ev.type === "tool_complete") {
      _handleToolComplete(ev, _hctx());
    } else if (ev.type === "tool_compacted") {
      _handleToolCompacted(ev, _hctx());
    } else if (ev.type === "round_usage") {
      _handleRoundUsage(ev, _hctx());
    } else if (ev.type === "artifact") {
      _handleArtifact(ev, _hctx());
    } else if (ev.type === "compaction" || ev.type === "compaction_done") {
      _handleCompaction(ev, _hctx());
    } else if (ev.type === "memory_prefetch") {
      _handleMemoryPrefetch(ev, _hctx());
    } else if (ev.type === "preferences_applied") {
      _handlePreferencesApplied(ev, _hctx());
    } else if (ev.type === "related_conversations") {
      _handleRelatedConversations(ev, _hctx());
    } else if (ev.type === "preference_learned") {
      _handlePreferenceLearned(ev, _hctx());
    } else if (ev.type === "project_external_edit") {
      _handleProjectExternalEdit(ev, _hctx());
    } else if (ev.type === "workspace_root_added") {
      _handleWorkspaceRootAdded(ev, _hctx());
    } else if (ev.type === "timer_poll_check") {
      _handleTimerPollCheck(ev, _hctx());
    } else if (ev.type === "swarm_phase") {
      _handleSwarmPhase(ev, _hctx());
    } else if (ev.type === "swarm_agent_phase" || ev.type === "swarm_agent_progress" ||
               ev.type === "swarm_agent_complete" || ev.type === "swarm_agent_error" ||
               ev.type === "swarm_agent_tool_call") {
      _handleSwarmAgent(ev, _hctx());
    } else if (ev.type === "swarm_inbox_inject") {
      _handleSwarmInboxInject(ev, _hctx());
    } else if (ev.type === "peer_inbox_inject") {
      _handlePeerInboxInject(ev, _hctx());
    } else if (ev.type === "user_steer_inject") {
      _handleUserSteerInject(ev, _hctx());
    } else if (ev.type === "messages_snapshot") {
      _handleMessagesSnapshot(ev, _hctx());
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
            /* ★ Dedup at the boundary (ConvView.startStreaming → _evictByMsgId). */
            window.ConvView.startStreaming(convId, { role: 'critic', msgId: _epCriticMsg._msgId || null });
          }

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
            /* ★ Dedup: remove any stale DB-loaded worker for this iteration.
             *   ONE row only — a one-arg splice would drop everything AFTER it
             *   too, including a persisted user follow-up queued behind the
             *   stale worker (reload + reconnect with a queued send). The
             *   to-end truncation contract belongs to endpoint_new_turn below
             *   (which re-streams the whole iteration); here only the stale
             *   worker twin is stale. */
            const staleIdx = conv.messages.findIndex(m =>
              m.role === "assistant" && m._epIteration === ev.iteration);
            if (staleIdx >= 0) {
              conv.messages.splice(staleIdx, 1);
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

            // Create streaming element — only if this conv is active
            if (_isActiveConv) {
              const inner = document.getElementById("chatInner");
              /* ★ Dedup at the boundary (ConvView.startStreaming → _evictByMsgId). */
              window.ConvView.startStreaming(convId, { role: 'worker', status: 'Thinking…', msgId: assistantMsg._msgId || null });
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
          /* ★ Carry the critic's reasoning onto the message so its thinking
           *   block survives finalize + DB sync + reload (the flow path now
           *   sends ev.thinking; the live endpoint path may omit it, hence
           *   the fallback to the value accumulated from live deltas). */
          if (ev.thinking !== undefined && ev.thinking !== null)
            _epCriticMsg.thinking = ev.thinking || _epCriticMsg.thinking || "";
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

          if (activeConvId === convId) {
            const inner = document.getElementById("chatInner");
            /* ★ Dedup at the boundary (ConvView.startStreaming → _evictByMsgId). */
            window.ConvView.startStreaming(convId, { role: 'planner', status: 'Replanning…', msgId: assistantMsg._msgId || null });
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

        // DOM operations — only if this conv is currently viewed
        if (activeConvId === convId) {
          const inner = document.getElementById("chatInner");
          /* ★ Dedup at the boundary (ConvView.startStreaming → _evictByMsgId). */
          window.ConvView.startStreaming(convId, { role: 'worker', status: 'Thinking…', msgId: assistantMsg._msgId || null });

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
      _handleSseTimeout(ev, _hctx());
      return false;
    } else if (ev.type === "round_committed") {
      _handleRoundCommitted(ev, _hctx());
      return false;
    } else if (ev.type === "done") {
      /* ★ #4: task terminated — drop the persisted reload cursor so a later
       *   reconnect for this (finished) task doesn't send a stale Last-Event-ID
       *   that forces the server's cold-replay path unnecessarily. */
      _clearSseCursor(taskId);
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
      if (ev.fallbackReason) assistantMsg.fallbackReason = ev.fallbackReason;
      if (ev.fallbackKind) assistantMsg.fallbackKind = ev.fallbackKind;
      /* Tool-schema latch held a pending tool toggle to keep the prompt
         cache intact — surface the "apply on next conversation" banner. */
      if (typeof onToolsetDiverged === 'function') onToolsetDiverged(!!ev.toolsetDiverged, convId, ev.toolsetDiff);
      /* The turn-ctx capsule on the triggering user turn was captured from
         the LIVE toolbar at send time. Two independent drifts settle here:
           1. The tool-schema latch held ON/OFF toggles back to keep prompt
              cache — corrected via `toolsetDiff = {added, removed}`.
           2. The user may have switched preset / toggled a mode in the
              pause between send and stream-start — corrected via the
              server-authoritative fact card
              `actualModel / actualDepth / actualModes` on the done frame.
         Both are handled by info-rail.js::reconcileTurnCtxCapsule. */
      if (typeof reconcileTurnCtxCapsule === 'function') {
        const _hasDiff = ev.toolsetDiverged && ev.toolsetDiff
          && ((ev.toolsetDiff.added && ev.toolsetDiff.added.length)
              || (ev.toolsetDiff.removed && ev.toolsetDiff.removed.length));
        const _hasFact = (typeof ev.actualModel === 'string' && ev.actualModel)
          || Object.prototype.hasOwnProperty.call(ev, 'actualDepth')
          || Array.isArray(ev.actualModes);
        if (_hasDiff || _hasFact) {
          try {
            const _rc = conversations.find((c) => c.id === convId);
            if (_rc && Array.isArray(_rc.messages)) {
              /* Anchor the reconcile to the user message THIS task ran on
                 (backend ships ev.userMsgId, stamped by _start_task_for_conv
                 → done_evt in _finalize_and_emit_done, inherited onto VU
                 sub-tasks from their parent). Falling back to "last user in
                 conv" is wrong under three concrete scenarios (autopilot VU
                 appended a newer user row; a concurrent send in the same
                 conv; a regenerate against a historical user turn) — it
                 either overwrites the wrong bubble or (worse) targets a msg
                 that has no _ctx and the correction never lands. Use last-user
                 ONLY when a legacy backend didn't ship userMsgId. */
              let _uIdx = -1;
              const _targetMsgId = (typeof ev.userMsgId === 'string' && ev.userMsgId)
                ? ev.userMsgId : '';
              if (_targetMsgId) {
                for (let i = _rc.messages.length - 1; i >= 0; i--) {
                  const _m = _rc.messages[i];
                  if (_m && _m.role === 'user' && _m._msgId === _targetMsgId) {
                    _uIdx = i;
                    break;
                  }
                }
                if (_uIdx < 0) {
                  console.debug('[turnCtx] userMsgId=%s not found in conv=%s — ' +
                    'the target user turn may have been truncated / not synced yet; ' +
                    'skipping reconcile.', _targetMsgId, convId.slice(0, 8));
                }
              } else {
                for (let i = _rc.messages.length - 1; i >= 0; i--) {
                  if (_rc.messages[i] && _rc.messages[i].role === 'user') { _uIdx = i; break; }
                }
                console.debug('[turnCtx] done frame carries no userMsgId — ' +
                  'legacy backend fallback to last-user in conv=%s (idx=%d)',
                  convId.slice(0, 8), _uIdx);
              }
              const _uMsg = _uIdx >= 0 ? _rc.messages[_uIdx] : null;
              if (_uMsg && _uMsg._ctx) {
                const _fact = {
                  toolsetDiff: _hasDiff ? ev.toolsetDiff : undefined,
                  actualModel: ev.actualModel,
                  actualDepth: ev.actualDepth,
                  actualModes: ev.actualModes,
                };
                if (reconcileTurnCtxCapsule(_uMsg._ctx, _fact)) {
                  if (typeof saveConversations === 'function') saveConversations(convId);
                  if (activeConvId === convId) {
                    window.ConvView.upsertMessage(convId, _uMsg, { idx: _uIdx });
                  }
                }
              }
            }
          } catch (e) {
            console.debug('[turnCtx] reconcile against done fact failed:', e);
          }
        }
      }
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
      /* ★ latestLiveTaskId wire (pt_8dc03017 completion): the backend ships
       *   the supersede successor (autopilot VU / follow-up / queued-dispatch
       *   task) on terminal frames — record it so _runTerminalContinuation's
       *   attach reducer can hop to it. Without this stamp the reducer is
       *   dead code and a queued turn starts invisibly (the 2026-07-25
       *   silent-queue incident). LATE done rides this same branch. */
      if (typeof _stampLatestLiveTask === 'function') _stampLatestLiveTask(_dConv, ev);
      /* ★ git-shim: round commit sha for redo/diff references */
      if (ev.gitSha) assistantMsg._gitSha = ev.gitSha;
      /* ★ Phase 1 (parity-gap closure): when the backend shipped the EXACT
       *   settled dict it committed to conversations.messages, PROJECT IT
       *   VERBATIM. This is the single source of truth for the settled bubble
       *   — no keep-longer/snapshot reconstruction of the DB record.
       *   `committedMessage` is ABSENT on skip paths (freshness/inline/
       *   CAS-exhaustion) and when the server died before committing; in that
       *   case we fall through to the transient-buffer projection below
       *   (the offline fallback) and the per-field assignments already applied.
       *   committedMessage is the EXACT dict the backend committed to the DB
       *   (stamped in manager.py _sync_result_to_conversation ONLY on CAS
       *   success), so when present it is AUTHORITATIVE and COMPLETE — project
       *   content/thinking/toolRounds VERBATIM (the separation-of-concerns
       *   directive: settled = verbatim projection of the backend record, NOT a
       *   local keep-longer reconstruction). Keep-longer stays only for the
       *   committedMessage-ABSENT path (skip/crash), handled by the per-field
       *   assignments above + the transient-buffer fallback below. Note-only
       *   frontend-local fields (_translate*, _msgId, branches) are preserved
       *   by copying settled fields onto the existing object, not replacing it. */
      const _cm = ev.committedMessage;
      if (_cm && typeof _cm === 'object') {
        assistantMsg.content = (_cm.content != null) ? _cm.content : (assistantMsg.content || '');
        assistantMsg.thinking = (_cm.thinking != null) ? _cm.thinking : (assistantMsg.thinking || '');
        if (Array.isArray(_cm.toolRounds)) assistantMsg.toolRounds = _cm.toolRounds;
        /* ★ Project the ordered typed-segment list so the settled interleaved
         *   timeline (renderSegmentTimelineHTML, which reads msg.segments)
         *   renders live at finalize — not only after a reload re-reads the DB.
         *   Without this the live-finalized turn falls back to the grouped tool
         *   panel and the auto-translate `segmentsByRound` frame has no segments
         *   to stamp translatedText onto. */
        if (Array.isArray(_cm.segments)) {
          assistantMsg.segments = _cm.segments;
          /* If the translate `done` push won the race and landed before these
           * segments existed, it stashed its {round:中文} map — apply it now so
           * the settled timeline shows Chinese narration inline without a
           * reload, regardless of which `done` arrived first. */
          if (assistantMsg._pendingSegTranslations
              && typeof _stampSegTranslations === 'function') {
            _stampSegTranslations(assistantMsg, assistantMsg._pendingSegTranslations);
          }
        }
        for (const _k of ['finishReason', 'usage', 'preset', 'toolSummary',
                          'model', 'provider_id', 'apiRounds', 'modifiedFiles',
                          'modifiedFileList', 'cost', '_taskId', '_msgId',
                          'fallbackModel', 'fallbackFrom', 'fallbackReason',
                          'fallbackKind', 'error', 'thinkingDepth', '_gitSha',
                          '_memoryPrefetch', '_preferencesApplied',
                          '_relatedConversations', '_preferencesLearned',
                          '_inboxInjects', '_peerInjects', '_userSteerInjects']) {
          /* ★ _msgId: adopt the server's committed id so the live bubble and
           *   the DB row share ONE identity. With the backend now honouring the
           *   client _assistantMsgId (manager._new_assistant_slot) they already
           *   match; this keeps them aligned even for legacy/headless turns
           *   where the server minted its own UUID — a reconnect / rescue-PUT
           *   dedup then recognises the bubble and never appends a duplicate. */
          if (_cm[_k] != null) assistantMsg[_k] = _cm[_k];
        }
        assistantMsg._committedProjection = true;
      }
      /* ★ Autopilot follow-up arrives in-band on the done event so the
       *   frontend can attach to the next task immediately, without
       *   polling /api/chat/active after the SSE stream has closed.
       *   finishStream() reads these to dispatch the follow-up. */
      if (ev.autopilotNextTaskId && ev.autopilotVuMessage) {
        const _apPayload = {
          nextTaskId: ev.autopilotNextTaskId,
          vuMessage: ev.autopilotVuMessage,
        };
        /* Kick-from-idle carrier: assistantMsg is a DETACHED dummy that was
         * never pushed into conv.messages, so stamping the baton on it would
         * make _findAutopilotPendingCarrier miss it.  Stamp on the finalized
         * VU user message at the tail instead (vu_done ran before this). */
        const _apConv = conversations.find(c => c.id === convId);
        const _apDetached = _apConv && _apConv.messages.indexOf(assistantMsg) === -1;
        let _apTarget = assistantMsg;
        if (_apDetached && _apConv && _apConv.messages.length) {
          _apTarget = _apConv.messages[_apConv.messages.length - 1];
        }
        _apTarget._autopilotPending = _apPayload;
        /* ★ AUTHORITATIVE baton on the conv object — survives any message
         *   splice (vu_cancel / edit) that could strip the positional
         *   stamp above.  _findAutopilotPendingCarrier reads this first. */
        if (_apConv) _apConv._apPendingBaton = _apPayload;
        console.info(
          `[connectToTask] 🤖 Autopilot follow-up attached to done — ` +
          `next task=${ev.autopilotNextTaskId.slice(0,8)} detachedCarrier=${!!_apDetached} ` +
          `vu="${(ev.autopilotVuMessage.content||'').slice(0,80)}${(ev.autopilotVuMessage.content||'').length>80?'…':''}"`
        );
      }
      /* ★ Autopilot: when a VU bubble took over the shared streaming
       *   substrate, THIS worker bubble was finalized to a static
       *   element EARLY (at autopilot_vu_start) — before this done event
       *   delivered the worker's usage / model / finishReason / cost.
       *   Re-render the now-static worker bubble so its finish bar shows
       *   tokens + cost instead of just the model tag.  The flag is set
       *   in _beginVuStreaming, so this covers BOTH the follow-up path
       *   AND the cancel path (VU bailed → bubble already removed).
       *   (Mirrors the streaming-msg-destroyed-by-renderChat fix.) */
      if (activeConvId === convId && assistantMsg._vuTookOverBubble) {
        delete assistantMsg._vuTookOverBubble;
        window.ConvView.upsertMessage(convId, assistantMsg);
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
      /* ★ Empty-bubble root fix ②: a GENUINELY-EMPTY settled turn (the model
       *   returned nothing — the "empty_stop" anomaly) must NOT survive as a
       *   finishReason-stamped shell. The backend already dropped it at the
       *   SOURCE: _sync_result_to_conversation early-returns on
       *   (no content/thinking/error) and _reconcile_orphan_placeholder_on_settle
       *   DELETES the placeholder from the DB — so `committedMessage` is ABSENT
       *   on the done event (the exact signal the backend persisted NOTHING).
       *   But the done event still ships finishReason:'stop' (metadata), which
       *   we stamped on assistantMsg above → it escapes _isOrphanEmptyAssistant
       *   (that belt keeps any finishReason-bearing msg) and renders as a blank
       *   "Agent" bubble until a reload re-reads the clean DB. Splice it here so
       *   the in-memory copy matches the DB (deleted) — mirrors the
       *   endpoint_complete `!assistantMsg.content → splice`. Guards:
       *     • only when the backend did NOT persist it (_committedProjection
       *       unset) — a real reply always carries committedMessage;
       *     • only a bare empty husk (no content/thinking/error + no REAL tool
       *       round) — a tool-only or thinking-only turn is legitimate output;
       *     • never a special turn (endpoint/VU) — those render even when empty;
       *     • never when an autopilot follow-up rode this event (the assistant
       *       may be a detached kick carrier the follow-up path owns). */
      if (!assistantMsg._committedProjection
          && !ev.autopilotNextTaskId
          && !assistantMsg._isEndpointReview && !assistantMsg._isEndpointPlanner
          && !assistantMsg._isVirtualUser
          && !(assistantMsg.content && String(assistantMsg.content).trim())
          && !(assistantMsg.thinking && String(assistantMsg.thinking).trim())
          && !assistantMsg.error
          && !(typeof _hasRealToolRound === 'function'
                ? _hasRealToolRound(assistantMsg)
                : (Array.isArray(assistantMsg.toolRounds) && assistantMsg.toolRounds.length))) {
        const _ec = conversations.find(c => c.id === convId);
        const _eIdx = _ec ? _ec.messages.indexOf(assistantMsg) : -1;
        if (_eIdx >= 0) {
          _ec.messages.splice(_eIdx, 1);
          if (activeConvId === convId) {
            window.ConvView.removeMessage(convId, assistantMsg._msgId || _eIdx);
          }
          /* Drop the live streaming bubble too — finishStream would otherwise
           * finalize a now-detached message into a static empty node. */
          if (activeConvId === convId) {
            const _sm = document.getElementById('streaming-msg');
            if (_sm) { try { _sm.remove(); } catch (e) { /* detached */ } }
          }
          console.info(
            `[connectToTask] 🧹 Empty-turn splice — task=${taskId.slice(0,8)} ` +
            `conv=${convId.slice(0,8)} produced no content/thinking/tools ` +
            `(finishReason=${ev.finishReason || 'none'}, committed=false) — ` +
            `removed placeholder so it matches the backend-deleted DB row.`
          );
        }
      }
      return true;
    }
    return false;
  } finally {
    ctx.assistantMsg = assistantMsg;
    ctx.epCriticPhase = _epCriticPhase;
    ctx.epCriticMsg = _epCriticMsg;
    ctx.roundThinkingLen = _roundThinkingLen;
    ctx.lastEventId = _lastEventId;
    ctx.pendingEventId = _pendingEventId;
  }
}

async function _trySSE(convId, taskId, stream, assistantMsg) {
  let lastSave = Date.now(),
    gotData = false;
  let sseTimeout = setTimeout(() => {
    if (!gotData) stream.controller.abort();
  }, 30000);
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
  let _lastEventId = null; // mirrors ctx.lastEventId for the reader loop below
  /* ── Mutable SSE dispatch context ──
   * The 7 values that the dispatcher reassigns (worker/critic targets,
   * buffers, phase flags, thinking counter, last-event-id) live here so
   * dispatchSSEEvent — now a module-level, unit-testable function — can
   * mutate them across events. _trySSE reads them back after each line. */
  const ctx = {
    convId, taskId, stream,
    assistantMsg,
    epCriticPhase: false,
    epCriticMsg: null,
    roundThinkingLen: 0,
    lastEventId: null,
    pendingEventId: null,
    pinnedMsgId: _pinnedMsgId,
  };
  /* Thin local alias preserving the original call shape. After dispatch we
   * sync the two locals the reader loop below still references directly. */
  function _processSSELine(line) {
    const _done = dispatchSSEEvent(line, ctx);
    assistantMsg = ctx.assistantMsg;
    _lastEventId = ctx.lastEventId;
    return _done;
  }
  try {
    // ★ Last-Event-ID resume: if we have a previous event id (from a
    //   prior connection attempt for this task), send it so the server
    //   resumes from that cursor instead of replaying the full state
    //   snapshot.
    // ★ #4: a page RELOAD wipes the in-memory stream._lastEventId. Seed it
    //   from the persisted per-task cursor (sessionStorage) so the reload
    //   reconnect offset-resumes via Last-Event-ID instead of replaying a full
    //   snapshot. Only seed when not already set this session (a live
    //   in-memory reconnect already has the freshest cursor).
    if (!stream._lastEventId) {
      const _persistedCursor = _loadSseCursor(taskId);
      if (_persistedCursor) {
        stream._lastEventId = _persistedCursor;
        console.info(`[_trySSE] Seeded Last-Event-ID=${_persistedCursor} from sessionStorage (reload resume) for task=${taskId.slice(0,8)}`);
      }
    }
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
    const streamDone = await readSSEStream(resp, {
      onChunk() {
        gotData = true;
        clearTimeout(sseTimeout);
        if (typeof _streamTimerTouch === 'function') _streamTimerTouch(convId); // ★ Any bytes (including keepalives) prove server is alive
      },
      onLine(line) {
        return _processSSELine(line);
      },
      afterChunk() {
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
      },
    });
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
    if (typeof twStop === 'function') twStop(convId);
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

/* _pollFallback(...)  → moved to static/js/ui/sse_poll_fallback.js (split 2026-06)
   updateSendButton()  → moved to static/js/ui/send_button.js (split 2026-06)
   Both are window-scope (no _trySSE closure capture); registered in
   _BUNDLE_FILES right after this file. */

/* ── Test seam ──
 * Exposes the dispatcher + a ctx factory so tests/test_frontend_sse_dispatch.py
 * can drive single SSE lines against a fresh ctx under jsdom. Pure additive —
 * production code never reads window.__sse_test__. */
if (typeof window !== 'undefined') {
  window.__sse_test__ = {
    dispatchSSEEvent,
    makeCtx(o) {
      o = o || {};
      return {
        convId: o.convId, taskId: o.taskId, stream: o.stream,
        assistantMsg: o.assistantMsg || null,
        epCriticPhase: false, epCriticMsg: null,
        roundThinkingLen: 0, lastEventId: null, pendingEventId: null,
        pinnedMsgId: (o.assistantMsg && o.assistantMsg._msgId) || null,
      };
    },
  };
}
