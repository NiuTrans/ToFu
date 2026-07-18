/* ═══════════════════════════════════════════════════════════════════
   main conv lifecycle — extracted from main.js (split 2026-05-28)

   Conversation lifecycle: newChat, loadConversation, deleteConversation, duplicateConversation, _build* config helpers.

   This file is concatenated by lib/js_bundler.py BEFORE main.js so
   the boot IIFE can reference these symbols. Symbols share `window`
   scope — no imports / exports needed.
   ═══════════════════════════════════════════════════════════════════ */

function newChat() {
  _purgeEmptyConvs();
  const prevConv = getActiveConv();
  if (prevConv) {
    prevConv.model = config.model || serverModel;
    prevConv.thinkingDepth = config.thinkingDepth;
    prevConv.searchMode = searchMode || "multi";
    prevConv.fetchEnabled = !!fetchEnabled;
    prevConv.codeExecEnabled = !!codeExecEnabled;
    prevConv.browserEnabled = !!browserEnabled;
    prevConv.memoryEnabled = !!memoryEnabled;
    prevConv.swarmEnabled = !!swarmEnabled;
    prevConv.endpointEnabled = !!endpointEnabled;
    prevConv.autopilotEnabled = !!autopilotEnabled;
    prevConv.activeFlow = activeFlow || '';
    prevConv.imageGenEnabled = !!imageGenEnabled;
    if (imageGenMode) {
      prevConv.imageGenAspect = _igSelectedAspect || '1:1';
      prevConv.imageGenResolution = _igSelectedResolution || '1K';
    }
    prevConv.autoTranslate = !!autoTranslate;
    /* ★ FIX: Save projectPath for the previous conv so it doesn't lose its
     * project association.  This mirrors the same sync logic in _saveConvToolState
     * — without it, prevConv.projectPath could be undefined for convs that were
     * never explicitly set via the project modal (e.g. inherited from projectState). */
    if (projectState.active && projectState.path) {
      prevConv.projectPath = projectState.path;
    }
  }
  const hasInput =
    document.getElementById("userInput").value.trim() ||
    pendingImages.length > 0 ||
    pendingPdfTexts.length > 0 ||
    (_pendingLogClean && _pendingLogClean.originalText);
  activeConvId = null;
  sessionStorage.removeItem('tofu_activeConvId');
  _lastRenderedFingerprint = "";
  /* ★ Show folder context in topbar when creating a new chat from folder view */
  const _newChatFolderId = typeof getActiveFolderId === 'function' ? getActiveFolderId() : null;
  const _newChatFolder = _newChatFolderId && typeof getFolderById === 'function' ? getFolderById(_newChatFolderId) : null;
  const topbarEl = document.getElementById("topbarTitle");
  if (_newChatFolder) {
    topbarEl.innerHTML = `New Chat <span class="topbar-folder-badge" style="color:${_newChatFolder.color || 'var(--text-tertiary)'}">● ${escapeHtml(_newChatFolder.name)}</span>`;
  } else {
    topbarEl.textContent = "New Chat";
  }
  renderConversationList();
  if (typeof clearDebug === "function") clearDebug();
  /* ★ Welcome screen: show folder indicator when new chat will be assigned to a folder */
  const _folderBadgeHtml = _newChatFolder
    ? `<div class="welcome-folder-badge"><span class="welcome-folder-dot" style="color:${_newChatFolder.color || '#888'}">●</span> ${escapeHtml(_newChatFolder.name)}</div>`
    : '';
  document.getElementById("chatInner").innerHTML =
    `<div class="welcome" id="welcome"><div class="welcome-icon"><img src="${BASE_PATH}/static/icons/tofu-welcome.svg" alt="Tofu" width="64" height="64"></div><h2 class="tofu-brand"><span class="tofu-brand-t">T</span><span class="tofu-brand-o1">o</span><span class="tofu-brand-f">f</span><span class="tofu-brand-u">u</span><small>豆腐</small></h2>${_folderBadgeHtml}<div class="feature-pills">${_welcomePillsHtml()}</div></div>`;
  buildTurnNav(null);
  renderPendingQueueUI(null);
  // ★ A brand-new conversation has no latch — hide any lingering banner.
  if (typeof syncToolsetBanner === 'function') syncToolsetBanner();
  updateSendButton();
  if (!hasInput) {
    _clearProjectStateLocal();
    _resetToolsToDefaults();
  }
  if (typeof updateContextBar === 'function') updateContextBar();
}
/* ★ Reconnect-on-open — the root-cause fix for "click into a conversation →
 *   stuck / stalled bubble that only a full page refresh clears".
 *
 *   loadConversation historically re-attached ONLY a stream already live in
 *   THIS tab (activeStreams). It never reconnected to a task still RUNNING on
 *   the SERVER when this tab holds no stream entry — the task was started in
 *   another tab, or the SSE dropped and finishStream cleared activeStreams while
 *   the backend kept generating. The conversation then rendered STATICALLY with
 *   no SSE, no poll and no twStart, so the trailing assistant placeholder sat
 *   frozen ("等待中…") until a full refresh ran initActiveTasks' reconnect.
 *
 *   The reconnect decision keys off the SERVER-AUTHORITATIVE conv.activeTaskId
 *   (persisted settings.activeTaskId) — never an inferred client guess — and
 *   delegates to the existing connectToTask, the single reconnect mechanism used
 *   by boot init / send / regen / edit / cross-tab. connectToTask resolves its
 *   accumulation slot by identity (_taskId / _msgId), so it re-targets the
 *   running task's already-persisted placeholder instead of appending a second
 *   assistant bubble, and it self-heals a stale activeTaskId for an
 *   already-finished task via its poll → 404 → finishStream path (no permanent
 *   placeholder, no hang).
 *
 *   Idempotent: a no-op when a stream is already live in this tab (the caller's
 *   activeStreams.has branch handles that) and connectToTask itself re-guards on
 *   !activeStreams.has(convId). Returns true when it kicked off a reconnect so
 *   the caller skips the static-render fall-through.
 */
function _reconnectServerTaskIfIdle(id) {
  if (typeof activeStreams === 'undefined' || activeStreams.has(id)) return false;
  const conv = conversations.find((x) => x.id === id);
  if (!conv || !conv.activeTaskId) return false;
  if (typeof connectToTask !== 'function') return false;
  console.info(
    `[loadConversation] 🔗 Reconnect-on-open — conv=${id.slice(0,8)} ` +
    `activeTaskId=${conv.activeTaskId.slice(0,8)} (no live stream in this tab, ` +
    `task running server-side)`
  );
  connectToTask(id, conv.activeTaskId);
  /* connectToTask synchronously sets the activeStreams entry + arms twStart
   * (before its first await), then inserts the streaming bubble. Repaint the
   * statics + the single streaming bubble exactly like boot's _ensureNewest so
   * the click-open paint matches a fresh reconnect (showStreamingUIForConv
   * slices the streaming bubble off the static list — no duplicate). */
  if (activeStreams.has(id) && typeof showStreamingUIForConv === 'function') {
    showStreamingUIForConv(id);
  }
  return true;
}
/* ★ "Click an old conversation → float it to the top" (durable).
 *
 * The sidebar sorts recency-first on `updatedAt` (_convSorter), and opening a
 * conversation historically did NOT touch `updatedAt` — so an old conv stayed
 * buried after you opened it. This bumps `updatedAt = now` on open and persists
 * it via a lightweight settings PATCH (touchUpdatedAt flag → server bumps the
 * `updated_at` column), so the float-to-top SURVIVES a page reload.
 *
 * GUARD (mirrors saveConversations): NEVER bump while the conversation has a
 * live/active task (`activeStreams.has(id) || conv.activeTaskId`). During
 * streaming, saveConversations already deliberately withholds the `updatedAt`
 * bump to stop sidebar flicker under the throttled refresh — an open-bump here
 * would fight that and reintroduce the flicker. Opening a streaming conv still
 * floats via _convSorter's active-first tier anyway.
 *
 * Called ONLY from the genuine user sidebar-click path (main.js), NOT from
 * programmatic opens (boot restore, undo, duplicate) which pass through
 * loadConversation directly — merely restoring/creating a conv must not
 * rewrite its recency.
 */
function _bumpConvOnOpen(id) {
  const conv = conversations.find((c) => c.id === id);
  if (!conv) return;
  // Active-task guard — see saveConversations' streaming carve-out.
  if (activeStreams.has(id) || conv.activeTaskId) return;
  conv.updatedAt = Date.now();
  // Re-sort + repaint the sidebar so the row floats up immediately.
  conversations.sort(_convSorter);
  if (typeof renderConversationList === 'function') renderConversationList();
  // Write-through to the IDB cache so a reload replays the new recency before
  // the server list arrives (keeps the float-to-top from flashing back).
  if (typeof ConvCache !== 'undefined') {
    try { ConvCache.put(conv); } catch (_e) { /* best-effort */ }
  }
  // Persist to the server so the new order is durable across reloads. The
  // settings PATCH carries ONLY the touchUpdatedAt control flag (no settings
  // keys) → the endpoint bumps `updated_at` without writing the settings blob.
  if (typeof Api !== 'undefined' && Api.conversations && Api.conversations.patchSettings) {
    Api.conversations.patchSettings(id, { touchUpdatedAt: true })
      .catch((e) => console.warn('[bumpConvOnOpen] PATCH failed:', e && e.message));
  }
}
function loadConversation(id) {
  _sendGeneration++;           // ★ invalidate any in-flight sendMessage
  _purgeEmptyConvs();
  _editingMsgIdx = null;
  _lastRenderedFingerprint = "";
  // ── Exit branch mode when switching conversations ──
  if (typeof closeBranchPanel === "function" && typeof isBranchModeActive === "function" && isBranchModeActive()) {
    closeBranchPanel();
  }
  /* ★ PERF: Snapshot the outgoing conv's tool state into its in-memory object
   * (cheap property copies), but DEFER the expensive syncConversationToServer
   * (JSON.stringify of all messages + network PUT) to AFTER the new conv renders.
   * Previously this ran synchronously before any rendering, adding 50-500ms+ of
   * JSON serialization time before the user saw any visual change. */
  const prevConv = getActiveConv();
  let _needsDeferredSave = false;
  if (prevConv && prevConv.id !== id) {
    delete prevConv._initialSwitchLoad;   // ★ clear stale flag from previous conv
    prevConv.model = config.model || serverModel;
    prevConv.thinkingDepth = config.thinkingDepth;
    prevConv.searchMode = searchMode || "multi";
    prevConv.fetchEnabled = !!fetchEnabled;
    prevConv.codeExecEnabled = !!codeExecEnabled;
    prevConv.browserEnabled = !!browserEnabled;
    prevConv.desktopEnabled = !!desktopEnabled;
    prevConv.memoryEnabled = !!memoryEnabled;
    prevConv.schedulerEnabled = !!schedulerEnabled;
    prevConv.swarmEnabled = !!swarmEnabled;
    prevConv.endpointEnabled = !!endpointEnabled;
    prevConv.autopilotEnabled = !!autopilotEnabled;
    prevConv.activeFlow = activeFlow || '';
    prevConv.imageGenEnabled = !!imageGenEnabled;
    prevConv.imageGenMode = !!imageGenMode;
    prevConv.humanGuidanceEnabled = !!humanGuidanceEnabled;
    if (imageGenMode) {
      prevConv.imageGenModel = _igSelectedModel || 'gemini-3.1-flash-image-preview';
      prevConv.imageGenCount = _igSelectedCount || 1;
      prevConv.imageGenAspect = _igSelectedAspect || '1:1';
      prevConv.imageGenResolution = _igSelectedResolution || '1K';
    }
    const _prevTaskActive = !!(prevConv.activeTaskId || activeStreams.has(prevConv.id));
    if (!_prevTaskActive) {
      prevConv.autoTranslate = !!autoTranslate;
    }
    if (projectState.active && projectState.path) {
      prevConv.projectPath = projectState.path;
      const allPaths = [projectState.path];
      if (projectState.extraRoots?.length) {
        for (const r of projectState.extraRoots) {
          const p = typeof r === 'string' ? r : r.path;
          if (p && !allPaths.includes(p)) allPaths.push(p);
        }
      }
      prevConv.projectPaths = allPaths;
    }
    _needsDeferredSave = true;
  }
  activeConvId = id;
  sessionStorage.setItem('tofu_activeConvId', id);
  /* ★ Reset the open-scroll latch for THIS open. Opening a conversation does
   *   NOT auto-scroll (owner directive): the first full render leaves the view
   *   at its natural post-render position and latches _openScrollConvId; later
   *   same-open renders (Phase-2 reconcile, the .then fallback) then take the
   *   anchor-preserve branch and HOLD that position instead of re-snapping —
   *   see renderChat. */
  if (typeof _openScrollConvId !== 'undefined') _openScrollConvId = null;
  /* ★ If loading a conv that doesn't belong to the active folder view, exit it */
  if (typeof getActiveFolderId === 'function' && getActiveFolderId()) {
    const _loadedConv = conversations.find(c => c.id === id);
    if (_loadedConv && _loadedConv.folderId !== getActiveFolderId()) {
      setActiveFolderId(null);
    }
  }
  if (typeof closeBranchPanel === "function") closeBranchPanel();
  const c = conversations.find((x) => x.id === id);
  if (!c) return;
  document.getElementById("topbarTitle").textContent = c.title;
  /* ★ PERF: Use fast-path O(1) active-class swap instead of O(N) full sidebar rebuild.
   * Full renderConversationList() is only needed when the conv isn't in the DOM yet
   * (e.g. newly created conversation). The fast path just moves the CSS .active class
   * between two existing DOM elements — zero HTML generation, zero innerHTML. */
  if (!_swapActiveConvItem(id)) {
    renderConversationList();
  }

  /* ── On-demand message loading for server-only conversations ── */
  if (c._needsLoad) {
    c._initialSwitchLoad = true;   // ★ flag for renderChat: use full-render, not surgical
    /* ★ Epic ②: first-open skeleton. Paint title + N shimmer bubbles IMMEDIATELY
     *   from the mirror's msgCount (_serverMsgCount) so opening an unopened conv
     *   never blanks / freezes the previous conv under the reader during the
     *   up-to-15s server round-trip on a flaky tunnel. The skeleton's DOM mirrors
     *   the real message structure (zero-CLS swap), and its bubbles are keyed
     *   `skeleton-msg-*` (not `msg-*`) so renderChat's surgical probe treats the
     *   first real render as a full wipe.
     *
     *   Only when we KNOW there's content to load (_serverMsgCount>0). An empty
     *   conv (count 0) shows nothing here — its render is the welcome/empty
     *   state. A cache HIT inside loadConversationMessages repaints in a few ms
     *   (over the skeleton); a slow server path leaves the skeleton up until the
     *   body arrives (or the failure UI replaces it with a Retry — see
     *   loadConversationMessages' timeout/404 branches). */
    const _skMsgCount = c._serverMsgCount || 0;
    if (_skMsgCount > 0 && activeConvId === id
        && typeof renderSkeletonChat === 'function') {
      renderSkeletonChat(c, _skMsgCount);
    }
    loadConversationMessages(id).then(() => {
      const stillExists = conversations.find(x => x.id === id);
      if (!stillExists) return;
      if (activeConvId === id) {
        if (activeStreams.has(id)) {
          showStreamingUIForConv(id);
        } else if (_reconnectServerTaskIfIdle(id)) {
          /* ★ Task still running server-side (persisted activeTaskId) but no
           *   live stream in this tab → reconnect instead of static-rendering a
           *   frozen placeholder. connectToTask + showStreamingUIForConv already
           *   painted; nothing more to do here. */
        } else if (c._needsLoad || c.messages.length === 0) {
          renderChat(c);
          if (typeof _restoreConvToolState === "function") _restoreConvToolState(c);
        }
        /* ★ No-auto-scroll-on-OPEN (owner directive): the former trailing
         *   `_forceScrollToBottom` fallback here (for an already-loaded cached
         *   conv) is removed. The Phase-1/Phase-2 renders inside
         *   loadConversationMessages already painted the conversation; per the
         *   directive we leave the view at its natural position instead of
         *   snapping it to the bottom on open. */
      }
      delete c._initialSwitchLoad;   // ★ clear flag after initial load completes
      if (!activeStreams.has(id)) _resumePendingTranslations(id);
    });
  } else if (activeStreams.has(id)) {
    showStreamingUIForConv(id);
  } else if (_reconnectServerTaskIfIdle(id)) {
    /* ★ Reconnect-on-open (already-loaded conv): persisted activeTaskId points
     *   at a task still running server-side, but this tab holds no stream →
     *   connectToTask re-attaches (self-healing to poll/finishStream if the task
     *   already finished) instead of leaving a static, frozen placeholder. */
  } else {
    renderChat(c);
    _resumePendingTranslations(id);
  }

  renderPendingQueueUI(id);
  // ★ Refresh server queue state for this conversation
  _refreshServerQueue(id);
  // ★ The tool-schema "apply now" banner is per-conversation — re-evaluate it
  //   for the conv we just switched to so it never lingers from another conv.
  if (typeof syncToolsetBanner === 'function') syncToolsetBanner();
  updateSendButton();
  if (typeof restoreDebugForConv === "function") restoreDebugForConv(id);
  const inp = document.getElementById("userInput"),
    hasInput =
      (inp && inp.value.trim().length > 0) ||
      pendingImages.length > 0 ||
      pendingPdfTexts.length > 0;
  _restoreConvProject(c);
  if (!hasInput) {
    if (!c._needsLoad) _restoreConvToolState(c);
  }

  /* ★ PERF: Deferred save — serialize & sync the outgoing conversation AFTER
   * the new conversation is fully rendered and interactive.  This moves the
   * expensive JSON.stringify + fetch PUT off the critical rendering path.
   * Using setTimeout(0) ensures it runs after the current call stack AND after
   * the browser has painted the new conversation. */
  if (_needsDeferredSave && prevConv) {
    const pc = prevConv;
    setTimeout(() => {
      /* ★ FIX: Pass null instead of pc.id — saving tool state on conversation
       * switch is a metadata-only change, NOT new conversation activity.
       * Passing pc.id would bump updatedAt = Date.now(), which makes the
       * outgoing conversation jump to the top of the sidebar even though
       * the user only viewed it without making any changes. */
      saveConversations(null);
      if (pc.messages && pc.messages.length > 0) {
        syncConversationToServer(pc);
      }
    }, 0);
  }
}
async function deleteConversation(id, e) {
  if (e && e.stopPropagation) e.stopPropagation();
  let conv = conversations.find((c) => c.id === id);
  if (!conv) return;

  /* ★ CRITICAL: undo re-creates the server row from a client-side snapshot, so
   *   that snapshot MUST hold the conversation's COMPLETE message history. Two
   *   ways it can be incomplete in memory:
   *     • a sidebar SHELL (`_needsLoad:true` / `messages:[]`) — history lives
   *       only on the server;
   *     • a WINDOWED open (`recordWindowState` loaded only the tail N and set
   *       `_serverMsgCount = totalCount`), so `messages.length < _serverMsgCount`
   *       — the oldest messages are absent locally.
   *   In BOTH cases snapshotting as-is and later restoring would re-create a
   *   conv missing its head — silent data loss. The single yardstick is
   *   "do we hold every message the server has?": `messages.length >=
   *   _serverMsgCount`. When we don't, materialise the full body BEFORE
   *   snapshotting, exactly like duplicateConversation. */
  const _serverTotal = () => (conv._serverMsgCount || 0);
  /* Local snapshot is missing history iff the server has messages we don't
   *   hold locally (covers both the empty-shell and windowed-tail cases). */
  const _snapshotIncomplete = () => _serverTotal() > 0 && conv.messages.length < _serverTotal();
  const _needsHydrate = !!conv._needsLoad || _snapshotIncomplete();
  /* When true, we could NOT capture a COMPLETE local snapshot (hydration failed
   *   on a flaky link, or a windowed body couldn't be completed), so the undo
   *   affordance can't faithfully restore history — the delete still proceeds
   *   (the server row is authoritative), but WITHOUT an undo toast, after
   *   explicit user consent below. */
  let _undoUnavailable = false;
  if (_needsHydrate) {
    try {
      await loadConversationMessages(id);
    } catch (err) {
      debugLog(`[deleteConv] hydrate-before-delete failed: ${err && err.message}`, 'warn');
    }
    /* Re-read: the conv may have been removed/replaced during the await. */
    conv = conversations.find((c) => c.id === id);
    if (!conv) return;
    /* ★ WINDOWED-TAIL COMPLETION: loadConversationMessages honours the default
     *   window (60), so on a long conv it leaves only the tail — `messages.length
     *   < _serverMsgCount`. Snapshotting that would let undo re-create a conv
     *   missing its oldest messages. Force a FULL (window=0) fetch to complete
     *   the body before we snapshot. (No-op when the standard load already
     *   returned everything, e.g. a short conv or a genuine empty-shell where
     *   the windowed fetch also came back empty.) */
    if (conv.messages.length > 0 && _snapshotIncomplete()) {
      try {
        const _full = await Api.conversations.get(id, { query: { window: '0' } });
        conv = conversations.find((c) => c.id === id);
        if (!conv) return;
        if (_full && Array.isArray(_full.messages) &&
            _full.messages.length >= conv.messages.length) {
          conv.messages = _full.messages;
          conv._serverMsgCount = _full.messages.length;
          /* The full array supersedes the windowed view — clear pagination
           *   state so the snapshot (and any subsequent render) treats it as
           *   the complete history, not a tail with more-above. */
          conv._windowed = false;
          conv._hasMoreEarlier = false;
          conv._trimmed = false;
          debugLog(`[deleteConv] completed windowed body for conv ${id.slice(0,8)} ` +
            `→ ${conv.messages.length} msgs before snapshot`, 'info');
        }
      } catch (err) {
        debugLog(`[deleteConv] full-body fetch before snapshot failed: ${err && err.message}`, 'warn');
      }
    }
    /* ★ SNAPSHOT STILL INCOMPLETE (server unreachable / load timed out → 0 msgs,
     *   OR a windowed tail we couldn't complete → messages.length < total). The
     *   DELETE itself is always safe — the server row is authoritative and needs
     *   no local body. What we'd lose is only a FAITHFUL undo snapshot.
     *   Historically we hard-refused the delete here, but that PERMANENTLY
     *   BLOCKS deleting a shell/long conv whenever the tunnel is slow (the
     *   reported "delete always fails" bug: windowed reads on + hundreds of
     *   shell convs → hydration frequently times out). Instead, ask for explicit
     *   consent, then delete WITHOUT the undo toast — fail-open-with-consent,
     *   not fail-closed. */
    if (_snapshotIncomplete()) {
      debugLog(`[deleteConv] incomplete snapshot for conv ${id.slice(0,8)} ` +
        `(${conv._serverMsgCount} server msgs, ${conv.messages.length} local); ` +
        `confirming delete-without-undo`, 'warn');
      const _confirmed = (typeof showConfirm === 'function')
        ? await showConfirm(t('sidebar.deleteNoUndoBody'), {
            title: t('sidebar.deleteNoUndoTitle'),
            okText: t('sidebar.deleteAnyway'),
            cancelText: t('folder.cancel'),
            danger: true,
          })
        : true;  /* no dialog available (headless/legacy) → proceed */
      if (!_confirmed) return;
      /* Re-read again: the user may have deliberated for a while and the conv
       *   could have been removed/replaced during the confirm await. */
      conv = conversations.find((c) => c.id === id);
      if (!conv) return;
      _undoUnavailable = true;
    }
  }

  const s = activeStreams.get(id);
  if (s) {
    s.controller.abort();
    activeStreams.delete(id);
  }

  /* ★ Snapshot AFTER hydration so it carries full messages → undo can re-create
   * the server row.  We capture a deep clone + the sidebar position + whether
   * it was active, so restore re-inserts it exactly where it was. */
  const snapshot = JSON.parse(JSON.stringify(conv));
  const origIndex = conversations.indexOf(conv);
  const wasActive = (activeConvId === id);

  if (conv.activeTaskId)
    Api.chat.abortTask(conv.activeTaskId)
      .catch(e => debugLog(`[deleteConv] abort failed: ${e.message}`, 'warn'));
  /* Server DELETE only AFTER the snapshot is complete. */
  Api.conversations.remove(id)
    .catch(e => debugLog(`[deleteConv] delete failed: ${e.message}`, 'warn'));
  /* ★ Remove from IndexedDB cache */
  ConvCache.remove(id);
  conversations = conversations.filter((c) => c.id !== id);
  _broadcastToTabs("conv_deleted", { convId: id });
  if (activeConvId === id) {
    if (conversations.length > 0) loadConversation(conversations[0].id);
    else newChat();
  } else renderConversationList();

  /* Only offer undo when we captured a restorable snapshot. When hydration
   *   failed (shell conv on a flaky link, user consented to delete-without-undo
   *   above), the snapshot is hollow — restoring it would re-create an EMPTY
   *   server row, so surface a plain "deleted" toast instead of a false undo. */
  if (_undoUnavailable) {
    if (typeof showToast === "function") showToast(t('sidebar.convDeleted'), 'success');
  } else {
    _showUndoDeleteToast(snapshot, origIndex, wasActive);
  }
}

/* ★ Restore a conversation deleted via deleteConversation. Re-inserts the
 * snapshot at its original sidebar position (clamped), re-caches it, and
 * re-creates the server row via the normal full-conv PUT. */
function _restoreDeletedConversation(snapshot, origIndex, wasActive) {
  if (!snapshot || !snapshot.id) return;
  // Guard: don't double-insert if the user mashed undo or it already came back.
  if (conversations.some(c => c.id === snapshot.id)) {
    if (wasActive) loadConversation(snapshot.id);
    else renderConversationList();
    return;
  }
  const restored = JSON.parse(JSON.stringify(snapshot));
  /* The snapshot already holds the full messages (deleteConversation hydrates
   * shell convs before snapshotting), so it's a complete in-memory conv —
   * clear the lazy-load flag and any transient streaming/task state. */
  delete restored._needsLoad;
  delete restored._initialSwitchLoad;
  delete restored.activeTaskId;
  delete restored._lastSyncMsgCount;
  /* ★ Preserve the ORIGINAL updatedAt so the restored conv keeps its place in
   * the sidebar instead of jumping to the top with the current time.
   * syncConversationToServer ships `conv.updatedAt || Date.now()`, so as long
   * as we don't bump it here (and we never call saveConversations(id), which
   * would), the original timestamp is what gets persisted. */
  restored.updatedAt = snapshot.updatedAt;
  const idx = (origIndex >= 0 && origIndex <= conversations.length)
    ? origIndex : 0;
  conversations.splice(idx, 0, restored);
  try { ConvCache.put(restored); } catch (_) { /* best-effort */ }
  /* Re-create the server row (full PUT). The snapshot is hydrated, so a conv
   * that had history always re-creates with its messages intact.  A genuinely
   * empty conv (zero messages, no server row) is restored in-memory only —
   * which is correct, the server never had a row for it either. */
  if (restored.messages && restored.messages.length > 0) {
    syncConversationToServer(restored).catch(err =>
      debugLog(`[deleteConv] restore sync failed: ${err && err.message}`, 'warn'));
  }
  _broadcastToTabs("conv_restored", { convId: restored.id });
  if (wasActive) loadConversation(restored.id);
  else renderConversationList();
  if (typeof showToast === "function") showToast(t('sidebar.convRestored'), 'success');
}

/* ★ Dedicated undo toast for conversation deletion. The generic showToast()
 * has no action-button affordance, so this builds a small toast with an Undo
 * button. Auto-dismisses after the timeout (deletion stands). */
function _showUndoDeleteToast(snapshot, origIndex, wasActive) {
  const c = document.getElementById('toastContainer');
  if (!c) return;
  const title = snapshot.title || 'Untitled';
  const el = document.createElement('div');
  el.className = 'toast t-info toast-undo';
  el.innerHTML =
    `<div class="toast-icon-wrap t-info"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></div>` +
    `<div class="toast-body">` +
      `<span class="toast-title">${escapeHtml(t('sidebar.convDeleted'))}</span>` +
      `<span class="toast-detail">${escapeHtml(title)}</span>` +
    `</div>` +
    `<button class="toast-undo-btn" type="button">${escapeHtml(t('sidebar.undoDelete'))}</button>` +
    `<div class="toast-progress t-info" style="width:100%;animation:toastTimer 6000ms linear forwards"></div>`;

  let timer, done = false;
  const dismiss = () => {
    if (done) return;
    done = true;
    el.classList.add('removing');
    setTimeout(() => el.remove(), 300);
  };
  el.querySelector('.toast-undo-btn').addEventListener('click', () => {
    if (done) return;
    done = true;
    clearTimeout(timer);
    el.remove();
    _restoreDeletedConversation(snapshot, origIndex, wasActive);
  });
  c.appendChild(el);
  timer = setTimeout(dismiss, 6000);
  /* Pause the countdown on hover so a deliberating user isn't rushed. */
  const prog = el.querySelector('.toast-progress');
  el.addEventListener('mouseenter', () => {
    clearTimeout(timer);
    if (prog) prog.style.animationPlayState = 'paused';
  });
  el.addEventListener('mouseleave', () => {
    if (prog) prog.style.animationPlayState = 'running';
    timer = setTimeout(dismiss, 2000);
  });
}

// ══════════════════════════════════════════════════════
// ★ Duplicate (copy) a conversation as a completely independent new conversation
// ══════════════════════════════════════════════════════
async function duplicateConversation(id, e) {
  if (e && e.stopPropagation) e.stopPropagation();
  const srcConv = conversations.find((c) => c.id === id);
  if (!srcConv) return;

  // If the source conversation hasn't been loaded from server yet, load it first
  if (srcConv._needsLoad) {
    try {
      await loadConversationMessages(srcConv.id);
    } catch (err) {
      console.warn(`[duplicateConv] Failed to load source conv: ${err.message}`);
      if (typeof showToast === "function") showToast("", t('convLifecycle.copyFailed'), t('convLifecycle.copyFailedBody'), 4000);
      return;
    }
  }

  const now = Date.now();
  const newId = generateId();

  // ★ PERF: Show toast immediately for instant feedback
  if (typeof showToast === "function") {
    showToast("", t('convLifecycle.copying'), t('convLifecycle.copyingBody', { title: srcConv.title }), 2000);
  }

  // ★ PERF: Defer heavy work (deep clone + serialize) to next frame
  // so the toast renders immediately without blocking
  requestAnimationFrame(() => {
    // Deep-clone messages, stripping runtime/streaming state
    const clonedMessages = JSON.parse(JSON.stringify(srcConv.messages || [])).map(msg => {
      delete msg._taskId;
      delete msg.activeTaskId;
      delete msg.approvalRequired;
      if (msg.branches) {
        for (const b of msg.branches) delete b.activeTaskId;
      }
      return msg;
    });

    const newConv = {
      id: newId,
      title: (srcConv.title || "Untitled") + t('convLifecycle.copySuffix'),
      messages: clonedMessages,
      createdAt: now,
      updatedAt: now,
      activeTaskId: null,
      ...(srcConv.projectPath ? { projectPath: srcConv.projectPath } : {}),
      ...(srcConv.projectPaths ? { projectPaths: [...srcConv.projectPaths] } : {}),
      ...(srcConv.model ? { model: srcConv.model } : {}),
      ...(srcConv.thinkingDepth !== undefined ? { thinkingDepth: srcConv.thinkingDepth } : {}),
      ...(srcConv.searchMode ? { searchMode: srcConv.searchMode } : {}),
      ...(srcConv.fetchEnabled !== undefined ? { fetchEnabled: srcConv.fetchEnabled } : {}),
      ...(srcConv.codeExecEnabled !== undefined ? { codeExecEnabled: srcConv.codeExecEnabled } : {}),
      ...(srcConv.browserEnabled !== undefined ? { browserEnabled: srcConv.browserEnabled } : {}),
      ...(srcConv.memoryEnabled !== undefined ? { memoryEnabled: srcConv.memoryEnabled } : {}),
      ...(srcConv.autoTranslate !== undefined ? { autoTranslate: srcConv.autoTranslate } : {}),
      ...(srcConv.folderId ? { folderId: srcConv.folderId } : {}),
    };

    // Add to conversation list (at top) & render sidebar
    conversations.unshift(newConv);
    saveConversations(newConv.id);

    // ★ PERF: Sync to server in background — don't block UI
    syncConversationToServer(newConv).catch(err => {
      console.warn('[duplicateConv] Background sync failed:', err);
    });

    // Switch to the new conversation
    loadConversation(newId);

    if (typeof showToast === "function") {
      showToast("", t('convLifecycle.copied'), t('convLifecycle.copiedBody', { title: srcConv.title }), 3000);
    }
    console.log(`[duplicateConv] Duplicated conv ${id.slice(0,8)} → ${newId.slice(0,8)} (${clonedMessages.length} msgs)`);
  });
}

// ══════════════════════════════════════════════════════
// ★ Rename a conversation — inline dialog + manual title edit
// ══════════════════════════════════════════════════════

/**
 * Apply a new title to a conversation: update in-memory state, the topbar
 * (if active), the sidebar, and persist title-only to the server. Marks the
 * conversation as user-titled so the auto-generator never overwrites it.
 */
function _applyConvTitle(conv, title) {
  conv.title = title;
  conv._titleEdited = true;          // ★ block auto-title from overwriting
  /* Pass null — a rename is a metadata-only change, NOT new activity, so we
   * don't want to bump updatedAt and reorder the sidebar. */
  saveConversations(null);
  if (activeConvId === conv.id) {
    const tb = document.getElementById("topbarTitle");
    if (tb) tb.textContent = title;
  }
  renderConversationList();
  if (typeof ConvCache !== "undefined") { try { ConvCache.put(conv); } catch (_) { /* best-effort */ } }
  Api.conversations.setTitle(conv.id, title)
    .catch(err => console.warn(`[renameConv] setTitle failed: ${err && err.message}`));
}

/** Show an inline dialog to rename a conversation (mirrors folder rename UX). */
function _promptRenameConversation(convId) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return;

  const existing = document.getElementById('_convRenameDialog');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = '_convRenameDialog';
  overlay.className = 'conv-rename-overlay';
  overlay.innerHTML = `
    <div class="conv-rename-card" role="dialog" aria-modal="true">
      <div class="conv-rename-head">
        <svg class="conv-rename-icon" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"></path><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"></path></svg>
        <span>${t('sidebar.renameConvTitle')}</span>
      </div>
      <input type="text" class="conv-rename-input" id="_convRenameInput"
             placeholder="${t('sidebar.renameConvPh')}" maxlength="60" autocomplete="off" spellcheck="false">
      <div class="conv-rename-actions">
        <button class="conv-rename-btn cancel" id="_convRenameCancel">${t('folder.cancel')}</button>
        <button class="conv-rename-btn ok" id="_convRenameOk">${t('folder.ok')}</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  const nameInput = document.getElementById('_convRenameInput');
  nameInput.value = conv.title || '';
  setTimeout(() => { nameInput.focus(); nameInput.select(); }, 50);

  function _close() { overlay.remove(); }

  function _submit() {
    const name = nameInput.value.trim();
    if (!name || name === conv.title) { _close(); return; }
    _applyConvTitle(conv, name);
    _close();
  }

  document.getElementById('_convRenameOk').addEventListener('click', _submit);
  document.getElementById('_convRenameCancel').addEventListener('click', _close);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) _close(); });
  nameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _submit(); }
    if (e.key === 'Escape') _close();
  });
}

/**
 * Auto-generate a conversation title after its first turn completes.
 * Fire-and-forget: skips when the user has manually edited the title, when
 * the conversation is too short, or when generation was already attempted.
 * On success, updates in-memory state + sidebar + topbar (the server has
 * already persisted the title as part of the generate-title endpoint).
 */
async function _maybeAutoGenerateTitle(convId) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return;
  /* Opt-in: only auto-generate when the user has enabled it in Settings.
   * Defaults to false — editors title conversations manually. */
  if (typeof config === 'undefined' || !config.autoGenerateTitle) return;
  if (conv._titleEdited || conv._titleAutoGenerated) return;
  const msgs = conv.messages || [];
  const hasUser = msgs.some(m => m.role === 'user');
  /* ★ Only fire on the FIRST assistant turn. The _title* flags are runtime-
   *   only (not persisted), so after a page reload they're gone; gating on
   *   "exactly one assistant message so far" ensures we never regenerate the
   *   title on later turns of a reloaded conversation — which would clobber a
   *   title the user manually set in a previous session. */
  const assistantCount = msgs.filter(m => m.role === 'assistant').length;
  if (!hasUser || assistantCount !== 1) return;
  conv._titleAutoGenerated = true;   // ★ attempt-once guard (set before await)
  try {
    const res = await Api.conversations.generateTitle(convId);
    if (!res || !res.title) return;
    // Re-find: the conv may have been deleted/replaced during the await.
    const fresh = conversations.find(c => c.id === convId);
    if (!fresh || fresh._titleEdited) return;   // user renamed mid-flight — respect it
    fresh.title = res.title;
    saveConversations(null);
    if (activeConvId === convId) {
      const tb = document.getElementById("topbarTitle");
      if (tb) tb.textContent = res.title;
    }
    renderConversationList();
    if (typeof ConvCache !== "undefined") { try { ConvCache.put(fresh); } catch (_) { /* best-effort */ } }
  } catch (err) {
    console.warn(`[autoTitle] generateTitle failed for ${convId.slice(0,8)}: ${err && err.message}`);
  }
}
if (typeof window !== 'undefined') {
  window._promptRenameConversation = _promptRenameConversation;
  window._maybeAutoGenerateTitle = _maybeAutoGenerateTitle;
}

// ══════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════
//  ★ Shared config/settings builders for /api/chat/send and /api/chat/regenerate
// ══════════════════════════════════════════════════════

// ── Conversation config + settings resolution (server-authoritative) ──
//
// Merge policy lives in `lib/conv_config.py`. Endpoints:
//   POST /api/v1/conversations/config/resolve   → 32-field runtime config
//   POST /api/v1/conversations/settings/resolve → 19-field stored settings
//
// JS sends only the inputs (per-conv stored fields + active overrides
// + server defaults); server merges and returns the canonical dict.
//
// Both functions are async and return a Promise<dict>. All callsites
// already live inside async functions, so adding `await` is the only
// change needed.

/**
 * Build the global-toolbar overrides dict — fields the user has
 * touched in the current session. Read from window-scoped globals.
 */
function _buildToolbarOverrides() {
  return {
    maxTokens: config.maxTokens,
    thinkingEnabled,
    model: config.model || serverModel,
    systemPrompt: config.systemPrompt || '',
    systemPromptMode: config.systemPromptMode || 'append',
    systemPromptBlocks: config.systemPromptBlocks || {},
    thinkingDepth: config.thinkingDepth,
    temperature: config.temperature,
    searchMode,
    fetchEnabled,
    codeExecEnabled,
    memoryEnabled,
    schedulerEnabled,
    swarmEnabled,
    browserEnabled,
    desktopEnabled,
    imageGenEnabled,
    humanGuidanceEnabled,
    endpointMode: endpointEnabled,
    autopilot: autopilotEnabled,
    activeFlow: activeFlow || '',
    autoTranslate: !!autoTranslate,
    // OUTPUT-side translate target: the UI language the reply is rendered into
    // (model → human). The backend maps this code to a language name and
    // translates the assistant reply to it instead of the old Chinese hard-pin.
    uiLang: (typeof _i18nLang !== 'undefined' ? _i18nLang : 'zh'),
    autoApply: autoApplyWrites,
    browserClientId: window._browserClientId || null,
    keepToolHistory: config.keepToolHistory,
    serverModel,
  };
}

/**
 * Build a per-conv "stored settings" snapshot for the resolver.
 * For active convs we resolve `projectPath` via `_getConvProjectPath()`
 * so the server doesn't need to know the multi-project-path UX.
 */
function _buildConvSnapshot(conv, isActive) {
  return {
    model: conv.model,
    thinkingDepth: conv.thinkingDepth,
    searchMode: conv.searchMode,
    fetchEnabled: conv.fetchEnabled,
    codeExecEnabled: conv.codeExecEnabled,
    browserEnabled: conv.browserEnabled,
    desktopEnabled: conv.desktopEnabled,
    memoryEnabled: conv.memoryEnabled,
    schedulerEnabled: conv.schedulerEnabled,
    swarmEnabled: conv.swarmEnabled,
    endpointEnabled: conv.endpointEnabled,
    autopilotEnabled: conv.autopilotEnabled,
    activeFlow: conv.activeFlow || '',
    imageGenEnabled: conv.imageGenEnabled,
    humanGuidanceEnabled: conv.humanGuidanceEnabled,
    projectPath: isActive ? _getConvProjectPath(conv) : conv.projectPath,
    projectPaths: conv.projectPaths || [],
    readOnlyPaths: conv.readOnlyPaths || [],
    autoTranslate: conv.autoTranslate,
    uiLang: conv.uiLang || (typeof _i18nLang !== 'undefined' ? _i18nLang : undefined),
    folderId: conv.folderId,
  };
}

function _stripEnvelope(body) {
  // api_ok merges the dict into top-level + adds {ok:true, request_id}.
  // Strip those wrapper keys before returning to callers.
  const out = { ...body };
  delete out.ok;
  delete out.request_id;
  return out;
}

async function _buildConvConfig(conv) {
  const isActive = (conv.id === activeConvId);
  const body = await Api.conversations.resolveConfig({
    conv_settings: _buildConvSnapshot(conv, isActive),
    overrides: _buildToolbarOverrides(),
    server_defaults: { serverModel },
    is_active: isActive,
  });
  return _stripEnvelope(body);
}

async function _buildConvSettings(conv) {
  const body = await Api.conversations.resolveSettings({
    conv_settings: _buildConvSnapshot(conv, false),
    overrides: _buildToolbarOverrides(),
  });
  return _stripEnvelope(body);
}
