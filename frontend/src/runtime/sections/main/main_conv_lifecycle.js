/* ===== migrated source: main/main_conv_lifecycle.js ===== */
/**
 * Responsibility: retained conversation shell create/open/delete/clone lifecycle.
 * Entry points: newChat, loadConversation, deleteConversation, duplicateConversation.
 * Dependencies: precomposed runtimeScope, catalog/settings/UI adapters; runs before main.js.
 */

function newChat() {
  _purgeEmptyConvs();
  const prevConv = getActiveConv();
  if (prevConv) {
    /* Never persist a provisional model paint. */
    if (!config._modelIsProvisional && config.model) prevConv.model = config.model;
    prevConv.thinkingDepth = config.thinkingDepth;
    prevConv.searchMode = searchMode || "multi";
    prevConv.fetchEnabled = !!fetchEnabled;
    prevConv.codeExecEnabled = !!codeExecEnabled;
    prevConv.browserEnabled = !!browserEnabled;
    prevConv.memoryEnabled = !!memoryEnabled;
    prevConv.autopilotEnabled = !!autopilotEnabled;
    prevConv.activeFlow = activeFlow || '';
    prevConv.imageGenEnabled = !!imageGenEnabled;
    prevConv.imageGenMode = !!imageGenMode;
    if (imageGenMode) {
      prevConv.imageGenModel = _igSelectedModel || 'gemini-3.1-flash-image-preview';
      prevConv.imageGenProviderId = _igSelectedProviderId || '';
      prevConv.imageGenCount = _igSelectedCount || 1;
      prevConv.imageGenAspect = _igSelectedAspect || '1:1';
      prevConv.imageGenResolution = _igSelectedResolution || '1K';
    }
    prevConv.autoTranslate = !!autoTranslate;
    /* Preserve inherited project binding when leaving an unopened shell. */
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
  runtimeScope.ConversationTurnStore?.reconcileConversationActivity?.(
    prevConv?.id,
  );
  runtimeScope.PlanDecisionPresentation?.activateConversation(null);
  /* Show folder context in topbar when creating a new chat from folder view */
  const _newChatFolderId = typeof getActiveFolderId === 'function' ? getActiveFolderId() : null;
  const _newChatFolder = _newChatFolderId && typeof getFolderById === 'function' ? getFolderById(_newChatFolderId) : null;
  const topbarEl = document.getElementById("topbarTitle");
  const _newChatLabel = _conversationDisplayTitle(
    'New Chat', t('chat.newConversation'));
  topbarEl.removeAttribute('data-i18n');
  topbarEl.removeAttribute('data-i18n-once');
  if (_newChatFolder) {
    topbarEl.innerHTML = `${escapeHtml(_newChatLabel)} <span class="topbar-folder-badge" style="color:${_newChatFolder.color || 'var(--text-tertiary)'}">● ${escapeHtml(_newChatFolder.name)}</span>`;
  } else {
    topbarEl.textContent = _newChatLabel;
  }
  renderConversationList();
  runtimeScope.DebugPresentationState?.clear();
  /* Welcome screen: show folder indicator when new chat will be assigned to a folder */
  const _folderBadgeHtml = _newChatFolder
    ? `<div class="welcome-folder-badge"><span class="welcome-folder-dot" style="color:${_newChatFolder.color || '#888'}">●</span> ${escapeHtml(_newChatFolder.name)}</div>`
    : '';
  document.getElementById("chatInner").innerHTML =
    `<div class="welcome" id="welcome"><div class="welcome-icon"><img ${brandLogoImgAttrs(64)}></div><h2 class="tofu-brand"><span class="tofu-brand-t">T</span><span class="tofu-brand-o1">o</span><span class="tofu-brand-f">f</span><span class="tofu-brand-u">u</span><small>豆腐</small></h2>${_folderBadgeHtml}<div class="feature-pills">${_welcomePillsHtml()}</div></div>`;
  buildTurnNav(null);
  renderPendingQueueUI(null);
  // A brand-new conversation has no latch — hide any lingering banner.
  updateSendButton();
  if (!hasInput) {
    _clearProjectStateLocal();
    _resetToolsToDefaults();
  }
  /* The Project-Brain surfaces re-resolve via the _updateProjectUI funnel
   *   (project.js) — !hasInput reaches it through _clearProjectStateLocal,
   *   and with pending input the project legitimately stays armed. */
  if (typeof runtimeScope.updateContextBar === 'function') runtimeScope.updateContextBar();
}
function loadConversation(id) {
  _purgeEmptyConvs();
  // ── Exit branch mode when switching conversations ──
  if (typeof closeBranchPanel === "function" && typeof isBranchModeActive === "function" && isBranchModeActive()) {
    closeBranchPanel();
  }
  /* Snapshot the outgoing conversation's settings, but defer persistence
   * until after the new Surface owns the DOM. Settings writes never carry a
   * transcript; ConversationTurnStore remains the only transcript authority. */
  const prevConv = getActiveConv();
  let _needsDeferredSave = false;
  if (prevConv && prevConv.id !== id) {
    /* See captureActiveConversationSettings: switching AWAY from a conv must not stamp a
     *   provisional default paint onto it. This is the exact path that turned
     *   a mispainted composer into persisted corruption. */
    if (!config._modelIsProvisional && config.model) prevConv.model = config.model;
    prevConv.thinkingDepth = config.thinkingDepth;
    prevConv.searchMode = searchMode || "multi";
    prevConv.fetchEnabled = !!fetchEnabled;
    prevConv.codeExecEnabled = !!codeExecEnabled;
    prevConv.browserEnabled = !!browserEnabled;
    prevConv.desktopEnabled = !!desktopEnabled;
    prevConv.memoryEnabled = !!memoryEnabled;
    prevConv.schedulerEnabled = !!schedulerEnabled;
    prevConv.autopilotEnabled = !!autopilotEnabled;
    prevConv.activeFlow = activeFlow || '';
    prevConv.imageGenEnabled = !!imageGenEnabled;
    prevConv.imageGenMode = !!imageGenMode;
    prevConv.humanGuidanceEnabled = !!humanGuidanceEnabled;
    if (imageGenMode) {
      prevConv.imageGenModel = _igSelectedModel || 'gemini-3.1-flash-image-preview';
      prevConv.imageGenProviderId = _igSelectedProviderId || '';
      prevConv.imageGenCount = _igSelectedCount || 1;
      prevConv.imageGenAspect = _igSelectedAspect || '1:1';
      prevConv.imageGenResolution = _igSelectedResolution || '1K';
    }
    const _prevTaskActive = typeof convIsBusy === 'function' && convIsBusy(prevConv);
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
  runtimeScope.ConversationTurnStore?.reconcileConversationActivity?.(
    prevConv?.id, id,
  );
  runtimeScope.PlanDecisionPresentation?.activateConversation(id);
  /* If loading a conv that doesn't belong to the active folder view, exit it */
  if (typeof getActiveFolderId === 'function' && getActiveFolderId()) {
    const _loadedConv = conversations.find(c => c.id === id);
    if (_loadedConv && _loadedConv.folderId !== getActiveFolderId()) {
      setActiveFolderId(null);
    }
  }
  if (typeof closeBranchPanel === "function") closeBranchPanel();
  const c = conversations.find((x) => x.id === id);
  if (!c) return;
  document.getElementById("topbarTitle").textContent =
    _conversationDisplayTitle(c.title, t('chat.newConversation'));
  /* PERF: Use fast-path O(1) active-class swap instead of O(N) full sidebar rebuild.
   * Full renderConversationList() is only needed when the conv isn't in the DOM yet
   * (e.g. newly created conversation). The fast path just moves the CSS .active class
   * between two existing DOM elements — zero HTML generation, zero innerHTML. */
  if (!_swapActiveConvItem(id)) {
    renderConversationList();
  }

  /* Transfer DOM ownership synchronously, then hydrate one authoritative
   * snapshot. The Surface clears the outgoing conversation immediately and
   * preserves its own scroll anchor while Turn data converges. */
  if (c._turnSnapshotRequired) {
    runtimeScope.requestAuthoritativeConversationRender(c.id);
    void (async () => {
      if (typeof hydrateConversationRuntime !== 'function') {
        console.error('[loadConversation] conversation turn runtime is unavailable');
        if (typeof showToast === 'function') {
          showToast('Conversation runtime is unavailable. Reload the page.', 'error');
        }
        return;
      }
      try {
        // One snapshot hydrates turns, attempts, revision, and settings.
        await hydrateConversationRuntime(c.id);
        if (activeConvId === id) {
          runtimeScope.requestAuthoritativeConversationRender(c.id);
          if (typeof restoreConversationSettingsToComposer === 'function') restoreConversationSettingsToComposer(c);
          _restoreConvProject(c);
        }
        _resumePendingTranslations(id);
      } catch (error) {
        c._turnSnapshotRequired = true;
        console.error('[loadConversation] authoritative hydration failed:',
                      error?.message || error);
        if (activeConvId === id && typeof showToast === 'function') {
          showToast(error?.message || 'Conversation failed to load', 'error');
        }
      }
    })();
  } else {
    runtimeScope.requestAuthoritativeConversationRender(c.id);
    _resumePendingTranslations(id);
  }

  /* A warm TurnStore already owns an exact snapshot cursor. Reopen its ordered
   * SSE suffix instead of downloading the Turn window again on every sidebar
   * selection. The typed wake boundary owns the cold-store fallback and reset
   * policy; this retained selection path never requests a snapshot directly. */
  if (!c._turnSnapshotRequired) {
    const turnStore = runtimeScope.ConversationTurnStore;
    if (typeof turnStore?.wakeConversation === 'function') {
      turnStore.wakeConversation(c).catch(error => {
        console.warn('[loadConversation] turn-native wake failed:', error?.message || error);
      });
    } else {
      console.warn('[loadConversation] conversation turn runtime is unavailable');
    }
  }

  renderPendingQueueUI(id);
  // Refresh server queue state for this conversation
  _refreshServerQueue(id);
  updateSendButton();
  runtimeScope.DebugPresentationState?.onConversationSwitch(id);
  /* Project binding is part of authoritative settings. Cold opens restore it
   * after Turn hydration above; restoring the shell copy here as well raced an
   * identical setPaths request, rotated the server undo session, and warmed
   * the same tree index twice. Warm opens already own exact settings. */
  if (!c._turnSnapshotRequired) _restoreConvProject(c);
  /* Restore settings only after the authoritative Turn snapshot is ready. */
  if (!c._turnSnapshotRequired) restoreConversationSettingsToComposer(c);

  /* Persist outgoing settings after the new conversation is interactive.
   * setTimeout(0) keeps the settings PATCH off the critical paint path. */
  if (_needsDeferredSave && prevConv) {
    const pc = prevConv;
    setTimeout(() => {
      /* FIX: Pass null instead of pc.id — saving tool state on conversation
       * switch is a metadata-only change, NOT new conversation activity.
       * Passing pc.id would bump updatedAt = Date.now(), which makes the
       * outgoing conversation jump to the top of the sidebar even though
       * the user only viewed it without making any changes. */
      reconcileConversationCatalogMetadata(null);
      if ((runtimeScope.ConversationTurnRead?.ordered?.(pc)?.length || 0) > 0) {
        persistConversationSettings(pc);
      }
    }, 0);
  }
}
async function deleteConversation(id, e) {
  if (e && e.stopPropagation) e.stopPropagation();
  const conv = conversations.find((c) => c.id === id);
  if (!conv) return;

  /* Optimistic UI is local to this tab. Cross-tab deletion is published only
   * after Sidecar commits, so a failed request never removes data elsewhere. */
  const attemptIds = typeof _activeConversationAttemptIds === 'function'
    ? _activeConversationAttemptIds(conv) : [];
  await Promise.allSettled(attemptIds.map(attemptId =>
    runtimeScope.ConversationTurnStore.abortAttempt(attemptId)));

  const origIndex = conversations.indexOf(conv);
  const wasActive = (activeConvId === id);
  conversations = conversations.filter((c) => c.id !== id);
  try { ConvCache.remove(id); } catch (_) { /* cache is best-effort */ }
  runtimeScope.ConversationTurnStore?.disposeConversation?.(id);
  if (wasActive) {
    if (conversations.length > 0) loadConversation(conversations[0].id);
    else newChat();
  } else renderConversationList();

  if (conv._localOnly) {
    if (typeof showToast === 'function') {
      showToast(t('sidebar.convDeleted'), 'success');
    }
    return;
  }
  try {
    const response = await Api.conversations.remove(id);
    if (!response?.ok) throw new Error(`HTTP ${response?.status || 0}`);
    if (typeof _broadcastToTabs === 'function') {
      _broadcastToTabs('conv_deleted', { convId: id });
    }
    _showUndoDeleteToast(conv, origIndex, wasActive);
  } catch (error) {
    const index = Math.max(0, Math.min(origIndex, conversations.length));
    if (!conversations.some((item) => item.id === id)) {
      conversations.splice(index, 0, conv);
    }
    try { ConvCache.put(conv); } catch (_) { /* cache is best-effort */ }
    if (wasActive) loadConversation(id);
    else renderConversationList();
    debugLog(`[deleteConv] authoritative delete failed: ${error?.message || error}`, 'warn');
    if (typeof showToast === 'function') {
      showToast(t('sidebar.convDeleteFailed'), 'error');
    }
  }
}

/* Restore only after the server has atomically rebuilt the header and turn
 * graph. Browser state is a disposable projection and is never uploaded. */
async function _restoreDeletedConversation(deletedConv, origIndex, wasActive) {
  if (!deletedConv?.id) return;
  if (conversations.some(c => c.id === deletedConv.id)) {
    if (wasActive) loadConversation(deletedConv.id);
    else renderConversationList();
    return;
  }
  try {
    const result = await Api.conversations.restore(deletedConv.id);
    const restored = deletedConv;
    restored._localOnly = false;
    restored._serverRev = Number(result?.rev || 0);
    restored._serverTurnCount = Number(result?.turnCount || 0);
    restored._turnSnapshotRequired = restored._serverTurnCount > 0;
    runtimeScope.ConversationTurnStore?.invalidateConversation?.(restored.id);
    const index = Math.max(0, Math.min(origIndex, conversations.length));
    conversations.splice(index, 0, restored);
    try { ConvCache.put(restored); } catch (_) { /* cache is best-effort */ }
    if (typeof _broadcastToTabs === 'function') {
      _broadcastToTabs('conv_restored', { convId: restored.id });
    }
    if (wasActive) loadConversation(restored.id);
    else renderConversationList();
    if (typeof showToast === 'function') {
      showToast(t('sidebar.convRestored'), 'success');
    }
  } catch (error) {
    debugLog(`[deleteConv] authoritative restore failed: ${error?.message || error}`, 'warn');
    if (typeof showToast === 'function') {
      showToast(t('sidebar.convRestoreFailed'), 'error');
    }
  }
}

/* Dedicated undo toast for conversation deletion. The generic showToast()
 * has no action-button affordance, so this builds a small toast with an Undo
 * button. Auto-dismisses after the timeout (deletion stands). */
function _showUndoDeleteToast(deletedConv, origIndex, wasActive) {
  const c = document.getElementById('toastContainer');
  if (!c) return;
  const title = deletedConv.title || 'Untitled';
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
    void _restoreDeletedConversation(deletedConv, origIndex, wasActive);
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
// Duplicate (copy) a conversation as a completely independent new conversation
// ══════════════════════════════════════════════════════
const _conversationCloneFlights = new Set();
async function duplicateConversation(id, e) {
  if (e && e.stopPropagation) e.stopPropagation();
  const srcConv = conversations.find((c) => c.id === id);
  if (!srcConv || srcConv._localOnly || _conversationCloneFlights.has(id)) return;
  const newId = generateId();
  const title = (srcConv.title || 'Untitled') + t('convLifecycle.copySuffix');
  _conversationCloneFlights.add(id);
  if (typeof showToast === "function") {
    showToast("", t('convLifecycle.copying'), t('convLifecycle.copyingBody', { title: srcConv.title }), 2000);
  }
  try {
    const result = await Api.conversations.clone(id, newId, title);
    await loadConversationCatalog();
    let newConv = conversations.find((item) => item.id === newId);
    if (!newConv) {
      newConv = _newCatalogShell({
        id: newId,
        title,
        msgCount: Number(result?.turnCount || 0),
        createdAt: Date.now(),
        updatedAt: Date.now(),
        rev: Number(result?.rev || 0),
        settings: { clonedFrom: id },
      });
      conversations.unshift(newConv);
      renderConversationList();
    }
    loadConversation(newId);
    if (typeof showToast === "function") {
      showToast("", t('convLifecycle.copied'), t('convLifecycle.copiedBody', { title: srcConv.title }), 3000);
    }
  } catch (error) {
    debugLog(`[duplicateConv] authoritative clone failed: ${error?.message || error}`, 'warn');
    if (typeof showToast === "function") {
      showToast("", t('convLifecycle.copyFailed'), t('convLifecycle.copyFailedBody'), 4000);
    }
  } finally {
    _conversationCloneFlights.delete(id);
  }
}

// ══════════════════════════════════════════════════════
// Rename a conversation — inline dialog + manual title edit
// ══════════════════════════════════════════════════════

/**
 * Apply a new title to a conversation: update in-memory state, the topbar
 * (if active), the sidebar, and persist title-only to the server. Marks the
 * conversation as user-titled so the auto-generator never overwrites it.
 */
function _applyConvTitle(conv, title) {
  conv.title = title;
  conv._titleEdited = true;          // block auto-title from overwriting
  /* Pass null — a rename is a metadata-only change, NOT new activity, so we
   * don't want to bump updatedAt and reorder the sidebar. */
  reconcileConversationCatalogMetadata(null);
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
function _maybeAutoGenerateTitleForSettledTurn(conv, turn) {
  if (!conv?.id || turn?.laneId !== 'main' || turn.status !== 'completed'
      || !['assistant', 'planner'].includes(turn.actor)) {
    return false;
  }
  void _maybeAutoGenerateTitle(conv.id);
  return true;
}

async function _maybeAutoGenerateTitle(convId) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return;
  /* Opt-in: only auto-generate when the user has enabled it in Settings.
   * Defaults to false — editors title conversations manually. */
  if (typeof config === 'undefined' || !config.autoGenerateTitle) return;
  if (conv._titleEdited || conv._titleAutoGenerated) return;
  const turns = runtimeScope.ConversationTurnRead?.ordered?.(conv) || [];
  const hasUser = turns.some(turn => turn.actor === 'human');
  /* Only fire on the FIRST user-facing output turn. Planner is a durable
   * actor in Plan/Autopilot modes and is presented as the assistant response.
   * The _title* flags are runtime-
   *   only (not persisted), so after a page reload they're gone; gating on
   *   "exactly one output so far" ensures we never regenerate the
   *   title on later turns of a reloaded conversation — which would clobber a
   *   title the user manually set in a previous session. */
  const outputCount = turns.filter(turn =>
    turn.actor === 'assistant' || turn.actor === 'planner').length;
  if (!hasUser || outputCount !== 1) return;
  conv._titleAutoGenerated = true;   // attempt-once guard (set before await)
  try {
    const res = await Api.conversations.generateTitle(convId);
    if (!res || !res.title) return;
    // Re-find: the conv may have been deleted/replaced during the await.
    const fresh = conversations.find(c => c.id === convId);
    if (!fresh || fresh._titleEdited) return;   // user renamed mid-flight — respect it
    fresh.title = res.title;
    reconcileConversationCatalogMetadata(null);
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
  runtimeScope._promptRenameConversation = _promptRenameConversation;
  runtimeScope._maybeAutoGenerateTitle = _maybeAutoGenerateTitle;
  runtimeScope._maybeAutoGenerateTitleForSettledTurn =
    _maybeAutoGenerateTitleForSettledTurn;
}

// ══════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════
//  Shared config/settings builders for /api/chat/send and /api/chat/regenerate
// ══════════════════════════════════════════════════════

// Merge policy remains server-authoritative in lib/conv_config/.

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
    browserEnabled,
    desktopEnabled,
    imageGenEnabled,
    imageGenMode,
    imageGenModel: _igSelectedModel,
    imageGenProviderId: _igSelectedProviderId,
    imageGenCount: _igSelectedCount,
    imageGenAspect: _igSelectedAspect,
    imageGenResolution: _igSelectedResolution,
    humanGuidanceEnabled,
    autopilot: autopilotEnabled,
    activeFlow: activeFlow || '',
    chatMode: (typeof chatMode !== 'undefined' ? chatMode : 'chat'),
    planMode: (typeof planMode !== 'undefined' ? !!planMode : false),
    autoTranslate: !!autoTranslate,
    // OUTPUT-side translate target: the UI language the reply is rendered into
    // (model → human). The backend maps this code to a language name and
    // translates the assistant reply to it instead of the old Chinese hard-pin.
    uiLang: (typeof _i18nLang !== 'undefined' ? _i18nLang : 'zh'),
    autoApply: autoApplyWrites,
    /* _browserClientId is refreshed only while the Local Control panel is
     * open. Outside it, fall back to the DOM stamp the LOCAL extension left
     * on this document (origin_marker.js) so a conversation started without
     * ever opening the panel still pins automation to THIS machine instead
     * of drifting across the owner's other connected browsers. */
    browserClientId: runtimeScope._browserClientId ||
      ((typeof document !== 'undefined' && document.documentElement)
        ? document.documentElement.getAttribute('data-tofu-browser-bridge')
        : '') || null,
    cache: Object.assign({}, config.cache || {}),
    tools: Object.assign({}, config.tools || {}),
    responses: Object.assign({}, config.responses || {}),
    compaction: Object.assign({}, config.compaction || {}),
    serverModel,
  };
}

function _buildConvSnapshot(conv, isActive) {
  return buildConversationSettingsSnapshot(
    conv,
    isActive ? _getConvProjectPath(conv) : conv.projectPath,
    typeof _i18nLang !== 'undefined' ? _i18nLang : undefined,
  );
}

function _buildConvConfigInputs(conv) {
  const isActive = (conv.id === activeConvId);
  return {
    conv_settings: _buildConvSnapshot(conv, isActive),
    overrides: _buildToolbarOverrides(),
    server_defaults: { serverModel },
    is_active: isActive,
  };
}

async function _buildConvConfig(conv) {
  return _conversationSettingsResolution.resolveConfig(
    _buildConvConfigInputs(conv),
  );
}

function _buildConvSettingsInputs(conv, overrides) {
  return {
    conv_settings: _buildConvSnapshot(conv, false),
    overrides: overrides || _buildToolbarOverrides(),
  };
}

async function _buildConvSettings(conv) {
  return _conversationSettingsResolution.resolveSettings(
    _buildConvSettingsInputs(conv),
  );
}

async function _buildConvSubmission(conv) {
  const configInputs = _buildConvConfigInputs(conv);
  return _conversationSettingsResolution.resolveSubmission(
    configInputs,
    _buildConvSettingsInputs(conv, configInputs.overrides),
  );
}
