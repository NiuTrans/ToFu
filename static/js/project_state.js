/* project_state.js — the project STATE subset (CORE bundle), split out of
 * project.js 2026-08-01 (Epic-E pt_3879f00e sub-7).
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
 * The DEFERRED sibling project.js (panel UI: folder modal, browser, recent
 * list, apply-code, drop zones, approval/stdin/HG submit handlers) calls
 * these at RUNTIME via window scope — core always loads first. In the other
 * direction, the two bare calls into the panel (saveRecentProject /
 * closeProjectModal / the _mpFolders reset) are typeof-guarded here.
 */

let _scanPollTimer = null;

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

/* RWA P4b-2a:伪路径约定 — conv.projectPath = 'remote:<agent_id>:<root>'
   表示项目是一棵远程工作树(docs/REMOTE_WORKTREE_DESIGN.md §5 P4)。
   服务器 fs 上没有这个路径:任何服务器侧项目机制(setPaths/scan)都
   不得碰它,工具执行走 desktop bridge 路由(cfg['project_remote'])。 */
function _isRemotePath(p) {
  return typeof p === 'string' && p.indexOf('remote:') === 0
    && p.slice(7).indexOf(':') > 0;
}

/* 伪路径会话的项目栏合成态:不调 setPaths(服务器无法扫描),
   只渲染 active bar + 徽章,身份仍保留完整伪路径。 */
function _applyRemoteProjectState(conv, pseudo) {
  _stopScanPoll();
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
    extraRoots: [],
  };
  debugLog("Remote worktree active for conversation: " + pseudo, "info");
  _updateProjectUI();
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
  // RWA: remote worktree pseudo-path — server-side project machinery
  // (setPaths/scan) must never touch it; render the synthetic bar state.
  if (_isRemotePath(savedPath)) {
    _applyRemoteProjectState(conv, savedPath);
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
    // ★ ALWAYS reconcile via the pruning multi-path endpoint — never the
    //   single-path setPath. set_project()'s `same_primary` guard PRESERVES
    //   any stale extra roots left in the process-global _roots registry by a
    //   PRIOR conversation (or a background task's absolute-path write
    //   auto-register), so setPath(chatui) would leave e.g. `tofu-search`
    //   showing on a chatui-only conversation. setPaths([chatui], []) prunes
    //   every global extra not in THIS conversation's saved set, making the
    //   conversation the single source of truth for the project bar.
    const resp = await Api.project.setPaths(allPaths, savedReadOnly);
    const data = resp ? await resp.json().catch(() => ({})) : {};
    if (resp && resp.ok) {
      _applyProjectData(data);
      // ★ BUG FIX: Update recent projects on restore so new projects appear
      //   in the recent list and last_used stays current.
      if (typeof saveRecentProject === 'function') saveRecentProject(data.path);  // deferred panel module (Epic-E sub-7)
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


let _projectBarFolders = [];

async function clearProject() {
  _stopScanPoll();
  await Api.project.clear().catch(e => debugLog(`[clearProject] ${e.message}`, 'warn'));
  _saveConvProjectPath("");
  if (typeof _mpFolders !== 'undefined') { _mpFolders = []; _mpReadOnly = new Set(); }  // modal state lives in the deferred panel (Epic-E sub-7)
  projectState = {
    active: false, path: "", fileCount: 0, dirCount: 0, totalSize: 0,
    languages: {}, scanning: false, scanProgress: "", scanDetail: "",
    scannedAt: 0, extraRoots: [], readOnly: false,
  };
  _updateProjectUI();
  if (typeof closeProjectModal === 'function') closeProjectModal();  // deferred panel module (Epic-E sub-7)
  /* ★ A project-less chat is never Studio — fall back to Pro (unless the user
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
  /* ★ Every projectState mutation funnels through here to repaint the
   *   project bar — attach / clear / rollback / restore / remote-state —
   *   with the state already final. So this is THE seam that re-resolves
   *   the Project-Brain surfaces (collab bar + an open Brain panel), which
   *   key on getActiveConv()/projectState: sprinkling the refresh at
   *   individual callers left the same stale-bar window on the clear and
   *   attach paths that the newChat-only fix closed. Cheap: presenceRefresh
   *   renders are fingerprint-gated and its refetch is debounced. */
  if (typeof presenceRefresh === 'function') presenceRefresh();
  if (typeof projectBrainRefresh === 'function') projectBrainRefresh();
  const bar = document.getElementById("projectBar");
  const badge = document.getElementById("projectBadge");
  const toggle = document.getElementById("projectToggle");
  const statsEl = document.getElementById("projectBarStats");
  const foldersEl = document.getElementById("projectBarFolders");

  if (!projectState.active) {
    if (bar) { bar.style.display = "none"; bar.classList.remove("scanning"); }
    badge?.classList.remove("visible");
    /* #projectToggle was retired when the toolbar collapsed into the
     * Air/Pro/Studio dial — the Studio segment IS the project affordance now.
     * Guard it so a project-less newChat/clear never NPEs here (the crash that
     * looked like "all CSS broke" — the throw aborted the render pipeline). */
    toggle?.classList.remove("active");
    return;
  }

  if (bar) bar.style.display = "flex";
  badge?.classList.add("visible");
  toggle?.classList.add("active");

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
    const short = _isRemotePath(p)
      ? p.slice('remote:'.length)
      : (p.split('/').filter(Boolean).pop() || p);
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
  // Try to check server status first — CONV-SCOPED so a background task's
  // global-registry mutation can never paint another conversation's bar.
  try {
    const data = await Api.project.status(conv && conv.id);
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
      if (!extrasMatch || !roMatch) {
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
        // ★ Always use the pruning multi-path endpoint (see _restoreConvProject):
        //   setPath's `same_primary` guard would preserve stale global extras.
        const setResp = await Api.project.setPaths(allPaths, savedReadOnly);
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
