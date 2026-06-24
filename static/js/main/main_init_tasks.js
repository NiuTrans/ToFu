/* ═══════════════════════════════════════════════════════════════════
   main init tasks — extracted from main.js (split 2026-05-28)

   initActiveTasks + _ensureNewest (the heavy startup-resume path).

   This file is concatenated by lib/js_bundler.py BEFORE main.js so
   the boot IIFE can reference these symbols. Symbols share `window`
   scope — no imports / exports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ── Init ──
async function initActiveTasks() {
  try {
    /* ── Parallel fetch: metadata + active tasks ── */
    /* Pass activeConvId (or restored conv from sessionStorage) to prefetch
       its messages in the same request, eliminating the second round-trip
       that shows "loading..." */
    const prefetchTarget = activeConvId || sessionStorage.getItem('tofu_activeConvId') || null;
    const [, , activeResp] = await Promise.all([
      loadConversationsFromServer(prefetchTarget),
      typeof loadFolders === 'function' ? loadFolders() : Promise.resolve(),
      Api.chat.activeResponse(),
    ]);
    /* ★ Migrate pinned conversations to a "⭐ 置顶" folder (one-time) */
    if (typeof _migratePinnedToFolder === 'function') _migratePinnedToFolder();
    if (!activeResp || !activeResp.ok) {
      _ensureNewest();
      return;
    }
    const serverTasks = await activeResp.json();
    // ★ Exclude aborted tasks — they are winding down and should not be reconnected to
    const runIds = new Set(
      serverTasks.filter((t) => t.status === "running" && !t.aborted).map((t) => t.id),
    );
    const toRecon = [];
    /* ★ Build a map from convId → running taskId for orphan recovery
          (handles the case where user refreshed before activeTaskId was saved) */
    const convIdToRunningTask = new Map();
    for (const t of serverTasks) {
      if (t.status === "running" && !t.aborted && t.convId) {
        // ★ Skip tasks that belong to branch streams — they are managed separately
        if (typeof isBranchTaskId === "function" && isBranchTaskId(t.id)) continue;
        convIdToRunningTask.set(t.convId, t.id);
      }
    }

    /* ── Batch-load messages only for convs that need task reconnection ── */
    const needMsgLoadIds = new Set();
    for (const conv of conversations) {
      if (conv._needsLoad) {
        if (conv.activeTaskId && runIds.has(conv.activeTaskId)) {
          needMsgLoadIds.add(conv.id);
        } else if (conv.activeTaskId) {
          needMsgLoadIds.add(conv.id);
        } else if (convIdToRunningTask.has(conv.id)) {
          needMsgLoadIds.add(conv.id);
        }
      }
    }
    if (needMsgLoadIds.size > 0) {
      await Promise.all(
        [...needMsgLoadIds].map((id) => loadConversationMessages(id)),
      );
    }

    /* ── Parallel poll for all finished tasks (Case B) ── */
    const caseBConvs = [];
    const caseEConvs = []; // Orphaned user messages (Case E)
    for (const conv of conversations) {
      /* Case A: conv has activeTaskId and that task is still running → reconnect */
      if (conv.activeTaskId && runIds.has(conv.activeTaskId)) {
        /* ★ Maintain the invariant "running task ⇒ trailing empty assistant
         *   placeholder".  After loadConversationsFromServer / Phase-2 message
         *   loads, conv.messages[-1] may be the previous (completed) assistant
         *   turn rather than an empty placeholder.  Without a fresh slot the
         *   SSE state-snapshot replay overwrites the prior turn's content into
         *   the bubble, producing the "old turn re-streams into the new one"
         *   visual bug.  Mirrors the Case C placeholder logic but gated on
         *   "last assistant belongs to a different / finished task". */
        const _amA = conv.messages[conv.messages.length - 1];
        const _staleTail = _amA && _amA.role === 'assistant'
          && !_amA._epIteration && !_amA._isEndpointReview && !_amA._isEndpointPlanner
          && ((_amA._taskId && _amA._taskId !== conv.activeTaskId) || !!_amA.finishReason);
        if (_staleTail) {
          console.info(
            `[initActiveTasks CaseA] Pushing fresh assistant placeholder for conv=${conv.id.slice(0,8)} ` +
            `(stale tail _taskId=${_amA._taskId?.slice(0,8)||'none'} ≠ activeTaskId=${conv.activeTaskId.slice(0,8)}, ` +
            `finishReason=${_amA.finishReason||'none'})`
          );
          conv.messages.push(_ensureMsgId({
            role: 'assistant',
            content: '',
            thinking: '',
            timestamp: Date.now(),
            toolRounds: [],
            model: conv.model || config.model || serverModel,
          }));
        }
        toRecon.push({ convId: conv.id, taskId: conv.activeTaskId });
        continue;
      }

      /* Case B: conv has activeTaskId but task finished/unknown → poll in batch */
      if (conv.activeTaskId) {
        caseBConvs.push(conv);
        continue;
      }

      /* Case C: ★ No activeTaskId, but server has a running task for this convId
            (user refreshed during "Preparing" before POST returned taskId)
            ★ Skip if the orphan is actually a branch task */
      const orphanTaskId = convIdToRunningTask.get(conv.id);
      if (orphanTaskId && !(typeof isBranchTaskId === "function" && isBranchTaskId(orphanTaskId))) {
        debugLog(
          `Recovering orphan task ${orphanTaskId.slice(0, 8)} for conv ${conv.id.slice(0, 8)}`,
          "warn",
        );
        const am = conv.messages[conv.messages.length - 1];
        /* Ensure there's an assistant message to stream into */
        if (!am || am.role !== "assistant") {
          conv.messages.push(_ensureMsgId({
            role: "assistant",
            content: "",
            thinking: "",
            timestamp: Date.now(),
            toolRounds: [],
            model: conv.model || config.model || serverModel,
          }));
        }
        conv.activeTaskId = orphanTaskId;
        toRecon.push({ convId: conv.id, taskId: orphanTaskId });
        continue;
      }

      /* Case D: No activeTaskId, no running server task — clean up ghost empty assistant messages
         (only for locally-loaded convs, not server-only shells) */
      if (!conv._needsLoad) {
        const lastMsg = conv.messages[conv.messages.length - 1];
        if (
          lastMsg &&
          lastMsg.role === "assistant" &&
          !lastMsg.content &&
          !lastMsg.thinking &&
          !lastMsg.error
        ) {
          console.warn(
            `[initActiveTasks CaseD] Removing ghost empty assistant message from conv ${conv.id.slice(0, 8)} ` +
            `(msgs=${conv.messages.length}, lastTimestamp=${lastMsg.timestamp ? new Date(lastMsg.timestamp).toISOString() : 'none'}). ` +
            `This could indicate a stream that started but never received any content.`,
          );
          conv.messages.pop();
          saveConversations(null);  // recovery cleanup, not new activity — don't bump updatedAt
          syncConversationToServer(conv, { allowTruncate: true });
        }
      }

      /* Case E: ★ Orphaned user message — last msg is user, no activeTaskId,
         no running server task. This happens when:
         (a) sendMessage() was interrupted by page refresh during blocking translation wait
         (b) startAssistantResponse() failed silently and wasn't persisted
         (c) Network error prevented the POST /api/chat/start from completing
         Recovery: auto-start the assistant response so the user doesn't have to
         re-send. Only trigger for recent messages (< 5 min old) to avoid
         accidentally re-sending ancient stale messages.
         ★ SKIP image gen messages (🎨 prefix / _isImageGen) — those are handled
         by generateImageDirect(), NOT the orchestrator. Re-sending them to
         startAssistantResponse() would send them to the LLM, causing a freeze.

         ★ FIX: Also detect orphans in _needsLoad shell convs using metadata.
         Before this fix, shell convs (messages not loaded) silently skipped Case E
         because the guard `!conv._needsLoad && conv.messages.length > 0` excluded them.
         Now we check settings.lastMsgRole/lastMsgTimestamp from metadata. */
      {
        let _caseELastRole = null;
        let _caseELastTimestamp = null;
        let _caseESource = null;  // 'messages' or 'metadata'
        if (!conv._needsLoad && conv.messages.length > 0) {
          const lastMsg = conv.messages[conv.messages.length - 1];
          _caseELastRole = lastMsg?.role;
          _caseELastTimestamp = lastMsg?.timestamp;
          _caseESource = 'messages';
        } else if (conv._needsLoad && conv.lastMsgRole) {
          /* ★ Shell conv: use metadata persisted by syncConversationToServer.
           *   _applySettingsToConv maps settings.lastMsgRole → conv.lastMsgRole
           *   and settings.lastMsgTimestamp → conv.lastMsgTimestamp. */
          _caseELastRole = conv.lastMsgRole;
          _caseELastTimestamp = conv.lastMsgTimestamp;
          _caseESource = 'metadata';
        }
        if (_caseELastRole === 'user' && _caseELastTimestamp) {
          // ★ Skip image gen orphans — they belong to the creative mode pipeline
          // (can only check content for loaded convs; metadata orphans are assumed non-image-gen)
          let isImageGenOrphan = false;
          if (_caseESource === 'messages') {
            const lastMsg = conv.messages[conv.messages.length - 1];
            isImageGenOrphan = lastMsg._isImageGen || (lastMsg.content || '').startsWith('🎨 ');  // backward compat
          }
          if (isImageGenOrphan) {
            console.warn(
              `[initActiveTasks CaseE] ⏭ Skipping image gen orphan in conv ${conv.id.slice(0, 8)}`
            );
          } else {
            const ageMs = Date.now() - _caseELastTimestamp;
            const MAX_ORPHAN_AGE_MS = 5 * 60 * 1000; // 5 minutes
            if (ageMs < MAX_ORPHAN_AGE_MS) {
              console.warn(
                `[initActiveTasks CaseE] ★ Orphaned user message detected in conv ${conv.id.slice(0, 8)} ` +
                `(source=${_caseESource}, age=${(ageMs/1000).toFixed(0)}s). ` +
                `Auto-starting assistant response…`
              );
              // Defer to after the main recovery loop completes
              // so that all message loading and reconnections finish first
              caseEConvs.push(conv);
            }
          }
        }
      }
    }

    /* ── Case A: Reconnect to running tasks immediately ── */
    /* ★ CROSS-TALK DETECTION: warn when reconnecting multiple tasks simultaneously */
    if (toRecon.length > 1) {
      console.warn(
        `[initActiveTasks] ⚠️ MULTI-TASK RECONNECT: reconnecting ${toRecon.length} tasks simultaneously — ` +
        `elevated cross-talk risk! Tasks: ${toRecon.map(t => `conv=${t.convId.slice(0,8)}→task=${t.taskId.slice(0,8)}`).join(', ')} ` +
        `activeConvId=${activeConvId?.slice(0,8)||'null'}`
      );
    }
    for (const { convId, taskId } of toRecon) connectToTask(convId, taskId);
    // ── Reconnect any in-flight branch streams ──
    if (typeof initBranchReconnect === "function") initBranchReconnect();

    /* ── Render sidebar + active conv IMMEDIATELY ──
     *   Case B/F recovery runs in the background so the user sees their
     *   conversation without waiting for recovery of ALL conversations.
     *   With the backend's recover_stale_tasks_on_startup(), most Case B
     *   convs are already cleaned up (activeTaskId cleared, content merged). */
    renderConversationList();
    _ensureNewest();

    /* ── Background recovery: Case B + F + E (non-blocking) ── */
    const _bgRecovery = async () => {
    /* ── Case B: Batch-poll finished tasks in parallel ── */
    if (caseBConvs.length > 0) {
      console.warn(`[initActiveTasks] Case B: recovering ${caseBConvs.length} conversations with finished tasks`);
      await Promise.all(
        caseBConvs.map(async (conv) => {
          let am = conv.messages[conv.messages.length - 1];
          /* ★ Safety: if messages is still empty after loadConversationMessages
             (shouldn't happen after the core.js fix, but defensive), force-load */
          if (conv.messages.length === 0) {
            console.warn(`[initActiveTasks CaseB] conv=${conv.id.slice(0,8)} has 0 messages after load — force-recovering from server`);
            try {
              const recData = await Api.conversations.get(conv.id);
              if (recData) {
                if (recData.messages?.length > 0) {
                  conv.messages = recData.messages;
                  conv.title = recData.title || conv.title;
                  conv._serverMsgCount = conv.messages.length;
                  am = conv.messages[conv.messages.length - 1];
                  console.warn(`[initActiveTasks CaseB] ✅ Recovered ${conv.messages.length} messages from server`);
                }
              }
            } catch (recErr) {
              console.error(`[initActiveTasks CaseB] Recovery fetch failed:`, recErr);
            }
          }
          const localContentLen = am?.content?.length || 0;
          const localThinkingLen = am?.thinking?.length || 0;
          console.warn(`[initActiveTasks CaseB] conv=${conv.id.slice(0,8)} taskId=${conv.activeTaskId?.slice(0,8)} ` +
            `msgs=${conv.messages.length} localContent=${localContentLen}chars localThinking=${localThinkingLen}chars — polling server for task data...`);
          try {
            const pr = await Api.chat.poll(conv.activeTaskId);
            if (pr && pr.ok) {
              const td = await pr.json();
              const serverContentLen = td.content?.length || 0;
              const serverThinkingLen = td.thinking?.length || 0;
              console.warn(`[initActiveTasks CaseB] conv=${conv.id.slice(0,8)} server returned: ` +
                `content=${serverContentLen}chars thinking=${serverThinkingLen}chars error=${td.error||'none'} status=${td.status}`);
              
              /* ★ Endpoint mode: rebuild conv.messages from server's endpointTurns */
              if (td.endpointMode && td.endpointTurns && td.endpointTurns.length > 0) {
                let baseEnd = 0;
                for (let i = 0; i < conv.messages.length; i++) {
                  if (!conv.messages[i]._epIteration && !conv.messages[i]._isEndpointReview && !conv.messages[i]._isEndpointPlanner) {
                    baseEnd = i + 1;
                  }
                }
                const baseMsgs = conv.messages.slice(0, baseEnd);
                conv.messages = baseMsgs.concat(td.endpointTurns);
                am = conv.messages[conv.messages.length - 1];
                console.warn(`[initActiveTasks CaseB] ♾️ Endpoint mode — rebuilt messages: ` +
                  `base=${baseMsgs.length} epTurns=${td.endpointTurns.length} total=${conv.messages.length}`);
              }

              /* ★ BUG FIX: If local already has more content than server, KEEP local content
                 This prevents data loss when SSE accumulated content but task result was incomplete */
              if (am && am.role === "assistant") {
                if (td.content) {
                  if (localContentLen > serverContentLen) {
                    console.warn(`[initActiveTasks CaseB] ⚠️ KEEPING LOCAL content (${localContentLen} > server ${serverContentLen}) — would lose data!`);
                  } else {
                    am.content = td.content;
                  }
                }
                if (td.thinking) {
                  if (localThinkingLen > serverThinkingLen) {
                    console.warn(`[initActiveTasks CaseB] ⚠️ KEEPING LOCAL thinking (${localThinkingLen} > server ${serverThinkingLen}) — would lose data!`);
                  } else {
                    am.thinking = td.thinking;
                  }
                }
                if (td.error) am.error = td.error;
                if (td.toolRounds) am.toolRounds = td.toolRounds;
                if (td.finishReason) am.finishReason = td.finishReason;
                if (td.usage) am.usage = td.usage;
                if (td.preset) am.preset = td.preset;
                else if (td.effort) am.preset = td.effort;
                if (td.fallbackModel) am.fallbackModel = td.fallbackModel;
                if (td.fallbackFrom) am.fallbackFrom = td.fallbackFrom;
                if (td.fallbackReason) am.fallbackReason = td.fallbackReason;
                if (td.fallbackKind) am.fallbackKind = td.fallbackKind;
                if (td.modifiedFiles) am.modifiedFiles = td.modifiedFiles;
              }
              /* ★ If server returned status='interrupted', the task was checkpointed
                 but the server crashed before completing. Mark it as interrupted
                 so the user knows the response is partial. */
              if (td.status === 'interrupted' && am && am.role === 'assistant') {
                const recoveredLen = (am.content?.length || 0) + (am.thinking?.length || 0);
                if (recoveredLen > 0) {
                  if (!am.finishReason) am.finishReason = 'interrupted';
                  console.warn(`[initActiveTasks CaseB] ✅ Recovered ${recoveredLen} chars from server checkpoint (task was interrupted by server crash)`);
                } else {
                  am.error = normalizeErrorEnvelope({
                    kind: 'internal', severity: 'error', retryable: false,
                    message: '⚠️ 任务被中断 — 服务器在生成任何内容之前重启了。\nTask interrupted — server restarted before any content was generated.',
                    hint: '', detail: 'task interrupted before any tokens',
                    model: '', context: 'case-b-recovery', source: 'frontend-recovery', raw: '',
                  });
                }
              }
            } else if (pr.status === 404) {
              /* Task not found in memory or DB — check if the conversation's
                 messages already have content from a partial checkpoint sync.
                 (checkpoint_task_partial writes directly to conversation messages too) */
              const dbContentLen = am?.content?.length || 0;
              const dbThinkingLen = am?.thinking?.length || 0;
              console.warn(`[initActiveTasks CaseB] ⚠️ 404 for task ${conv.activeTaskId?.slice(0,8)} — task expired/cleaned up. ` +
                `Local content: ${dbContentLen}chars, thinking: ${dbThinkingLen}chars. ` +
                (dbContentLen > 0 || dbThinkingLen > 0 ? 'Preserving recovered data.' : 'No data — marking error.'));
              if (am && am.role === "assistant") {
                if (dbContentLen > 0 || dbThinkingLen > 0) {
                  am.finishReason = 'interrupted';
                } else {
                  am.error = normalizeErrorEnvelope({
                    kind: 'internal', severity: 'error', retryable: false,
                    message: '⚠️ 任务已过期。\nTask expired.',
                    hint: '• 服务器上未找到这个任务记录。可能是服务器已清理过期任务。\n• The server no longer has a record of this task. It may have been cleaned up.',
                    detail: '404 from /api/chat/poll',
                    model: '', context: 'case-b-recovery', source: 'frontend-recovery', raw: '',
                  });
                }
              }
            }
          } catch (e) {
            console.error(`[initActiveTasks CaseB] Fetch error for conv=${conv.id.slice(0,8)}: ${e.message}`);
          }
          /* ★ FIX: Clean up orphaned awaiting_human / submitted HG rounds.
           *   Task is finished — any unanswered HG request is now dead. */
          let _hgCleaned = 0;
          let _timerCleaned = 0;
          for (const m of conv.messages) {
            if (m.toolRounds) {
              for (const r of m.toolRounds) {
                if (r.status === 'awaiting_human' || r.status === 'submitted') {
                  r.status = 'done';
                  r.guidanceId = null;
                  r._hgSkipped = true;
                  _hgCleaned++;
                }
                // ★ Clean up orphaned timer_create rounds — the task is dead,
                //   so the blocking poll can't complete. Mark as done and try
                //   to recover poll data from the API.
                if (r.toolName === 'timer_create' && r.status === 'searching') {
                  r.status = 'done';
                  r._timerOrphaned = true;
                  _timerCleaned++;
                  // Async: try to recover poll log from the timer API
                  if (r._timerTimerId && typeof _recoverTimerPolls === 'function') {
                    _recoverTimerPolls(r);
                  }
                }
              }
            }
          }
          if (_hgCleaned > 0) {
            console.info(`[initActiveTasks CaseB] 🧹 Cleaned ${_hgCleaned} orphaned HG round(s) — conv=${conv.id.slice(0,8)}`);
          }
          if (_timerCleaned > 0) {
            console.info(`[initActiveTasks CaseB] 🧹 Cleaned ${_timerCleaned} orphaned timer round(s) — conv=${conv.id.slice(0,8)}`);
          }
          conv.activeTaskId = null;
          conv._activeTaskClearedAt = Date.now();
          saveConversations(null);  // clearing a stale finished-task ref — not new activity, don't bump updatedAt
          syncConversationToServer(conv);
        }),
      );
    }

    /* ── Case F: Clear stale "server offline" errors now that server is back ── */
    /* When the frontend detected server offline (health check failure), it stamps
     * finishReason='server_offline' and error='Server offline — ...' on the last
     * assistant message and persists it.  On page refresh, this error text persists
     * even though the server is clearly back online (we just fetched /api/chat/active).
     *
     * Recovery: fetch the server's version of the conversation.  If the server has
     * a completed result (from _sync_result_to_conversation), adopt it.  Otherwise,
     * just clear the misleading error text — the "Server Offline" finish badge
     * already conveys the information without the alarming red error block. */
    {
      const offlineConvs = [];
      for (const conv of conversations) {
        if (conv._needsLoad) continue;
        const last = conv.messages[conv.messages.length - 1];
        if (last && last.role === 'assistant' && last.finishReason === 'server_offline') {
          offlineConvs.push(conv);
        }
      }
      if (offlineConvs.length > 0) {
        console.warn(`[initActiveTasks CaseF] ★ Clearing ${offlineConvs.length} stale "server_offline" error(s) — server is back online`);
        await Promise.all(offlineConvs.map(async (conv) => {
          const am = conv.messages[conv.messages.length - 1];
          const localContentLen = am.content?.length || 0;
          try {
            // Try to get server's version — it may have the completed result
            const data = await Api.conversations.get(conv.id);
            if (data) {
              const serverMsgs = data.messages || [];
              if (serverMsgs.length > 0) {
                const serverLast = serverMsgs[serverMsgs.length - 1];
                if (serverLast && serverLast.role === 'assistant') {
                  const serverContentLen = serverLast.content?.length || 0;
                  // If server has more content, adopt it (task completed after frontend gave up)
                  if (serverContentLen > localContentLen) {
                    console.warn(
                      `[initActiveTasks CaseF] conv=${conv.id.slice(0,8)}: server has MORE content ` +
                      `(${serverContentLen} > local ${localContentLen}) — adopting server version`
                    );
                    am.content = serverLast.content;
                    if (serverLast.thinking) am.thinking = serverLast.thinking;
                    if (serverLast.toolRounds) am.toolRounds = serverLast.toolRounds;
                    if (serverLast.finishReason && serverLast.finishReason !== 'server_offline') {
                      am.finishReason = serverLast.finishReason;
                    }
                    if (serverLast.usage) am.usage = serverLast.usage;
                    if (serverLast.model) am.model = serverLast.model;
                    if (serverLast.modifiedFiles) am.modifiedFiles = serverLast.modifiedFiles;
                    if (serverLast.modifiedFileList) am.modifiedFileList = serverLast.modifiedFileList;
                  }
                }
              }
            }
          } catch (e) {
            console.debug(`[initActiveTasks CaseF] Server fetch failed for conv=${conv.id.slice(0,8)}: ${e.message}`);
          }
          // Always clear the misleading error text — server is online now
          if (am.error && errorEnvelopeKind(am.error) === 'server_offline') {
            console.info(
              `[initActiveTasks CaseF] conv=${conv.id.slice(0,8)}: clearing stale error text ` +
              `(content=${(am.content?.length||0)}chars, finishReason=${am.finishReason})`
            );
            delete am.error;
            saveConversations(null);  // clearing a stale offline error — not new activity, don't bump updatedAt
            syncConversationToServer(conv);
          }
        }));
      }
    }

    /* ── Case E dispatch: auto-start responses for orphaned user messages ── */
    if (caseEConvs.length > 0) {
      console.warn(`[initActiveTasks CaseE] ★ Auto-starting ${caseEConvs.length} orphaned conversations`);
      /* ★ FIX: Delay Case E dispatch by 3s to give the user time to interact.
       *   Without delay, Case E fires startAssistantResponse immediately after
       *   page load, racing with the user's own sendMessage() if they quickly
       *   click a conversation and hit Send.  Both paths push assistant messages
       *   and POST /api/chat/start concurrently → broken SSE.
       *   The 3s delay lets the user's action take priority.  The re-check
       *   guard (activeTaskId / activeStreams) catches any user-initiated task. */
      setTimeout(() => {
        for (const conv of caseEConvs) {
          // Re-check: user may have already started a response for this conv
          if (conv.activeTaskId || activeStreams.has(conv.id)) {
            console.log(
              `[initActiveTasks CaseE] ⏭ Skipping conv ${conv.id.slice(0,8)} — ` +
              `task already started (activeTaskId=${conv.activeTaskId?.slice(0,8) || 'none'}, ` +
              `streaming=${activeStreams.has(conv.id)})`
            );
            continue;
          }
          debugLog(
            `Recovering orphaned message in "${conv.title?.slice(0,30)}…" — auto-starting assistant response`,
            "warn",
          );
          startAssistantResponse(conv.id);
        }
      }, 3000);
    }
    }; /* end _bgRecovery */

    /* Fire background recovery — don't await it */
    _bgRecovery().then(() => {
      /* Re-render after background recovery completes to show updated state */
      renderConversationList();
      if (activeConvId && !activeStreams.has(activeConvId)) {
        const c = getActiveConv();
        if (c && !c.activeTaskId) renderChat(c, false);
      }
    }).catch(e => console.warn('[initActiveTasks] Background recovery error:', e.message));

  } catch (e) {
    debugLog("initActiveTasks: " + e.message, "warn");
  }
}
function _ensureNewest() {
  if (_editingMsgIdx !== null) return;
  if (activeConvId) {
    if (activeStreams.has(activeConvId)) showStreamingUIForConv(activeConvId);
    else {
      const c = getActiveConv();
      if (c) renderChat(c);
    }
    // ★ Restore server-side queue state (survives page refresh)
    _refreshServerQueue(activeConvId);
  }
}
