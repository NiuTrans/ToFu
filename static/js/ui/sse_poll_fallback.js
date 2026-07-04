/* ═══════════════════════════════════════════════════════════════════
   sse poll fallback — extracted from ui/sse_pipeline.js (split 2026-06)

   `_pollFallback(convId, taskId, stream, assistantMsg)` — the polling
   path used when SSE never delivers data (connection blocked / proxy
   strips event-stream). Polls Api.chat.poll(taskId) with an RTT-adaptive
   interval, a circuit breaker, and a 2-minute network-recovery wait for
   VS Code tunnel drops. Handles endpoint multi-turn rebuild, regression-
   safe content/thinking merge, continue-checkpoint merge, and offline
   recovery.

   Pure window-scope: takes all live state via parameters and reads only
   globals (streamBufs, conversations, activeConvId, Api, twUpdate/twStop,
   finishStream, saveConversations, renderChat, showToast,
   _checkServerHealth, _startOfflineRecoveryPolling, debugLog, …). No
   closure capture from _trySSE. Concatenated by lib/js_bundler.py AFTER
   ui/sse_pipeline.js; symbols share window scope, no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

async function _pollFallback(convId, taskId, stream, assistantMsg) {
  let lastSave = Date.now();
  const buf = streamBufs.get(convId);
  const _preExistingContent = assistantMsg.content?.length || 0;
  const _preExistingThinking = assistantMsg.thinking?.length || 0;
  /* ★ Reset the endpoint poll-turn counter at the start of every poll
   *   session.  It gates the "new completed turns arrived → renderChat"
   *   check below; if a prior session left it equal to the server's turn
   *   count (e.g. SSE timed out after the last turn, then poll takes over),
   *   the first endpoint poll would compute newEpCount === prevEpCount and
   *   skip the re-render, leaving a stale streaming bubble until the
   *   terminal finishStream re-render.  Clearing it forces the first
   *   endpoint poll of this session to repaint from the authoritative turns. */
  {
    const _startConv = conversations.find(c => c.id === convId);
    if (_startConv) _startConv._epPollTurnCount = 0;
  }
  console.warn(`[_pollFallback] START — conv=${convId.slice(0,8)} taskId=${taskId.slice(0,8)} preExistingContent=${_preExistingContent}chars preExistingThinking=${_preExistingThinking}chars`);
  // Poll until the task finishes, the user aborts, or server is confirmed dead.
  let _pollIter = 0;
  let _consecutiveErrors = 0;     // ★ Circuit breaker: track consecutive network failures
  const _MAX_CONSECUTIVE_ERRORS = 10; // ★ After 10 failures (~5s), do health check
  let _rttEma = 300; // ★ Item 8: exponential moving average of poll RTT (ms), seed 300ms
  /* ★ Epic C sharded-backend affinity re-route: bounded count of SSE re-open
   *   attempts triggered by a `reconnect:true` poll hint (see the guard in the
   *   loop below). Capped so a pathological setup where SSE never re-attaches
   *   cannot spin the re-open path forever — after the cap we fall through and
   *   keep polling (the bubble stays live, terminal/offline paths still apply). */
  let _reconnectAttempts = 0;
  const _MAX_RECONNECT_ATTEMPTS = 3;
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
        /* ★ resp===null means Api.chat.poll swallowed a network failure
         *   (onError:'null' — VS Code tunnel drop / fetch threw). Dereferencing
         *   resp.status here would itself throw the misleading TypeError
         *   "Cannot read properties of null (reading 'status')" that was
         *   surfacing at ERROR level in the client-error log. Feed the circuit
         *   breaker a clean network-failure message instead. */
        throw new Error(resp ? `Poll HTTP ${resp.status}` : 'Poll network error (no response)');
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

      /* ★ Epic C sharded-backend affinity hint. Under TOFU_RUNTIME_STATE_BACKEND
       *   =redis the poll endpoint (routes/chat.py) returns status='running'
       *   PLUS reconnect:true when the DB has a live 'running' checkpoint but
       *   the task is absent from THIS replica's memory — it is (probably) alive
       *   on another replica. Polling here would report 'running' forever
       *   against a replica that has no live task, so the bubble hangs. Re-open
       *   the SSE stream instead: taskId affinity re-routes us to the owning
       *   replica. If SSE now attaches it takes over (return); if it still
       *   fails, resume polling — bounded by _MAX_RECONNECT_ATTEMPTS so we never
       *   loop the re-open path forever. (inproc default never sets reconnect, so
       *   this whole block is dead code on a single-box install.) */
      if (data.reconnect === true && data.status === 'running'
          && typeof _trySSE === 'function'
          && !stream.controller.signal.aborted) {
        if (_reconnectAttempts < _MAX_RECONNECT_ATTEMPTS) {
          _reconnectAttempts++;
          console.warn(`[_pollFallback] ↻ reconnect hint (sharded) — taskId=${taskId.slice(0,8)} ` +
            `likely on another replica; re-opening SSE (attempt ${_reconnectAttempts}/${_MAX_RECONNECT_ATTEMPTS})`);
          let _sseTookOver = false;
          try {
            _sseTookOver = await _trySSE(convId, taskId, stream, assistantMsg);
          } catch (e) {
            if (e && e.name === 'AbortError') { twStop(convId); finishStream(convId); return; }
            console.warn(`[_pollFallback] reconnect SSE re-open threw: ${e && e.message} — resuming poll`);
          }
          if (stream.controller.signal.aborted) { twStop(convId); finishStream(convId); return; }
          if (_sseTookOver) {
            /* SSE re-attached and ran to completion on the owning replica; it
             *   owns finishStream/twStop. Nothing left to poll. */
            console.info(`[_pollFallback] ✅ SSE re-attached after reconnect hint — conv=${convId.slice(0,8)}; poll yielding`);
            return;
          }
          /* SSE still couldn't attach — fall through to keep polling; the next
           *   poll may either re-emit the hint (retried, bounded) or the task
           *   may migrate/settle. */
        } else {
          console.warn(`[_pollFallback] reconnect hint exhausted (${_MAX_RECONNECT_ATTEMPTS}) for taskId=${taskId.slice(0,8)} — continuing to poll this replica`);
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
      if (data.fallbackReason) assistantMsg.fallbackReason = data.fallbackReason;
      if (data.fallbackKind) assistantMsg.fallbackKind = data.fallbackKind;
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
      /* ★ preferences-applied: recover chip state from poll response */
      if (data.preferencesApplied) assistantMsg._preferencesApplied = data.preferencesApplied;
      /* ★ preferences-learned: recover "Noted: you prefer X" moment(s) */
      if (data.preferencesLearned) assistantMsg._preferencesLearned = data.preferencesLearned;
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
        /* ★ Autopilot follow-up: mirror the SSE done handler
         *   (sse_pipeline.js).  The baton now rides on the poll response too
         *   (routes/chat.py), so finishStream's _findAutopilotPendingCarrier
         *   hands off to the already-spawned follow-up task — keeping the
         *   sidebar dot / pause button / translation gating in the running
         *   state instead of going idle until a manual refresh. */
        if (data.autopilotNextTaskId && data.autopilotVuMessage) {
          const _apPayload = {
            nextTaskId: data.autopilotNextTaskId,
            vuMessage: data.autopilotVuMessage,
          };
          /* Kick-from-idle carrier: assistantMsg is a DETACHED dummy never
           * pushed into conv.messages — stamp the baton on the finalized VU
           * user msg at the tail so _findAutopilotPendingCarrier finds it. */
          const _apConv = conversations.find(c => c.id === convId);
          const _apDetached = _apConv && _apConv.messages.indexOf(assistantMsg) === -1;
          let _apTarget = assistantMsg;
          if (_apDetached && _apConv && _apConv.messages.length) {
            _apTarget = _apConv.messages[_apConv.messages.length - 1];
          }
          _apTarget._autopilotPending = _apPayload;
          console.info(
            `[_pollFallback] 🤖 Autopilot follow-up attached via poll — ` +
            `next task=${data.autopilotNextTaskId.slice(0,8)} detachedCarrier=${!!_apDetached} ` +
            `vu="${(data.autopilotVuMessage.content||'').slice(0,80)}${(data.autopilotVuMessage.content||'').length>80?'…':''}"`
          );
        }
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
