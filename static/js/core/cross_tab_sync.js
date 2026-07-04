/* ═══════════════════════════════════════════════════════════════════
   core/cross_tab_sync.js — extracted from core.js (split 2026-05-28)

   Cross-tab broadcast (BroadcastChannel) + offline-recovery polling.

   This file is concatenated by lib/js_bundler.py AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

function _broadcastToTabs(type, extra) {
  if (!_syncChannel) return;
  try {
    _syncChannel.postMessage({ type, sourceTab: TAB_ID, ...(extra || {}) });
  } catch (e) {
    debugLog(`[broadcastToTabs] ${e.message}`, 'warn');
  }
}
let _crossTabMergeTimer = 0;
function _handleCrossTabMsg(msg) {
  switch (msg.type) {
    case "conv_saved":
      /* ★ Cross-tab: another tab saved a conversation → refresh from server
       *   to pick up the new/updated conversation metadata. */
      clearTimeout(_crossTabMergeTimer);
      _crossTabMergeTimer = setTimeout(() => {
        if (
          document.visibilityState === "visible" &&
          activeStreams.size === 0 &&
          _editingMsgIdx === null
        )
          loadConversationsFromServer();
      }, 600);
      break;
    case "conv_deleted": {
      const id = msg.convId;
      if (!id) return;
      /* ★ Remove from IndexedDB cache in this tab too */
      ConvCache.remove(id);
      const s = activeStreams.get(id);
      if (s) {
        s.controller.abort();
        activeStreams.delete(id);
      }
      const idx = conversations.findIndex((c) => c.id === id);
      if (idx !== -1) {
        conversations.splice(idx, 1);
        if (activeConvId === id) {
          if (conversations.length > 0) loadConversation(conversations[0].id);
          else newChat();
        } else renderConversationList();
      }
      break;
    }
    case "conv_restored": {
      /* ★ Another tab undid a deletion → refresh from server to pick the
       *   re-created conversation back up (mirrors conv_saved). */
      clearTimeout(_crossTabMergeTimer);
      _crossTabMergeTimer = setTimeout(() => {
        if (
          document.visibilityState === "visible" &&
          activeStreams.size === 0 &&
          _editingMsgIdx === null
        )
          loadConversationsFromServer();
      }, 600);
      break;
    }
  }
}
/* ★ No longer listening to localStorage 'storage' events for conversations.
 *   Cross-tab sync now uses BroadcastChannel → server refresh only. */
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    /* ★ PERF: When switching back to the tab during active streaming,
     *   immediately flush a render from the current buffer.  Even though
     *   the setTimeout fallback keeps rendering in background tabs, the
     *   browser throttles it to ~1s intervals.  This ensures the UI is
     *   fully caught up the instant the user sees the tab. */
    if (activeStreams.size > 0 && activeConvId && streamBufs.has(activeConvId)) {
      /* ★ Message-checkpoint fallback (see _streamFrameArg in
       *   health_stream_timer.js): switching a background tab back into a
       *   mid-stream conv whose buffer hasn't been seeded yet must render the
       *   persisted checkpoint, not paint "等待中…" over it (same class of wipe
       *   the _twFlush raw-buffer read caused). */
      const arg = (typeof _streamFrameArg === 'function')
        ? _streamFrameArg(activeConvId) : null;
      if (arg) {
        updateStreamingUI(arg);
        scrollToBottom();
      }
    } else if (activeStreams.size === 0 && _editingMsgIdx === null) {
      loadConversationsFromServer();
    }
    /* ★ Network recovery: when the tab becomes visible (e.g. after VSCode
     *   reconnects and user switches to the browser), recover any conversations
     *   that were marked server_offline while the connection was down. */
    _recoverOfflineConversations('visibilitychange');
  }
});

/* ★ Network recovery listeners: detect when connectivity is restored
 *   (e.g. VSCode port forwarding re-established) and recover conversations
 *   that were marked server_offline during the outage. The 'online' event
 *   fires when the browser detects network connectivity is back, but for
 *   tunneled connections (VS Code), we also use a periodic health check. */
window.addEventListener('online', () => {
  console.info('[NetworkRecovery] 🌐 Browser "online" event fired — checking for offline conversations to recover');
  _recoverOfflineConversations('online_event');
});

/** Debounce guard for _recoverOfflineConversations */
let _lastOfflineRecoveryAttempt = 0;
const _OFFLINE_RECOVERY_COOLDOWN = 5000; // ms — don't run more than once per 5s

/**
 * Re-attach to a server task that is STILL RUNNING after a tunnel drop
 * (case a: the Codelab/VS Code port-forward died but `server.py` and its
 * daemon worker thread are alive).  Instead of a static content adopt, we
 * clear the frontend-only `server_offline` verdict on the trailing assistant
 * message and re-bind the live SSE stream to that SAME bubble.
 *
 * Why clear the markers first: `connectToTask`'s stale-tail guard pushes a
 * fresh empty placeholder whenever the last assistant carries a
 * `finishReason` (or a foreign `_taskId`).  On an offline reconnect that
 * produced the reported bug — the previous turn froze tagged "Server
 * Offline" while a new bubble appeared holding only the tool panel.  By
 * stripping the offline verdict and aligning `_taskId`, the guard reuses the
 * existing message and `connectToTask` pre-populates its partial
 * content + toolRounds, so streaming resumes seamlessly in place.
 *
 * @returns {boolean} true if a live re-attach was initiated.
 */
function _reattachLiveOfflineTask(conv, task) {
  const am = conv.messages[conv.messages.length - 1];
  if (!am || am.role !== 'assistant') return false;
  console.warn(
    `[NetworkRecovery] ▶ Live re-attach — conv=${conv.id.slice(0,8)} ` +
    `task=${task.id.slice(0,8)} still RUNNING on server; resuming SSE in place`
  );
  // Drop the frontend-only offline/interrupted verdict so connectToTask's
  // stale-tail guard reuses THIS message instead of spawning a ghost placeholder.
  if (am.finishReason === 'server_offline' || am.finishReason === 'interrupted') delete am.finishReason;
  if (am.error && errorEnvelopeKind(am.error) === 'server_offline') delete am.error;
  // Align the message + conv with the live task id (guard checks _taskId).
  am._taskId = task.id;
  conv.activeTaskId = task.id;
  delete conv._activeTaskClearedAt;  // allow reattach (see settings restore guard)
  saveConversations(conv.id);
  try { ConvCache.put(conv); } catch (e) { console.debug(`[NetworkRecovery] ConvCache.put failed: ${e && e.message}`); }
  if (activeConvId === conv.id) renderChat(conv);
  renderConversationList();
  connectToTask(conv.id, task.id);
  return true;
}

/**
 * Recover conversations marked with finishReason='server_offline'.
 * Runs Case-F-style recovery without requiring a full page reload.
 * Called from: visibilitychange, online event, periodic health check,
 * and the manual "Reconnect" button.
 *
 * @param {string} trigger - What triggered this recovery (for logging)
 * @returns {Promise<number>} Number of conversations recovered
 */
async function _recoverOfflineConversations(trigger) {
  const now = Date.now();
  if (now - _lastOfflineRecoveryAttempt < _OFFLINE_RECOVERY_COOLDOWN) return 0;
  _lastOfflineRecoveryAttempt = now;

  // Find conversations needing recovery:
  //  • server_offline → connection drop; reattach-if-running else static-adopt.
  //  • interrupted    → a still-running task can be transiently mislabeled
  //    'interrupted' by the racy "task not in memory" poll check
  //    (routes/chat.py). Treat it as a reattach candidate, but ONLY act when
  //    Api.chat.active() confirms the task is still running — otherwise it is a
  //    genuine crash checkpoint whose recovery path is Case B, so we leave it
  //    untouched (tracked via _interruptedOnlyIds and skipped before static-adopt).
  const offlineConvs = [];
  const _interruptedOnlyIds = new Set();
  for (const conv of conversations) {
    if (conv._needsLoad) continue;
    const last = conv.messages[conv.messages.length - 1];
    if (!last || last.role !== 'assistant') continue;
    if (last.finishReason === 'server_offline') {
      offlineConvs.push(conv);
    } else if (last.finishReason === 'interrupted') {
      offlineConvs.push(conv);
      _interruptedOnlyIds.add(conv.id);
    }
  }
  if (offlineConvs.length === 0) return 0;

  // First check if server is actually reachable
  try {
    const healthResp = await Api.health.check({ signal: AbortSignal.timeout(5000) });
    if (!healthResp || !healthResp.ok) return 0;
  } catch {
    return 0; // Server still unreachable — don't recover yet
  }

  console.warn(
    `[NetworkRecovery] ★ Recovering ${offlineConvs.length} server_offline conversation(s) — trigger=${trigger}`
  );

  /* ★ Live re-attach probe: if any offline conv's task is STILL RUNNING on
   *   the server (tunnel dropped, but server.py + worker thread alive), we
   *   re-bind the live SSE stream in place rather than statically adopting a
   *   frozen snapshot. This is the seamless-resume path (case a). */
  let _activeTasks = null;
  try {
    _activeTasks = await Api.chat.active({ signal: AbortSignal.timeout(8000) });
  } catch (e) {
    console.debug(`[NetworkRecovery] active() probe failed (will static-adopt): ${e && e.message}`);
  }
  const _runningByConv = new Map();
  if (Array.isArray(_activeTasks)) {
    for (const t of _activeTasks) {
      if (t && t.convId && t.status === 'running' && !t.aborted) _runningByConv.set(t.convId, t);
    }
  }

  let recovered = 0;
  const _reattachedIds = new Set();  // convs re-bound to a live stream (NOT static-adopted)
  await Promise.all(offlineConvs.map(async (conv) => {
    const am = conv.messages[conv.messages.length - 1];
    const localContentLen = am.content?.length || 0;
    // ★ Seamless live re-attach takes priority over static adopt.
    if (!activeStreams.has(conv.id)) {
      const liveTask = _runningByConv.get(conv.id);
      if (liveTask && _reattachLiveOfflineTask(conv, liveTask)) {
        _reattachedIds.add(conv.id);
        return;
      }
    }
    // Interrupted convs with no live task stay as-is — their recovery is the
    // crash-checkpoint path (Case B), not the server_offline static adopt.
    if (_interruptedOnlyIds.has(conv.id)) return;
    try {
      const data = await Api.conversations.get(conv.id, { signal: AbortSignal.timeout(10000) });
      if (!data) return;
      const serverMsgs = data.messages || [];
      if (serverMsgs.length === 0) return;
      const serverLast = serverMsgs[serverMsgs.length - 1];
      if (!serverLast || serverLast.role !== 'assistant') return;

      const serverContentLen = serverLast.content?.length || 0;
      const serverFinish = serverLast.finishReason || '';
      let changed = false;

      // Adopt server version if it has more content (task completed after frontend gave up)
      if (serverContentLen > localContentLen) {
        console.warn(
          `[NetworkRecovery] conv=${conv.id.slice(0,8)}: server has MORE content ` +
          `(${serverContentLen} > local ${localContentLen}) — adopting server version`
        );
        am.content = serverLast.content;
        if (serverLast.thinking) am.thinking = serverLast.thinking;
        if (serverLast.toolRounds) am.toolRounds = serverLast.toolRounds;
        if (serverLast.usage) am.usage = serverLast.usage;
        if (serverLast.model) am.model = serverLast.model;
        if (serverLast.modifiedFiles) am.modifiedFiles = serverLast.modifiedFiles;
        if (serverLast.modifiedFileList) am.modifiedFileList = serverLast.modifiedFileList;
        changed = true;
      }
      // ★ Fix: adopt server's finishReason even when content lengths are equal.
      //   The frontend may have captured all streaming tokens before the disconnect
      //   but the server has the proper finishReason (e.g. 'stop') while local is
      //   stuck on 'server_offline'. Without this, recovery never completes and
      //   the periodic polling runs forever.
      if (serverFinish && serverFinish !== 'server_offline' && am.finishReason === 'server_offline') {
        am.finishReason = serverFinish;
        if (serverLast.usage) am.usage = serverLast.usage;
        changed = true;
      }
      // Clear the misleading error text — server is online now
      if (am.error && errorEnvelopeKind(am.error) === 'server_offline') {
        delete am.error;
        changed = true;
      }
      // ★ Only persist and count as recovered when something actually changed.
      //   Avoids false toasts and prevents pushing stale server_offline back
      //   to the server when nothing was updated.
      if (changed) {
        saveConversations(conv.id);
        ConvCache.put(conv);
        recovered++;
      }
    } catch (e) {
      console.debug(`[NetworkRecovery] Server fetch failed for conv=${conv.id.slice(0,8)}: ${e.message}`);
    }
  }));

  const _reattachedCount = _reattachedIds.size;
  if (recovered > 0 || _reattachedCount > 0) {
    if (_reattachedCount > 0) {
      showToast('🔄', 'Connection Restored',
        `Reconnected ${_reattachedCount} running task(s) — streaming resumed.`, 6000);
    }
    if (recovered > 0) {
      showToast('🔄', 'Connection Restored',
        `Recovered ${recovered} conversation(s) from server. Results updated.`, 6000);
    }
    // Re-render active conversation if it was STATICALLY recovered.  Skip
    // live-reattached convs — _reattachLiveOfflineTask already created the
    // streaming bubble via connectToTask; a renderChat here would destroy it.
    if (activeConvId && !_reattachedIds.has(activeConvId)) {
      const activeConv = conversations.find(c => c.id === activeConvId);
      if (activeConv && offlineConvs.includes(activeConv)) {
        renderChat(activeConv);
      }
    }
    renderConversationList();
  }
  // Count live-reattached convs as recovered for the periodic-poll stop check.
  return recovered + _reattachedCount;
}

/* ★ Periodic background recovery: when VSCode tunnel drops, the browser's
 *   'online' event may not fire (the machine is still online, just the tunnel
 *   is down). This interval checks every 15s if we have server_offline convs
 *   and the server is reachable, triggering recovery automatically.
 *   Only runs when there are offline conversations to recover. */
let _offlineRecoveryInterval = null;
function _startOfflineRecoveryPolling() {
  if (_offlineRecoveryInterval) return; // already running
  _offlineRecoveryInterval = setInterval(async () => {
    // Check if there are any offline conversations to recover
    const hasOffline = conversations.some(c => {
      if (c._needsLoad) return false;
      const last = c.messages[c.messages.length - 1];
      return last && last.role === 'assistant' && last.finishReason === 'server_offline';
    });
    if (!hasOffline) {
      // No more offline conversations — stop polling
      clearInterval(_offlineRecoveryInterval);
      _offlineRecoveryInterval = null;
      console.debug('[NetworkRecovery] No more offline conversations — stopping recovery polling');
      return;
    }
    // Only check when tab is visible (avoid wasting resources in background)
    if (document.visibilityState !== 'visible') return;
    const recovered = await _recoverOfflineConversations('periodic_check');
    if (recovered > 0) {
      // Check if all offline conversations are now recovered
      const stillOffline = conversations.some(c => {
        if (c._needsLoad) return false;
        const last = c.messages[c.messages.length - 1];
        return last && last.role === 'assistant' && last.finishReason === 'server_offline';
      });
      if (!stillOffline) {
        clearInterval(_offlineRecoveryInterval);
        _offlineRecoveryInterval = null;
      }
    }
  }, 15000); // Check every 15 seconds
}

