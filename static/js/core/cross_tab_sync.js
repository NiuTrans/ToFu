/* ═══════════════════════════════════════════════════════════════════
   core/cross_tab_sync.js — extracted from core.js (split 2026-05-28)

   Cross-tab broadcast (BroadcastChannel) + offline-recovery polling.

   This file is concatenated by lib/js_bundler.py AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* ★ Remove a conversation locally in response to a REMOTE delete signal —
 *   shared by the same-machine BroadcastChannel (`conv_deleted`) path and the
 *   cross-device `notify` push (`conv_deleted` frame). Aborts any live stream,
 *   drops the IDB cache entry, splices it out of the in-memory list, and
 *   navigates away if it was the active conv. Idempotent (no-op if unknown). */
function _applyRemoteConvDeleted(id) {
  if (!id) return;
  try { ConvCache.remove(id); } catch (e) { /* cache miss is fine */ }
  const s = activeStreams.get(id);
  if (s) {
    try { s.controller.abort(); } catch (e) { /* already aborted */ }
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
}

/* ══════════════════════════════════════════════════════════════════════
   Event-driven cross-DEVICE sync (2026-07-08)

   The server emits a tiny `notify` push frame on EVERY authoritative
   conversation mutation (task-result save, PUT, rename, folder/settings,
   message delete/edit, conversation delete) — see
   lib/conversations/meta_cache.py::notify_conv_changed. This is the real-time
   replacement for "reconcile only on refocus or the 25s poll": a sibling
   device now reconciles the instant a change lands, no manual refresh.

   The frame is a targeting HINT, not the data:
     { type:'conv_changed'|'conv_deleted', convId, rev?, userId }

   Handling rules (the four robustness items):
   • rev-GATE + self-echo: a `conv_changed` whose `rev` is <= the conv's known
     `_serverRev` is a no-op — this is exactly what makes the ORIGINATING
     device's own echo free (it already advanced `_serverRev` from the PUT/GET
     response). Only a strictly-newer rev triggers a body refetch. A metadata-
     only change (rename/folder → rev omitted) never carries rev, so it routes
     to a debounced sidebar refresh instead of a body refetch.
   • TARGETED, not a full list re-pull: a known conv with a newer rev refetches
     ONLY that conv's body (loadConversationMessages); an UNKNOWN conv (created
     on another device) or a metadata-only change triggers ONE debounced
     `loadConversationsFromServer()` so a busy conv can't spam the list endpoint.
   • conv_deleted: reuse `_applyRemoteConvDeleted`.
   • multi-user forward-safety: ignore any frame whose `userId` is not ours
     (best-effort — `window._currentUserId` when auth lands; absent today = the
     single-user default, so every frame is ours).
   ══════════════════════════════════════════════════════════════════════ */
/* Self-echo window: a `conv_changed` for a conv this device just PUT within
 *   this many ms is treated as our own backend task-save echo and skipped (the
 *   two-writer race — see the marker in syncConversationToServer). */
const _CONV_SELF_ECHO_MS = 6000;
/* Active-conv verify debounce: wait this long before a NON-DESTRUCTIVE server
 *   verify of the OPEN conv, so our own finishStream PUT lands first and the
 *   frame collapses to a rev-gate no-op instead of a spurious repaint. */
const _CONV_ACTIVE_VERIFY_DELAY_MS = 1000;
let _convActiveVerifyTimer = 0;
let _convNotifyListRefreshTimer = 0;
function _scheduleConvListRefresh() {
  clearTimeout(_convNotifyListRefreshTimer);
  _convNotifyListRefreshTimer = setTimeout(() => {
    /* Same idle guard as the conv_saved cross-tab refresh: never clobber a
     *   live stream or an in-progress edit; share the boot-load latch. */
    if (
      document.visibilityState === "visible" &&
      activeStreams.size === 0 &&
      _editingMsgIdx === null &&
      !window._bootLoadInFlight
    ) {
      loadConversationsFromServer();
    }
  }, 400);
}

/* ── NON-DESTRUCTIVE active-conv verify for the notify path ──
 *   Called (debounced) when the OPEN conversation gets a genuinely-newer-rev
 *   `conv_changed` frame. It fetches the authoritative server copy and adopts
 *   in place, covering BOTH cases the reported symptom needs:
 *
 *   (1) server has MORE messages  → adopt the full set (a new turn landed).
 *   (2) SAME message count but the trailing assistant turn GREW in place
 *       (filled-in / regenerated / continued, or persisted short then extended
 *       by the backend task-save). `forceRecoverFromServer` MISSES this — its
 *       guard is `serverMsgs.length > localMsgs.length` only, so a same-count
 *       content-extend leaves the viewing device on a stale/empty bubble until
 *       a manual refresh. We mirror the equal-count `serverContentLen >
 *       localContentLen` in-place adopt that `_recoverOfflineConversations`
 *       already does for the offline lane.
 *
 *   NON-DESTRUCTIVE contract: update the trailing turn's fields in place (never
 *   `conv.messages = cache`), keep-longer (never clobber a longer local content
 *   — the two-writer race can make local briefly longer), advance `_serverRev`
 *   from the authoritative GET on EVERY outcome (so a no-op frame doesn't
 *   re-verify), and re-render ONLY when something actually changed (so scroll
 *   position is never reset on a no-op). */
async function _verifyActiveConvFromServer(convId) {
  const conv = conversations.find((c) => c.id === convId);
  if (!conv) return;
  let data;
  try {
    data = await Api.conversations.get(convId);
  } catch (e) {
    debugLog(`[conv-notify] active verify GET failed: ${e && e.message}`, "warn");
    return;
  }
  if (!data) return;
  const serverMsgs = data.messages || [];
  const localMsgs = conv.messages || [];
  let changed = false;

  if (serverMsgs.length > localMsgs.length) {
    /* Case 1: server has new messages — adopt the full set. */
    conv.messages = serverMsgs;
    conv.title = data.title || conv.title;
    conv.updatedAt = data.updatedAt || data.updated_at || conv.updatedAt;
    conv._serverMsgCount = serverMsgs.length;
    changed = true;
  } else if (serverMsgs.length === localMsgs.length && serverMsgs.length > 0) {
    /* Case 2: equal count — did the trailing assistant turn grow in place? */
    const serverLast = serverMsgs[serverMsgs.length - 1];
    const am = localMsgs[localMsgs.length - 1];
    if (serverLast && am && serverLast.role === "assistant" && am.role === "assistant") {
      const sc = serverLast.content?.length || 0, lc = am.content?.length || 0;
      const st = serverLast.thinking?.length || 0, lt = am.thinking?.length || 0;
      const sr = Array.isArray(serverLast.toolRounds) ? serverLast.toolRounds.length : 0;
      const lr = Array.isArray(am.toolRounds) ? am.toolRounds.length : 0;
      /* Adopt when the server's trailing turn is genuinely larger in ANY of
       *   content / thinking / toolRounds, AND is NOT shorter in content
       *   (keep-longer: never shrink a locally-longer answer). */
      if ((sc > lc || st > lt || sr > lr) && sc >= lc) {
        am.content = serverLast.content;
        if (st >= lt && serverLast.thinking !== undefined) am.thinking = serverLast.thinking;
        if (sr >= lr && serverLast.toolRounds) am.toolRounds = serverLast.toolRounds;
        if (serverLast.usage) am.usage = serverLast.usage;
        if (serverLast.model) am.model = serverLast.model;
        if (serverLast.finishReason) am.finishReason = serverLast.finishReason;
        if (serverLast.modifiedFiles) am.modifiedFiles = serverLast.modifiedFiles;
        if (serverLast.modifiedFileList) am.modifiedFileList = serverLast.modifiedFileList;
        conv.updatedAt = data.updatedAt || data.updated_at || conv.updatedAt;
        changed = true;
      }
    }
  }

  /* Advance the CAS base from the authoritative GET on EVERY outcome — even a
   *   no-op — so this frame's rev is now "known" and won't re-trigger. */
  if (typeof data.rev === "number") conv._serverRev = data.rev;

  if (changed) {
    conv._needsLoad = false;
    const keepPinned = conv.pinned, keepPinnedAt = conv.pinnedAt;
    _applySettingsToConv(conv, data.settings);
    conv.pinned = keepPinned; conv.pinnedAt = keepPinnedAt;
    saveConversations(convId);
    try { ConvCache.put(conv); } catch (e) { debugLog(`[conv-notify] cache put skipped: ${e && e.message}`, "warn"); }
    if (activeConvId === convId) {
      renderChat(conv, false);
      if (typeof _restoreConvToolState === "function") _restoreConvToolState(conv);
    }
  }
}

function _onConvNotifyPush(frame) {
  try {
    if (!frame) return;
    const type = frame.type;
    if (type !== "conv_changed" && type !== "conv_deleted") return;
    /* Multi-user gate (forward-safe): drop a frame for another user. When no
     *   user identity is established (single-user today) every frame is ours. */
    const myUser = (typeof window._currentUserId !== "undefined" && window._currentUserId !== null)
      ? window._currentUserId : null;
    if (myUser !== null && frame.userId !== undefined && frame.userId !== myUser) return;

    const convId = frame.convId;
    if (!convId) return;

    if (type === "conv_deleted") {
      _applyRemoteConvDeleted(convId);
      return;
    }

    const conv = conversations.find((c) => c.id === convId);
    const frameRev = (typeof frame.rev === "number") ? frame.rev : null;

    /* Unknown conv (created elsewhere) → a debounced list refresh discovers it. */
    if (!conv) { _scheduleConvListRefresh(); return; }

    /* rev-GATE / self-echo skip: a content change we already have (our own
     *   echo, or an older/equal rev) is a no-op. */
    if (frameRev !== null) {
      const known = (typeof conv._serverRev === "number") ? conv._serverRev : -1;
      if (frameRev <= known) return;   // stale / self-echo → cheap no-op
    } else {
      /* Metadata-only change (rename / folder / pin / activeTaskId): no rev
       *   bump → refresh the sidebar list (title/order), not the body. */
      _scheduleConvListRefresh();
      return;
    }

    /* A genuinely-newer content rev. Never disturb a conv the user is actively
     *   streaming or editing — its own SSE/poll lifecycle owns the update. */
    if (activeStreams.has(convId)) return;
    if (activeConvId === convId && _editingMsgIdx !== null) return;

    /* ── SELF-ECHO fast-path (the two-writer race) ──
     *   A completed turn has TWO independent server writers: the backend
     *   task-save (which emits this notify frame) AND this device's own
     *   finishStream PUT. The backend's frame can arrive BEFORE our PUT's
     *   response advances `_serverRev`, so the rev-gate above can't yet see it
     *   as our own. If we just wrote this conv locally, treat a frame in that
     *   window as our echo and skip — our PUT is the authoritative sync. */
    if (conv._localWriteAt && (Date.now() - conv._localWriteAt) < _CONV_SELF_ECHO_MS) return;

    if (activeConvId === convId) {
      /* ── ACTIVE conv: NON-DESTRUCTIVE verify, DEBOUNCED ──
       *   Never call loadConversationMessages here — its Phase-1 replaces
       *   conv.messages with the (possibly stale/empty) IndexedDB cache and does
       *   a full renderChat, which flashes an empty bubble and resets scroll
       *   position on every turn. Instead debounce ~1s (so our own PUT can land
       *   and turn this into a rev-gate no-op) then adopt via the unified
       *   _verifyActiveConvFromServer — which covers BOTH a new message AND a
       *   trailing turn that GREW in place at the same message count (the case
       *   forceRecoverFromServer's count-only guard misses). It re-renders ONLY
       *   when something actually changed. */
      clearTimeout(_convActiveVerifyTimer);
      const _pendingRev = frameRev;
      _convActiveVerifyTimer = setTimeout(() => {
        const c = conversations.find((x) => x.id === convId);
        if (!c || activeConvId !== convId) return;
        if (activeStreams.has(convId) || _editingMsgIdx !== null) return;
        /* Our own PUT (or a prior verify) may have already advanced _serverRev
         *   past this frame → nothing new; silent no-op (kills the self-echo). */
        if (typeof c._serverRev === "number" && _pendingRev !== null && _pendingRev <= c._serverRev) return;
        if (c._localWriteAt && (Date.now() - c._localWriteAt) < _CONV_SELF_ECHO_MS) return;
        _verifyActiveConvFromServer(convId).catch((e) =>
          debugLog(`[conv-notify] active verify failed: ${e && e.message}`, "warn"));
      }, _CONV_ACTIVE_VERIFY_DELAY_MS);
    } else {
      /* Background conv: mark stale so its NEXT open re-fetches from server
       *   (loadConversationMessages early-returns unless _needsLoad), and nudge
       *   the sidebar so metadata carried alongside the change (title/updatedAt/
       *   order) is reflected without opening it. Never repaints the viewport. */
      conv._needsLoad = true;
      conv._serverMsgCount = Math.max(conv._serverMsgCount || 0, (conv.messages || []).length);
      _scheduleConvListRefresh();
    }
  } catch (e) {
    debugLog(`[conv-notify] handler error: ${e && e.message}`, "warn");
  }
}

/* ══════════════════════════════════════════════════════════════════════
   Event-driven cross-DEVICE folder sync (2026-07-09)

   Folders live in a SEPARATE per-install store (data/config/folders.json), not
   the conversations table, so they don't ride the conv_changed rev signal. The
   server now emits a dedicated `folders_changed` frame on the SAME `notify`
   channel (routes/api_v1/folders.py::_notify_folders_changed) on every folder
   mutation (create / rename / recolor / collapse / reorder / delete):

       { type:'folders_changed', deletedFolderId?, userId }

   `_onFoldersChangedPush` consumes it, mirroring the conversation-sync pattern:
   • DELETE (deletedFolderId present) → unassign local conversations off the
     removed folder ON THIS DEVICE (the clicking device did it client-side in
     deleteFolder(); this makes every OTHER device reconcile too), then reload
     the folder tree.
   • create / rename / reorder → DEBOUNCED reload of the folder list in place
     (loadFolders re-renders the sidebar), so bursts collapse to one fetch.
   Idle-safe: reload is gated the same way as the conv list refresh (visible,
   not editing) so it never repaints over an in-progress edit. */
let _foldersRefreshTimer = 0;
function _scheduleFoldersRefresh() {
  clearTimeout(_foldersRefreshTimer);
  _foldersRefreshTimer = setTimeout(() => {
    if (
      document.visibilityState === "visible" &&
      _editingMsgIdx === null &&
      typeof loadFolders === "function"
    ) {
      Promise.resolve(loadFolders()).catch((e) =>
        debugLog(`[folders-notify] reload failed: ${e && e.message}`, "warn"));
    }
  }, 400);
}

function _onFoldersChangedPush(frame) {
  try {
    if (!frame || frame.type !== "folders_changed") return;
    /* Multi-user gate (forward-safe): drop a frame for another user. When no
     *   user identity is established (single-user today) every frame is ours. */
    const myUser = (typeof window._currentUserId !== "undefined" && window._currentUserId !== null)
      ? window._currentUserId : null;
    if (myUser !== null && frame.userId !== undefined && frame.userId !== myUser) return;

    const deletedId = frame.deletedFolderId;
    if (deletedId) {
      /* A folder was deleted elsewhere → reconcile local conversations that
       *   still reference it (the deleting device already did this via
       *   deleteFolder()'s client-side unassign; here we replay it so a SECOND
       *   device drops the stale folderId too). Metadata-only, in place. */
      let touched = false;
      for (const c of conversations) {
        if (c && c.folderId === deletedId) {
          c.folderId = null;
          touched = true;
          try { if (typeof ConvCache !== "undefined") ConvCache.put(c); }
          catch (e) { debugLog(`[folders-notify] cache put skipped: ${e && e.message}`, "warn"); }
        }
      }
      if (touched && typeof saveConversations === "function") saveConversations(null);
      /* Drop it from the in-memory folder array immediately so the tree
       *   doesn't flash the dead folder before loadFolders() returns. */
      if (typeof getFolders === "function") {
        const arr = getFolders();
        if (Array.isArray(arr)) {
          const i = arr.findIndex((f) => f && f.id === deletedId);
          if (i >= 0) arr.splice(i, 1);
        }
      }
      if (typeof renderConversationList === "function") renderConversationList();
    }
    /* Reload the authoritative folder list in place (covers create / rename /
     *   recolor / collapse / reorder, and refreshes after a delete). */
    _scheduleFoldersRefresh();
  } catch (e) {
    debugLog(`[folders-notify] handler error: ${e && e.message}`, "warn");
  }
}
if (typeof window !== "undefined") window._onFoldersChangedPush = _onFoldersChangedPush;

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
      _applyRemoteConvDeleted(msg.convId);
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
    /* ★ Re-attempt durable pending-sync messages (poor-network send failures
     *   carried across a reload) now that the tab is focused. */
    if (typeof _flushPendingSyncs === 'function') _flushPendingSyncs('visibilitychange');
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
  /* ★ Also re-attempt any message whose send failed on a poor network and
   *   whose rescue PUT never landed (durable _pendingSync markers). */
  if (typeof _flushPendingSyncs === 'function') _flushPendingSyncs('online_event');
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
    // But if such a conv ALSO carries a stale server_offline error envelope
    // (server is reachable now, since the health check above passed), clear it
    // so the bubble doesn't keep a misleading "Server Offline" badge on top of
    // the (correct) interrupted verdict.
    if (_interruptedOnlyIds.has(conv.id)) {
      if (am.error && errorEnvelopeKind(am.error) === 'server_offline') {
        delete am.error;
        saveConversations(conv.id);
        try { ConvCache.put(conv); } catch (e) { console.debug(`[NetworkRecovery] ConvCache.put failed: ${e && e.message}`); }
        if (activeConvId === conv.id && typeof renderChat === 'function') renderChat(conv);
      }
      return;
    }
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
    // Guarded t() — zh primary; literal fallback keeps jsdom harnesses safe.
    const _tt = (typeof t === 'function')
      ? t
      : (k, p) => ({
          'conn.restoredTitle': '连接已恢复',
          'conn.restoredReattach': '已重连 ' + (p && p.n) + ' 个进行中的任务，流式已恢复。',
          'conn.restoredRecovered': '已从服务器恢复 ' + (p && p.n) + ' 个对话，结果已更新。',
        }[k] || k);
    if (_reattachedCount > 0) {
      showToast('🔄', _tt('conn.restoredTitle'),
        _tt('conn.restoredReattach', { n: _reattachedCount }), 6000);
    }
    if (recovered > 0) {
      showToast('🔄', _tt('conn.restoredTitle'),
        _tt('conn.restoredRecovered', { n: recovered }), 6000);
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

/* ★ Cross-device reconciliation poll (2026-07-05)
 *
 *   The conversation list is reconciled with the server by PULL only — there
 *   is no cross-DEVICE push channel (BroadcastChannel is same-machine only;
 *   Epic B push fan-out is §10-gated/parked). So two devices (e.g. a phone
 *   over a flaky VS Code port-forward and a desktop) drift apart and only
 *   self-heal when the stale device's tab REGAINS focus while idle
 *   (`visibilitychange` above). A phone left visible-but-untouched never
 *   re-pulls in the normal case: the only other list-refresh interval
 *   (`_startOfflineRecoveryPolling`) is gated on the presence of
 *   `server_offline` convs and no-ops otherwise.
 *
 *   This interval closes that gap: while the tab is VISIBLE and fully IDLE it
 *   re-pulls the list on a fixed cadence, so a device catches a sibling's
 *   changes without needing a refocus. It reuses `loadConversationsFromServer`
 *   verbatim — the id-keyed merge and its 304 / count-drop / allowTruncate
 *   guards already prevent a stale device from truncating fresher server
 *   state, so a periodic re-pull can only ADD/UPDATE, never clobber.
 *
 *   Idle guard is byte-identical to the `conv_saved` cross-tab refresh above
 *   (visible AND no active stream AND not editing) so it can never fire over a
 *   live stream or an in-progress edit. It shares the window-scoped
 *   `_bootLoadInFlight` latch with the boot-reconnect backoff and the 60s
 *   main.js refresh timer, so overlapping loads are impossible. Backgrounded
 *   tabs cost nothing — the visibility guard short-circuits before any fetch.
 */
/**
 * Conv-agnostic sweep for STALE ``activeTaskId`` pins whose backend task is no
 * longer running — the "sidebar busy dot outlives the work" root cause.
 *
 * A conversation can hold ``conv.activeTaskId`` while having NO ``activeStreams``
 * entry (its SSE finished/dropped, or the pin was set on load without a live
 * reconnect). If the task then dies WITHOUT cleanly finalizing, the server-side
 * reaper (reap_stuck_running_tasks) flips it terminal + drops it from the
 * in-memory registry, so ``/api/v1/chat/active`` STOPS reporting it running.
 * But nothing on the client clears the pin: the stream timer's self-heal is
 * gated on both a live ``_streamTimers`` entry AND ``activeConvId === convId``,
 * so a BACKGROUND orphan is never evaluated → ``convIsBusy`` stays true → the
 * sidebar dot never clears until a manual refresh.
 *
 * This sweep closes that lane. It is deliberately NOT ``activeConvId``-gated and
 * NOT ``_streamTimers``-gated — it inspects EVERY conversation. It reuses the
 * SAME ``/api/v1/chat/active`` probe the offline-recovery path already issues (no
 * new polling loop) and the SAME ``_healStuckPlaceholder`` reclaim mechanism (no
 * second system). A pin is cleared ONLY when the task is CONFIRMED absent from
 * the running set — never on a probe failure (fail-safe: a transient ``/active``
 * error leaves every pin untouched, retried next sweep).
 *
 * @param {Array} activeTasks — the parsed ``/api/v1/chat/active`` array.
 * @returns {number} count of stale pins reconciled.
 */
function _reconcileStuckActiveTaskPins(activeTasks) {
  if (!Array.isArray(activeTasks)) return 0;  // probe failed → touch nothing
  const _running = new Set();
  for (const t of activeTasks) {
    if (t && t.id && t.status === 'running' && !t.aborted) _running.add(t.id);
  }
  let cleared = 0;
  for (const conv of conversations) {
    const taskId = conv && conv.activeTaskId;
    if (!taskId) continue;
    // A live stream in THIS tab owns its own SSE/poll lifecycle + the stream
    // timer's foreground self-heal — never race it.
    if (activeStreams.has(conv.id)) continue;
    // Only reclaim when the backend CONFIRMS the task is not running. A task
    // still in the running set is legitimately slow/alive — leave it (the
    // server reaper is the sole authority on "wedged", per its dual-clock gate).
    if (_running.has(taskId)) continue;
    if (typeof _healStuckPlaceholder === 'function'
        && _healStuckPlaceholder(conv.id, { background: true })) {
      cleared++;
    }
  }
  if (cleared > 0) {
    console.warn(`[StalePinSweep] cleared ${cleared} stale activeTaskId pin(s) — backend task(s) no longer running`);
  }
  return cleared;
}

/* Poll cadence. The periodic re-pull is now the FALLBACK behind the
 *   event-driven `notify` push (see _wireConvSyncPush): when the push socket is
 *   connected the server tells us the instant anything changes, so a tight 25s
 *   poll is wasteful. We keep polling as the safety net for when the WebSocket
 *   is DOWN (tunnel drop / half-open), but stretch the interval while it's UP.
 *   The interval fn re-reads this each tick via _reconcileIntervalMs(). */
const _RECONCILE_MS_PUSH_UP = 90000;   // 90s — push carries the load; poll is a backstop
const _RECONCILE_MS_PUSH_DOWN = 25000; // 25s — WS down: poll is the only path (prior default)
function _reconcileIntervalMs() {
  try {
    if (typeof pushIsConnected === 'function' && pushIsConnected())
      return _RECONCILE_MS_PUSH_UP;
  } catch (e) { /* fall through to the safe short cadence */ }
  return _RECONCILE_MS_PUSH_DOWN;
}
function _crossDeviceReconcile() {
  /* Same idle guard as the conv_saved refresh — never clobber a live stream
   *   or an in-progress edit. This is now the SOLE visible-idle list
   *   reconciler (the divergent 60s main.js timer, which omitted the
   *   activeStreams guard, was consolidated into this one). */
  if (
    document.visibilityState !== "visible" ||
    activeStreams.size !== 0 ||
    _editingMsgIdx !== null
  )
    return false;
  /* Share the boot-reconnect in-flight latch so no two timers can issue
   *   overlapping loads through a flaky tunnel. */
  if (window._bootLoadInFlight) return false;
  /* Yield to pending input: the merge + conv-list rebuild is ~tens of ms of
   *   main-thread work; running it on a bare timer adds input delay (poor INP)
   *   when a click lands mid-poll. requestIdleCallback defers it until the main
   *   thread is free. Fallback to a plain call where rIC is unavailable.
   *   (Preserved from the folded-in 60s timer.) */
  const _run = () => {
    if (window._bootLoadInFlight) return;
    window._bootLoadInFlight = true;
    Promise.resolve(loadConversationsFromServer())
      .catch((e) => debugLog(`[cross-device-reconcile] ${e && e.message}`, "warn"))
      .finally(() => { window._bootLoadInFlight = false; });
    /* ★ Conv-agnostic stale-pin sweep — piggyback the SAME /api/v1/chat/active
     *   probe (no new polling loop). Reconcile every conv whose activeTaskId is
     *   pinned but whose backend task no longer runs (reaped/finished), so a
     *   BACKGROUND orphan's sidebar busy-dot clears without a refresh. Runs
     *   independently of the list-load promise above (its own catch); a probe
     *   failure touches nothing (fail-safe). */
    Promise.resolve(Api.chat.active({ signal: AbortSignal.timeout(8000) }))
      .then((activeTasks) => {
        if (typeof _reconcileStuckActiveTaskPins === 'function') {
          _reconcileStuckActiveTaskPins(activeTasks);
        }
      })
      .catch((e) => debugLog(`[stale-pin-sweep] active() probe failed: ${e && e.message}`, "warn"));
  };
  if (typeof requestIdleCallback === "function")
    requestIdleCallback(_run, { timeout: 5000 });
  else
    _run();
  return true;
}
/* Self-rescheduling reconcile: re-arm each tick at the cadence that matches
 *   the current push-socket state (long when push is up, short when it's down).
 *   Using a recursive setTimeout (not a fixed setInterval) lets the fallback
 *   tighten the instant the WebSocket drops, and relax again when it recovers. */
function _scheduleNextReconcile() {
  setTimeout(() => {
    try { _crossDeviceReconcile(); }
    catch (e) { debugLog(`[cross-device-reconcile] tick error: ${e && e.message}`, "warn"); }
    _scheduleNextReconcile();
  }, _reconcileIntervalMs());
}
_scheduleNextReconcile();

/* ★ Wire the event-driven cross-device sync subscription. Called from main.js
 *   boot AFTER push.js has defined pushSubscribe (this file is bundled BEFORE
 *   push.js, so pushSubscribe is undefined at load time — do NOT subscribe at
 *   IIFE load). Idempotent. The 'notify' channel is a fleet signal; the handler
 *   itself does the per-conv rev-gate + multi-user scoping. */
let _convSyncPushWired = false;
function _wireConvSyncPush() {
  if (_convSyncPushWired) return;
  if (typeof pushSubscribe !== "function") return;
  _convSyncPushWired = true;
  pushSubscribe("notify", "*", (frame) => {
    if (frame && (frame.type === "conv_changed" || frame.type === "conv_deleted"))
      _onConvNotifyPush(frame);
    else if (frame && frame.type === "folders_changed")
      _onFoldersChangedPush(frame);
  });
  debugLog("[conv-notify] ✓ cross-device sync push subscription wired", "info");
}
if (typeof window !== "undefined") window._wireConvSyncPush = _wireConvSyncPush;

