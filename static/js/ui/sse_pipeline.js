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


/* ── State-snapshot content-regression guard ─────────────────────────────
 * A reconnect `state` snapshot replays the server's LAST checkpoint of the
 * turn. On a flaky network the reconnect can land BEFORE the server has
 * written a fresh 5s checkpoint (cold-replay race, wrong replica), so the
 * snapshot's `content`/`thinking` can be EMPTY or SHORTER than what the
 * client already accumulated from live deltas. Assigning it raw
 * (`msg.content = ev.content || ""`) then BLANKS an in-progress message —
 * the "sent and generating, later found completely GONE" symptom.
 *
 * The invariant (same as _pollFallback's merge and the 2026-05-31
 * content-regression detector): a snapshot may only GROW a field, never
 * shrink it. Returns the longer of the incoming snapshot vs. the value the
 * message already holds. `field` is 'content' or 'thinking'.
 */
function _snapshotLonger(msg, ev, field) {
  const incoming = (ev && ev[field]) || "";
  const current = (msg && msg[field]) || "";
  return incoming.length >= current.length ? incoming : current;
}


/**
 * toolRounds sibling of _snapshotLonger — a reconnect `state` snapshot (or a
 * racey `done` committedMessage) may replay a COLD/empty checkpoint whose
 * toolRounds array is shorter than what the client already accumulated from
 * live deltas. Assigning it verbatim then COLLAPSES the on-screen tool-round
 * panel (searches / file edits) mid-stream — the same content-regression class
 * _snapshotLonger fixes for text, extended to rounds.
 *
 * Invariant: a snapshot may only GROW the rounds, never shrink them. Returns
 * the incoming array only when it is at least as long as the current one.
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
  twStart(convId);

  const stream = activeStreams.get(convId);
  let sseWorked = false;
  try {
    sseWorked = await _trySSE(convId, taskId, stream, dummyAssistant);
  } catch (e) {
    if (e.name === 'AbortError') {
      twStop(convId);
      finishStream(convId);
      return;
    }
    debugLog(`Autopilot kick SSE failed: ${e.message}`, 'warn');
  }
  if (!sseWorked && !stream.controller.signal.aborted) {
    debugLog(`Autopilot kick falling back to polling for ${taskId.slice(0, 8)}`, 'warn');
    await _pollFallback(convId, taskId, stream, dummyAssistant);
  }
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
   *   bubble itself is created by the `autopilot_vu_start` event. */
  if (opts && opts.autopilotKick) {
    return _connectAutopilotKick(convId, taskId);
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
    const _staleTaskId = assistantMsg._taskId && assistantMsg._taskId !== taskId;
    /* ★ A completed turn (finishReason set) is only OUR slot to reuse when it
     *   is bound to THIS taskId.  Any other completed tail is stale → push
     *   fresh.  Two cases collapse into `_taskId !== taskId`:
     *     • carries a DIFFERENT _taskId → a prior turn, stale (push fresh).
     *     • carries NO _taskId → AMBIGUOUS, and _taskId is NOT persisted to the
     *       DB, so a page-reload DB-loaded completed tail always lacks it.  On
     *       reload it is a FOREIGN previous turn (the running task's own reply
     *       is not yet persisted; Case A already pushes a placeholder for it),
     *       so it MUST push fresh — reusing it would replay the old turn into
     *       the new bubble (the original stale-tail bug).
     *   A fresh placeholder we're about to stream into has NO finishReason, so
     *   it never reaches here — the `finishReason` gate already protects it, and
     *   the reconnect-to-just-finished slot is stamped `_taskId===taskId` at
     *   bind time so it correctly reuses (no duplicate bubble). */
    const _isCompletedTurn = !!assistantMsg.finishReason
      && assistantMsg._taskId !== taskId;
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
        if (window.ConvView && typeof window.ConvView.startStreaming === 'function') {
          window.ConvView.startStreaming(convId, {
            role: _reconRole, status: _reconStatus,
            timeStr: _reconTime, msgId: assistantMsg._msgId || null,
          });
        } else {
          inner.insertAdjacentHTML('beforeend', _streamingBubbleHTML(_reconRole, _reconStatus, _reconTime, assistantMsg._msgId || null));
        }
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
    twStart(convId);
    const buf = streamBufs.get(convId);
    /* ★ FIX (stuck "等待中…" on reconnect): seed the fresh stream buffer with
     *   whatever content/thinking the server already checkpointed onto this
     *   assistant turn.  twStart() creates an EMPTY buffer; without this the
     *   buffer stays empty until the first NEW SSE delta arrives, so any
     *   buffer-driven render (the 300ms deferred re-render in
     *   showStreamingUIForConv, or a twUpdate triggered by an unrelated field)
     *   paints updateStreamingUI({content:''}) → the "wait" branch → the bubble
     *   snaps back to "等待中…" even though the task is mid-generation and the
     *   English is already persisted.  Mirror assistantMsg → buf exactly like
     *   the SSE `state`/`delta` handlers do. */
    if (assistantMsg.content) buf.content = assistantMsg.content;
    if (assistantMsg.thinking) buf.thinking = assistantMsg.thinking;
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
  /* ★ Defensive re-entry seed (strictly additive, decouples the twUpdate path):
   *   the twStart+seed block above only runs on the FRESH-connection branch
   *   (`!activeStreams.has(convId)`).  A re-entry that finds an existing stream
   *   (e.g. an overlapping reconnect race, or a caller that re-invokes
   *   connectToTask for a conv that already has a live entry) SKIPS twStart and
   *   its seed — leaving whatever buffer existed.  `_twFlush` (health_stream_timer)
   *   reads `buf.content` RAW with no message fallback, so an empty buffer there
   *   would still paint the "等待中…" wait branch over already-checkpointed
   *   content.  Fill any GAP from the persisted assistant turn — guarded on the
   *   field being empty so this can NEVER clobber deltas the live `_trySSE`
   *   closure has been accumulating (that buffer is authoritative once it has
   *   data). No-op on the fresh path (the block above already seeded it). */
  const _reentryBuf = streamBufs.get(convId);
  if (_reentryBuf) {
    if (!_reentryBuf.content && assistantMsg.content) _reentryBuf.content = assistantMsg.content;
    if (!_reentryBuf.thinking && assistantMsg.thinking) _reentryBuf.thinking = assistantMsg.thinking;
    if (!(_reentryBuf.toolRounds && _reentryBuf.toolRounds.length) && assistantMsg.toolRounds && assistantMsg.toolRounds.length)
      _reentryBuf.toolRounds = [...assistantMsg.toolRounds];
  }
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

/* ════════════════════════════════════════════════════════════════════
   SSE event dispatcher — extracted from the _processSSELine closure
   inside _trySSE (refactor 2026-06). The former closure-locals now live
   on a mutable `ctx` object so this is a plain module-level function that
   can be unit-tested (see tests/test_frontend_sse_dispatch.py via the
   window.__sse_test__ seam). Body is byte-for-byte the original dispatch
   logic; only the 7 reassigned locals were lifted to `ctx` (destructured
   on entry, written back in `finally` so every early `return` propagates).

   ctx = { convId, taskId, stream, assistantMsg, buf, epCriticPhase,
           epCriticMsg, epCriticBuf, roundThinkingLen, lastEventId,
           pinnedMsgId }
   Returns truthy only for the `done` event (signals stream end). */
function dispatchSSEEvent(line, ctx) {
  let { assistantMsg, buf, pinnedMsgId: _pinnedMsgId } = ctx;
  let _epCriticPhase = ctx.epCriticPhase;
  let _epCriticMsg = ctx.epCriticMsg;
  let _epCriticBuf = ctx.epCriticBuf;
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
   * mutate object PROPERTIES of assistantMsg/buf/_epCritic* (same refs we
   * hold here) and never reassign the locals, so no write-back is needed. */
  function _hctx() {
    return { convId, taskId, stream, assistantMsg, buf,
             epCriticPhase: _epCriticPhase, epCriticMsg: _epCriticMsg,
             epCriticBuf: _epCriticBuf };
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
              plannerMsg.content = _snapshotLonger(plannerMsg, ev, 'content');
              plannerMsg.thinking = _snapshotLonger(plannerMsg, ev, 'thinking');
              plannerMsg.toolRounds = _snapshotLongerRounds(plannerMsg.toolRounds, ev.toolRounds);
              assistantMsg = plannerMsg;
              if (buf) {
                buf.thinking = assistantMsg.thinking;
                buf.content = assistantMsg.content;
                buf.toolRounds = assistantMsg.toolRounds;
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
            plannerMsg.content = _snapshotLonger(plannerMsg, ev, 'content');
            plannerMsg.thinking = _snapshotLonger(plannerMsg, ev, 'thinking');
            plannerMsg.toolRounds = _snapshotLongerRounds(plannerMsg.toolRounds, ev.toolRounds);
            assistantMsg = plannerMsg;
            if (buf) {
              buf.thinking = assistantMsg.thinking;
              buf.content = assistantMsg.content;
              buf.toolRounds = assistantMsg.toolRounds;
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
              workerMsg.content = _snapshotLonger(workerMsg, ev, 'content');
              workerMsg.thinking = _snapshotLonger(workerMsg, ev, 'thinking');
              workerMsg.toolRounds = _snapshotLongerRounds(workerMsg.toolRounds, ev.toolRounds);
              assistantMsg = workerMsg;
              if (buf) {
                buf.thinking = assistantMsg.thinking;
                buf.content = assistantMsg.content;
                buf.toolRounds = assistantMsg.toolRounds;
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
            if (inner) inner.insertAdjacentHTML("beforeend", _streamingBubbleHTML(_reconRole, _reconStatus, undefined, assistantMsg._msgId || null));
            buildTurnNav(conv);
          }
        }
        } /* close else (full reconnection) */
      } else if (_epCriticPhase && _epCriticMsg) {
        /* State snapshot during critic phase → update critic msg */
        _epCriticMsg.content = _snapshotLonger(_epCriticMsg, ev, 'content');
        _epCriticMsg.thinking = _snapshotLonger(_epCriticMsg, ev, 'thinking');
        if (_epCriticBuf) {
          _epCriticBuf.content = (_epCriticMsg.content || "").replace(/\[VERDICT:\s*(?:STOP|CONTINUE)\s*\]\s*$/i, "").trimEnd();
          _epCriticBuf.thinking = _epCriticMsg.thinking;
        }
      } else {
        assistantMsg.content = _snapshotLonger(assistantMsg, ev, 'content');
        assistantMsg.thinking = _snapshotLonger(assistantMsg, ev, 'thinking');
        if (ev.error) assistantMsg.error = ev.error;
        if (ev.toolRounds) {
          /* Merge: keep checkpoint rounds + new ones from state snapshot.
           * Keep-longer guards a cold/empty reconnect snapshot from SHRINKING
           * the tool-round panel the user already watched accumulate. */
          const existing = assistantMsg._continueToolRounds || [];
          const merged = existing.concat(ev.toolRounds || []);
          assistantMsg.toolRounds = _snapshotLongerRounds(assistantMsg.toolRounds, merged);
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
      // ★ Restore preferences-applied chip from snapshot (mid-stream reconnect)
      if (ev.preferencesApplied) {
        assistantMsg._preferencesApplied = ev.preferencesApplied;
        if (buf) buf._preferencesApplied = ev.preferencesApplied;
      }
      if (ev.relatedConversations) {
        assistantMsg._relatedConversations = ev.relatedConversations;
        if (buf) buf._relatedConversations = ev.relatedConversations;
      }
      if (ev.preferencesLearned) {
        assistantMsg._preferencesLearned = ev.preferencesLearned;
        if (buf) buf._preferencesLearned = ev.preferencesLearned;
      }
      twUpdate(convId);
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
      const _rrBuf = (_epCriticPhase && _epCriticBuf) ? _epCriticBuf : buf;
      if (_rrTarget) {
        _rrTarget.content = "";
        _rrTarget.thinking = "";
        delete _rrTarget.error;
        /* Keep any pre-turn checkpoint rounds (Continue), drop this attempt's. */
        _rrTarget.toolRounds = _rrTarget._continueToolRounds
          ? _rrTarget._continueToolRounds.slice() : [];
      }
      if (_rrBuf) {
        _rrBuf.content = "";
        _rrBuf.thinking = "";
        _rrBuf.toolRounds = _rrTarget ? _rrTarget.toolRounds : [];
        _rrBuf.phase = null;
      }
      twUpdate(convId);
    } else if (ev.type === "delta_reset") {
      /* The just-ended LLM round issued TOOL CALLS, so the prose it streamed
       * before those calls was inter-round narration ("Now let me check the
       * utility functions."), NOT the final answer. Those content deltas were
       * already appended to the live bubble (see the "delta" branch:
       * assistantMsg.content += ev.content). Clear the accumulated
       * content/thinking so the narration isn't concatenated in front of the
       * terminal round's real answer. UNLIKE retry_reset, KEEP toolRounds —
       * the tool calls from this turn are legitimate and keep rendering. */
      _roundThinkingLen = 0;
      const _drTarget = (_epCriticPhase && _epCriticMsg) ? _epCriticMsg : assistantMsg;
      const _drBuf = (_epCriticPhase && _epCriticBuf) ? _epCriticBuf : buf;
      if (_drTarget) {
        /* ★ Interleave capture (segment-timeline step 5b): the narration +
         * thinking this round streamed BEFORE issuing tool calls is about to be
         * cleared from the live accumulators — but it is genuine per-round
         * prose that must stay ADJACENT to the tools it introduced (today it
         * was simply LOST live until finalize re-derived it from segments).
         * Stamp it onto the FIRST tool round of this llmRound batch — exactly
         * where the backend `assemble_segments` puts it (prose on the first
         * entry of the batch) — so the live view matches the settled render and
         * finalize is a visual no-op. ev.round === the round's llmRound
         * (both are the orchestrator loop index). Backend stays the source of
         * truth: the terminal `committedMessage` overwrites these fields with
         * the authoritative copy, so a live-vs-settled drift is impossible. */
        const _trs = Array.isArray(_drTarget.toolRounds) ? _drTarget.toolRounds : null;
        if (_trs && _trs.length) {
          let _lr = ev.round;
          if (_lr == null) _lr = _trs[_trs.length - 1].llmRound;
          const _batch = (_lr != null)
            ? _trs.filter(r => r.llmRound === _lr)
            : [_trs[_trs.length - 1]];
          const _first = _batch[0];
          if (_first) {
            if (_drTarget.content && !_first.assistantContent) _first.assistantContent = _drTarget.content;
            if (_drTarget.thinking && !_first.thinking) _first.thinking = _drTarget.thinking;
          }
        }
        _drTarget.content = "";
        _drTarget.thinking = "";
      }
      if (_drBuf) {
        _drBuf.content = "";
        _drBuf.thinking = "";
        /* Keep the buffer's toolRounds pointed at the (now prose-stamped)
         * live list so the reactive render picks up the captured prose. The
         * per-round prose (assistantContent/thinking length) is in the
         * _syncToolRoundsDOM fingerprint, so the twUpdate below re-renders the
         * group and _renderStreamRoundProse paints the captured narration. */
        if (_drTarget && Array.isArray(_drTarget.toolRounds)) _drBuf.toolRounds = _drTarget.toolRounds;
      }
      twUpdate(convId);
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
            const inner = document.getElementById("chatInner");
            if (inner) inner.insertAdjacentHTML("beforeend", _streamingBubbleHTML('critic', undefined, undefined, _epCriticMsg._msgId || null));
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
              if (inner) inner.insertAdjacentHTML("beforeend", _streamingBubbleHTML('worker', 'Thinking…', undefined, assistantMsg._msgId || null));
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
            if (inner) inner.insertAdjacentHTML("beforeend", _streamingBubbleHTML('planner', 'Replanning…', undefined, assistantMsg._msgId || null));
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
          if (inner) inner.insertAdjacentHTML("beforeend", _streamingBubbleHTML('worker', 'Thinking…', undefined, assistantMsg._msgId || null));

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
         the LIVE toolbar at send time, but the latch held back this diff so
         the turn actually ran with the FROZEN tool set. Correct that turn's
         _ctx note in place (added=held-back-on → drop; removed=held-back-off
         → restore) so the gutter capsule reflects what truly ran, then
         persist + re-render. See info-rail.js::reconcileTurnCtxCapsule. */
      if (ev.toolsetDiverged && ev.toolsetDiff
          && typeof reconcileTurnCtxCapsule === 'function') {
        try {
          const _rc = conversations.find((c) => c.id === convId);
          if (_rc && Array.isArray(_rc.messages)) {
            let _uIdx = -1;
            for (let i = _rc.messages.length - 1; i >= 0; i--) {
              if (_rc.messages[i] && _rc.messages[i].role === 'user') { _uIdx = i; break; }
            }
            const _uMsg = _uIdx >= 0 ? _rc.messages[_uIdx] : null;
            if (_uMsg && _uMsg._ctx && reconcileTurnCtxCapsule(_uMsg._ctx, ev.toolsetDiff)) {
              if (typeof saveConversations === 'function') saveConversations(convId);
              if (activeConvId === convId && window.ConvView
                  && typeof window.ConvView.upsertMessage === 'function') {
                window.ConvView.upsertMessage(convId, _uMsg, { idx: _uIdx });
              }
            }
          }
        } catch (e) {
          console.debug('[turnCtx] reconcile against toolsetDiff failed:', e);
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
                          'modifiedFileList', 'cost', '_taskId',
                          'fallbackModel', 'fallbackFrom', 'fallbackReason',
                          'fallbackKind', 'error', 'thinkingDepth', '_gitSha',
                          '_memoryPrefetch', '_preferencesApplied',
                          '_relatedConversations', '_preferencesLearned']) {
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
      if (activeConvId === convId && assistantMsg._vuTookOverBubble
          && window.ConvView
          && typeof window.ConvView.upsertMessage === 'function') {
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
      return true;
    }
    return false;
  } finally {
    ctx.assistantMsg = assistantMsg;
    ctx.buf = buf;
    ctx.epCriticPhase = _epCriticPhase;
    ctx.epCriticMsg = _epCriticMsg;
    ctx.epCriticBuf = _epCriticBuf;
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
  let _lastEventId = null; // mirrors ctx.lastEventId for the reader loop below
  /* ── Mutable SSE dispatch context ──
   * The 7 values that the dispatcher reassigns (worker/critic targets,
   * buffers, phase flags, thinking counter, last-event-id) live here so
   * dispatchSSEEvent — now a module-level, unit-testable function — can
   * mutate them across events. _trySSE reads them back after each line. */
  const ctx = {
    convId, taskId, stream,
    assistantMsg,
    buf,
    epCriticPhase: false,
    epCriticMsg: null,
    epCriticBuf: null,
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
    buf = ctx.buf;
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
        buf: o.buf || null,
        epCriticPhase: false, epCriticMsg: null, epCriticBuf: null,
        roundThinkingLen: 0, lastEventId: null, pendingEventId: null,
        pinnedMsgId: (o.assistantMsg && o.assistantMsg._msgId) || null,
      };
    },
  };
}
