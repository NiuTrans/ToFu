/* ═══════════════════════════════════════════════════════════════════
   send button — extracted from ui/sse_pipeline.js (split 2026-06)

   `updateSendButton()` — toggles the composer button between Send (⏎)
   and Stop (■ + queue badge), and wires the Stop handler's 4-priority
   abort cascade (translation → active branch → any branch → main stream).

   Pure window-scope: reads only globals (activeStreams, activeConvId,
   conversations, _branchStreams, _activeBranch, pendingMessageQueue, Api,
   twStop, finishStream, …). No closure capture from _trySSE — safe to
   live in its own bundled file. Concatenated by lib/js_bundler.py AFTER
   ui/sse_pipeline.js; symbols share window scope, no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

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

  // ★ SINGLE SOURCE OF TRUTH: the same busy-predicate the sidebar uses
  //   (convIsBusy in ui/conversation_list.js). Previously this recomputed
  //   `activeStreams.has(id) || activeTaskId` inline and LACKED the
  //   activeStreams key-prefix scan the sidebar had — so the composer and the
  //   sidebar dot could disagree about the SAME conv. Routing both through
  //   convIsBusy makes that divergence impossible by construction. (Branch
  //   stop-cascade below still keys off _branchStreams for the abort priorities.)
  const mainStreaming = convIsBusy(conv);
  const translating = conv && conv._translating;
  /* ★ Generation-STARTUP window (pt_fa32a2351b3840ad): a send / regenerate /
   *   edit-resend POST is in flight (the '连接中…' placeholder is up) but no
   *   task is registered yet, so every predicate above is false. Without this
   *   the composer showed a SEND-shaped button that dead-clicked on the empty
   *   composer — the user could not cancel the startup on exactly the slow
   *   seconds they most want to. */
  const startupConnecting = !!(conv && conv._genStartCtrl);
  const streaming = branchStreaming || mainStreaming || anyBranchStreaming || translating || startupConnecting;

  if (streaming) {
    /* Count only DISPATCHABLE queued messages — the autopilot armed-marker
     * sentinel is never dispatched as a task, so it must not inflate the
     * badge (see _dispatchableQueueCount in main_send_pipeline.js). */
    const queueCount = (conv && typeof _dispatchableQueueCount === 'function')
      ? _dispatchableQueueCount(conv.id) : 0;
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
      // ── Priority 0.5: stop generation STARTUP (send/regenerate/edit POST window) ──
      if (startupConnecting && conv) {
        console.log(`[stopBtn] Aborting generation startup — conv=${conv.id.slice(0,8)}`);
        const _gsCtrl = conv._genStartCtrl;
        /* ★ Owner-tag the flag WITH the controller: the pipeline's catch
         *   matches `conv._genStartStop === <its own ctrl>`, so a newer send
         *   racing this pipeline's finally can never be mistaken for the
         *   stopped one. Nulling the marker flips the button back on THIS
         *   frame; the aborted fetch drives the shared rollback
         *   (_userStopDuringStartup) in the pipeline's catch. */
        conv._genStartStop = _gsCtrl;
        conv._genStartCtrl = null;
        try { _gsCtrl.abort(); }
        catch (_e) { console.warn('[stopBtn] startup abort threw:', _e); }
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
        // ★ Autopilot VU streaming: abort() below tears down the SSE reader
        //   BEFORE the backend's autopilot_vu_cancel frame can be read, so the
        //   event-driven splice never runs and a dangling _streamingVu ghost
        //   bubble is left rendering the frozen "Autopilot…" pulse. Remove it
        //   LOCALLY here (preserves conv._apPendingBaton — see the helper).
        if (conv && typeof _removeStreamingVuBubbleIfTail === 'function') {
          _removeStreamingVuBubbleIfTail(conv, activeConvId);
        }
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
        try { if (typeof twStop === 'function') twStop(activeConvId); }
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
    btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 12L20 12"/><path d="M13 5l7 7-7 7"/></svg>`;
    btn.onclick = sendMessage;
  }
}

/* ═══════════════════════════════════════════════════════════════════
   ★ Shared rollback for a USER-STOP during the generation-STARTUP window
   (send / regenerate / edit-resend POST in flight, no task registered yet).

   Generalizes the translation-stop branch (it is NOT a new semantic):
   tear the '连接中…' placeholder down, re-render the user message so it
   stays EDITABLE in the list, persist locally, and tell the backend to
   abort anything that snuck through before the abort landed. NO task is
   ever started here.

   opts:
     userMsg / userMsgIdx — re-render this message after the teardown (send
                            path only; regen/edit truncated instead);
     syncOpts             — forwarded to syncConversationToServer (e.g.
                            { allowTruncate: true } for regen/edit);
     rescue               — await the sync and markConvPendingSync on
                            failure (send path's poor-network durability).
   ═══════════════════════════════════════════════════════════════════ */
async function _userStopDuringStartup(conv, convId, opts) {
  opts = opts || {};
  _removeTranslatingBubble();
  if (activeConvId === convId && opts.userMsg && opts.userMsgIdx != null) {
    const _mEl = document.getElementById('msg-' + opts.userMsgIdx);
    if (_mEl) window.ConvView.apply(convId, opts.userMsgIdx, opts.userMsg);
  }
  saveConversations(convId);
  const _syncP = syncConversationToServer(conv, opts.syncOpts);
  if (opts.rescue) {
    const _synced = await _syncP;
    if (!_synced && typeof markConvPendingSync === 'function') markConvPendingSync(conv);
  } else {
    _syncP.catch(() => {});
  }
  buildTurnNav(conv);
  Api.chat.abortConv(convId);
}
if (typeof window !== 'undefined') window._userStopDuringStartup = _userStopDuringStartup;
