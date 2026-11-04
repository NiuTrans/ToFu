/* ===== migrated source: project.js ===== */
/* Responsibility: demand-loaded Project workspace and folder interactions.
   Entries: project modal, local/remote browse, root policy, and folder writes.
   Dependencies: live core project authority, Api.project, and the shared
   folder browser and workspace rail (features/project).
   Coding approvals/stdin/apply-code live in execution-interactions.js. */
// ── Multi-path folder state for the modal ──
let _mpFolders = []; // array of path strings being edited in the modal
let _mpReadOnly = new Set(); // subset of _mpFolders the user marked read-only
// Mirror of the badges currently painted in the project bar ({path, readOnly}),
// so the bar's click-to-toggle handler can look an entry up by index.

function _syncFoldersFromState() {
  /* Build _mpFolders from the live project state (single source of truth).
   * ORDER IS SEMANTIC: index 0 IS the primary root (star + `root` badge, sent
   * to the backend as the primary in setPaths). The primary is pushed FIRST
   * here so the root always lands at the very top of the Workspace list —
   * extra roots follow in server order. The drag-to-reorder gesture
   * (_mpReorder) is the ONLY thing allowed to change which path is index 0. */
  _mpFolders = [];
  _mpReadOnly = new Set();
  const currentProjectState = ProjectPresentationShellState.projectState;
  if (currentProjectState.path) {
    _mpFolders.push(currentProjectState.path);
    if (currentProjectState.readOnly) _mpReadOnly.add(currentProjectState.path);
  }
  if (currentProjectState.extraRoots && currentProjectState.extraRoots.length) {
    for (const r of currentProjectState.extraRoots) {
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
  _relinkOldPath = "";
  _gitRootHint = null;
  _renderProjectModalHint();
  _syncFoldersFromState();
  _mpRenderTags();
  _updateProjectModalStatus();
  _projectWorkspaceRail().resetRecentFilter();
  renderRecentProjects();
  document.getElementById("projectModal").classList.add("open");
  // Tell the full-page chat-drop handler (main.js) to stand down so a file
  // dropped onto the folder browser is SAVED to disk, not attached to chat.
  runtimeScope._tofuProjectModalOpen = true;
  // Default mobile view to Browse on each open (no-op on desktop).
  pmMobileTab("browse");
  // Docked browser: populate it from the primary folder (or home) on open.
  browseDirectory(_mpFolders.length ? _mpFolders[0] : "~");
  _renderRemoteDevicesSection();
  _attachFolderDropZone();
  setTimeout(() => _projectWorkspaceRail().input.focus(), 100);
}

function closeProjectModal() {
  document.getElementById("projectModal").classList.remove("open");
  runtimeScope._tofuProjectModalOpen = false;
  if (_projectFolderBrowserHandle) _projectFolderBrowserHandle.cancel();
  else _projectBrowseCoordinator.cancel();
}

// The eager Project state owner uses this optional port only after this chunk
// has loaded. Clearing a project while the owner is idle must not fetch the
// workspace merely to reset modal-local draft arrays.
const ProjectModalPresentation = Object.freeze({
  resetAfterProjectClear() {
    _mpFolders = [];
    _mpReadOnly = new Set();
    closeProjectModal();
  },
});

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

/* ── Workspace roots: the shared rail paints them; this section owns the
 * ordered draft and the mobile tab count. ── */
function _mpRenderTags() {
  const mCountEl = document.getElementById("pmMobileCount");
  if (mCountEl) mCountEl.textContent = _mpFolders.length ? `${_mpFolders.length}` : '';
  _syncBrowserAddedState();
  _projectWorkspaceRail().renderRoots();
}

/* ── Reorder the Workspace list ──
 * ORDER IS SEMANTIC: `_mpFolders[0]` is the PRIMARY root (star + `root`
 * badge; sent to the backend as the primary in setPaths, the rest as extra
 * roots). So moving a folder to the top IS the "promote to root" gesture —
 * there is no separate control, and the root is always the top row. The
 * shared rail's drag, touch and keyboard (grip + arrow keys) moves land here. */
/** Move `from` → `to` within _mpFolders and repaint. `to` is the final index
 *  the entry should occupy. No-ops on out-of-range or same-position moves.
 *  Returns true when the array actually changed (drives the tests). */
function _mpReorder(from, to) {
  const n = _mpFolders.length;
  if (!Number.isInteger(from) || !Number.isInteger(to)) return false;
  if (from < 0 || from >= n) return false;
  const dest = Math.max(0, Math.min(n - 1, to));
  if (dest === from) return false;
  const moved = _mpFolders.splice(from, 1)[0];
  _mpFolders.splice(dest, 0, moved);
  _mpRenderTags();
  return true;
}

/* Rail add port: stage a typed path. Blanks and duplicates stay silent, as
   they always have in the Co-Pilot. */
function _mpAddTypedPath(path) {
  if (path && !_mpFolders.includes(path)) {
    _mpFolders.push(path);
    _mpRenderTags();
  }
  return null;
}

function _mpRemove(index) {
  const removed = _mpFolders.splice(index, 1);
  if (removed && removed[0]) _mpReadOnly.delete(removed[0]);
  _mpRenderTags();
}

/* mpApplyFolders — the "Set Project" action.
   First path → primary project; remaining → extra roots.

   Optimistic UI: paint the project bar + close the modal immediately
   from the user's typed paths, then fire /api/project/set_paths in the
   background and reconcile when it returns.  The backend response
   canonicalises paths (~ expansion, abs path) and adds crossDC info,
   so reconciliation may rewrite the bar — that's fine and expected.
   On failure we revert to the previous state and reopen the modal. */
async function mpApplyFolders() {
  // A folder delete in flight may still restore a staged root it removed.
  if (_projectFolderBrowserHandle && _projectFolderBrowserHandle.busy()) {
    await _projectFolderBrowserHandle.settled();
  }
  if (!_mpFolders.length) return;
  // Ensure we have an active conversation
  if (!ProjectPresentationShellState.activeConversationId) {
    const now = Date.now();
    const conv = {
      /* Durable titles stay language-neutral; the localized 新对话 label is
       * a display-only mapping (shell-localization.ts). Persisting the
       * localized label here defeats the server's first-message title
       * derivation (command_service only derives when title is empty). */
      id: generateId(), title: 'New Chat',
      createdAt: now, updatedAt: now, projectPath: "",
      _localOnly: true,
    };
    /* FIX: Auto-assign to active folder when creating a conv from project modal.
     * Without this, the conv stays uncategorized even though the user selected
     * a folder tab before clicking New Chat → project tool → send message. */
    const _curFolderId = typeof getActiveFolderId === 'function' ? getActiveFolderId() : null;
    if (_curFolderId) conv.folderId = _curFolderId;
    ProjectPresentationShellState.conversations.unshift(conv);
    ProjectPresentationShellState.activeConversationId = conv.id;
    /* Persist the metadata shell locally. The first accepted Turn creates the
     * server conversation; settings changes before then must never PATCH an
     * object that does not exist at the storage authority. */
    reconcileConversationCatalogMetadata(conv.id);
    renderConversationList();
  }

  const folders = _mpFolders.slice();
  const primary = folders[0];
  const extras = folders.slice(1);
  const readOnly = folders.filter(p => _mpReadOnly.has(p));

  // ── Optimistic apply: paint UI from typed paths and close the modal ──
  const currentProjectState = ProjectPresentationShellState.projectState;
  const _prevProjectState = {
    ...currentProjectState,
    extraRoots: (currentProjectState.extraRoots || []).slice(),
  };
  _saveConvProjectPath(primary, extras, readOnly);
  closeProjectModal();
  const nExtras = extras.length;
  const nRO = readOnly.length;
  debugLog(`Project set: ${primary}` + (nExtras ? ` + ${nExtras} extra folder(s)` : '') + (nRO ? ` (${nRO} read-only)` : ''), "success");
  // Attachment only repaints the optional project affordance. Execution
  // strategy, Flow and model remain independent of the chosen roots.
  if (typeof onProjectAttached === 'function') onProjectAttached();
  else if (typeof captureActiveConversationSettings === 'function') captureActiveConversationSettings();

  // ── Reconcile with the server in the background ──

  const current = ProjectPresentationShellState.beginProjectMutation();
  try {
    const resp = await Api.project.setPaths(folders, readOnly, folders);
    const data = resp ? await resp.json().catch(() => ({})) : {};
    if (!resp || !resp.ok) throw new Error(data.error || (typeof t === 'function' ? t('pm.applyFailed') : "Failed"));
    if (!current()) return;
    const binding = ProjectPresentationShellState.captureProjectStatus(data);
    _saveConvProjectPath(binding.projectPath, binding.projectPaths.slice(1), binding.readOnlyPaths);
    _applyProjectData(data);

    if (typeof showToast === 'function') {
      showToast(
        typeof t === 'function' ? t('pm.appliedToast')
          : 'Workspace updated. New messages and regenerated replies will use it.',
        'success',
      );
    }
  } catch (e) {
    if (!current()) return; // stale error must not roll back another selection
    // Revert all previous roots AND their read-only policy.
    const previous = ProjectPresentationShellState.captureProjectStatus(_prevProjectState);
    _saveConvProjectPath(previous.projectPath, previous.projectPaths.slice(1), previous.readOnlyPaths);
    _updateProjectUI();
    // Rollback repaints only this still-current attachment, never a capability tier.
    if (!_prevProjectState.path && typeof onProjectCleared === 'function') onProjectCleared();
    document.getElementById("projectModal").classList.add("open");
    const statusEl = document.getElementById("projectModalStatus");
    if (statusEl) {
      statusEl.innerHTML = `<div style="color:var(--error-text);font-size:12px">${escapeHtml(e.message)}</div>`;
    }
    debugLog(`Project set failed: ${e.message}`, "error");
  }
}

// ── Recent Project Paths (server-side persistence) ──

let _recentProjects = [];

/* The rail shows a failed read with a retry instead of an empty account. */
async function renderRecentProjects() {
  const rail = _projectWorkspaceRail();
  const _t = (typeof t === "function") ? t : (k) => k;
  let data;
  try {
    data = await Api.project.recentList();
  } catch (e) {
    rail.setRecents({ error: (e && e.message) || _t("folder.settings.recentFailed") });
    return;
  }
  const list = data ? (Array.isArray(data) ? data : data.projects || []) : [];
  _recentProjects = list.filter((item) => item && typeof item.path === "string");
  rail.setRecents(_recentProjects.map((item) => ({
    path: item.path,
    count: Math.max(0, Number(item.count) || 0),
    exists: item.exists !== false,
  })));
}

function selectRecentProject(path) {
  if (!path) return;

  const _entry = _recentProjects.find((entry) => entry && entry.path === path);
  if (_entry && _entry.exists === false) { beginRelinkRecent(path); return; }
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

// ── Project identity: git-root hint + rename relink ──
// A subdirectory of a repo is usually NOT the workspace the user wants —
// probe for the enclosing .git root and offer it. A stored recent path that
// stopped resolving usually means the directory was renamed/moved — relink
// re-keys the owner's aggregates instead of losing the project's history.

let _gitRootHint = null;   // { forPath, gitRoot } — advisory, per staged add
let _relinkOldPath = "";  // armed while the user locates the moved directory

function _renderProjectModalHint() {
  const el = document.getElementById("projectModalHint");
  if (!el) return;
  const _t = (typeof t === "function") ? t : (k) => k;
  if (_relinkOldPath) {
    el.innerHTML = `<div class="pm-hint pm-hint-relink">
      <span class="pm-hint-text">${escapeHtml(_t("pm.relinkBanner", { path: _relinkOldPath }))}</span>
      <button type="button" class="pm-hint-btn" data-tofu-action="cancelRelinkRecent()">${escapeHtml(_t("pm.relinkCancel"))}</button>
    </div>`;
    return;
  }
  if (_gitRootHint) {
    el.innerHTML = `<div class="pm-hint pm-hint-gitroot">
      <span class="pm-hint-text">${escapeHtml(_t("pm.gitRootHint", { root: _gitRootHint.gitRoot }))}</span>
      <button type="button" class="pm-hint-btn pm-hint-btn-primary" data-tofu-action="useGitRootHint()">${escapeHtml(_t("pm.gitRootUse"))}</button>
      <button type="button" class="pm-hint-btn" data-tofu-action="dismissGitRootHint()">${escapeHtml(_t("pm.gitRootDismiss"))}</button>
    </div>`;
    return;
  }
  el.innerHTML = "";
}

async function _checkGitRootHint(path) {
  // Advisory probe: remote pseudo-paths and probe failures stay silent.
  if (!path || path.indexOf("remote:") === 0) return;
  if (typeof Api === "undefined" || !Api.project || !Api.project.gitRootHint) return;
  try {
    const data = await Api.project.gitRootHint(path);
    const gitRoot = data && data.gitRoot;
    // Stale guard: the staged folders may have changed while probing.
    if (gitRoot && gitRoot !== path && _mpFolders.includes(path)
        && !_mpFolders.includes(gitRoot)) {
      _gitRootHint = { forPath: path, gitRoot: gitRoot };
      _renderProjectModalHint();
    }
  } catch (_e) { /* advisory only */ }
}

function useGitRootHint() {
  if (!_gitRootHint) return;
  const forPath = _gitRootHint.forPath;
  const gitRoot = _gitRootHint.gitRoot;
  const idx = _mpFolders.indexOf(forPath);
  if (idx >= 0) {
    if (_mpFolders.includes(gitRoot)) _mpFolders.splice(idx, 1);
    else _mpFolders[idx] = gitRoot;
  }
  _gitRootHint = null;
  _mpRenderTags();
  _renderProjectModalHint();
}

function dismissGitRootHint() {
  _gitRootHint = null;
  _renderProjectModalHint();
}

function beginRelinkRecent(path) {
  if (!path) return;
  _relinkOldPath = path;
  _gitRootHint = null;
  _renderProjectModalHint();
  _syncBrowserAddedState();
  // Land the browser near the old location so the user can spot the
  // renamed/moved directory, then arm the browser's + as the relink target.
  const parts = String(path).split("/").filter(Boolean);
  const parent = parts.length > 1 ? "/" + parts.slice(0, -1).join("/") : "~";
  browseDirectory(parent);
}

function cancelRelinkRecent() {
  _relinkOldPath = "";
  _renderProjectModalHint();
  _syncBrowserAddedState();
}

async function _confirmRelinkRecent(newPath) {
  const oldPath = _relinkOldPath;
  if (!oldPath || !newPath || newPath === oldPath) { cancelRelinkRecent(); return; }
  const _t = (typeof t === "function") ? t : (k) => k;
  try {
    const data = await Api.project.relinkRecent(oldPath, newPath);
    if (!data) throw new Error(_t("pm.relinkFailed"));
    _relinkOldPath = "";
    _renderProjectModalHint();
    _syncBrowserAddedState();
    if (typeof showToast === "function") {
      showToast(_t("pm.relinkedToast", { path: newPath }), "success");
    }
    renderRecentProjects();
    // The active workspace follows the move: swap the staged path and
    // re-apply so the conversation's project pin stops pointing at the
    // renamed location.
    const cur = (typeof ProjectPresentationShellState !== "undefined")
      ? ProjectPresentationShellState.projectState : null;
    if (cur && cur.path === oldPath) {
      const idx = _mpFolders.indexOf(oldPath);
      if (idx >= 0) _mpFolders[idx] = newPath;
      else _mpFolders.unshift(newPath);
      mpApplyFolders();
    } else {
      _mpRenderTags();
    }
  } catch (e) {
    if (typeof showToast === "function") {
      showToast((e && e.message) || _t("pm.relinkFailed"), "error");
    }
  }
}

function _updateProjectModalStatus() {
  const el = document.getElementById("projectModalStatus");
  if (!el) return;
  if (!ProjectPresentationShellState.projectState.active) {
    el.innerHTML = "";
    return;
  }
  const total = _mpFolders.length;
  el.innerHTML = `<div style="font-size:12px;color:#34d399;margin-bottom:12px">
    ${escapeHtml(typeof t === 'function' ? t('pm.foldersActive', { n: total }) : `${total} folder(s) active`)}
  </div>`;
}

// ══════════════════════════════════════════════════════
//  Folder Browser
// ══════════════════════════════════════════════════════
/* The typed shared browser (features/project/folder-browser) owns
   navigation, filtering, folder create/delete and rendering. This section
   only binds the workbench's staged workspace list, drop zone and remote
   devices group to it; browseDirectory serves this section's open, recent
   and relink flows. */
const _projectBrowseCoordinator = createProjectBrowseCoordinator(function () {
  return ProjectPresentationShellState.sessionStorage;
});
let _projectFolderBrowserHandle = null;

function _projectFolderBrowser() {
  if (_projectFolderBrowserHandle) return _projectFolderBrowserHandle;
  const handle = createFolderBrowser({
    t: function (key, values) {
      return (typeof t === "function") ? t(key, values) : key;
    },
    coordinator: _projectBrowseCoordinator,
    browse: function (path, showHidden, signal) {
      return Api.project.browse(path, showHidden, signal ? { signal: signal } : {});
    },
    mkdir: function (parent, name) { return Api.project.mkdir(parent, name); },
    rmdir: function (path) { return Api.project.rmdir(path); },
    // Relink mode arms every + as "this is the moved folder", staged or not.
    isAdded: function (path) { return !_relinkOldPath && _mpFolders.includes(path); },
    onAdd: mpAddBrowsedPath,
    onDeleted: _unstageDeletedFolder,
    notify: function (message, variant) {
      if (typeof showToast === "function") showToast(message, variant);
    },
    prelude: document.getElementById("remoteDevicesSection"),
  });
  // The drop zone and its tests address the browser by this stable id.
  handle.el.id = "folderBrowser";
  const pane = document.querySelector("#projectModal .pm-browser-pane");
  if (pane) pane.append(handle.el);
  _projectFolderBrowserHandle = handle;
  // The pane lives for the page; its copy follows UI language switches.
  window.addEventListener("tofu:language-change", function () { handle.relabel(); });
  return handle;
}

function browseDirectory(path) {
  return _projectFolderBrowser().navigate(path || "~");
}

/* Repaint the browser's added markers after the staged workspace changed. */
function _syncBrowserAddedState() {
  if (_projectFolderBrowserHandle) _projectFolderBrowserHandle.syncAdded();
}

/* ── Workspace rail ── The typed shared rail (features/project/workspace-rail)
   renders the Workspace and Recent cards beside the browser, exactly as in
   sidebar folder settings. This section keeps the staged roots, recent fetch,
   relink and apply; the rail calls back through these ports. */
let _projectWorkspaceRailHandle = null;

function _projectWorkspaceRail() {
  if (_projectWorkspaceRailHandle) return _projectWorkspaceRailHandle;
  const rail = createWorkspaceRail({
    t: function (key, values) {
      return (typeof t === "function") ? t(key, values) : key;
    },
    roots: function () { return _mpFolders; },
    isReadOnly: function (path) { return _mpReadOnly.has(path); },
    add: _mpAddTypedPath,
    remove: _mpRemove,
    toggleReadOnly: _mpToggleReadOnly,
    move: _mpReorder,
    pickRecent: selectRecentProject,
    relinkRecent: beginRelinkRecent,
    clearRecents: clearRecentProjects,
    retryRecents: renderRecentProjects,
  });
  const body = document.querySelector("#projectModal .pm-body");
  if (body) body.append(rail.el);
  _projectWorkspaceRailHandle = rail;
  // The rail lives for the page; its copy follows UI language switches.
  window.addEventListener("tofu:language-change", function () { rail.relabel(); });
  return rail;
}

/* Add a folder shown in the browser straight into the workspace list. */
function mpAddBrowsedPath(path) {
  if (!path) return;
  // Relink mode armed: the browser's + names the moved directory's new home.
  if (_relinkOldPath) { _confirmRelinkRecent(path); return; }
  if (!_mpFolders.includes(path)) {
    _mpFolders.push(path);
    _mpRenderTags();
    _checkGitRootHint(path);
  }
}

/* A deleted directory leaves the staged workspace in the same task as the
   confirmation (the server moves it to a recoverable trash bin); the
   returned function re-stages it if the server refuses the delete. */
function _unstageDeletedFolder(path) {
  const index = _mpFolders.indexOf(path);
  if (index === -1) return undefined;
  const wasReadOnly = _mpReadOnly.has(path);
  _mpFolders.splice(index, 1);
  _mpReadOnly.delete(path);
  _mpRenderTags();
  return function () {
    if (_mpFolders.includes(path)) return;
    _mpFolders.splice(Math.min(index, _mpFolders.length), 0, path);
    if (wasReadOnly) _mpReadOnly.add(path);
    _mpRenderTags();
  };
}

/* RWA P4b-2a(拍板 6A):目录浏览弹窗顶部「远程设备」分组。
   在线 agent 的每个共享根一行,点 + 把伪路径加入工作区
   (与本地文件夹同一套 _mpFolders/保存/持久化机制);离线 agent 灰显、
   不可加。无 agent 时整段隐藏(本地使用零干扰)。 */
async function _renderRemoteDevicesSection() {
  const sec = document.getElementById('remoteDevicesSection');
  if (!sec || typeof Api === 'undefined' || !Api.desktop) return;
  let data = null;
  try {
    data = await Api.desktop.devices();
  } catch (e) {
    data = null;
  }
  const agents = (data && data.agents) || [];
  if (!agents.length) {
    sec.innerHTML = '';
    sec.style.display = 'none';
    return;
  }
  let html = '<div class="remote-devices-title">' +
    escapeHtml(t('devices.remoteGroup')) + '</div>';
  agents.forEach(function (a) {
    const roots = a.share_roots || [];
    const online = !!a.online;
    html += '<div class="remote-agent-row' +
      (online ? '' : ' remote-agent-offline') + '">' +
      '<span class="remote-agent-name">' +
      (online ? '● ' : '○ ') + escapeHtml(a.name || a.agent_id) +
      ' <span class="stg-dim">' + escapeHtml(a.platform || '') + '</span></span>';
    roots.forEach(function (r) {
      const pseudo = 'remote:' + a.agent_id + ':' + (r.name || r.path);
      html += '<span class="remote-root-row">' +
        '<span class="remote-root-name">' +
        escapeHtml(r.name || r.path) + '</span>';
      if (online) {
        html += '<button class="remote-root-add" data-pseudo="' +
          escapeHtml(pseudo) + '" title="' + escapeHtml(pseudo) + '">+</button>';
      }
      html += '</span>';
    });
    html += '</div>';
  });
  sec.innerHTML = html;
  sec.style.display = '';
  sec.querySelectorAll('.remote-root-add').forEach(function (btn) {
    btn.onclick = function (ev) {
      ev.stopPropagation();
      mpAddBrowsedPath(btn.getAttribute('data-pseudo'));
    };
  });
}


/* ═══════════════════════════════════════════════════════
   Drag-and-drop files INTO a project folder (folder browser)
   ═══════════════════════════════════════════════════════
   Distinct from the full-page chat drop (main.js), which attaches files to
   the next MESSAGE. Dropping onto the folder browser SAVES the raw bytes to
   disk inside the attached workspace (binary-safe, sandboxed, undoable).

   The full-page chat overlay lives at document level (z-index 200); this
   handler is bound directly to #folderBrowser and runs in the CAPTURE phase +
   stopPropagation so a file dropped on the browser is saved to disk and NOT
   also attached to chat. main.js additionally skips its overlay while the
   project modal is open (see _tofuProjectModalOpen). */
var _folderDropAttached = false;

/** Resolve which directory a drop landed on: a specific folder row's path,
 *  else the directory currently shown in the browser. Returns '' if unknown. */
function _folderDropTargetDir(target) {
  var row = target && target.closest ? target.closest('.folder-browser-row[data-dir-path]') : null;
  if (row && row.dataset.dirPath) return row.dataset.dirPath;
  return _projectFolderBrowserHandle ? _projectFolderBrowserHandle.currentPath() : '';
}

function _clearFolderDropHighlight() {
  var el = document.getElementById('folderBrowser');
  if (el) el.classList.remove('fb-drop-active');
  document.querySelectorAll('.folder-browser-row.fb-drop-row')
    .forEach(function (r) { r.classList.remove('fb-drop-row'); });
}

/** Attached workspace roots (single source of truth: live Project state). A drop is
 *  only accepted inside one of these — the backend save_uploaded_file refuses
 *  anything else, so we mirror that guard client-side for a clear, proactive
 *  message instead of a generic post-hoc "failed to save". */
function _attachedRootPaths() {
  var roots = [];
  const currentProjectState = ProjectPresentationShellState.projectState;
  if (currentProjectState) {
    if (currentProjectState.path) roots.push(currentProjectState.path);
    (currentProjectState.extraRoots || []).forEach(function (r) {
      var p = typeof r === 'string' ? r : (r && r.path);
      if (p) roots.push(p);
    });
  }
  return roots;
}

/** True if `dir` is (or is under) an attached root. Empty dir → the active
 *  project root, which the backend resolves, so it's allowed. */
function _dirInsideAttachedRoot(dir) {
  if (!dir) return true;
  var norm = function (p) { return String(p).replace(/\/+$/, ''); };
  var d = norm(dir);
  return _attachedRootPaths().some(function (root) {
    var r = norm(root);
    return d === r || d.indexOf(r + '/') === 0;
  });
}

/** Save one dropped File into `dir` via the binary-safe upload endpoint. */
async function _uploadDroppedFile(file, dir) {
  var fd = new FormData();
  fd.append('file', file, file.name);
  if (dir) fd.append('dir', dir);
  var resp = await Api.project.upload(fd);
  var data = resp ? await resp.json().catch(function () { return {}; }) : {};
  if (resp && resp.ok && data && data.ok) return { ok: true, data: data };
  return { ok: false, error: (data && data.error) || ('HTTP ' + (resp ? resp.status : '?')) };
}

/** Archive shapes the extraction endpoint owns (.zip/.tar + gzip/bzip2/xz). */
function _isProjectArchive(name) {
  return /\.(?:zip|tar|tar\.gz|tgz|tar\.bz2|tbz2|tar\.xz|txz)$/i.test(name || '');
}

/** Extract one dropped archive into `<dir>/<stem>/` (atomic, never merging). */
async function _uploadDroppedArchive(file, dir) {
  var fd = new FormData();
  fd.append('file', file, file.name);
  if (dir) fd.append('dir', dir);
  var resp = await Api.project.uploadArchive(fd);
  var data = resp ? await resp.json().catch(function () { return {}; }) : {};
  if (resp && resp.ok && data && data.ok) return { ok: true, data: data };
  return { ok: false, error: (data && data.error) || ('HTTP ' + (resp ? resp.status : '?')) };
}

function _attachFolderDropZone() {
  if (_folderDropAttached) return;
  var el = document.getElementById('folderBrowser');
  if (!el) return;
  _folderDropAttached = true;

  var hasFiles = function (e) {
    return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') !== -1;
  };

  el.addEventListener('dragover', function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    el.classList.add('fb-drop-active');
    var row = e.target.closest ? e.target.closest('.folder-browser-row[data-dir-path]') : null;
    document.querySelectorAll('.folder-browser-row.fb-drop-row')
      .forEach(function (r) { if (r !== row) r.classList.remove('fb-drop-row'); });
    if (row) row.classList.add('fb-drop-row');
  }, true);

  el.addEventListener('dragleave', function (e) {
    // Only clear when the pointer actually leaves the browser box.
    if (e.target === el && !el.contains(/** @type {Node} */(e.relatedTarget))) _clearFolderDropHighlight();
  }, true);

  el.addEventListener('drop', function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();  // beat the document-level chat-drop handler
    var dir = _folderDropTargetDir(e.target);
    _clearFolderDropHighlight();
    var files = Array.prototype.slice.call((e.dataTransfer && e.dataTransfer.files) || []);
    if (!files.length) return;
    _runFolderDrop(files, dir);
  }, true);
}

/** Add `dir` to the workspace as an extra root (or primary if none yet),
 *  reusing the tested setPaths apply path. Preserves existing roots + their
 *  read-only flags. Throws on server failure. */
async function _addDropDirAsRoot(dir) {
  var folders = _attachedRootPaths().slice();
  if (folders.indexOf(dir) === -1) folders.push(dir);
  var readOnly = [];
  const currentProjectState = ProjectPresentationShellState.projectState;
  if (currentProjectState) {
    if (currentProjectState.readOnly && currentProjectState.path) {
      readOnly.push(currentProjectState.path);
    }
    (currentProjectState.extraRoots || []).forEach(function (r) {
      if (r && typeof r === 'object' && r.readOnly && r.path) readOnly.push(r.path);
    });
  }
  var resp = await Api.project.setPaths(folders, readOnly);
  var data = resp ? await resp.json().catch(function () { return {}; }) : {};
  if (!resp || !resp.ok) throw new Error((data && data.error) || 'Failed to add folder');
  if (typeof _applyProjectData === 'function') _applyProjectData(data);
  if (typeof _saveConvProjectPath === 'function') {
    _saveConvProjectPath(data.path,
      (data.extraRoots || []).map(function (r) { return typeof r === 'string' ? r : r.path; }),
      readOnly);
  }
}

async function _runFolderDrop(files, dir) {
  var dirLabel = dir ? (dir.split('/').filter(Boolean).slice(-1)[0] || dir) : 'project root';
  // A drop outside every attached root would be refused by save_uploaded_file
  // (it never auto-registers a workspace). Rather than dead-ending, OFFER to
  // add the folder in one click — the upload itself is harmless, but adding a
  // root has a visible side effect (a scan + a new project-bar folder), so we
  // ask first instead of doing it silently.
  if (dir && !_dirInsideAttachedRoot(dir)) {
    var ok = (typeof showConfirm === 'function')
      ? await showConfirm(t('folderDrop.addRootConfirm', { dir: dirLabel }),
          { title: t('folderDrop.notInWorkspace'),
            okText: t('folderDrop.addAndSave') })
      : true;
    if (!ok) return;
    try {
      await _addDropDirAsRoot(dir);
    } catch (e) {
      if (typeof showToast === 'function') {
        showToast(t('folderDrop.failed', { n: files.length }), 'error',
          (e && e.message) || '', 6000);
      }
      return;
    }
  }
  var results = await Promise.allSettled(files.map(function (f) {
    return _isProjectArchive(f.name) ? _uploadDroppedArchive(f, dir) : _uploadDroppedFile(f, dir);
  }));
  var saved = 0, renamed = 0;
  var errors = [];
  var extracted = [];
  results.forEach(function (r, i) {
    if (r.status === 'fulfilled' && r.value && r.value.ok) {
      saved++;
      if (r.value.data && r.value.data.renamed) renamed++;
      if (r.value.data && r.value.data.action === 'upload_archive') extracted.push(r.value.data);
    } else {
      var msg = (r.status === 'fulfilled' && r.value)
        ? r.value.error
        : (r.status === 'rejected' && r.reason && r.reason.message);
      errors.push(files[i].name + ': ' + (msg || 'failed'));
    }
  });
  if (typeof showToast === 'function') {
    if (saved > 0) {
      var detail = t('folderDrop.savedInto', { dir: dirLabel })
        + (renamed ? ' · ' + t('folderDrop.renamedNote', { n: renamed }) : '');
      showToast(t('folderDrop.saved', { n: saved }), '', detail, 4200);
    }
    extracted.forEach(function (row) {
      var texCount = (row.texMainCandidates || []).length;
      var detail = t('folderDrop.extractedInto', { dir: row.directory || '', n: row.files || 0 })
        + (texCount ? ' · ' + t('folderDrop.texFound', { n: texCount }) : '');
      showToast(t('folderDrop.extracted'), '', detail, 6000);
    });
    if (errors.length) {
      showToast(t('folderDrop.failed', { n: errors.length }), 'error',
        errors.slice(0, 3).join('\n'), 6000);
    }
  }
  // An extracted TeX project feeds the same discovery hint the preview's
  // cold start consumes, so the panel can offer its document immediately.
  var _dropConvId = '';
  try { _dropConvId = (typeof runtimeScope !== 'undefined' && runtimeScope.activeConvId) || ''; } catch (_e) { _dropConvId = ''; }
  if (extracted.some(function (row) { return (row.texMainCandidates || []).length; })
      && _dropConvId
      && typeof runtimeScope !== 'undefined' && runtimeScope._paperPreviewDiscover) {
    try { runtimeScope._paperPreviewDiscover(_dropConvId, { path: extracted[0].path }); } catch (_e) { /* hint only */ }
  }
  // Refresh so the newly-saved files reflect in the browser's file count.
  if (_projectFolderBrowserHandle && _projectFolderBrowserHandle.currentPath()) {
    _projectFolderBrowserHandle.refresh();
  }
  // Surface the write in the project bar's file-changes affordance.
  if (typeof loadProjectStatus === 'function') { try { loadProjectStatus(); } catch (_e) { /* best-effort */ } }
}
