/* ═══════════════════════════════════════════
   project.js — Project Co-Pilot
   ═══════════════════════════════════════════ */
// ══════════════════════════════════════════════════════
//  ★ Project Co-Pilot — Non-blocking async scan & index
// ══════════════════════════════════════════════════════

let _scanPollTimer = null;
let _pendingWriteApprovals = new Map();

async function resolveWriteApproval(approvalId, approved) {
  try {
    const data = await Api.project.writeApproval(approvalId, approved);
    if (!data || data.error) {
      debugLog("Approval failed: " + ((data && data.error) || "Unknown"), "warn");
      return;
    }
    debugLog(
      `Write ${approved ? "approved" : "rejected"}: ${approvalId.slice(0, 16)}`,
      approved ? "success" : "warn",
    );
  } catch (e) {
    debugLog("Approval error: " + e.message, "error");
  }
}

// ══════════════════════════════════════════════════════
//  ★ Interactive Stdin — subprocess waiting for keyboard input
// ══════════════════════════════════════════════════════

async function submitStdinInput(stdinId, inputText) {
  if (!stdinId) return;
  try {
    const data = await Api.chat.stdinResponse(stdinId, inputText);
    if (!data || data.error) {
      debugLog("Stdin submit failed: " + ((data && data.error) || "Unknown"), "warn");
      if (typeof showToast === 'function')
        showToast("", "Stdin Error", (data && data.error) || "Failed to send input", 5000);
      return;
    }
    debugLog(`Stdin input sent: ${stdinId}`, "success");
  } catch (e) {
    debugLog("Stdin error: " + e.message, "error");
    if (typeof showToast === 'function')
      showToast("", "Stdin Error", e.message, 5000);
  }
}

async function submitStdinEof(stdinId) {
  // Send EOF flag to signal stdin close
  if (!stdinId) return;
  try {
    const data = await Api.chat.stdinResponse(stdinId, "", true);
    if (!data || data.error) {
      debugLog("Stdin EOF failed: " + ((data && data.error) || "Unknown"), "warn");
      return;
    }
    debugLog(`Stdin EOF sent: ${stdinId}`, "success");
  } catch (e) {
    debugLog("Stdin EOF error: " + e.message, "error");
  }
}

// ══════════════════════════════════════════════════════
//  ★ Human Guidance — interactive Q&A during tool use
// ══════════════════════════════════════════════════════

async function _submitHumanGuidanceResponse(guidanceId, responseText) {
  try {
    const data = await Api.chat.humanResponse(guidanceId, responseText);
    if (!data || data.error) {
      debugLog("Human guidance submit failed: " + ((data && data.error) || "Unknown"), "warn");
      if (typeof showToast === 'function')
        showToast("Failed to submit response", "error");
      return false;
    }
    debugLog(`Human guidance answered: ${guidanceId}`, "success");
    return true;
  } catch (e) {
    debugLog("Human guidance error: " + e.message, "error");
    if (typeof showToast === 'function')
      showToast("Network error submitting response", "error");
    return false;
  }
}

async function submitHumanGuidanceFreeText(guidanceId) {
  const textarea = document.getElementById(`hg-input-${guidanceId}`);
  if (!textarea) return;
  const text = textarea.value.trim();
  if (!text) {
    textarea.classList.add('hg-shake');
    setTimeout(() => textarea.classList.remove('hg-shake'), 500);
    return;
  }
  const card = textarea.closest('.hg-card');
  if (card) card.classList.add('hg-submitting');

  // ★ Auto-translate CN→EN: if autoTranslate is ON and text contains Chinese,
  //   translate before sending to backend — same as sendMessage() flow.
  const conv = conversations.find(c => c.id === activeConvId);
  const _hgAutoTrans = convAutoTranslate(conv);
  const hasChinese = /[\u4e00-\u9fff\u3400-\u4dbf]/.test(text);
  let finalText = text;

  if (_hgAutoTrans && hasChinese) {
    // Show translating state on the submit button
    const submitBtn = card?.querySelector('.hg-submit-btn');
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.innerHTML = '<span class="hg-spinner"></span> 翻译中…';
    }
    try {
      console.log(`[HG-Submit] Auto-translating user response CN→EN (${text.length} chars)`);
      finalText = await _callTranslateAPI(text, 'English', 'Chinese');
      console.log(`[HG-Submit] ✓ Translated: ${text.length}→${finalText.length} chars`);
    } catch (e) {
      console.warn(`[HG-Submit] Translation failed, sending original: ${e.message}`);
      if (typeof showToast === 'function')
        showToast('翻译失败，已发送原文', 'warning');
      finalText = text; // fallback to original
    }
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg> 提交';
    }
  }

  const ok = await _submitHumanGuidanceResponse(guidanceId, finalText);
  if (!ok && card) card.classList.remove('hg-submitting');
  if (ok) _collapseHgRoundAfterSubmit(guidanceId, text);
}

async function submitHumanGuidanceChoice(guidanceId, choiceLabel) {
  // Find and highlight the selected option card
  const allBtns = document.querySelectorAll(`button.hg-option-card[data-gid="${guidanceId}"]`);
  allBtns.forEach(btn => {
    btn.classList.remove('hg-selected');
    if (btn.dataset.label === choiceLabel) btn.classList.add('hg-selected');
    btn.disabled = true;
  });
  const card = document.querySelector(`button.hg-option-card[data-gid="${guidanceId}"]`)?.closest('.hg-card');
  if (card) card.classList.add('hg-submitting');
  const ok = await _submitHumanGuidanceResponse(guidanceId, choiceLabel);
  if (!ok) {
    allBtns.forEach(btn => { btn.disabled = false; btn.classList.remove('hg-selected'); });
    if (card) card.classList.remove('hg-submitting');
  }
  if (ok) _collapseHgRoundAfterSubmit(guidanceId, choiceLabel);
}

/* translateHumanGuidanceInput removed — auto-translation is now fully automatic:
 * - EN→CN: LLM question & options translated on arrival (_autoTranslateHumanGuidance)
 * - CN→EN: User's free-text reply translated on submit (submitHumanGuidanceFreeText)
 */

/**
 * ★ Immediately collapse the HG interactive card after successful submission.
 * Sets local round status to "submitted" so the card collapses to a compact line
 * ("✓ 已回答") instead of staying grayed-out until the server sends tool_result.
 * When tool_result arrives, it will overwrite status to "done" as normal.
 */
function _collapseHgRoundAfterSubmit(guidanceId, responseText) {
  const conv = conversations.find(c => c.id === activeConvId);
  if (!conv) return;
  const assistantMsg = [...conv.messages].reverse().find(m => m.role === 'assistant');
  if (!assistantMsg || !assistantMsg.toolRounds) return;
  const round = assistantMsg.toolRounds.find(r => r.guidanceId === guidanceId);
  if (!round) return;
  // Transition: awaiting_human → submitted (immediately collapses card)
  round.status = 'submitted';
  round._hgUserResponse = responseText;
  round.guidanceId = null; // prevent re-rendering the interactive card
  console.log(`[HG] ✓ Card collapsed: round=${round.roundNum}, response="${responseText.slice(0, 60)}"`);
  // Update sidebar: conversation no longer awaiting human (amber dot → streaming dot)
  renderConversationList();
  // Force-refresh streaming UI to show collapsed state
  const buf = typeof streamBufs !== 'undefined' ? streamBufs.get(activeConvId) : null;
  if (buf) {
    buf.toolRounds = assistantMsg.toolRounds.map(r => ({...r}));
  }
  twUpdate(activeConvId);
}

function toggleAutoApply() {
  autoApplyWrites = !autoApplyWrites;
  localStorage.setItem("claude_auto_apply", JSON.stringify(autoApplyWrites));
  _updateAutoApplyUI();
  debugLog(
    "Write mode: " +
      (autoApplyWrites ? "Auto (no confirmation)" : "Manual (confirm each)"),
    "success",
  );
}

function _updateAutoApplyUI() {
  const btn = document.getElementById("autoApplyToggle");
  if (!btn) return;
  btn.classList.toggle("auto-mode", autoApplyWrites);
  btn.querySelector(".autoapply-label").textContent = autoApplyWrites
    ? "Auto"
    : "Manual";
  btn.title = autoApplyWrites
    ? "Writes auto-apply (click for manual)"
    : "Writes need confirmation (click for auto)";
}

// ★ Per-conversation project path helpers
function _saveConvProjectPath(path, extraPaths, readOnlyPaths) {
  const conv = getActiveConv();
  if (conv) {
    conv.projectPath = path || "";
    // ★ Persist ALL project paths (primary + extras) so they survive conversation switches
    conv.projectPaths = [];
    if (path) conv.projectPaths.push(path);
    if (Array.isArray(extraPaths)) {
      for (const ep of extraPaths) {
        if (ep && !conv.projectPaths.includes(ep)) conv.projectPaths.push(ep);
      }
    }
    // ★ Persist the read-only subset (parallel list, mirrors backend model).
    //   Only keep entries that are actually among the configured paths.
    if (Array.isArray(readOnlyPaths)) {
      conv.readOnlyPaths = readOnlyPaths.filter(p => p && conv.projectPaths.includes(p));
    } else {
      conv.readOnlyPaths = [];
    }
    saveConversations(conv.id);
    syncConversationToServer(conv);
  }
}

function _getConvProjectPath(conv) {
  return (conv && conv.projectPath) || "";
}

function _clearProjectStateLocal() {
  // Reset local projectState without touching server — used when switching to a conv with no project
  // ★ BUG FIX: Stop background polls BEFORE clearing state.
  // Without this, _doScanPoll keeps fetching the old project
  // from the server and _applyProjectData resurrects projectState.active=true,
  // making it impossible to clear the project bar (e.g. on "New Chat").
  _stopScanPoll();
  projectState = {
    active: false,
    path: "",
    fileCount: 0,
    dirCount: 0,
    totalSize: 0,
    languages: {},
    scanning: false,
    scanProgress: "",
    scanDetail: "",
    scannedAt: 0,
    extraRoots: [],
  };
  _updateProjectUI();
}

async function _restoreConvProject(conv) {
  const savedPath = _getConvProjectPath(conv);
  if (!savedPath) {
    // This conversation has no project — clear UI
    _clearProjectStateLocal();
    return;
  }
  // ★ Gather all saved paths (primary + extras) from the conversation
  const allPaths = (Array.isArray(conv.projectPaths) && conv.projectPaths.length)
    ? conv.projectPaths
    : [savedPath];
  const hasExtras = allPaths.length > 1;
  // If already active on the same primary path with same extras, just update UI
  const currentExtras = (projectState.extraRoots || []).map(r => typeof r === 'string' ? r : r.path);
  const savedExtras = allPaths.slice(1);
  const extrasMatch = savedExtras.length === currentExtras.length &&
    savedExtras.every(p => currentExtras.includes(p));
  if (projectState.active && projectState.path === savedPath && extrasMatch) {
    _updateProjectUI();
    return;
  }
  // Need to set/restore this project on server
  _clearProjectStateLocal();
  const savedReadOnly = (Array.isArray(conv.readOnlyPaths) ? conv.readOnlyPaths : [])
    .filter(p => allPaths.includes(p));
  try {
    // ★ Use multi-path API when there are extra roots OR any read-only root
    //   (read-only is only expressible through the multi-path endpoint).
    const resp = (hasExtras || savedReadOnly.length)
      ? await Api.project.setPaths(allPaths, savedReadOnly)
      : await Api.project.setPath(savedPath);
    const data = resp ? await resp.json().catch(() => ({})) : {};
    if (resp && resp.ok) {
      _applyProjectData(data);
      // ★ BUG FIX: Update recent projects on restore so new projects appear
      //   in the recent list and last_used stays current.
      saveRecentProject(data.path);
      /* ★ FIX: Sync conv.projectPath from the server response.
       * _restoreConvProject only reads conv.projectPath — it never writes it
       * back after a successful /api/project/set.  If conv.projectPath was
       * undefined (e.g. new conv inherited from projectState, or loaded from
       * cache without settings), it stays undefined even though the project
       * is now active.  This caused "UI shows project, backend gets no path"
       * because startAssistantResponse reads conv.projectPath (undefined → ""). */
      conv.projectPath = data.path || savedPath;
      debugLog("Project restored for conversation: " + savedPath, "success");
    } else {
      debugLog("Saved project path no longer valid: " + savedPath, "warn");
      // Clear the invalid path from conversation
      conv.projectPath = "";
      /* ★ FIX: Pass null — clearing a stale project path is a metadata-only
       * change, NOT new conversation activity.  Passing conv.id bumps
       * updatedAt = Date.now(), making the conversation jump to the top
       * of the sidebar just because its saved project path was invalid. */
      saveConversations(null);
    }
  } catch (e) {
    debugLog("Project restore failed: " + e.message, "warn");
  }
}

// ── Multi-path folder state for the modal ──
let _mpFolders = []; // array of path strings being edited in the modal
let _mpReadOnly = new Set(); // subset of _mpFolders the user marked read-only
// Mirror of the badges currently painted in the project bar ({path, readOnly}),
// so the bar's click-to-toggle handler can look an entry up by index.
let _projectBarFolders = [];

function _syncFoldersFromState() {
  // Build _mpFolders from projectState (single source of truth)
  _mpFolders = [];
  _mpReadOnly = new Set();
  if (projectState.path) {
    _mpFolders.push(projectState.path);
    if (projectState.readOnly) _mpReadOnly.add(projectState.path);
  }
  if (projectState.extraRoots && projectState.extraRoots.length) {
    for (const r of projectState.extraRoots) {
      const p = typeof r === 'string' ? r : r.path;
      if (p && !_mpFolders.includes(p)) {
        _mpFolders.push(p);
        if (r && typeof r === 'object' && r.readOnly) _mpReadOnly.add(p);
      }
    }
  }
}

function _mpToggleReadOnly(index) {
  const p = _mpFolders[index];
  if (!p) return;
  if (_mpReadOnly.has(p)) _mpReadOnly.delete(p);
  else _mpReadOnly.add(p);
  _mpRenderTags();
}

function openProjectModal() {
  _syncFoldersFromState();
  _mpRenderTags();
  _updateProjectModalStatus();
  renderRecentProjects();
  document.getElementById("projectModal").classList.add("open");
  // Default mobile view to Browse on each open (no-op on desktop).
  pmMobileTab("browse");
  // Docked browser: populate it from the primary folder (or home) on open.
  browseDirectory(_mpFolders.length ? _mpFolders[0] : "~");
  setTimeout(() => document.getElementById("mpPathInput").focus(), 100);
}

function closeProjectModal() {
  document.getElementById("projectModal").classList.remove("open");
}

/* ── Mobile segmented tab toggle (Browse / Workspace) ──
 * Desktop shows both panes side-by-side; on narrow screens the .pm-body
 * is driven by a data-pm-view attribute so only one pane is visible at a
 * time, giving each the full viewport height instead of two cramped halves. */
function pmMobileTab(view) {
  const modal = document.querySelector('#projectModal .pm-workbench');
  if (!modal) return;
  modal.setAttribute('data-pm-view', view);
  modal.querySelectorAll('.pm-mtab').forEach((b) => {
    b.classList.toggle('active', b.getAttribute('data-pm-tab') === view);
  });
}

/* ── Multi-path tag rendering ── */
function _mpRenderTags() {
  const container = document.getElementById("mpFolderTags");
  const countEl = document.getElementById("mpFolderCount");
  if (countEl) countEl.textContent = _mpFolders.length ? `${_mpFolders.length}` : '';
  const mCountEl = document.getElementById("pmMobileCount");
  if (mCountEl) mCountEl.textContent = _mpFolders.length ? `${_mpFolders.length}` : '';
  if (!_mpFolders.length) {
    container.innerHTML = '<div class="mp-empty-hint">No folders yet — type a path or pick one from the browser.</div>';
    return;
  }
  container.innerHTML = _mpFolders.map((p, i) => {
    const parts = p.split('/').filter(Boolean);
    const name = parts[parts.length - 1] || p;
    const short = parts.length > 2 ? '…/' + parts.slice(-2).join('/') : p;
    const isPrimary = i === 0;
    const isRO = _mpReadOnly.has(p);
    // Lock (read-only) / pencil (writable) toggle. Click flips the access
    // policy for this root — sent to the backend as readOnlyPaths.
    const lockIcon = isRO
      ? '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>'
      : '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
    return `<div class="mp-row${isPrimary ? ' mp-row-primary' : ''}${isRO ? ' mp-row-readonly' : ''}" title="${escapeHtml(p)}">
      <span class="mp-row-icon">${isPrimary
        ? '<svg width="15" height="15" viewBox="0 0 24 24" fill="#f59e0b" stroke="#f59e0b" stroke-width="1.5" stroke-linejoin="round"><polygon points="12 2 15 9 22 9 16.5 13.5 18.5 21 12 16.5 5.5 21 7.5 13.5 2 9 9 9"/></svg>'
        : '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" opacity="0.55"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>'}</span>
      <span class="mp-row-text">
        <span class="mp-row-name">${escapeHtml(name)}</span>
        <span class="mp-row-path">${escapeHtml(short)}</span>
      </span>
      ${isPrimary ? '<span class="mp-row-badge">root</span>' : ''}
      ${isRO ? '<span class="mp-row-badge mp-row-badge-ro">read-only</span>' : ''}
      <button class="mp-row-lock${isRO ? ' active' : ''}" onclick="_mpToggleReadOnly(${i})" title="${isRO ? 'Read-only — click to allow edits' : 'Writable — click to make read-only'}">${lockIcon}</button>
      <button class="mp-row-remove" onclick="_mpRemove(${i})" title="Remove">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>`;
  }).join('');
}

function mpAddFolder() {
  const input = document.getElementById("mpPathInput");
  const p = input.value.trim();
  if (!p) return;
  if (_mpFolders.includes(p)) {
    input.value = '';
    input.focus();
    return;
  }
  _mpFolders.push(p);
  input.value = '';
  _mpRenderTags();
  input.focus();
}

function _mpRemove(index) {
  const removed = _mpFolders.splice(index, 1);
  if (removed && removed[0]) _mpReadOnly.delete(removed[0]);
  _mpRenderTags();
}

function _mpBrowseForAdd() {
  const el = document.getElementById("folderBrowser");
  const wasHidden = el.hidden;
  el.hidden = !wasHidden;
  if (wasHidden) {
    const inputPath = document.getElementById("mpPathInput").value.trim();
    browseDirectory(inputPath || (_mpFolders.length ? _mpFolders[0] : "~"));
  }
}

function _mpSelectBrowsed() {
  const path = document.getElementById("mpPathInput").value.trim();
  if (path) {
    if (!_mpFolders.includes(path)) {
      _mpFolders.push(path);
      _mpRenderTags();
    }
    document.getElementById("mpPathInput").value = '';
    document.getElementById("folderBrowser").hidden = true;
  }
}

/* ★ mpApplyFolders — the "Set Project" action.
   First path → primary project; remaining → extra roots.

   Optimistic UI: paint the project bar + close the modal immediately
   from the user's typed paths, then fire /api/project/set_paths in the
   background and reconcile when it returns.  The backend response
   canonicalises paths (~ expansion, abs path) and adds crossDC info,
   so reconciliation may rewrite the bar — that's fine and expected.
   On failure we revert to the previous state and reopen the modal. */
async function mpApplyFolders() {
  if (!_mpFolders.length) return;
  // ★ Ensure we have an active conversation
  if (!activeConvId) {
    const now = Date.now();
    const conv = {
      id: generateId(), title: "New Chat", messages: [],
      createdAt: now, updatedAt: now, activeTaskId: null, projectPath: "",
    };
    /* ★ FIX: Auto-assign to active folder when creating a conv from project modal.
     * Without this, the conv stays uncategorized even though the user selected
     * a folder tab before clicking New Chat → project tool → send message. */
    const _curFolderId = typeof getActiveFolderId === 'function' ? getActiveFolderId() : null;
    if (_curFolderId) conv.folderId = _curFolderId;
    conversations.unshift(conv);
    activeConvId = conv.id;
    /* ★ Persist immediately — the conv only exists in memory until synced.
     * saveConversations sorts + broadcasts; the conv will reach the server
     * once the user sends a message (via syncConversationToServer). */
    saveConversations(conv.id);
    renderConversationList();
  }

  const folders = _mpFolders.slice();
  const primary = folders[0];
  const extras = folders.slice(1);
  const readOnly = folders.filter(p => _mpReadOnly.has(p));

  // ── Optimistic apply: paint UI from typed paths and close the modal ──
  const _prevProjectState = { ...projectState, extraRoots: (projectState.extraRoots || []).slice() };
  _applyProjectData({
    path: primary,
    readOnly: _mpReadOnly.has(primary),
    extraRoots: extras.map(p => ({ path: p, readOnly: _mpReadOnly.has(p) })),
    crossDC: null,
  });
  _saveConvProjectPath(primary, extras, readOnly);
  for (const p of folders) {
    if (p) saveRecentProject(p);
  }
  closeProjectModal();
  const nExtras = extras.length;
  const nRO = readOnly.length;
  debugLog(`Project set: ${primary}` + (nExtras ? ` + ${nExtras} extra folder(s)` : '') + (nRO ? ` (${nRO} read-only)` : ''), "success");

  // ── Reconcile with the server in the background ──
  try {
    const resp = await Api.project.setPaths(folders, readOnly);
    const data = resp ? await resp.json().catch(() => ({})) : {};
    if (!resp || !resp.ok) throw new Error(data.error || "Failed");
    _applyProjectData(data);
    _saveConvProjectPath(data.path, _mpFolders.slice(1), readOnly);
  } catch (e) {
    // Revert the optimistic state and reopen the modal so the user can fix it.
    projectState = _prevProjectState;
    _saveConvProjectPath(_prevProjectState.path || "",
                         (_prevProjectState.extraRoots || []).map(r => typeof r === 'string' ? r : r.path));
    _updateProjectUI();
    document.getElementById("projectModal").classList.add("open");
    const statusEl = document.getElementById("projectModalStatus");
    if (statusEl) {
      statusEl.innerHTML = `<div style="color:var(--error-text);font-size:12px">${escapeHtml(e.message)}</div>`;
    }
    debugLog(`Project set failed: ${e.message}`, "error");
  }
}

// Kept for backward compat / quick-set from recent list
async function setProject(pathOverride) {
  const path = pathOverride || (document.getElementById("mpPathInput") ? document.getElementById("mpPathInput").value.trim() : "");
  if (!path) return;
  _mpFolders = [path];
  _mpRenderTags();
  await mpApplyFolders();
}

async function clearProject() {
  _stopScanPoll();
  await Api.project.clear().catch(e => debugLog(`[clearProject] ${e.message}`, 'warn'));
  _saveConvProjectPath("");
  _mpFolders = [];
  _mpReadOnly = new Set();
  projectState = {
    active: false, path: "", fileCount: 0, dirCount: 0, totalSize: 0,
    languages: {}, scanning: false, scanProgress: "", scanDetail: "",
    scannedAt: 0, extraRoots: [], readOnly: false,
  };
  _updateProjectUI();
  closeProjectModal();
  debugLog("Project cleared", "success");
}

// ── Recent Project Paths (server-side persistence) ──

function saveRecentProject(path) {
  if (!path) return;
  Api.project.recentSave(path)
    .catch(e => debugLog(`[saveRecentProject] ${e.message}`, 'warn'));
}

async function renderRecentProjects() {
  const container = document.getElementById("recentProjectPaths");
  const listEl = document.getElementById("recentPathsList");
  if (!container || !listEl) return;
  let list = [];
  try {
    const data = await Api.project.recentList();
    if (data) {
      list = Array.isArray(data) ? data : data.projects || [];
    }
  } catch {}
  if (list.length === 0) {
    container.hidden = true;
    return;
  }
  container.hidden = false;
  listEl.innerHTML = list
    .map((item) => {
      const parts = item.path.split("/").filter(Boolean);
      const name = parts[parts.length - 1] || item.path;
      return `<div class="recent-path-item" onclick="selectRecentProject('${escapeHtml(item.path)}')" title="${escapeHtml(item.path)}">
         <span class="recent-path-text">
           <span class="recent-path-name">${escapeHtml(name)}</span>
           <span class="recent-path-full">${escapeHtml(item.path)}</span>
         </span>
         ${item.count > 1 ? `<span class="recent-path-count">×${item.count}</span>` : ""}
       </div>`;
    })
    .join("");
}

function selectRecentProject(path) {
  if (!path) return;
  // Add the recent project path into the multi-path list and apply
  if (!_mpFolders.includes(path)) {
    _mpFolders.push(path);
    _mpRenderTags();
  }
  // Switch the left-hand folder browser to that item's location so the user
  // sees where it lives (breadcrumb + listing reflect the selected path).
  browseDirectory(path);
}

async function clearRecentProjects() {
  await Api.project.recentClear().catch(() => {});
  renderRecentProjects();
}

async function rescanProject() {
  if (!projectState.active) return;
  try {
    const resp = await Api.project.rescan();
    const data = resp ? await resp.json().catch(() => ({})) : {};
    if (resp && resp.ok) {
      _applyProjectData(data);
      debugLog("Project refreshed", "success");
    }
  } catch (e) {
    debugLog("Rescan failed: " + e.message, "warn");
  }
}

async function undoConvModifications(msgIdx) {
  if (!projectState.active) return;
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[msgIdx];
  if (!msg || msg.role !== "assistant" || !msg.modifiedFiles) return;
  const count = msg.modifiedFiles;
  if (
    !await showConfirm(
      `确定要撤销本轮对话的 ${count} 处代码修改吗？\n此操作将恢复这些文件到修改前的状态。`,
      { danger: true },
    )
  )
    return;
  try {
    // ★ Per-round undo: prefer taskId (specific to this round), fallback to convId.
    //   ALWAYS pin the conversation's own project path so undo targets the
    //   session that recorded the change — NOT whichever project is globally
    //   active in the UI (which may differ when another conversation switched
    //   projects mid-task). Without this, undo silently no-ops (undone=0).
    const body = msg._taskId
      ? { taskId: msg._taskId }
      : { convId: conv.id };
    const convProjectPath = _getConvProjectPath(conv);
    if (convProjectPath) body.projectPath = convProjectPath;
    const resp = await Api.project.undo(body);
    const data = resp ? await resp.json().catch(() => ({})) : {};
    if (resp && resp.ok && data.ok) {
      if (typeof showToast === 'function') {
        showToast('↩️', 'Undo Complete',
          `Reverted ${data.undone} file change${data.undone !== 1 ? 's' : ''}` +
          (data.failed ? ` (${data.failed} failed)` : ''),
          4000);
      }
      // Clear the modifiedFiles flag on this message
      msg.modifiedFiles = 0;
      msg.modifiedFileList = null;
      saveConversations(conv.id);
      // Re-render the message to remove the undo button
      const el = document.getElementById(`msg-${msgIdx}`);
      if (el) el.outerHTML = renderMessage(msg, msgIdx);
      _lastRenderedFingerprint = _convRenderFingerprint(conv);
      // Re-scan project to update file counts
      rescanProject();
    } else {
      debugLog("Undo failed: " + (data.error || "unknown error"), "warn");
    }
  } catch (e) {
    debugLog("Undo failed: " + e.message, "warn");
  }
}

async function undoAllModifications() {
  if (!projectState.active) return;
  if (
    !await showConfirm(
      "确定要撤销所有代码修改吗？\n\n此操作将恢复所有被修改的文件到原始状态，包括所有对话中的修改。",
      { danger: true },
    )
  )
    return;
  try {
    const resp = await Api.project.undoAll();
    const data = resp ? await resp.json().catch(() => ({})) : {};
    if (resp && resp.ok && data.ok) {
      if (typeof showToast === 'function') {
        showToast('↩️', 'Undo All Complete',
          `Reverted ${data.undone} file change${data.undone !== 1 ? 's' : ''}` +
          (data.failed ? ` (${data.failed} failed)` : ''),
          5000);
      }
      // Clear modifiedFiles on all messages in all conversations
      for (const c of conversations) {
        for (const m of c.messages) {
          if (m.modifiedFiles) {
            m.modifiedFiles = 0;
            m.modifiedFileList = null;
          }
        }
      }
      saveConversations(activeConvId);
      // Re-render current chat
      const conv = getActiveConv();
      if (conv) renderChat(conv);
      rescanProject();
    } else {
      debugLog("Undo all failed: " + (data.error || "unknown error"), "warn");
    }
  } catch (e) {
    debugLog("Undo all failed: " + e.message, "warn");
  }
}


function _applyProjectData(data) {
  projectState = {
    ...projectState,
    active: true,
    path: data.path || projectState.path,
    fileCount: data.fileCount ?? projectState.fileCount,
    dirCount: data.dirCount ?? projectState.dirCount,
    totalSize: data.totalSize ?? projectState.totalSize,
    languages: data.languages || projectState.languages,
    scanning: data.scanning ?? false,
    scanProgress: data.scanProgress || "",
    scanDetail: data.scanDetail || "",
    scannedAt: data.scannedAt ?? projectState.scannedAt,
  };
  // ★ Merge in extra roots — backend always sends extraRoots[] in get_state()
  if (Array.isArray(data.extraRoots)) {
    projectState.extraRoots = data.extraRoots;
  }
  // ★ Primary root read-only flag (backend sends `readOnly` in get_state()).
  if (data.readOnly !== undefined) {
    projectState.readOnly = !!data.readOnly;
  }
  // ★ Cross-DC indicator from backend
  if (data.crossDC) {
    projectState.crossDC = data.crossDC;
  } else {
    projectState.crossDC = null;
  }
  _updateProjectUI();
}

function _startScanPoll() {
  // No-op: scanning was removed — project relies on tools for exploration
}

function _stopScanPoll() {
  if (_scanPollTimer) {
    clearInterval(_scanPollTimer);
    _scanPollTimer = null;
  }
}


function _updateProjectUI() {
  const bar = document.getElementById("projectBar");
  const badge = document.getElementById("projectBadge");
  const toggle = document.getElementById("projectToggle");
  const statsEl = document.getElementById("projectBarStats");
  const foldersEl = document.getElementById("projectBarFolders");

  if (!projectState.active) {
    bar.style.display = "none";
    bar.classList.remove("scanning");
    badge?.classList.remove("visible");
    toggle.classList.remove("active");
    return;
  }

  bar.style.display = "flex";
  badge?.classList.add("visible");
  toggle.classList.add("active");

  // ── Render folder badges ──
  // ★ BUG FIX: Build badge list directly from projectState instead of
  // calling _syncFoldersFromState() which would overwrite _mpFolders.
  // _mpFolders is the user's in-progress edits in the modal — clobbering
  // it here creates a race: background poll → _updateProjectUI →
  // _syncFoldersFromState silently restores removed paths, making them
  // impossible to delete from the modal.
  const _barFolders = [];
  if (projectState.path) {
    _barFolders.push({ path: projectState.path, readOnly: !!projectState.readOnly });
  }
  if (projectState.extraRoots && projectState.extraRoots.length) {
    for (const r of projectState.extraRoots) {
      const p = typeof r === 'string' ? r : r.path;
      const ro = typeof r === 'object' ? !!r.readOnly : false;
      if (p && !_barFolders.some((b) => b.path === p)) {
        _barFolders.push({ path: p, readOnly: ro });
      }
    }
  }
  _projectBarFolders = _barFolders;
  const _lockGlyph = '<svg class="folder-badge-lock" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>';
  const badges = _barFolders.map(({ path: p, readOnly }, i) => {
    const short = p.split('/').filter(Boolean).pop() || p;
    const cls = 'folder-badge' + (readOnly ? ' folder-badge-ro' : '');
    const tip = escapeHtml(p) + (readOnly ? ' (read-only — click to allow edits)' : ' (writable — click to make read-only)');
    return `<span class="${cls}" title="${tip}" onclick="toggleProjectBarReadOnly(${i}, event)">${readOnly ? _lockGlyph : ''}${escapeHtml(short)}</span>`;
  });
  foldersEl.innerHTML = badges.join('');

  // ── Stats line ──
  bar.classList.remove("scanning");
  if (projectState.crossDC && projectState.crossDC.latencyClass !== 'local') {
    const dc = projectState.crossDC;
    const cls = dc.latencyClass === 'very_slow' ? 'color:#ef4444' : 'color:#f59e0b';
    const _snailSvg = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px"><path d="M2 13a6 6 0 1 0 12 0 4 4 0 1 0-8 0 2 2 0 0 0 4 0"/><circle cx="10" cy="13" r="8"/><path d="M2 21h12c4.4 0 8-3.6 8-8V7a2 2 0 1 0-4 0v6"/><path d="M18 3 19.1 5.2"/><path d="M22 3 20.9 5.2"/></svg>';
    const _zapSvg = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px"><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/></svg>';
    const icon = dc.latencyClass === 'very_slow' ? _snailSvg : _zapSvg;
    const lat = dc.latencyMs ? `${dc.latencyMs}ms` : '?';
    statsEl.innerHTML = `<span style="${cls};font-size:11px" title="Cross-DC: cluster=${dc.cluster}, latency=${lat}">${icon} ${dc.cluster} (${lat})</span>`;
  } else {
    statsEl.innerHTML = '';
  }
}

/* Toggle a project-bar folder badge between writable and read-only in place,
   without opening the modal.  Mirrors _mpToggleReadOnly + mpApplyFolders:
   recompute the full path / read-only sets from the current projectState,
   flip the clicked root, then optimistically repaint + reconcile with the
   backend via /api/project/set_paths (the only endpoint that carries the
   read-only policy).  On failure we revert and report the error. */
/* Derive the conversation's {allPaths, roList} from an _applyProjectData-shaped
   server state object (get_state() output). The BACKEND is the single source of
   truth for the read-only policy, so we persist what it returns — never a guess. */
function _deriveConvPathsFromState(data) {
  const allPaths = [];
  const roList = [];
  if (data && data.path) {
    allPaths.push(data.path);
    if (data.readOnly) roList.push(data.path);
  }
  for (const r of (data && data.extraRoots) || []) {
    const p = typeof r === 'string' ? r : r.path;
    const ro = typeof r === 'object' && !!r.readOnly;
    if (p && !allPaths.includes(p)) {
      allPaths.push(p);
      if (ro) roList.push(p);
    }
  }
  return { allPaths, roList };
}

// Monotonic toggle sequence — only the newest in-flight toggle is allowed to
// paint/persist its result, so out-of-order responses can't flip the badge.
let _roToggleSeq = 0;

async function toggleProjectBarReadOnly(index, event) {
  // The badge lives inside #projectBarFolders, whose own onclick opens the
  // project modal. Stop the click here so toggling never pops the modal.
  if (event) { event.stopPropagation(); event.preventDefault(); }
  const entry = _projectBarFolders[index];
  if (!entry || !entry.path) return;

  // Rebuild the ordered path list + read-only set from current state so we
  // never drop extra roots or lose ordering (primary must stay first).
  const folders = _projectBarFolders.map((f) => f.path);
  const readOnly = new Set(_projectBarFolders.filter((f) => f.readOnly).map((f) => f.path));
  if (readOnly.has(entry.path)) readOnly.delete(entry.path);
  else readOnly.add(entry.path);
  const roList = folders.filter((p) => readOnly.has(p));

  const seq = ++_roToggleSeq;
  const _prevProjectState = { ...projectState, extraRoots: (projectState.extraRoots || []).slice() };

  // Optimistic LOCAL repaint only (instant feedback). We do NOT sync the
  // conversation here — the backend round-trip below owns persistence, so we
  // never write a guessed state that could race the authoritative response.
  _applyProjectData({
    path: folders[0],
    readOnly: readOnly.has(folders[0]),
    extraRoots: folders.slice(1).map((p) => ({ path: p, readOnly: readOnly.has(p) })),
    crossDC: projectState.crossDC || null,
  });
  debugLog(`${entry.path} → ${readOnly.has(entry.path) ? 'read-only' : 'writable'}`, "success");

  try {
    const resp = await Api.project.setPaths(folders, roList);
    const data = resp ? await resp.json().catch(() => ({})) : {};
    if (!resp || !resp.ok) throw new Error(data.error || "Failed");
    if (seq !== _roToggleSeq) return;   // superseded by a newer toggle — drop stale paint
    // Single source of truth: render + persist EXACTLY what the backend returned.
    _applyProjectData(data);
    const { allPaths, roList: srvRo } = _deriveConvPathsFromState(data);
    _saveConvProjectPath(allPaths[0] || '', allPaths.slice(1), srvRo);
  } catch (e) {
    if (seq !== _roToggleSeq) return;   // a newer toggle is in charge — let it settle
    projectState = _prevProjectState;
    _updateProjectUI();
    debugLog(`Read-only toggle failed: ${e.message}`, "error");
  }
}

function _updateProjectModalStatus() {
  const el = document.getElementById("projectModalStatus");
  if (!el) return;
  if (!projectState.active) { el.innerHTML = ""; return; }
  const total = _mpFolders.length;
  el.innerHTML = `<div style="font-size:12px;color:#34d399;margin-bottom:12px">
    ✓ ${total} folder${total > 1 ? 's' : ''} active
  </div>`;
}

async function loadProjectStatus() {
  // ★ Per-conversation: restore project for the active conversation
  const conv = getActiveConv();
  const savedPath = _getConvProjectPath(conv);
  if (!savedPath) {
    // Active conv has no project — check if server still has one active (from before),
    // and clear it since we don't need it for this conv
    _clearProjectStateLocal();
    return;
  }
  // Try to check server status first
  try {
    const data = await Api.project.status();
    if (!data) return;
    if (data.path && data.path === savedPath) {
      // Server already has this project active — check extras too
      const allPaths = (Array.isArray(conv.projectPaths) && conv.projectPaths.length)
        ? conv.projectPaths : [savedPath];
      const savedExtras = allPaths.slice(1);
      const savedReadOnly = (Array.isArray(conv.readOnlyPaths) ? conv.readOnlyPaths : [])
        .filter(p => allPaths.includes(p));
      const currentExtras = (data.extraRoots || []).map(r => typeof r === 'string' ? r : r.path);
      const extrasMatch = savedExtras.length === currentExtras.length &&
        savedExtras.every(p => currentExtras.includes(p));
      // ★ Detect read-only drift: the server's RO set must match what the
      //   conversation saved, else the locks vanish after a reload / server
      //   restart. Build the server's current RO set from get_state().
      const currentRO = [];
      if (data.readOnly && data.path) currentRO.push(data.path);
      for (const r of (data.extraRoots || [])) {
        if (r && typeof r === 'object' && r.readOnly) currentRO.push(r.path);
      }
      const roMatch = savedReadOnly.length === currentRO.length &&
        savedReadOnly.every(p => currentRO.includes(p));
      if ((!extrasMatch && savedExtras.length > 0) || !roMatch) {
        // Primary matches but extras or read-only policy don't. The server's
        // in-memory RO flag is ephemeral (reset on restart), so the durable
        // per-conv readOnlyPaths is the authority here — paint the bar from it
        // IMMEDIATELY, then re-hydrate the server in the BACKGROUND so the load
        // isn't gated on the slow setPaths round-trip (fs isdir + cross-DC probe).
        _applyProjectData({
          path: allPaths[0],
          readOnly: savedReadOnly.includes(allPaths[0]),
          extraRoots: allPaths.slice(1).map(p => ({ path: p, readOnly: savedReadOnly.includes(p) })),
          crossDC: data.crossDC || null,
        });
        debugLog("Re-hydrating server read-only policy in background", "info");
        Api.project.setPaths(allPaths, savedReadOnly)
          .then(fixResp => fixResp && fixResp.ok
            ? fixResp.json().catch(() => null) : null)
          .then(fixData => { if (fixData) _applyProjectData(fixData); })
          .catch(e2 => debugLog("Background RO re-hydrate failed: " + e2.message, "warn"));
      } else {
        _applyProjectData(data);
      }
      /* ★ FIX: Ensure conv.projectPath is set — same reason as _restoreConvProject fix. */
      if (conv) conv.projectPath = data.path || savedPath;

    } else {
      // Server has no project or a different one — restore from conv
      debugLog("Restoring project from conversation: " + savedPath, "info");
      try {
        // ★ Use multi-path API when conversation has extra roots OR any
        //   read-only root (read-only is only expressible via setPaths).
        const allPaths = (Array.isArray(conv.projectPaths) && conv.projectPaths.length)
          ? conv.projectPaths : [savedPath];
        const savedReadOnly = (Array.isArray(conv.readOnlyPaths) ? conv.readOnlyPaths : [])
          .filter(p => allPaths.includes(p));
        const hasExtras = allPaths.length > 1;
        const setResp = (hasExtras || savedReadOnly.length)
          ? await Api.project.setPaths(allPaths, savedReadOnly)
          : await Api.project.setPath(savedPath);
        const setData = setResp ? await setResp.json().catch(() => ({})) : {};
        if (setResp && setResp.ok) {
          _applyProjectData(setData);
          /* ★ FIX: Sync conv.projectPath after successful restore. */
          if (conv) conv.projectPath = setData.path || savedPath;
          debugLog("Project restored: " + savedPath + (hasExtras ? ` + ${allPaths.length - 1} extras` : ''), "success");
        } else {
          debugLog("Saved project path no longer valid, clearing", "warn");
          if (conv) {
            conv.projectPath = "";
            saveConversations(conv.id);
          }
          _clearProjectStateLocal();
        }
      } catch (e2) {
        debugLog("Project restore failed: " + e2.message, "warn");
      }
    }
  } catch (e) {
    debugLog("Project status load failed", "warn");
  }
}

// ══════════════════════════════════════════════════════
//  ★ Folder Browser
// ══════════════════════════════════════════════════════

let _browseState = { path: "", dirs: [], parent: null, showHidden: false };

function toggleFolderBrowser() {
  const el = document.getElementById("folderBrowser");
  const wasHidden = el.hidden;
  el.hidden = !wasHidden;
  if (wasHidden) {
    const inputPath = document.getElementById("mpPathInput").value.trim();
    browseDirectory(inputPath || "~");
  }
}

async function browseDirectory(path) {
  const listEl = document.getElementById("browseList");
  listEl.innerHTML =
    '<div class="fb-state"><div class="fb-state-spinner"></div><span>Loading…</span></div>';
  try {
    const data = await Api.project.browse(path, _browseState.showHidden);
    if (!data) {
      listEl.innerHTML = '<div class="fb-state fb-state-error"><span>Browse failed</span></div>';
      return;
    }
    if (data.error) {
      listEl.innerHTML =
        '<div class="fb-state fb-state-error"><span>' +
        escapeHtml(data.error) +
        "</span></div>";
      return;
    }
    _browseState.path = data.path;
    _browseState.dirs = data.dirs || [];
    _browseState.parent = data.parent;

    _renderBreadcrumb(data.path);
    document.getElementById("browseBackBtn").disabled = !data.parent;

    if (_browseState.dirs.length === 0) {
      listEl.innerHTML =
        '<div class="fb-state"><span>No subdirectories' +
        (data.filesCount ? " · " + data.filesCount + " files" : "") +
        "</span></div>";
      return;
    }

    listEl.innerHTML = _browseState.dirs
      .map(function (d) {
        var badge = d.hasCode
          ? '<span class="folder-code-badge">code</span>'
          : "";
        var hidden = d.hidden ? " folder-hidden" : "";
        var added = _mpFolders.includes(d.path) ? " folder-added" : "";
        var items =
          d.itemCount > 0
            ? '<span class="folder-item-count">' +
              (d.itemCount > 100 ? "100+" : d.itemCount) +
              "</span>"
            : "";
        var safePath = d.path.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
        var icon = d.hasCode
          ? '<svg class="fi-folder" width="16" height="16" viewBox="0 0 24 24" fill="rgba(245,158,11,0.14)" stroke="#f59e0b" stroke-width="1.8" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>'
          : '<svg class="fi-folder" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" opacity="0.55"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
        return (
          '<div class="folder-item' + hidden + added +
          '" onclick="browseDirectory(\'' + safePath + '\')" title="Open ' + escapeHtml(d.name) + '">' +
          '<span class="folder-icon">' + icon + "</span>" +
          '<span class="folder-name">' + escapeHtml(d.name) + "</span>" +
          badge + items +
          '<button class="folder-add-btn" onclick="event.stopPropagation();mpAddBrowsedPath(\'' + safePath + '\')" title="Add to workspace">' +
          '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>' +
          '</button>' +
          '<svg class="folder-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>' +
          "</div>"
        );
      })
      .join("");
  } catch (e) {
    listEl.innerHTML =
      '<div class="fb-state fb-state-error"><span>' +
      escapeHtml(e.message) +
      "</span></div>";
  }
}

/* Render a clickable breadcrumb trail (VS Code style) for the current path.
   Each crumb navigates to that ancestor on click. */
function _renderBreadcrumb(path) {
  const el = document.getElementById("browseCrumbs");
  if (!el) return;
  const parts = path.split("/").filter(Boolean);
  const isAbs = path.startsWith("/");
  const crumbs = [];
  // Root crumb: "/" for absolute paths, else the first segment.
  const rootPath = isAbs ? "/" : (parts[0] || path);
  crumbs.push(
    '<button class="pm-crumb pm-crumb-root" onclick="browseDirectory(\'' +
    rootPath.replace(/\\/g, "\\\\").replace(/'/g, "\\'") +
    '\')" title="' + escapeHtml(rootPath) + '">' +
    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>' +
    '</button>'
  );
  let walk = isAbs ? "" : parts[0] || "";
  const startIdx = isAbs ? 0 : 1;
  for (let i = startIdx; i < parts.length; i++) {
    walk = walk + "/" + parts[i];
    const full = isAbs ? walk : walk.replace(/^\//, "");
    const last = i === parts.length - 1;
    const safe = full.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
    crumbs.push('<span class="pm-crumb-sep">/</span>');
    crumbs.push(
      '<button class="pm-crumb' + (last ? ' pm-crumb-current' : '') +
      '" onclick="browseDirectory(\'' + safe + '\')" title="' + escapeHtml(full) + '">' +
      escapeHtml(parts[i]) + '</button>'
    );
  }
  el.innerHTML = crumbs.join("");
  // Scroll to the end so the deepest crumb is visible.
  el.scrollLeft = el.scrollWidth;
}

function selectFolderItem(el, path) {
  document.querySelectorAll(".folder-item.selected").forEach(function (e) {
    e.classList.remove("selected");
  });
  el.classList.add("selected");
  document.getElementById("mpPathInput").value = path;
}

/* Add a folder shown in the browser straight into the workspace list.
   Re-renders the current directory so the row's "added" state updates. */
function mpAddBrowsedPath(path) {
  if (path && !_mpFolders.includes(path)) {
    _mpFolders.push(path);
    _mpRenderTags();
    browseDirectory(_browseState.path);
  }
}

function browseParent() {
  if (_browseState.parent) browseDirectory(_browseState.parent);
}

function toggleHiddenDirs() {
  _browseState.showHidden = !_browseState.showHidden;
  var btn = document.getElementById("browseHiddenBtn");
  btn.classList.toggle("active", _browseState.showHidden);
  btn.title = _browseState.showHidden ? "Hide hidden dirs" : "Show hidden dirs";
  browseDirectory(_browseState.path);
}

function selectBrowsedFolder() {
  // Docked browser: add the current open directory to the workspace list.
  // Single-click now navigates, so the footer button targets the dir the
  // browser is currently showing. The browser stays open for more adds.
  const input = document.getElementById("mpPathInput");
  const path = input.value.trim() || _browseState.path;
  if (path && !_mpFolders.includes(path)) {
    _mpFolders.push(path);
    _mpRenderTags();
    browseDirectory(_browseState.path);
  }
  input.value = "";
}

// ══════════════════════════════════════════════════════
//  ★ Apply Code to File
// ══════════════════════════════════════════════════════

let _applyPendingCode = "";

function openApplyModal(btn) {
  if (!projectState.active) {
    debugLog("No project set — cannot apply code", "warn");
    return;
  }
  var pre = btn.closest("pre");
  var code = pre.querySelector("code");
  if (!code) return;
  _applyPendingCode = code.textContent;

  var detectedPath = _detectFilePath(pre, code);
  document.getElementById("applyFilePath").value = detectedPath || "";

  var lines = _applyPendingCode.split("\n");
  var preview =
    lines.length > 20
      ? lines.slice(0, 10).join("\n") +
        "\n  … (" +
        (lines.length - 20) +
        " more lines) …\n" +
        lines.slice(-10).join("\n")
      : _applyPendingCode;
  document.getElementById("applyPreview").innerHTML =
    '<div style="font-size:11px;color:var(--text-tertiary);margin-bottom:4px">' +
    lines.length +
    " lines · " +
    _applyPendingCode.length.toLocaleString() +
    " chars</div>" +
    '<pre style="max-height:300px;overflow:auto;font-size:12px;padding:8px;background:var(--bg-primary);border-radius:6px;margin:0"><code>' +
    escapeHtml(preview) +
    "</code></pre>";

  document.getElementById("applyStatus").innerHTML = "";
  document.getElementById("applyConfirmBtn").disabled = false;
  document.getElementById("applyConfirmBtn").textContent = "Write File";
  document.getElementById("applyModal").classList.add("open");
  setTimeout(function () {
    document.getElementById("applyFilePath").focus();
  }, 100);
}

function closeApplyModal() {
  document.getElementById("applyModal").classList.remove("open");
  _applyPendingCode = "";
}

function _detectFilePath(preEl, codeEl) {
  var firstLine = (codeEl.textContent || "").split("\n")[0] || "";
  var fileCommentMatch = firstLine.match(
    /^(?:#|\/\/|\/\*|<!--)\s*(?:file|path|filename):\s*(.+?)(?:\s*(?:\*\/|-->))?$/i,
  );
  if (fileCommentMatch) return fileCommentMatch[1].trim();
  var node = preEl.previousElementSibling;
  for (var i = 0; i < 3 && node; i++) {
    var text = node.textContent || "";
    var pathMatch = text.match(/`([^`]+\.\w{1,10})`\s*[:：]?\s*$/);
    if (
      pathMatch &&
      (pathMatch[1].indexOf("/") >= 0 || pathMatch[1].indexOf(".") >= 0)
    ) {
      return pathMatch[1];
    }
    var fileMatch = text.match(
      /(?:file|文件)[：:]\s*[`"']?([^\s`"']+\.\w{1,10})/i,
    );
    if (fileMatch) return fileMatch[1];
    node = node.previousElementSibling;
  }
  return "";
}

async function confirmApplyCode() {
  var path = document.getElementById("applyFilePath").value.trim();
  if (!path) {
    document.getElementById("applyStatus").innerHTML =
      '<div style="color:var(--error-text);font-size:12px;margin-top:8px">Please enter a file path</div>';
    return;
  }
  if (!_applyPendingCode) return;

  var btn = document.getElementById("applyConfirmBtn");
  btn.disabled = true;
  btn.textContent = "Writing…";
  document.getElementById("applyStatus").innerHTML = "";

  try {
    var data = await Api.project.write(path, _applyPendingCode);
    if (data && data.ok) {
      var action = data.created ? "Created" : "Updated";
      document.getElementById("applyStatus").innerHTML =
        '<div style="color:#34d399;font-size:12px;margin-top:8px">' +
        action +
        ": " +
        escapeHtml(data.path) +
        " (" +
        data.lines +
        " lines)</div>";
      debugLog(
        "Applied code to " +
          data.path +
          " (" +
          data.lines +
          " lines, " +
          (data.created ? "created" : "updated") +
          ")",
        "success",
      );
      setTimeout(function () {
        closeApplyModal();
      }, 1200);
    } else {
      throw new Error(data.error || "Write failed");
    }
  } catch (e) {
    document.getElementById("applyStatus").innerHTML =
      '<div style="color:var(--error-text);font-size:12px;margin-top:8px">' +
      escapeHtml(e.message) +
      "</div>";
    btn.disabled = false;
    btn.textContent = "Write File";
  }
}
