/* ===== migrated source: project_state.js ===== */
/* project_state.js — the project STATE subset (CORE bundle), split out of
 * project.js 2026-08-01 (Epic-E  sub-7).
 *
 * Stays in core because these are called from boot / first-paint / SSE paths:
 * main.js:1354-1355 (loadProjectStatus + _updateAutoApplyUI at boot, BARE),
 * sse_handlers_tool.js:177 + sse_handlers_misc.js:389 (_applyProjectData on
 * project SSE events, BARE), main_conv_lifecycle.js (_restoreConvProject /
 * _clearProjectStateLocal on conv switch), main.js:588 + presence.js +
 * project-brain*.js (_saveConvProjectPath / _getConvProjectPath), and the
 * always-visible project-bar render+interact cluster (_updateProjectUI /
 * toggleProjectBarReadOnly / toggleAutoApply / clearProject).
 *
 * The demand-loaded sibling project.js owns the workspace modal, browser,
 * and folder-drop UI. Coding approvals/stdin/apply-code remain in the small
 * retained execution-interactions.js section. Core state is exposed to the
 * modal through an explicit live port; reverse cleanup uses an optional owner
 * object and never loads the modal merely because a project was cleared.
 */

function toggleAutoApply() {
  autoApplyWrites = !autoApplyWrites;
  _updateAutoApplyUI();
  /* Per-conversation override (2026-08-21 policy: default Auto; Manual only
   * when the user explicitly switches THIS conversation). Stamp the conv and
   * persist through the standard conversation-settings seam (local capture +
   * debounced
   * server PATCH), so the choice survives reloads and conv switches. */
  const conv = getActiveConv();
  if (conv) {
    conv.autoApply = autoApplyWrites;
    captureActiveConversationSettings();
  }
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
  btn.querySelector(".autoapply-label").textContent = t(autoApplyWrites
    ? 'conversationBar.auto' : 'conversationBar.manual');
  btn.title = t(autoApplyWrites ? 'conversationBar.autoHint' : 'conversationBar.manualHint');
  btn.setAttribute('aria-label', btn.title);
  btn.setAttribute('aria-pressed', String(autoApplyWrites));
}

// Per-conversation project path helpers

function _saveConvProjectPath(path, extraPaths, readOnlyPaths) {
  _conversationProjectController.begin();
  const conv = getActiveConv();
  if (conv) {
    conv.projectPath = path || "";
    // Persist ALL project paths (primary + extras) so they survive conversation switches
    conv.projectPaths = [];
    if (path) conv.projectPaths.push(path);
    if (Array.isArray(extraPaths)) {
      for (const ep of extraPaths) {
        if (ep && !conv.projectPaths.includes(ep)) conv.projectPaths.push(ep);
      }
    }
    // Persist the read-only subset (parallel list, mirrors backend model).
    //   Only keep entries that are actually among the configured paths.
    if (Array.isArray(readOnlyPaths)) {
      conv.readOnlyPaths = readOnlyPaths.filter(p => p && conv.projectPaths.includes(p));
    } else {
      conv.readOnlyPaths = [];
    }
    reconcileConversationCatalogMetadata(conv.id);
    persistConversationSettings(conv);
  }
}

function _getConvProjectPath(conv) {
  return (conv && conv.projectPath) || "";
}

// Turn reconciliation requests a background refresh independently of whether
// the Project workspace presenter has ever been opened.
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

/* 伪路径会话的项目栏合成态:不调 setPaths(服务器无法扫描),
   只渲染 active bar + 徽章,身份仍保留完整伪路径。 */
function _applyRemoteProjectState(conv, pseudo) {
  projectState = {
    active: true,
    path: pseudo,
    fileCount: 0,
    dirCount: 0,
    totalSize: 0,
    languages: {},
    scanning: false,
    scanProgress: "",
    scanDetail: "",
    scannedAt: Date.now(),
    readOnly: (conv.readOnlyPaths || []).includes(pseudo),
    extraRoots: (conv.projectPaths || []).filter(path => path !== pseudo).map(path => ({
      path, readOnly: (conv.readOnlyPaths || []).includes(path),
    })),
  };
  debugLog("Remote worktree active for conversation: " + pseudo, "info");
  _updateProjectUI();
}

function _clearProjectStateLocal() {
  // Reset local projectState without touching server when the next
  // conversation has no project. Project exploration no longer owns a poll.
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

// Temporary retained adapter; delete when project presentation moves to TypeScript.
function _hasProjectComposerDraft() {
  // Cleanup suggestions are derived from a past paste, not sendable draft
  // content. A late suggestion can outlive an emptied textarea; counting it
  // independently blocks both model and project restoration on navigation.
  return !!(document.getElementById('userInput')?.value.trim()
    || pendingImages.length || pendingPdfTexts.length
    || (typeof pendingVideos !== 'undefined' && pendingVideos.length));
}
const _conversationProjectController = createConversationProjectController({
  activeId: () => activeConvId,
  exists: id => conversations.some(conv => conv && conv.id === id),
  hasDraft: _hasProjectComposerDraft,
  selection: () => projectState,
  clear: _clearProjectStateLocal,
  remote: _applyRemoteProjectState,
  apply: _applyProjectData,
  setPaths: (paths, readOnly, recent) => Api.project.setPaths(paths, readOnly, recent),
  warn: message => debugLog(message, 'warn'),
});
function _restoreConvProject(conv, eligible) {
  return _conversationProjectController.restore(conv, eligible);
}


let _projectBarFolders = [];

let _projectContextView = null;

async function clearProject() {
  const current = _conversationProjectController.beginMutation();
  await Api.project.clear().catch(e => debugLog(`[clearProject] ${e.message}`, 'warn'));
  if (!current()) return;
  _saveConvProjectPath("");
  projectState = {
    active: false, path: "", fileCount: 0, dirCount: 0, totalSize: 0,
    languages: {}, scanning: false, scanProgress: "", scanDetail: "",
    scannedAt: 0, extraRoots: [], readOnly: false,
  };
  _updateProjectUI();
  const projectModalPresentation = runtimeScope.ProjectModalPresentation;
  if (projectModalPresentation
      && typeof projectModalPresentation.resetAfterProjectClear === 'function') {
    projectModalPresentation.resetAfterProjectClear();
  }
  /* A project-less chat is never Studio — fall back to Pro (unless the user
   * is deliberately in Air). Keeps the dial truthful. */
  if (typeof onProjectCleared === 'function') onProjectCleared();
  debugLog("Project cleared", "success");
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
  // Merge in extra roots — backend always sends extraRoots[] in get_state()
  if (Array.isArray(data.extraRoots)) {
    projectState.extraRoots = data.extraRoots;
  }
  // Primary root read-only flag (backend sends `readOnly` in get_state()).
  if (data.readOnly !== undefined) {
    projectState.readOnly = !!data.readOnly;
  }
  // Cross-DC indicator from backend
  if (data.crossDC) {
    projectState.crossDC = data.crossDC;
  } else {
    projectState.crossDC = null;
  }
  /* Git branch context — the backend now probes each root's checked-out
   * branch (gitBranch / gitBranches). Hand it to the info-rail's live
   * branch state FIRST (it owns the per-turn rail re-render + the
   * branch-switch signal), then repaint the project bar. A mid-run
   * `git checkout` surfaces here via the turn-reconciliation rescan that
   * re-fetches project status. */
  if (typeof runtimeScope.applyProjectGitState === 'function'
      && (Object.prototype.hasOwnProperty.call(data, 'gitBranch')
          || Object.prototype.hasOwnProperty.call(data, 'gitBranches'))) {
    runtimeScope.applyProjectGitState(data);
  }
  _updateProjectUI();
}

function _updateProjectUI() {
  /* Every projectState mutation funnels through here to repaint the
   *   project bar — attach / clear / rollback / restore / remote-state —
   *   with the state already final. So this is THE seam that re-resolves
   *   the Project-Brain surfaces (collab bar + an open Brain panel), which
   *   key on getActiveConv()/projectState: sprinkling the refresh at
   *   individual callers left the same stale-bar window on the clear and
   *   attach paths that the newChat-only fix closed. Cheap: presenceRefresh
   *   renders are fingerprint-gated and its refetch is debounced. */
  if (typeof runtimeScope.presenceRefresh === 'function') runtimeScope.presenceRefresh();
  if (typeof runtimeScope.projectBrainRefresh === 'function') runtimeScope.projectBrainRefresh();
  const bar = document.getElementById("projectBar");
  const badge = document.getElementById("projectBadge");
  const toggle = document.getElementById("projectToggle");
  badge?.classList.toggle('visible', !!projectState.active);
  toggle?.classList.toggle('active', !!projectState.active);
  if (bar && !_projectContextView) {
    _projectContextView = createProjectContext({
      bar, t,
      manage: () => runtimeScope.openProjectModal(),

      openPaper: () => { if (activeConvId) runtimeScope._paperPreviewOpen(activeConvId); },
      clear: () => { void clearProject(); },
      toggleReadOnly: index => { void toggleProjectBarReadOnly(index); },
    });
  }

  // ── Render folder badges ──
  // BUG FIX: Build badge list directly from projectState instead of
  // calling _syncFoldersFromState() which would overwrite _mpFolders.
  // _mpFolders is the user's in-progress edits in the modal — clobbering
  // it here creates a race: background poll → _updateProjectUI →
  // _syncFoldersFromState silently restores removed paths, making them
  // impossible to delete from the modal.
  const _barFolders = [];
  if (projectState.active && projectState.path) {
    _barFolders.push({ path: projectState.path, readOnly: !!projectState.readOnly });
  }
  if (projectState.active && projectState.extraRoots && projectState.extraRoots.length) {
    for (const r of projectState.extraRoots) {
      const p = typeof r === 'string' ? r : r.path;
      const ro = typeof r === 'object' ? !!r.readOnly : false;
      if (p && !_barFolders.some((b) => b.path === p)) {
        _barFolders.push({ path: p, readOnly: ro });
      }
    }
  }
  _projectBarFolders = _barFolders;
  const dc = projectState.active && projectState.crossDC;
  _projectContextView?.update({
    roots: _barFolders,
    status: dc && dc.latencyClass !== 'local'
      ? t('conversationBar.remoteStatus', { cluster: dc.cluster || '', latency: dc.latencyMs || '?' }) : '',
  });
  _updateAutoApplyUI();
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

  const current = _conversationProjectController.beginMutation();

  try {
    const resp = await Api.project.setPaths(folders, roList);
    const data = resp ? await resp.json().catch(() => ({})) : {};
    if (!resp || !resp.ok) throw new Error(data.error || "Failed");
    if (!current()) return; // navigation, restore or a newer selection owns the bar
    // Single source of truth: render + persist EXACTLY what the backend returned.
    _applyProjectData(data);
    const { allPaths, roList: srvRo } = _deriveConvPathsFromState(data);
    _saveConvProjectPath(allPaths[0] || '', allPaths.slice(1), srvRo);
  } catch (e) {
    if (!current()) return; // never restore the previous conversation's roots
    projectState = _prevProjectState;
    _updateProjectUI();
    debugLog(`Read-only toggle failed: ${e.message}`, "error");
  }
}


async function loadProjectStatus() {

  _updateProjectUI();
  // Cold shells restore only after authoritative settings hydration.
  const conv = getActiveConv();
  if (conv?._turnSnapshotRequired) return;
  return _restoreConvProject(conv);
}
