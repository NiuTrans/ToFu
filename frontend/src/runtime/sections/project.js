/* ===== migrated source: project.js ===== */
/* Responsibility: demand-loaded Project workspace and folder interactions.
   Entries: project modal, local/remote browse, root policy, and folder writes.
   Dependencies: live core project authority, Api.project, and browse owner.
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
  _recentFilter = "";
  const _recentInput = document.getElementById("recentSearchInput");
  if (_recentInput) _recentInput.value = "";
  renderRecentProjects();
  document.getElementById("projectModal").classList.add("open");
  // Tell the full-page chat-drop handler (main.js) to stand down so a file
  // dropped onto the folder browser is SAVED to disk, not attached to chat.
  runtimeScope._tofuProjectModalOpen = true;
  // Default mobile view to Browse on each open (no-op on desktop).
  pmMobileTab("browse");
  // Docked browser: populate it from the primary folder (or home) on open.
  browseDirectory(_mpFolders.length ? _mpFolders[0] : "~");
  _attachFolderDropZone();
  _attachMpReorder();
  setTimeout(() => document.getElementById("mpPathInput").focus(), 100);
}

function closeProjectModal() {
  document.getElementById("projectModal").classList.remove("open");
  runtimeScope._tofuProjectModalOpen = false;
  _projectBrowseCoordinator.cancel();
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

/* ── Multi-path tag rendering ── */
function _mpRenderTags() {
  const container = document.getElementById("mpFolderTags");
  const countEl = document.getElementById("mpFolderCount");
  if (countEl) countEl.textContent = _mpFolders.length ? `${_mpFolders.length}` : '';
  const mCountEl = document.getElementById("pmMobileCount");
  if (mCountEl) mCountEl.textContent = _mpFolders.length ? `${_mpFolders.length}` : '';
  const _t = (typeof t === "function") ? t : (k) => k;
  if (!_mpFolders.length) {
    container.innerHTML = '<div class="mp-empty-hint">' + escapeHtml(_t("pm.emptyFolders")) + '</div>';
    return;
  }
  const _gripTip = escapeHtml(_t("pm.dragReorder"));
  container.innerHTML = _mpFolders.map((p, i) => {
    const parts = p.split('/').filter(Boolean);
    const name = parts[parts.length - 1] || p;
    const short = parts.length > 2 ? '…/' + parts.slice(-2).join('/') : p;
    const isPrimary = i === 0;
    const isRO = _mpReadOnly.has(p);
    // Lock (read-only) / pencil (writable) toggle. Click flips the access
    // policy for this root — sent to the backend as readOnlyPaths.
    const lockIcon = isRO
      ? PROJECT_PRESENTATION_ASSETS.readOnlyLock
      : PROJECT_PRESENTATION_ASSETS.writablePencil;
    // 6-dot grip: the drag affordance. The WHOLE row is draggable (the grip is
    // the visual hint), except its buttons — see _attachMpReorder's dragstart.
    const grip = '<span class="mp-row-grip icon-box" title="' + _gripTip
      + '" aria-hidden="true">' + PROJECT_PRESENTATION_ASSETS.reorderGrip
      + '</span>';
    return `<div class="mp-row${isPrimary ? ' mp-row-primary' : ''}${isRO ? ' mp-row-readonly' : ''}" draggable="true" data-mp-idx="${i}" title="${escapeHtml(p)}">
      ${grip}
      <span class="mp-row-icon">${isPrimary
        ? PROJECT_PRESENTATION_ASSETS.primaryStar
        : PROJECT_PRESENTATION_ASSETS.workspaceFolder}</span>
      <span class="mp-row-text">
        <span class="mp-row-name">${escapeHtml(name)}</span>
        <span class="mp-row-path">${escapeHtml(short)}</span>
      </span>
      ${isPrimary ? '<span class="mp-row-badge">' + escapeHtml(_t("pm.rootBadge")) + '</span>' : ''}
      ${isRO ? '<span class="mp-row-badge mp-row-badge-ro">' + escapeHtml(_t("pm.readOnlyBadge")) + '</span>' : ''}
      <button class="mp-row-lock${isRO ? ' active' : ''}" draggable="false" data-tofu-action="_mpToggleReadOnly(${i})" title="${escapeHtml(isRO ? _t("pm.lockRo") : _t("pm.lockRw"))}">${lockIcon}</button>
      <button class="mp-row-remove" draggable="false" data-tofu-action="_mpRemove(${i})" title="${escapeHtml(_t("common.remove"))}">
        ${PROJECT_PRESENTATION_ASSETS.remove}
      </button>
    </div>`;
  }).join('');
}

/* ── Drag-to-reorder the Workspace list ──────────────────────────────────
 * ORDER IS SEMANTIC: `_mpFolders[0]` is the PRIMARY root (star + `root`
 * badge; sent to the backend as the primary in setPaths, the rest as extra
 * roots). So dragging a folder to the top IS the "promote to root" gesture —
 * there is no separate control, and the root is always the top row.
 *
 * The rows are rebuilt wholesale by _mpRenderTags (innerHTML), so the
 * listeners are DELEGATED onto the stable #mpFolderTags container and wired
 * exactly once (_mpReorderAttached), mirroring the image-chip reorder in
 * upload.js. */
var _mpReorderAttached = false;
var _mpDragFrom = null;

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

/** Row element under the pointer, plus whether the pointer sits in its TOP
 *  half (insert before) or bottom half (insert after). */
function _mpRowAt(target, clientY) {
  const row = target && target.closest ? target.closest('.mp-row[data-mp-idx]') : null;
  if (!row) return null;
  const idx = parseInt(row.dataset.mpIdx, 10);
  if (!Number.isInteger(idx)) return null;
  const rect = row.getBoundingClientRect();
  const before = rect.height ? (clientY - rect.top) < rect.height / 2 : true;
  return { row, idx, before };
}

/** Translate a hover position into the destination index for _mpReorder. */
function _mpDropIndex(from, hit) {
  let to = hit.before ? hit.idx : hit.idx + 1;
  if (from < to) to -= 1;   // removing `from` first shifts everything after it
  return to;
}

function _mpClearDropMarks() {
  document.querySelectorAll('.mp-row.mp-drop-before, .mp-row.mp-drop-after')
    .forEach((el) => el.classList.remove('mp-drop-before', 'mp-drop-after'));
}

function _mpMarkDrop(hit) {
  _mpClearDropMarks();
  if (!hit) return;
  hit.row.classList.add(hit.before ? 'mp-drop-before' : 'mp-drop-after');
}

function _mpEndDrag() {
  _mpClearDropMarks();
  document.querySelectorAll('.mp-row.mp-row-dragging')
    .forEach((el) => el.classList.remove('mp-row-dragging'));
  _mpDragFrom = null;
}

function _attachMpReorder() {
  if (_mpReorderAttached) return;
  const list = document.getElementById('mpFolderTags');
  if (!list) return;
  _mpReorderAttached = true;

  // ── Desktop: HTML5 drag-and-drop ──
  list.addEventListener('dragstart', (e) => {
    // Buttons keep their click semantics — never start a drag from one.
    if (e.target.closest && e.target.closest('button')) { e.preventDefault(); return; }
    const hit = _mpRowAt(e.target, e.clientY);
    if (!hit) return;
    _mpDragFrom = hit.idx;
    hit.row.classList.add('mp-row-dragging');
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = 'move';
      // Firefox refuses to start a drag without payload.
      try { e.dataTransfer.setData('text/plain', String(hit.idx)); } catch (_e) { /* ignore */ }
    }
  });

  list.addEventListener('dragover', (e) => {
    if (_mpDragFrom === null) return;   // a file drag — not ours, let it pass
    e.preventDefault();                  // accept → the OS shows the move cursor
    e.stopPropagation();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
    _mpMarkDrop(_mpRowAt(e.target, e.clientY));
  });

  list.addEventListener('dragleave', (e) => {
    if (_mpDragFrom === null) return;
    if (e.target === list && !list.contains(/** @type {Node} */(e.relatedTarget))) _mpClearDropMarks();
  });

  list.addEventListener('drop', (e) => {
    if (_mpDragFrom === null) return;
    // Swallow it: releasing over the modal must never reach the page-level
    // file-drop handler nor paste the text/plain index anywhere.
    e.preventDefault();
    e.stopPropagation();
    const from = _mpDragFrom;
    const hit = _mpRowAt(e.target, e.clientY);
    _mpEndDrag();
    if (hit) _mpReorder(from, _mpDropIndex(from, hit));
  });

  list.addEventListener('dragend', () => { _mpEndDrag(); });

  /* ── Touch: HTML5 DnD does not fire on touch devices, and the modal has a
   * dedicated mobile Workspace pane. Dragging starts from the GRIP only so a
   * finger anywhere else still scrolls the list. touchmove is non-passive
   * because it must preventDefault to stop the page scrolling mid-drag. */
  list.addEventListener('touchstart', (e) => {
    const touch = e.touches && e.touches[0];
    if (!touch || !e.target.closest || !e.target.closest('.mp-row-grip')) return;
    const hit = _mpRowAt(e.target, touch.clientY);
    if (!hit) return;
    _mpDragFrom = hit.idx;
    hit.row.classList.add('mp-row-dragging');
  }, { passive: true });

  list.addEventListener('touchmove', (e) => {
    if (_mpDragFrom === null) return;
    const touch = e.touches && e.touches[0];
    if (!touch) return;
    e.preventDefault();   // hold the page still while repositioning
    const under = document.elementFromPoint(touch.clientX, touch.clientY);
    _mpMarkDrop(under ? _mpRowAt(under, touch.clientY) : null);
  }, { passive: false });

  const _touchEnd = (e) => {
    if (_mpDragFrom === null) return;
    const touch = (e.changedTouches && e.changedTouches[0]) || null;
    const from = _mpDragFrom;
    const under = touch ? document.elementFromPoint(touch.clientX, touch.clientY) : null;
    const hit = under ? _mpRowAt(under, touch.clientY) : null;
    _mpEndDrag();
    if (hit) _mpReorder(from, _mpDropIndex(from, hit));
  };
  list.addEventListener('touchend', _touchEnd);
  list.addEventListener('touchcancel', () => { _mpEndDrag(); });
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

/* mpApplyFolders — the "Set Project" action.
   First path → primary project; remaining → extra roots.

   Optimistic UI: paint the project bar + close the modal immediately
   from the user's typed paths, then fire /api/project/set_paths in the
   background and reconcile when it returns.  The backend response
   canonicalises paths (~ expansion, abs path) and adds crossDC info,
   so reconciliation may rewrite the bar — that's fine and expected.
   On failure we revert to the previous state and reopen the modal. */
async function mpApplyFolders() {
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
  _applyProjectData({
    path: primary,
    readOnly: _mpReadOnly.has(primary),
    extraRoots: extras.map(p => ({ path: p, readOnly: _mpReadOnly.has(p) })),
    crossDC: null,
  });
  _saveConvProjectPath(primary, extras, readOnly);
  closeProjectModal();
  const nExtras = extras.length;
  const nRO = readOnly.length;
  debugLog(`Project set: ${primary}` + (nExtras ? ` + ${nExtras} extra folder(s)` : '') + (nRO ? ` (${nRO} read-only)` : ''), "success");
  /* Attaching a project IS the Studio tier. Promote the capability dial so
   * the UI + derived flags stay truthful. NOTE (owner-directed 2026-07-19):
   * the tier is DECOUPLED from execution strategy — attaching a project no
   * longer auto-enables Swarm / Autopilot (those are orthogonal B-axis modes
   * the user turns on explicitly). */
  if (typeof onProjectAttached === 'function') onProjectAttached();
  else if (typeof captureActiveConversationSettings === 'function') captureActiveConversationSettings();

  // ── Reconcile with the server in the background ──
  try {
    const resp = await Api.project.setPaths(folders, readOnly, folders);
    const data = resp ? await resp.json().catch(() => ({})) : {};
    if (!resp || !resp.ok) throw new Error(data.error || (typeof t === 'function' ? t('pm.applyFailed') : "Failed"));
    _applyProjectData(data);
    _saveConvProjectPath(data.path, _mpFolders.slice(1), readOnly);

    if (typeof showToast === 'function') {
      showToast(
        typeof t === 'function' ? t('pm.appliedToast')
          : 'Workspace updated. New messages and regenerated replies will use it.',
        'success',
      );
    }
  } catch (e) {
    // Revert the optimistic state and reopen the modal so the user can fix it.
    ProjectPresentationShellState.projectState = _prevProjectState;
    _saveConvProjectPath(_prevProjectState.path || "",
                         (_prevProjectState.extraRoots || []).map(r => typeof r === 'string' ? r : r.path));
    _updateProjectUI();
    /* Studio ⟺ a project is attached. Rolling back to a state with NO
     * project must demote the dial as well — the optimistic promotion was
     * already persisted by onProjectAttached, so without this the conv is
     * left in the poisoned "Studio + no project" shape (durably so, since
     * the promotion now saves immediately). onProjectCleared repaints AND
     * persists the fallback. When the previous state DID have a project the
     * tier stays Studio — still truthful. */
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
let _recentFilter = "";

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
  _recentProjects = list;
  if (list.length === 0) {
    container.hidden = true;
    return;
  }
  container.hidden = false;
  _renderRecentList();
}

// Basename of a path (last non-empty segment).
function _recentName(path) {
  const parts = String(path).split("/").filter(Boolean);
  return parts[parts.length - 1] || path;
}

// Highlight the matched substring of `text` (case-insensitive), HTML-escaping
// both the surrounding text and the match so a path can't inject markup.
function _recentHighlight(text, query) {
  const safe = escapeHtml(text);
  if (!query) return safe;
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx < 0) return safe;
  const before = escapeHtml(text.slice(0, idx));
  const match = escapeHtml(text.slice(idx, idx + query.length));
  const after = escapeHtml(text.slice(idx + query.length));
  return `${before}<mark class="recent-hl">${match}</mark>${after}`;
}

function _renderRecentList() {
  const listEl = document.getElementById("recentPathsList");
  const countEl = document.getElementById("recentCount");
  const clearBtn = document.getElementById("recentSearchClear");
  if (!listEl) return;
  const _t = (typeof t === "function") ? t : (k) => k;
  const q = _recentFilter.trim();
  const filtered = q
    ? _recentProjects.filter(
        (item) => item.path.toLowerCase().includes(q.toLowerCase()),
      )
    : _recentProjects;
  if (countEl) countEl.textContent = q ? `${filtered.length}/${_recentProjects.length}` : String(_recentProjects.length);
  if (clearBtn) clearBtn.hidden = !q;
  if (filtered.length === 0) {
    const _t = (typeof t === "function") ? t : (k) => k;
    const msg = q ? _t("pm.recentNoMatch") : _t("pm.recentEmpty");
    listEl.innerHTML = `<div class="recent-paths-empty">${escapeHtml(msg)}</div>`;
    return;
  }
  listEl.innerHTML = filtered
    .map((item) => {
      const name = _recentName(item.path);
      if (item.exists === false) {
        return `<div class="recent-path-item recent-path-missing" data-tofu-action="beginRelinkRecent('${escapeHtml(item.path)}')" title="${escapeHtml(_t("pm.recentMissingTitle", { path: item.path }))}">
         <span class="recent-path-text">
           <span class="recent-path-name">${_recentHighlight(name, q)}<span class="recent-missing-badge">${escapeHtml(_t("pm.recentMissing"))}</span></span>
           <span class="recent-path-full">${_recentHighlight(item.path, q)}</span>
         </span>
       </div>`;
      }
      return `<div class="recent-path-item" data-tofu-action="selectRecentProject('${escapeHtml(item.path)}')" title="${escapeHtml(item.path)}">
         <span class="recent-path-text">
           <span class="recent-path-name">${_recentHighlight(name, q)}</span>
           <span class="recent-path-full">${_recentHighlight(item.path, q)}</span>
         </span>
         ${item.count > 1 ? `<span class="recent-path-count">×${item.count}</span>` : ""}
       </div>`;
    })
    .join("");
}

function _filterRecentProjects(value) {
  _recentFilter = value || "";
  _renderRecentList();
}

function _clearRecentSearch() {
  _recentFilter = "";
  const input = document.getElementById("recentSearchInput");
  if (input) {
    input.value = "";
    input.focus();
  }
  _renderRecentList();
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
  _renderBrowseList();
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
  // Land the browser near the old location so the user can spot the
  // renamed/moved directory, then arm the browser's + as the relink target.
  const parts = String(path).split("/").filter(Boolean);
  const parent = parts.length > 1 ? "/" + parts.slice(0, -1).join("/") : "~";
  browseDirectory(parent);
}

function cancelRelinkRecent() {
  _relinkOldPath = "";
  _renderProjectModalHint();
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

let _browseState = {
  path: "", dirs: [], parent: null, showHidden: false, truncated: false
};
const _projectBrowseCoordinator = createProjectBrowseCoordinator(function () {
  return ProjectPresentationShellState.sessionStorage;
});
const _projectDirectoryBrowser = createProjectDirectoryBrowser({
  escapeHtml: escapeHtml,
  translate: function (key, values) {
    return (typeof t === "function") ? t(key, values) : key;
  },
  assets: PROJECT_PRESENTATION_ASSETS,
});

function _applyBrowseData(data) {
  _browseState.path = data.path;
  _browseState.dirs = data.dirs;
  _browseState.parent = data.parent;
  _browseState.filesCount = data.filesCount;
  _browseState.truncated = data.truncated;
  _renderBreadcrumb(data.path);
  const back = document.getElementById("browseBackBtn");
  if (back) back.disabled = !data.parent;
  _renderBrowseList();
}

async function browseDirectory(path) {
  const listEl = document.getElementById("browseList");
  if (!listEl) return;
  const requestedPath = String(path || "~");
  if (_projectDirectoryBrowser.resetForNavigation(
    _browseState.path, requestedPath)) _syncBrowseFilterUi(false);
  const showHidden = !!_browseState.showHidden;
  const load = _projectBrowseCoordinator.load(
    requestedPath,
    showHidden,
    function (signal) {
      return Api.project.browse(
        requestedPath, showHidden, signal ? { signal: signal } : {});
    },
  );
  const cached = load.cached;
  if (cached) {
    _applyBrowseData(cached);
  } else {
    listEl.innerHTML =
      '<div class="fb-state"><div class="fb-state-spinner"></div><span>' +
      escapeHtml(typeof t === 'function' ? t('common.loading') : 'Loading…') +
      '</span></div>';
  }
  _renderRemoteDevicesSection();
  const outcome = await load.completion;
  if (outcome.kind === 'cancelled') return;
  if (outcome.kind === 'failed') {
    if (!cached) listEl.innerHTML =
      '<div class="fb-state fb-state-error"><span>' +
      escapeHtml(outcome.message) +
      "</span></div>";
    return;
  }
  _applyBrowseData(outcome.data);
}

function _renderBrowseList() {
  const listEl = document.getElementById("browseList");
  if (!listEl) return;
  listEl.innerHTML = _projectDirectoryBrowser.render(_browseState, _mpFolders);
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
    '<button class="pm-crumb pm-crumb-root" data-tofu-action="browseDirectory(\'' +
    rootPath.replace(/\\/g, "\\\\").replace(/'/g, "\\'") +
    '\')" title="' + escapeHtml(rootPath) + '">' +
    PROJECT_PRESENTATION_ASSETS.home +
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
      '" data-tofu-action="browseDirectory(\'' + safe + '\')" title="' + escapeHtml(full) + '">' +
      escapeHtml(parts[i]) + '</button>'
    );
  }
  el.innerHTML = crumbs.join("");
  // Scroll to the end so the deepest crumb is visible.
  el.scrollLeft = el.scrollWidth;
}

/* Add a folder shown in the browser straight into the workspace list.
   Re-renders the current directory so the row's "added" state updates. */
function mpAddBrowsedPath(path) {
  if (!path) return;
  // Relink mode armed: the browser's + names the moved directory's new home.
  if (_relinkOldPath) { _confirmRelinkRecent(path); return; }
  if (!_mpFolders.includes(path)) {
    _mpFolders.push(path);
    _mpRenderTags();
    _renderBrowseList();
    _checkGitRootHint(path);
  }
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

function browseParent() {
  if (_browseState.parent) browseDirectory(_browseState.parent);
}

function _syncBrowseFilterUi(focusInput) {
  const filter = _projectDirectoryBrowser.filterValue();
  const input = document.getElementById("browseSearchInput");
  if (input) {
    input.value = filter;
    if (focusInput) input.focus();
  }
  const clear = document.getElementById("browseSearchClear");
  if (clear) clear.hidden = !filter;
}

function _filterBrowseDirs(value) {
  _projectDirectoryBrowser.setFilter(value);
  _syncBrowseFilterUi(false);
  _renderBrowseList();
}

function _clearBrowseSearch() {
  _projectDirectoryBrowser.clearFilter();
  _syncBrowseFilterUi(true);
  _renderBrowseList();
}

/* Create a new sub-folder inside the directory the browser is currently
   showing, then navigate straight into it (owner directive 2026-08-31) —
   the parent listing is still invalidated so going back up shows it. */
async function mpNewFolder() {
  const parent = _browseState.path;
  if (!parent) return;
  const name = await showPrompt(t('folder.createInHint', { dir: parent }), {
    title: t('folder.createTitle'),
    placeholder: t('folder.namePh'),
    okText: t('folder.create'),
  });
  if (name == null) return; // cancelled
  const clean = String(name).trim();
  if (!clean) return;
  try {
    const resp = await Api.project.mkdir(parent, clean);
    const data = resp ? await resp.json().catch(() => ({})) : {};
    if (resp && resp.ok && data.ok) {
      if (typeof showToast === 'function') showToast(t('folder.created'), 'success');
      _projectBrowseCoordinator.invalidate(parent);
      browseDirectory(data.path || (parent.replace(/\/+$/, "") + "/" + clean));
    } else {
      await showAlert((data && data.error) || t('folder.createFailed'),
        { title: t('folder.createFailed') });
    }
  } catch (e) {
    await showAlert(e.message || t('folder.createFailed'),
      { title: t('folder.createFailed') });
  }
}

/* Delete a folder shown in the browser (moved to a recoverable trash bin),
   then refresh the current directory. */
async function mpDeleteFolder(path, name) {
  if (!path) return;
  if (!await showConfirm(
    t('folder.deleteDirConfirm', { name: name || path }) + '\n' +
    t('folder.deleteDirHint'),
    { danger: true, title: t('folder.deleteTitle') })) return;
  /* INSTANT-UI (owner directive 2026-07-31, ): the row
   *   leaves the browser list AND the staged workspace tag disappears in the
   *   SAME task as the confirm — the old code awaited the rmdir RTT first.
   *   The deletion lands in .tofu_trash (recoverable by design), so the
   *   optimistic removal is the safest in the app: on failure we re-fetch
   *   the directory (server truth restores the row) and re-stage the tag. */
  const _rowIdx = _browseState.dirs.findIndex((d) => d.path === path);
  _projectBrowseCoordinator.invalidate(_browseState.path);
  if (_rowIdx >= 0) {
    _browseState.dirs.splice(_rowIdx, 1);
    _renderBrowseList();
  }
  const _stagedIdx = _mpFolders.indexOf(path);
  const _wasStaged = _stagedIdx !== -1;
  if (_wasStaged) { _mpFolders.splice(_stagedIdx, 1); _mpRenderTags(); }
  try {
    const resp = await Api.project.rmdir(path);
    const data = resp ? await resp.json().catch(() => ({})) : {};
    if (resp && resp.ok && data.ok) {
      if (typeof showToast === 'function') showToast(t('folder.deleted'), 'success');
      _mpReadOnly.delete(path);
      browseDirectory(_browseState.path);
    } else {
      throw new Error((data && data.error) || t('folder.deleteFailed'));
    }
  } catch (e) {
    /* Rollback: re-stage the workspace tag at its original index, then
     *   re-fetch the list so the row comes back from server truth. */
    if (_wasStaged) {
      _mpFolders.splice(Math.min(_stagedIdx, _mpFolders.length), 0, path);
      _mpRenderTags();
    }
    browseDirectory(_browseState.path);
    await showAlert(e.message || t('folder.deleteFailed'),
      { title: t('folder.deleteFailed') });
  }
}

function toggleHiddenDirs() {
  _browseState.showHidden = !_browseState.showHidden;
  var btn = document.getElementById("browseHiddenBtn");
  btn.classList.toggle("active", _browseState.showHidden);
  btn.title = _browseState.showHidden
    ? (typeof t === 'function' ? t('pm.hideHidden') : "Hide hidden dirs")
    : (typeof t === 'function' ? t('pm.showHidden') : "Show hidden dirs");
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
    _renderBrowseList();
  }
  input.value = "";
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
  var row = target && target.closest ? target.closest('.folder-item[data-dir-path]') : null;
  if (row && row.dataset.dirPath) return row.dataset.dirPath;
  return (typeof _browseState !== 'undefined' && _browseState.path) ? _browseState.path : '';
}

function _clearFolderDropHighlight() {
  var el = document.getElementById('folderBrowser');
  if (el) el.classList.remove('fb-drop-active');
  document.querySelectorAll('.folder-item.fb-drop-row')
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
    var row = e.target.closest ? e.target.closest('.folder-item[data-dir-path]') : null;
    document.querySelectorAll('.folder-item.fb-drop-row')
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
    return _uploadDroppedFile(f, dir);
  }));
  var saved = 0, renamed = 0;
  var errors = [];
  results.forEach(function (r, i) {
    if (r.status === 'fulfilled' && r.value && r.value.ok) {
      saved++;
      if (r.value.data && r.value.data.renamed) renamed++;
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
    if (errors.length) {
      showToast(t('folderDrop.failed', { n: errors.length }), 'error',
        errors.slice(0, 3).join('\n'), 6000);
    }
  }
  // Refresh so the newly-saved files reflect in the browser's file count.
  if (typeof browseDirectory === 'function' && typeof _browseState !== 'undefined' && _browseState.path) {
    _projectBrowseCoordinator.invalidate(_browseState.path);
    browseDirectory(_browseState.path);
  }
  // Surface the write in the project bar's file-changes affordance.
  if (typeof loadProjectStatus === 'function') { try { loadProjectStatus(); } catch (_e) { /* best-effort */ } }
}
