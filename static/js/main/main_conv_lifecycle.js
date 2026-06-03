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
    `<div class="welcome" id="welcome"><div class="welcome-icon"><img src="${BASE_PATH}/static/icons/tofu-welcome.svg" alt="Tofu" width="64" height="64"></div><h2 class="tofu-brand"><span class="tofu-brand-t">T</span><span class="tofu-brand-o1">o</span><span class="tofu-brand-f">f</span><span class="tofu-brand-u">u</span><small>豆腐</small></h2>${_folderBadgeHtml}<p>${t('welcome.subtitle')}</p><div class="feature-pills"><span class="feature-pill">Extended Thinking</span><span class="feature-pill">Search</span><span class="feature-pill">URL Fetch</span><span class="feature-pill">Image Input</span><span class="feature-pill">Co-Pilot</span><span class="feature-pill">Browser</span></div></div>`;
  buildTurnNav(null);
  renderPendingQueueUI(null);
  updateSendButton();
  if (!hasInput) {
    _clearProjectStateLocal();
    _resetToolsToDefaults();
  }
  if (typeof updateContextBar === 'function') updateContextBar();
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
    /* ★ FIX: Don't render the loading skeleton immediately — it shows a small
     * centered div at the top of the viewport, and when messages arrive
     * milliseconds later, _forceScrollToBottom jumps to the bottom → visible
     * top→bottom flash.
     *
     * Instead, keep the previous conversation's content visible during the
     * async IndexedDB/server fetch (typically <50ms for cache hits).  When
     * messages arrive, renderChat does a full render + _forceScrollToBottom
     * atomically, so the user sees a direct transition to the new conversation
     * already scrolled to the bottom — no intermediate state.
     *
     * For the rare case where both cache AND server are slow (>400ms), show
     * the skeleton as a fallback so the user knows something is loading. */
    let _skeletonTimer = setTimeout(() => {
      if (activeConvId === id && c._needsLoad) renderChat(c);
    }, 400);
    loadConversationMessages(id).then(() => {
      clearTimeout(_skeletonTimer);
      const stillExists = conversations.find(x => x.id === id);
      if (!stillExists) return;
      if (activeConvId === id) {
        if (activeStreams.has(id)) {
          showStreamingUIForConv(id);
        } else if (c._needsLoad || c.messages.length === 0) {
          renderChat(c);
          if (typeof _restoreConvToolState === "function") _restoreConvToolState(c);
        } else {
          _forceScrollToBottom(null, true);
        }
      }
      delete c._initialSwitchLoad;   // ★ clear flag after initial load completes
      if (!activeStreams.has(id)) _resumePendingTranslations(id);
    });
  } else if (activeStreams.has(id)) {
    showStreamingUIForConv(id);
  } else {
    renderChat(c);
    _resumePendingTranslations(id);
  }

  renderPendingQueueUI(id);
  // ★ Refresh server queue state for this conversation
  _refreshServerQueue(id);
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
function deleteConversation(id, e) {
  if (e && e.stopPropagation) e.stopPropagation();
  const s = activeStreams.get(id);
  if (s) {
    s.controller.abort();
    activeStreams.delete(id);
  }
  const conv = conversations.find((c) => c.id === id);
  if (conv && conv.activeTaskId)
    Api.chat.abortTask(conv.activeTaskId)
      .catch(e => debugLog(`[deleteConv] abort failed: ${e.message}`, 'warn'));
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
      if (typeof showToast === "function") showToast("", "复制失败", "无法加载原始对话内容", 4000);
      return;
    }
  }

  const now = Date.now();
  const newId = generateId();

  // ★ PERF: Show toast immediately for instant feedback
  if (typeof showToast === "function") {
    showToast("", "对话复制中…", `正在复制 "${srcConv.title}"`, 2000);
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
      title: (srcConv.title || "Untitled") + " (副本)",
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
      showToast("", "对话已复制 ✓", `"${srcConv.title}" → 独立副本已创建`, 3000);
    }
    console.log(`[duplicateConv] Duplicated conv ${id.slice(0,8)} → ${newId.slice(0,8)} (${clonedMessages.length} msgs)`);
  });
}

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
    agentBackend: activeAgentBackend || 'builtin',
    autoTranslate: !!autoTranslate,
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
    imageGenEnabled: conv.imageGenEnabled,
    humanGuidanceEnabled: conv.humanGuidanceEnabled,
    projectPath: isActive ? _getConvProjectPath(conv) : conv.projectPath,
    projectPaths: conv.projectPaths || [],
    autoTranslate: conv.autoTranslate,
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
  const url = (typeof apiUrl === 'function')
    ? apiUrl('/api/v1/conversations/config/resolve')
    : '/api/v1/conversations/config/resolve';
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      conv_settings: _buildConvSnapshot(conv, isActive),
      overrides: _buildToolbarOverrides(),
      server_defaults: { serverModel },
      is_active: isActive,
    }),
    credentials: 'same-origin',
  });
  if (!r.ok) throw new Error(`config/resolve failed: HTTP ${r.status}`);
  return _stripEnvelope(await r.json());
}

async function _buildConvSettings(conv) {
  const url = (typeof apiUrl === 'function')
    ? apiUrl('/api/v1/conversations/settings/resolve')
    : '/api/v1/conversations/settings/resolve';
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      conv_settings: _buildConvSnapshot(conv, false),
      overrides: _buildToolbarOverrides(),
    }),
    credentials: 'same-origin',
  });
  if (!r.ok) throw new Error(`settings/resolve failed: HTTP ${r.status}`);
  return _stripEnvelope(await r.json());
}
