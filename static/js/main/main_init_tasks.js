/* ═══════════════════════════════════════════════════════════════════
   main init tasks — extracted from main.js (split 2026-05-28)

   initActiveTasks + _ensureNewest (the heavy startup-resume path).

   This file is concatenated by lib/js_bundler.py BEFORE main.js so
   the boot IIFE can reference these symbols. Symbols share `window`
   scope — no imports / exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* NOTE: the ENTIRE frontend ghost-lifecycle classifier family is now RETIRED.
 * `_isBuriedEmptyGhost` / `_sweepBuriedGhostAssistants` went first (2026-07-07),
 * then `_classifyGhostTail` (2026-07-11) once the backend reconcile reached
 * every render path. All INFERRED settled lifecycle state in JS — the
 * separation-of-concerns violation. The verdict (buried-ghost sweep +
 * superseded-error-husk collapse + trailing delete/interrupt) now lives ONLY on
 * the backend (lib/conversations/reconcile.py), applied + persisted +
 * _reconciledAt-stamped in one commit on the GET path
 * (routes/conversations.py::_reconcile_conv_on_get_blocking), the prefetch path
 * (_prefetch_reconciled_dict), and startup recovery. Proven byte-equivalent by
 * tests/test_reconcile_js_backend_equivalence.py. The frontend keeps ONLY
 * network/DOM orchestration (reconnect a live SSE, poll a finished task); it no
 * longer infers settled lifecycle at all. */

// ── Init ──
async function initActiveTasks() {
  try {
    /* ── Parallel fetch: metadata + active tasks ── */
    /* Pass activeConvId (or restored conv from sessionStorage) to prefetch
       its messages in the same request, eliminating the second round-trip
       that shows "loading..." */
    const prefetchTarget = activeConvId || sessionStorage.getItem('tofu_activeConvId') || null;

    /* ★ Folder loading is DECOUPLED from the conversation/active-task fetch.
     *   It used to be a third leg of the Promise.all below — but Promise.all
     *   short-circuits on the FIRST sibling rejection and discards the other
     *   legs, so a transient failure in loadConversationsFromServer /
     *   activeResponse would skip folder loading entirely AND jump past the
     *   catch, leaving _folders empty with no recovery (the folder rail
     *   vanishes and only the "+ New folder" quick-add shows even though 19
     *   folders exist server-side). loadFolders owns its own bounded-backoff
     *   self-heal, but that only fires from loadFolders' OWN catch — which
     *   never ran when a sibling sank the shared Promise.all first. Kick it off
     *   in parallel (no perf loss) but isolate its fate: its own .catch fires
     *   the retry, and _migratePinnedToFolder is chained AFTER folders resolve
     *   so it doesn't race an unloaded folder list. */
    const _foldersDone = (typeof loadFolders === 'function'
      ? Promise.resolve(loadFolders())
      : Promise.resolve()
    ).then(() => {
      if (typeof _migratePinnedToFolder === 'function') _migratePinnedToFolder();
    }).catch((e) => {
      console.warn('[initActiveTasks] folder load failed (isolated):', e && e.message);
      if (typeof _scheduleFolderLoadRetry === 'function') _scheduleFolderLoadRetry();
    });

    const [, activeResp] = await Promise.all([
      loadConversationsFromServer(prefetchTarget),
      Api.chat.activeResponse(),
    ]);
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
        const _staleTail = _amA
          && !_amA._epIteration && !_amA._isEndpointReview && !_amA._isEndpointPlanner
          && assistantTailIsPriorTurn(_amA, conv.activeTaskId);
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

      /* Case D: No activeTaskId, no running server task — reconcile a ghost
         trailing assistant message left behind by an interrupted turn
         (only for locally-loaded convs, not server-only shells, and never
         while a stream is live for this conv — Cases A/B/C already consumed
         active-task convs, but assert it explicitly as belt-and-suspenders). */
      /* ★ Phase 3: DEFER to the backend when it already reconciled this conv.
       *   recover_stale_tasks_on_startup runs the SAME sweep + tail
       *   classification server-side (lib/conversations/reconcile.py) and
       *   persists it in one commit, then stamps settings._reconciledAt (→
       *   conv._reconciledAt). When present, the frontend must NOT re-infer
       *   lifecycle state (the separation-of-concerns directive) — the DB the
       *   frontend just loaded is already clean. This is what makes the
       *   resurrect + auto-fire regressions structurally impossible on the
       *   crash-recovery path: no frontend pop / allowTruncate PUT happens. */
      /* Case D: RETIRED (2026-07-11). The trailing-assistant ghost verdict —
       *   'delete' an empty husk, 'interrupt'-stamp a thinking-only husk — is
       *   now applied ENTIRELY server-side by
       *   lib/conversations/reconcile.py::reconcile_conversation_messages, which
       *   both persists the cleaned list AND stamps settings._reconciledAt in
       *   the SAME commit. That reconcile runs on EVERY render path the client
       *   can take: the single-conv GET (_reconcile_conv_on_get_blocking), the
       *   ?meta=1&prefetch= path (_prefetch_reconciled_dict — the last bypass,
       *   closed 2026-07-11), and startup recovery. So any idle conv the
       *   frontend renders has already had the identical verdict applied +
       *   _reconciledAt set — the JS `_classifyGhostTail` belt (and its
       *   non-destructive interrupt stamp) is now dead code and is removed.
       *   Backend/frontend equivalence proven by
       *   tests/test_reconcile_js_backend_equivalence.py; the prefetch bypass
       *   closure by tests/test_prefetch_path_reconcile.py. */

      /* Case E: orphaned trailing user message.
         The old frontend logic INFERRED an orphan from an age<5min heuristic
         (on messages OR a stale _needsLoad shell's settings.lastMsgRole) and
         then AUTO-STARTED a billed LLM turn behind a 3s setTimeout. That was a
         frontend lifecycle inference driving a costly, hard-to-reverse action
         — and on a stale shell it could DOUBLE-ANSWER (fire a second turn on
         top of an answer that already existed but whose metadata still said
         trailing-user).

         That auto-fire is GONE and NOT replaced: an orphaned user turn is
         simply left unanswered until the user re-sends. There is NO
         auto-dispatch and NO age heuristic here anymore — the race class is
         eliminated, not managed. Nothing to do in this loop. */
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

              /* ★ Server-authoritative merge (separation-of-concerns): the
                 local-vs-server winner is decided by the SERVER-ISSUED task
                 STATUS, never by a frontend content-length compare. A `done`
                 poll is a settled TERMINAL verdict — the persisted tail IS the
                 single source of truth, so adopt it VERBATIM even when a stale
                 local buffer happens to be longer. The old
                 `localContentLen > serverContentLen` referee let a stale-longer
                 local win and then re-PUT over fresh server truth (the same
                 "looks-longer wins" data-conflict class as the retired
                 wall-clock tiebreaker). Keep-longer-local survives ONLY as an
                 OFFLINE RESCUE when the task did NOT cleanly settle
                 (interrupted / server crash): the local SSE buffer may hold
                 un-acked stream content the checkpoint never captured. */
              if (am && am.role === "assistant") {
                const _serverSettled = td.status === 'done';
                /* ★ P1b flicker guard: if the tail is ALREADY settled (carries a
                 *   finishReason from a prior recovery pass or a different task),
                 *   a Case-B poll snapshot may only be adopted when it strictly
                 *   grows the content — otherwise the competing SSE-cold vs poll
                 *   folds swap the displayed text back and forth on every reload.
                 *   Also blocks a snapshot from a foreign task rewriting a
                 *   settled tail. A live tail (no finishReason) is untouched. */
                const _clobberSettled = (typeof pollWriteWouldClobberSettledTail === 'function')
                  && pollWriteWouldClobberSettledTail(am, conv.activeTaskId, td);
                if (td.content) {
                  if (_clobberSettled) {
                    console.info(`[initActiveTasks CaseB] settled-tail write suppressed (flicker guard) — ` +
                      `conv=${conv.id.slice(0,8)} finishReason=${am.finishReason} localLen=${localContentLen} serverLen=${serverContentLen}`);
                  } else if (!_serverSettled && localContentLen > serverContentLen) {
                    console.warn(`[initActiveTasks CaseB] ⚠️ KEEPING LOCAL content (${localContentLen} > server ${serverContentLen}, status=${td.status||'?'}) — task not cleanly settled, local SSE buffer may hold un-acked content`);
                  } else {
                    am.content = td.content;
                  }
                }
                if (td.thinking) {
                  if (!_serverSettled && localThinkingLen > serverThinkingLen) {
                    console.warn(`[initActiveTasks CaseB] ⚠️ KEEPING LOCAL thinking (${localThinkingLen} > server ${serverThinkingLen}, status=${td.status||'?'}) — task not cleanly settled`);
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
          let _cleared = false;
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
                    if (serverLast.usage) am.usage = serverLast.usage;
                    if (serverLast.model) am.model = serverLast.model;
                    if (serverLast.modifiedFiles) am.modifiedFiles = serverLast.modifiedFiles;
                    if (serverLast.modifiedFileList) am.modifiedFileList = serverLast.modifiedFileList;
                    _cleared = true;
                  }
                  /* ★ AC3: adopt the server's REAL terminal reason regardless of
                   *   content length. The badge on the bubble is the visible
                   *   residue — if the server settled the turn (e.g. 'stop'),
                   *   the stale 'server_offline' badge must be REPLACED by it,
                   *   not merely have its error text wiped. */
                  if (serverLast.finishReason && serverLast.finishReason !== 'server_offline'
                      && am.finishReason === 'server_offline') {
                    am.finishReason = serverLast.finishReason;
                    if (serverLast.usage) am.usage = serverLast.usage;
                    _cleared = true;
                  }
                }
              }
            }
          } catch (e) {
            console.debug(`[initActiveTasks CaseF] Server fetch failed for conv=${conv.id.slice(0,8)}: ${e.message}`);
          }
          // Clear the misleading error text — server is online now.
          if (am.error && errorEnvelopeKind(am.error) === 'server_offline') {
            delete am.error;
            _cleared = true;
          }
          /* ★ AC3: if the server had no fresher finishReason to adopt but the
           *   bubble is STILL stuck on the frontend-only 'server_offline' badge
           *   (server is provably back — we just fetched /api/chat/active), drop
           *   the badge so no "connection lost / server offline" residue lingers
           *   on a bubble whose answer is already complete. */
          if (am.finishReason === 'server_offline') {
            console.info(
              `[initActiveTasks CaseF] conv=${conv.id.slice(0,8)}: dropping stale server_offline badge ` +
              `(server back online, content=${(am.content?.length||0)}chars)`
            );
            delete am.finishReason;
            _cleared = true;
          }
          if (_cleared) {
            saveConversations(null);  // clearing stale offline residue — not new activity, don't bump updatedAt
            syncConversationToServer(conv);
          }
        }));
      }
    }

    /* ── Case E: no auto-dispatch. Deleting the old 3s setTimeout +
     *   startAssistantResponse auto-fire is the fundamental fix — a billed turn
     *   is never minted from a client-side inference. An orphaned user turn is
     *   left unanswered until the user re-sends. ── */
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
      /* ★ Don't clobber an in-flight first-open skeleton. When boot restored the
       *   last conversation from cache BEFORE the server round-trip (see
       *   main.js), an idle restored conv may still be mid initial-switch-load
       *   (`_initialSwitchLoad` set, messages not yet arrived) with the shimmer
       *   skeleton painted. A full renderChat here would repaint the generic
       *   "Loading conversation…" welcome over that skeleton — a downgrade
       *   flash before loadConversationMessages lands the real messages. Leave
       *   the skeleton up; the load's own `.then` paints the messages. */
      if (c && !(c._initialSwitchLoad && c._needsLoad && c.messages.length === 0)) {
        renderChat(c);
      }
    }
    // ★ Restore server-side queue state (survives page refresh)
    _refreshServerQueue(activeConvId);
  }
}
