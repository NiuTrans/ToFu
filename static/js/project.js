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
    const resp = await fetch(apiUrl("/api/project/write_approval"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approvalId, approved }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      debugLog("Approval failed: " + (data.error || "Unknown"), "warn");
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
    const resp = await fetch(apiUrl("/api/chat/stdin_response"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stdinId, input: inputText }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      debugLog("Stdin submit failed: " + (data.error || "Unknown"), "warn");
      if (typeof showToast === 'function')
        showToast("", "Stdin Error", data.error || "Failed to send input", 5000);
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
    const resp = await fetch(apiUrl("/api/chat/stdin_response"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stdinId, input: "", eof: true }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      debugLog("Stdin EOF failed: " + (data.error || "Unknown"), "warn");
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
    const resp = await fetch(apiUrl("/api/chat/human_response"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ guidanceId, response: responseText }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      debugLog("Human guidance submit failed: " + (data.error || "Unknown"), "warn");
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
  const _hgAutoTrans = conv ? (conv.autoTranslate !== undefined ? !!conv.autoTranslate : true) : !!autoTranslate;
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
  if (!assistantMsg || !assistantMsg.searchRounds) return;
  const round = assistantMsg.searchRounds.find(r => r.guidanceId === guidanceId);
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
    buf.searchRounds = assistantMsg.searchRounds.map(r => ({...r}));
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
function _saveConvProjectPath(path, extraPaths) {
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
  // Without this, _doScanPoll / _pollIndexStatus keep fetching the old project
  // from the server and _applyProjectData resurrects projectState.active=true,
  // making it impossible to clear the project bar (e.g. on "New Chat").
  _stopScanPoll();
  _indexPollAborted = true;   // signal _pollIndexStatus loop to stop
  projectState = {
    active: false,
    path: "",
    fileCount: 0,
    dirCount: 0,
    totalSize: 0,
    languages: {},
    indexed: false,
    indexedCount: 0,
    indexing: false,
    indexProgress: "",
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
  try {
    // ★ Use multi-path API when there are extra roots, single-path otherwise
    const endpoint = hasExtras ? "/api/project/set_paths" : "/api/project/set";
    const payload = hasExtras ? { paths: allPaths } : { path: savedPath };
    const resp = await fetch(apiUrl(endpoint), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (resp.ok) {
      _applyProjectData(data);
      if (data.scanning) _startScanPoll();
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
      saveConversations(conv.id);
    }
  } catch (e) {
    debugLog("Project restore failed: " + e.message, "warn");
  }
}

// ── Multi-path folder state for the modal ──
let _mpFolders = []; // array of path strings being edited in the modal

function _syncFoldersFromState() {
  // Build _mpFolders from projectState (single source of truth)
  _mpFolders = [];
  if (projectState.path) _mpFolders.push(projectState.path);
  if (projectState.extraRoots && projectState.extraRoots.length) {
    for (const r of projectState.extraRoots) {
      const p = typeof r === 'string' ? r : r.path;
      if (p && !_mpFolders.includes(p)) _mpFolders.push(p);
    }
  }
}

function openProjectModal() {
  _syncFoldersFromState();
  _mpRenderTags();
  _updateProjectModalStatus();
  renderRecentProjects();
  document.getElementById("projectModal").classList.add("open");
  setTimeout(() => document.getElementById("mpPathInput").focus(), 100);
}

function closeProjectModal() {
  document.getElementById("projectModal").classList.remove("open");
}

/* ── Multi-path tag rendering ── */
function _mpRenderTags() {
  const container = document.getElementById("mpFolderTags");
  if (!_mpFolders.length) {
    container.innerHTML = '<div class="mp-empty-hint">No folders added yet — type a path below or browse.</div>';
    return;
  }
  container.innerHTML = _mpFolders.map((p, i) => {
    const short = p.split('/').filter(Boolean).slice(-2).join('/') || p;
    return `<div class="mp-tag" title="${escapeHtml(p)}">
      <span class="mp-tag-path">${escapeHtml(short)}</span>
      <button class="mp-tag-remove" onclick="_mpRemove(${i})" title="Remove">✕</button>
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
  _mpFolders.splice(index, 1);
  _mpRenderTags();
}

function _mpBrowseForAdd() {
  const el = document.getElementById("folderBrowser");
  const visible = el.style.display !== "none";
  el.style.display = visible ? "none" : "block";
  if (!visible) {
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
    document.getElementById("folderBrowser").style.display = "none";
  }
}

/* ★ mpApplyFolders — the new unified "Set & Scan" action.
   First path → primary project; remaining → extra roots. */
async function mpApplyFolders() {
  if (!_mpFolders.length) return;
  // ★ Ensure we have an active conversation
  if (!activeConvId) {
    const now = Date.now();
    const conv = {
      id: generateId(), title: "New Chat", messages: [],
      createdAt: now, updatedAt: now, activeTaskId: null, projectPath: "",
    };
    conversations.unshift(conv);
    activeConvId = conv.id;
    /* ★ Persist immediately — the conv only exists in memory until synced.
     * saveConversations sorts + broadcasts; the conv will reach the server
     * once the user sends a message (via syncConversationToServer). */
    saveConversations(conv.id);
    renderConversationList();
  }
  const statusEl = document.getElementById("projectModalStatus");
  statusEl.innerHTML = '<div style="color:var(--thinking-text);font-size:12px">Applying…</div>';
  try {
    // ★ Single atomic call — send all paths at once
    const resp = await fetch(apiUrl("/api/project/set_paths"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: _mpFolders }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Failed");
    _applyProjectData(data);
    _saveConvProjectPath(data.path, _mpFolders.slice(1));
    saveRecentProject(data.path);

    closeProjectModal();
    const nExtras = _mpFolders.length - 1;
    debugLog(`Project set: ${data.path}` + (nExtras ? ` + ${nExtras} extra folder(s)` : ''), "success");
    if (data.scanning) _startScanPoll();
  } catch (e) {
    statusEl.innerHTML = `<div style="color:var(--error-text);font-size:12px">${escapeHtml(e.message)}</div>`;
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
  await fetch(apiUrl("/api/project/clear"), { method: "POST" }).catch(e => debugLog(`[clearProject] ${e.message}`, 'warn'));
  _saveConvProjectPath("");
  _mpFolders = [];
  projectState = {
    active: false, path: "", fileCount: 0, dirCount: 0, totalSize: 0,
    languages: {}, indexed: false, indexedCount: 0, indexing: false,
    indexProgress: "", scanning: false, scanProgress: "", scanDetail: "",
    scannedAt: 0, extraRoots: [],
  };
  _updateProjectUI();
  closeProjectModal();
  debugLog("Project cleared", "success");
}

// ── Recent Project Paths (server-side persistence) ──

function saveRecentProject(path) {
  if (!path) return;
  fetch(apiUrl("/api/project/recent"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  }).catch(e => debugLog(`[saveRecentProject] ${e.message}`, 'warn'));
}

async function renderRecentProjects() {
  const container = document.getElementById("recentProjectPaths");
  const listEl = document.getElementById("recentPathsList");
  if (!container || !listEl) return;
  let list = [];
  try {
    const resp = await fetch(apiUrl("/api/project/recent"));
    if (resp.ok) {
      const data = await resp.json();
      list = Array.isArray(data) ? data : data.projects || [];
    }
  } catch {}
  if (list.length === 0) {
    container.style.display = "none";
    return;
  }
  container.style.display = "";
  listEl.innerHTML = list
    .map((item) => {
      const parts = item.path.split("/").filter(Boolean);
      const name = parts.pop() || item.path;
      const shortPath =
        parts.length <= 2
          ? item.path
          : "…/" + parts.slice(-1).join("/") + "/" + name;
      return `<div class="recent-path-item" onclick="selectRecentProject('${escapeHtml(item.path)}')" title="${escapeHtml(item.path)}">
         <span class="recent-path-name">${escapeHtml(name)}</span>
         <span class="recent-path-full">${escapeHtml(shortPath)}</span>
         ${item.count > 1 ? `<span class="recent-path-count">×${item.count}</span>` : ""}
       </div>`;
    })
    .join("");
}

function selectRecentProject(path) {
  // Add the recent project path into the multi-path list and apply
  if (path && !_mpFolders.includes(path)) {
    _mpFolders.push(path);
    _mpRenderTags();
  }
}

async function clearRecentProjects() {
  await fetch(apiUrl("/api/project/recent"), { method: "DELETE" }).catch(
    () => {},
  );
  renderRecentProjects();
}

async function rescanProject() {
  if (!projectState.active) return;
  try {
    const resp = await fetch(apiUrl("/api/project/rescan"), { method: "POST" });
    const data = await resp.json();
    if (resp.ok) {
      _applyProjectData(data);
      if (data.scanning) _startScanPoll();
      debugLog("Project rescan started", "success");
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
    !confirm(
      `确定要撤销本轮对话的 ${count} 处代码修改吗？\n此操作将恢复这些文件到修改前的状态。`,
    )
  )
    return;
  try {
    // ★ Per-round undo: prefer taskId (specific to this round), fallback to convId
    const body = msg._taskId
      ? { taskId: msg._taskId }
      : { convId: conv.id };
    const resp = await fetch(apiUrl("/api/project/undo"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (resp.ok && data.ok) {
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
    !confirm(
      "确定要撤销所有代码修改吗？\n\n此操作将恢复所有被修改的文件到原始状态，包括所有对话中的修改。",
    )
  )
    return;
  try {
    const resp = await fetch(apiUrl("/api/project/undo_all"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await resp.json();
    if (resp.ok && data.ok) {
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

async function indexProject() {
  if (!projectState.active || projectState.scanning || projectState.indexing)
    return;
  try {
    const resp = await fetch(apiUrl("/api/project/index"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await resp.json();
    if (data.status === "error") {
      debugLog("Index: " + (data.message || "error"), "warn");
      return;
    }
    if (data.status === "skipped") {
      debugLog(
        "Index skipped: " +
          (data.message || "project too large, using tool-based exploration"),
        "info",
      );
      return;
    }
    if (data.status === "started" || data.status === "already_running") {
      projectState.indexing = true;
      _updateProjectUI();
      _pollIndexStatus();
      debugLog("Project indexing started", "success");
    }
  } catch (e) {
    debugLog("Index failed: " + e.message, "warn");
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
    indexed: data.indexed ?? projectState.indexed,
    indexedCount: data.indexedCount ?? projectState.indexedCount,
    indexing: data.indexing ?? projectState.indexing,
    indexProgress: data.indexProgress || projectState.indexProgress,
    scanning: data.scanning ?? false,
    scanProgress: data.scanProgress || "",
    scanDetail: data.scanDetail || "",
    scannedAt: data.scannedAt ?? projectState.scannedAt,
  };
  // ★ Merge in extra roots — backend always sends extraRoots[] in get_state()
  if (Array.isArray(data.extraRoots)) {
    projectState.extraRoots = data.extraRoots;
  }
  _updateProjectUI();
}

function _startScanPoll() {
  _stopScanPoll();
  _scanPollTimer = setInterval(_doScanPoll, 250);
}

function _stopScanPoll() {
  if (_scanPollTimer) {
    clearInterval(_scanPollTimer);
    _scanPollTimer = null;
  }
}

async function _doScanPoll() {
  try {
    const resp = await fetch(apiUrl("/api/project/status"));
    if (!resp.ok) {
      _stopScanPoll();
      return;
    }
    const data = await resp.json();
    _applyProjectData(data);
    if (!data.scanning) {
      _stopScanPoll();
      debugLog(
        `Scan complete: ${data.fileCount} files, ${data.dirCount} dirs`,
        "success",
      );
      // Auto-index after scan completes (if not already indexed/indexing)
      if (
        !projectState.indexed &&
        !projectState.indexing &&
        projectState.fileCount > 0
      ) {
        debugLog("Auto-indexing project…", "info");
        indexProject();
      }
    }
  } catch (e) {
    _stopScanPoll();
    debugLog("Scan poll error: " + e.message, "warn");
  }
}

let _indexPollAborted = false;

async function _pollIndexStatus() {
  _indexPollAborted = false;
  for (let i = 0; i < 300; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    if (_indexPollAborted) break;   // ★ Stop if project was cleared
    try {
      const resp = await fetch(apiUrl("/api/project/status"));
      const data = await resp.json();
      if (_indexPollAborted) break;  // ★ Re-check after await
      projectState.indexing = data.indexing;
      projectState.indexProgress = data.indexProgress;
      projectState.indexed = data.indexed;
      projectState.indexedCount = data.indexedCount || 0;
      _updateProjectUI();
      if (!data.indexing) break;
    } catch {
      break;
    }
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
    bar.classList.remove("scanning", "indexing");
    badge.classList.remove("visible");
    toggle.classList.remove("active");
    return;
  }

  bar.style.display = "flex";
  badge.classList.add("visible");
  toggle.classList.add("active");

  // ── Render folder badges ──
  // ★ BUG FIX: Build badge list directly from projectState instead of
  // calling _syncFoldersFromState() which would overwrite _mpFolders.
  // _mpFolders is the user's in-progress edits in the modal — clobbering
  // it here creates a race: background poll → _updateProjectUI →
  // _syncFoldersFromState silently restores removed paths, making them
  // impossible to delete from the modal.
  const _barFolders = [];
  if (projectState.path) _barFolders.push(projectState.path);
  if (projectState.extraRoots && projectState.extraRoots.length) {
    for (const r of projectState.extraRoots) {
      const p = typeof r === 'string' ? r : r.path;
      if (p && !_barFolders.includes(p)) _barFolders.push(p);
    }
  }
  const badges = _barFolders.map((p) => {
    const short = p.split('/').filter(Boolean).pop() || p;
    return `<span class="folder-badge" title="${escapeHtml(p)}">${escapeHtml(short)}</span>`;
  });
  foldersEl.innerHTML = badges.join('');

  // ── Stats line ──
  if (projectState.scanning) {
    bar.classList.add("scanning");
    bar.classList.remove("indexing");
    let txt = (projectState.scanProgress || "Scanning…");
    if (projectState.scanDetail) txt += " · " + projectState.scanDetail;
    statsEl.textContent = txt;
  } else if (projectState.indexing) {
    bar.classList.remove("scanning");
    bar.classList.add("indexing");
    statsEl.innerHTML = `${projectState.fileCount} files, ${projectState.dirCount} dirs &middot; ${projectState.indexProgress || "Indexing…"}`;
  } else {
    bar.classList.remove("scanning", "indexing");
    let parts = [`${projectState.fileCount} files, ${projectState.dirCount} dirs`];
    if (projectState.indexed) parts.push(`${projectState.indexedCount} indexed`);
    statsEl.innerHTML = parts.join(" &middot; ");
  }
}

function _updateProjectModalStatus() {
  const el = document.getElementById("projectModalStatus");
  if (!el) return;
  if (!projectState.active) { el.innerHTML = ""; return; }
  const total = _mpFolders.length;
  const scan = projectState.scanning ? " · Scanning…" : "";
  el.innerHTML = `<div style="font-size:12px;color:#34d399;margin-bottom:12px">
    ✓ ${total} folder${total > 1 ? 's' : ''} active &middot; ${projectState.fileCount} files, ${projectState.dirCount} dirs${projectState.indexed ? ` &middot; <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px;opacity:0.7"><rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/></svg> Indexed` : ""}${scan}
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
    const resp = await fetch(apiUrl("/api/project/status"));
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.path && data.path === savedPath) {
      // Server already has this project active — great
      _applyProjectData(data);
      /* ★ FIX: Ensure conv.projectPath is set — same reason as _restoreConvProject fix. */
      if (conv) conv.projectPath = data.path || savedPath;
      if (data.scanning) _startScanPoll();
      if (data.indexing) _pollIndexStatus();
    } else {
      // Server has no project or a different one — restore from conv
      debugLog("Restoring project from conversation: " + savedPath, "info");
      try {
        const setResp = await fetch(apiUrl("/api/project/set"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: savedPath }),
        });
        const setData = await setResp.json();
        if (setResp.ok) {
          _applyProjectData(setData);
          /* ★ FIX: Sync conv.projectPath after successful restore. */
          if (conv) conv.projectPath = setData.path || savedPath;
          if (setData.scanning) _startScanPoll();
          debugLog("Project restored: " + savedPath, "success");
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
  const visible = el.style.display !== "none";
  el.style.display = visible ? "none" : "block";
  if (!visible) {
    const inputPath = document.getElementById("mpPathInput").value.trim();
    browseDirectory(inputPath || "~");
  }
}

async function browseDirectory(path) {
  const listEl = document.getElementById("browseList");
  listEl.innerHTML =
    '<div style="color:var(--text-tertiary);padding:16px;text-align:center;font-size:12px">Loading…</div>';
  try {
    const resp = await fetch(apiUrl("/api/project/browse"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, showHidden: _browseState.showHidden }),
    });
    const data = await resp.json();
    if (data.error) {
      listEl.innerHTML =
        '<div style="color:var(--error-text);padding:16px;text-align:center;font-size:12px">' +
        escapeHtml(data.error) +
        "</div>";
      return;
    }
    _browseState.path = data.path;
    _browseState.dirs = data.dirs || [];
    _browseState.parent = data.parent;

    document.getElementById("browsePath").textContent = data.path;
    document.getElementById("browsePath").title = data.path;
    document.getElementById("browseBackBtn").disabled = !data.parent;

    if (_browseState.dirs.length === 0) {
      listEl.innerHTML =
        '<div style="color:var(--text-tertiary);padding:16px;text-align:center;font-size:12px">No subdirectories' +
        (data.filesCount ? " (" + data.filesCount + " files)" : "") +
        "</div>";
      return;
    }

    listEl.innerHTML = _browseState.dirs
      .map(function (d) {
        var badge = d.hasCode
          ? '<span class="folder-code-badge">code</span>'
          : "";
        var hidden = d.hidden ? " folder-hidden" : "";
        var items =
          d.itemCount > 0
            ? '<span class="folder-item-count">' +
              (d.itemCount > 100 ? "100+" : d.itemCount) +
              "</span>"
            : "";
        var safePath = d.path.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
        var icon = d.hasCode
          ? '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>'
          : '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" opacity="0.5"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
        return (
          '<div class="folder-item' +
          hidden +
          '" ondblclick="browseDirectory(\'' +
          safePath +
          "')\" onclick=\"selectFolderItem(this, '" +
          safePath +
          "')\">" +
          '<span class="folder-icon">' +
          icon +
          "</span>" +
          '<span class="folder-name">' +
          escapeHtml(d.name) +
          "</span>" +
          badge +
          items +
          "</div>"
        );
      })
      .join("");
  } catch (e) {
    listEl.innerHTML =
      '<div style="color:var(--error-text);padding:16px;text-align:center;font-size:12px">' +
      escapeHtml(e.message) +
      "</div>";
  }
}

function selectFolderItem(el, path) {
  document.querySelectorAll(".folder-item.selected").forEach(function (e) {
    e.classList.remove("selected");
  });
  el.classList.add("selected");
  document.getElementById("mpPathInput").value = path;
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
  // In the new multi-path design, selecting from the folder browser
  // adds the currently shown (or selected) path directly to the tag list
  const input = document.getElementById("mpPathInput");
  const path = input.value.trim() || _browseState.path;
  if (path && !_mpFolders.includes(path)) {
    _mpFolders.push(path);
    _mpRenderTags();
  }
  input.value = "";
  document.getElementById("folderBrowser").style.display = "none";
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
    var resp = await fetch(apiUrl("/api/project/write"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: path, content: _applyPendingCode }),
    });
    var data = await resp.json();
    if (data.ok) {
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
