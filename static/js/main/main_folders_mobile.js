/* ═══════════════════════════════════════════════════════════════════
   main folders mobile — extracted from main.js (split 2026-05-28)

   Folder picker / tabs / drag-drop + sidebar + mobile sheet + mobile backend section.

   This file is concatenated by lib/js_bundler.py BEFORE main.js so
   the boot IIFE can reference these symbols. Symbols share `window`
   scope — no imports / exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/** Show a dropdown under the folder-assign button to pick or create a folder */
function _showFolderPicker(convId, anchorEl) {
  // Remove any existing picker
  _closeFolderPicker();
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return;

  const folders = typeof getFolders === 'function' ? getFolders() : [];
  const picker = document.createElement('div');
  picker.className = 'folder-picker';
  picker.id = '_folderPicker';

  let html = `<div class="folder-picker-title">${t('folder.moveToFolder')}</div>`;
  // "Remove from folder" option if currently in a folder
  if (conv.folderId) {
    html += `<div class="folder-picker-item folder-picker-remove" data-folder-id="">` +
      `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>` +
      `${t('folder.removeFromFolder')}</div>`;
  }
  // Existing folders
  for (const f of folders) {
    const active = conv.folderId === f.id ? ' active' : '';
    const dot = f.color ? `<span class="folder-color-dot" style="background:${escapeHtml(f.color)}"></span>` : '';
    html += `<div class="folder-picker-item${active}" data-folder-id="${escapeHtml(f.id)}">${dot}${escapeHtml(f.name)}</div>`;
  }
  // "New folder" option
  html += `<div class="folder-picker-divider"></div>`;
  html += `<div class="folder-picker-item folder-picker-new">` +
    `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>` +
    `${t('folder.newFolder')}</div>`;

  picker.innerHTML = html;

  // Position near the anchor button
  document.body.appendChild(picker);
  const rect = anchorEl.getBoundingClientRect();
  picker.style.top = Math.min(rect.bottom + 4, window.innerHeight - picker.offsetHeight - 8) + 'px';
  picker.style.left = Math.min(rect.left, window.innerWidth - picker.offsetWidth - 8) + 'px';

  // Handle clicks
  picker.addEventListener('click', async (ev) => {
    ev.stopPropagation();
    const item = ev.target.closest('.folder-picker-item');
    if (!item) return;

    if (item.classList.contains('folder-picker-new')) {
      _closeFolderPicker();
      _promptCreateFolder(convId);
      return;
    }
    const folderId = item.dataset.folderId;
    setConversationFolder(convId, folderId || null);
    _closeFolderPicker();
    if (folderId) {
      const f = getFolderById(folderId);
      if (typeof showToast === 'function') showToast('', t('folder.movedToFolder'), f ? f.name : '', 2000);
    } else {
      if (typeof showToast === 'function') showToast('', t('folder.removedFromFolder'), '', 2000);
    }
  });

  // Close on outside click
  setTimeout(() => {
    document.addEventListener('click', _closeFolderPicker, { once: true });
  }, 0);
}

function _closeFolderPicker() {
  const p = document.getElementById('_folderPicker');
  if (p) p.remove();
}

/** Show inline dialog to create a new folder, optionally assigning a conv to it */
function _promptCreateFolder(assignConvId) {
  // Remove any existing dialog
  const existing = document.getElementById('_folderCreateDialog');
  if (existing) existing.remove();

  const colors = ['#6e56cf', '#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#ec4899', '#8b5cf6', '#14b8a6'];
  let selectedColor = colors[Math.floor(Math.random() * colors.length)];

  const overlay = document.createElement('div');
  overlay.id = '_folderCreateDialog';
  overlay.className = 'folder-dialog-overlay';
  overlay.innerHTML = `
    <div class="folder-dialog">
      <div class="folder-dialog-title">${t('folder.createTitle')}</div>
      <input type="text" class="folder-dialog-input" id="_folderNameInput"
             placeholder="${t('folder.namePh')}" maxlength="50" autocomplete="off" spellcheck="false">
      <div class="folder-dialog-colors" id="_folderColorPicker">
        ${colors.map(c => `<span class="folder-color-dot${c === selectedColor ? ' selected' : ''}" data-color="${c}" style="background:${c}"></span>`).join('')}
      </div>
      <div class="folder-dialog-actions">
        <button class="folder-dialog-cancel" id="_folderDialogCancel">${t('folder.cancel')}</button>
        <button class="folder-dialog-ok" id="_folderDialogOk">${t('folder.create')}</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  const nameInput = document.getElementById('_folderNameInput');
  const colorPicker = document.getElementById('_folderColorPicker');
  const okBtn = document.getElementById('_folderDialogOk');
  const cancelBtn = document.getElementById('_folderDialogCancel');

  // Focus input
  setTimeout(() => nameInput.focus(), 50);

  // Color selection
  colorPicker.addEventListener('click', (e) => {
    const dot = e.target.closest('.folder-color-dot');
    if (!dot) return;
    colorPicker.querySelectorAll('.folder-color-dot').forEach(d => d.classList.remove('selected'));
    dot.classList.add('selected');
    selectedColor = dot.dataset.color;
  });

  function _closeDialog() { overlay.remove(); }

  async function _submit() {
    const name = nameInput.value.trim();
    if (!name) { nameInput.focus(); return; }
    okBtn.disabled = true;
    okBtn.textContent = t('folder.creating');
    const folder = await createFolder(name, selectedColor);
    _closeDialog();
    if (!folder) {
      if (typeof showToast === 'function') showToast('', t('folder.createFailed'), t('folder.cannotCreate'), 3000);
      return;
    }
    if (assignConvId) {
      setConversationFolder(assignConvId, folder.id);
    }
    renderConversationList();
    if (typeof showToast === 'function') showToast('', t('folder.created'), folder.name, 2000);
  }

  okBtn.addEventListener('click', _submit);
  cancelBtn.addEventListener('click', _closeDialog);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) _closeDialog(); });
  nameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _submit(); }
    if (e.key === 'Escape') _closeDialog();
  });
}

function _promptRenameFolder(folderId) {
  const f = getFolderById(folderId);
  if (!f) return;

  // Remove any existing dialog
  const existing = document.getElementById('_folderCreateDialog');
  if (existing) existing.remove();

  const colors = ['#6e56cf', '#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#ec4899', '#8b5cf6', '#14b8a6'];
  let selectedColor = f.color || '';

  const overlay = document.createElement('div');
  overlay.id = '_folderCreateDialog';
  overlay.className = 'folder-dialog-overlay';
  overlay.innerHTML = `
    <div class="folder-dialog">
      <div class="folder-dialog-title">${t('folder.renameTitle')}</div>
      <input type="text" class="folder-dialog-input" id="_folderNameInput"
             placeholder="${t('folder.namePh')}" maxlength="50" autocomplete="off" spellcheck="false">
      <div class="folder-dialog-colors" id="_folderColorPicker">
        ${colors.map(c => `<span class="folder-color-dot${c === selectedColor ? ' selected' : ''}" data-color="${c}" style="background:${c}"></span>`).join('')}
      </div>
      <div class="folder-dialog-actions">
        <button class="folder-dialog-cancel" id="_folderDialogCancel">${t('folder.cancel')}</button>
        <button class="folder-dialog-ok" id="_folderDialogOk">${t('folder.ok')}</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  const nameInput = document.getElementById('_folderNameInput');
  const colorPicker = document.getElementById('_folderColorPicker');
  nameInput.value = f.name;
  setTimeout(() => { nameInput.focus(); nameInput.select(); }, 50);

  // Color selection — a second tap on the selected dot clears the color.
  colorPicker.addEventListener('click', (e) => {
    const dot = e.target.closest('.folder-color-dot');
    if (!dot) return;
    const wasSelected = dot.classList.contains('selected');
    colorPicker.querySelectorAll('.folder-color-dot').forEach(d => d.classList.remove('selected'));
    if (wasSelected) {
      selectedColor = '';
    } else {
      dot.classList.add('selected');
      selectedColor = dot.dataset.color;
    }
  });

  function _closeDialog() { overlay.remove(); }

  async function _submit() {
    const name = nameInput.value.trim();
    const colorChanged = selectedColor !== (f.color || '');
    if ((!name || name === f.name) && !colorChanged) { _closeDialog(); return; }
    const updates = {};
    if (name && name !== f.name) updates.name = name;
    if (colorChanged) updates.color = selectedColor;
    await updateFolder(folderId, updates);
    _closeDialog();
    renderConversationList();
  }

  document.getElementById('_folderDialogOk').addEventListener('click', _submit);
  document.getElementById('_folderDialogCancel').addEventListener('click', _closeDialog);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) _closeDialog(); });
  nameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _submit(); }
    if (e.key === 'Escape') _closeDialog();
  });
}

function _confirmDeleteFolder(folderId) {
  const f = getFolderById(folderId);
  if (!f) return;

  const existing = document.getElementById('_folderCreateDialog');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = '_folderCreateDialog';
  overlay.className = 'folder-dialog-overlay';
  overlay.innerHTML = `
    <div class="folder-dialog">
      <div class="folder-dialog-title">${t('folder.deleteTitle')}</div>
      <div style="font-size:13px;color:var(--text-secondary);margin-bottom:16px;line-height:1.5">
        ${t('folder.deleteConfirm')} <b style="color:var(--text-primary)">${f.name}</b>？<br>
        ${t('folder.deleteHint')}
      </div>
      <div class="folder-dialog-actions">
        <button class="folder-dialog-cancel" id="_folderDialogCancel">${t('folder.cancel')}</button>
        <button class="folder-dialog-ok" id="_folderDialogOk" style="background:#ef4444">${t('common.delete')}</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  /* This is a DESTRUCTIVE modal with no text input, so (unlike the
   * create/rename dialogs which hang Escape off their <input>) we bind a
   * document-level keydown and clean it up on close. Escape dismisses it —
   * matching the create/rename dialogs — and we focus Cancel by default so
   * an accidental long-press → delete isn't one stray tap from destruction. */
  function _closeDialog() {
    document.removeEventListener('keydown', _onKey);
    overlay.remove();
  }
  function _onKey(e) { if (e.key === 'Escape') { e.preventDefault(); _closeDialog(); } }
  document.addEventListener('keydown', _onKey);

  document.getElementById('_folderDialogOk').addEventListener('click', async () => {
    _closeDialog();
    // If we're viewing this folder, exit folder view first
    if (typeof getActiveFolderId === 'function' && getActiveFolderId() === folderId) {
      setActiveFolderId(null);
    }
    await deleteFolder(folderId);
    renderConversationList();
    if (typeof showToast === 'function') showToast('', t('folder.deleted'), f.name, 2000);
  });
  const _cancelBtn = document.getElementById('_folderDialogCancel');
  _cancelBtn.addEventListener('click', _closeDialog);
  setTimeout(() => _cancelBtn.focus(), 50);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) _closeDialog(); });
}

/** Toggle the vertical project rail between labeled and icon-only, persisting
 * the choice (same localStorage key the renderer reads back). */
const _RAIL_COLLAPSE_KEY = 'tofu_project_rail_collapsed';
function _toggleProjectRail() {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  const collapsed = !sidebar.classList.contains('rail-collapsed');
  sidebar.classList.toggle('rail-collapsed', collapsed);
  try { localStorage.setItem(_RAIL_COLLAPSE_KEY, collapsed ? '1' : '0'); }
  catch (e) { console.warn('[rail] persist collapse failed: %s', e); }
  // Keep the pre-paint html[data-rail] hint in sync so the NEXT load paints at
  // the matching (full vs collapsed) rail width — zero CLS across the toggle.
  try {
    if (document.documentElement.hasAttribute('data-rail')) {
      document.documentElement.setAttribute('data-rail', collapsed ? 'collapsed' : 'full');
    }
  } catch (e) { /* non-DOM context */ }
  // Refresh the collapse button's tooltip to match the new state.
  const btn = document.querySelector('.project-rail-collapse');
  if (btn) {
    const tip = collapsed ? t('sidebar.expandRail') : t('sidebar.collapseRail');
    btn.title = tip;
    btn.setAttribute('aria-label', tip);
  }
}

/** Initialize the project-rail interactions (rail lives in #folderTabs). */
function _initFolderTabs() {
  const tabsEl = document.getElementById('folderTabs');
  if (!tabsEl) return;

  // ── Quick "+ New folder" entry point (shown when 0 folders exist) ──
  const quickAdd = document.getElementById('folderQuickAdd');
  if (quickAdd) {
    quickAdd.addEventListener('click', (e) => {
      e.stopPropagation();
      _promptCreateFolder(null);
    });
  }

  // Click: rail collapse toggle, new-folder footer, or switch project.
  tabsEl.addEventListener('click', (e) => {
    const collapseBtn = e.target.closest('.project-rail-collapse');
    if (collapseBtn) { e.stopPropagation(); _toggleProjectRail(); return; }
    const tab = e.target.closest('.folder-tab');
    if (!tab) return;
    e.stopPropagation();
    if (tab.classList.contains('folder-tab-add')) {
      _promptCreateFolder(null);
      return;
    }
    const folderId = tab.dataset.folderId;
    setActiveFolderId(folderId || null);
  });

  // Right-click / context menu on folder tabs (rename/delete)
  tabsEl.addEventListener('contextmenu', (e) => {
    const tab = e.target.closest('.folder-tab');
    if (!tab || tab.classList.contains('folder-tab-add') || !tab.dataset.folderId) return;
    e.preventDefault();
    e.stopPropagation();
    _showFolderTabMenu(tab.dataset.folderId, e.clientX, e.clientY);
  });

  /* Touch long-press → rename/delete menu. Mobile browsers don't reliably
   * fire `contextmenu` on a long-press, so on touch devices that menu was
   * unreachable. A 500ms hold (cancelled by movement or an early release)
   * opens the same _showFolderTabMenu. `_lpFired` suppresses the click that
   * a tap-release would otherwise deliver (which switches folders). */
  if ("ontouchstart" in window) {
    let _lpTimer = null, _lpFired = false, _lpX = 0, _lpY = 0;
    const _lpCancel = () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } };
    tabsEl.addEventListener('touchstart', (e) => {
      _lpFired = false;
      const touch = e.touches[0];
      const tab = touch && e.target.closest('.folder-tab');
      if (!tab || tab.classList.contains('folder-tab-add') || !tab.dataset.folderId) return;
      _lpX = touch.clientX; _lpY = touch.clientY;
      const fid = tab.dataset.folderId;
      _lpCancel();
      _lpTimer = setTimeout(() => {
        _lpTimer = null; _lpFired = true;
        _showFolderTabMenu(fid, _lpX, _lpY);
      }, 500);
    }, { passive: true });
    tabsEl.addEventListener('touchmove', (e) => {
      const touch = e.touches[0];
      if (touch && (Math.abs(touch.clientX - _lpX) > 10 || Math.abs(touch.clientY - _lpY) > 10)) _lpCancel();
    }, { passive: true });
    tabsEl.addEventListener('touchend', _lpCancel, { passive: true });
    tabsEl.addEventListener('touchcancel', _lpCancel, { passive: true });
    // Swallow the click that follows a long-press so the folder isn't switched.
    tabsEl.addEventListener('click', (e) => {
      if (_lpFired) { _lpFired = false; e.stopPropagation(); e.preventDefault(); }
    }, true);
  }

  // Drag-and-drop: allow dragging conversations onto folder tabs
  tabsEl.addEventListener('dragover', (e) => {
    if (!_dragConvId) return;
    const tab = e.target.closest('.folder-tab');
    if (!tab || tab.classList.contains('folder-tab-add')) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    tabsEl.querySelectorAll('.folder-tab').forEach(t => t.classList.remove('folder-tab-drop'));
    tab.classList.add('folder-tab-drop');
  });

  tabsEl.addEventListener('dragleave', (e) => {
    const tab = e.target.closest('.folder-tab');
    if (tab) tab.classList.remove('folder-tab-drop');
  });

  tabsEl.addEventListener('drop', (e) => {
    e.preventDefault();
    const convId = _dragConvId || e.dataTransfer.getData('text/plain');
    if (!convId) return;
    const tab = e.target.closest('.folder-tab');
    if (!tab || tab.classList.contains('folder-tab-add')) return;
    const folderId = tab.dataset.folderId || null;
    tabsEl.querySelectorAll('.folder-tab').forEach(t => t.classList.remove('folder-tab-drop'));
    setConversationFolder(convId, folderId);
    const f = folderId ? getFolderById(folderId) : null;
    if (f) {
      if (typeof showToast === 'function') showToast('', t('folder.movedToFolder'), f.name, 2000);
    } else if (!folderId) {
      if (typeof showToast === 'function') showToast('', t('folder.removedFromFolder'), '', 2000);
    }
  });
}

/** Show context menu for a folder tab (rename/delete) */
function _showFolderTabMenu(folderId, x, y) {
  // Remove existing
  const old = document.getElementById('_folderTabMenu');
  if (old) old.remove();

  const f = getFolderById(folderId);
  if (!f) return;

  const menu = document.createElement('div');
  menu.id = '_folderTabMenu';
  menu.className = 'folder-tab-menu';
  menu.innerHTML = `
    <div class="folder-tab-menu-item" data-action="rename">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>
      ${t('folder.rename')}
    </div>
    <div class="folder-tab-menu-item folder-tab-menu-delete" data-action="delete">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
      ${t('folder.deleteAction')}
    </div>
  `;
  document.body.appendChild(menu);

  // Position
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  menu.style.left = Math.min(x, window.innerWidth - mw - 8) + 'px';
  menu.style.top = Math.min(y, window.innerHeight - mh - 8) + 'px';

  menu.addEventListener('click', (e) => {
    const item = e.target.closest('.folder-tab-menu-item');
    if (!item) return;
    menu.remove();
    if (item.dataset.action === 'rename') _promptRenameFolder(folderId);
    else if (item.dataset.action === 'delete') _confirmDeleteFolder(folderId);
  });

  // Close on outside click
  setTimeout(() => {
    document.addEventListener('click', function _close() {
      menu.remove();
      document.removeEventListener('click', _close);
    }, { once: true });
  }, 0);
}

/* ── Drag-and-drop conversations — drag from convList, drop on folder tabs ── */
let _dragConvId = null;
function _initFolderDragDrop() {
  const convList = document.getElementById('convList');
  if (!convList) return;

  function _onDragStart(e) {
    const item = e.target.closest('.conv-item');
    if (!item || !item.dataset.convId) return;
    _dragConvId = item.dataset.convId;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', item.dataset.convId);
    item.classList.add('conv-dragging');
    // Highlight folder tabs as drop targets
    setTimeout(() => {
      const tabsEl = document.getElementById('folderTabs');
      if (tabsEl) tabsEl.classList.add('folder-tabs-drop-active');
    }, 0);
  }

  function _onDragEnd() {
    _dragConvId = null;
    document.querySelectorAll('.conv-dragging').forEach(el => el.classList.remove('conv-dragging'));
    const tabsEl = document.getElementById('folderTabs');
    if (tabsEl) {
      tabsEl.classList.remove('folder-tabs-drop-active');
      tabsEl.querySelectorAll('.folder-tab-drop').forEach(t => t.classList.remove('folder-tab-drop'));
    }
  }

  convList.addEventListener('dragstart', _onDragStart);
  convList.addEventListener('dragend', _onDragEnd);
}


function toggleSidebar() {
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("sidebarBackdrop");
  sidebar.classList.toggle("collapsed");
  // Mobile only: show/hide backdrop overlay
  if (backdrop) {
    const isDrawer = isDrawerViewport();
    const isOpen = !sidebar.classList.contains("collapsed");
    backdrop.classList.toggle("visible", isDrawer && isOpen);
  }
  /* Sidebar width change affects available space for toolbar */
  setTimeout(_scheduleReflow, 250);  /* after sidebar transition finishes */
}

/* ── Mobile: auto-collapse sidebar on load ── */
(function initMobileLayout() {
  if (isDrawerViewport()) {
    const sidebar = document.getElementById("sidebar");
    if (sidebar && !sidebar.classList.contains("collapsed")) {
      sidebar.classList.add("collapsed");
    }
  }
})();

/* ═══ Mobile "More" Bottom Sheet ═══
 * Mirrors the state of the desktop toolbar toggles (codeExec, memory,
 * translate, browser, imageGen, humanGuidance, swarm, endpoint).
 * Each item reads the live state from the existing toggle elements. */

function toggleMobileSheet() {
  const sheet = document.getElementById("mobileSheet");
  const backdrop = document.getElementById("mobileSheetBackdrop");
  if (!sheet) return;
  const isOpen = sheet.classList.contains("open");
  if (isOpen) {
    closeMobileSheet();
  } else {
    updateMobileSheet();
    sheet.classList.add("open");
    if (backdrop) backdrop.classList.add("open");
  }
}

function closeMobileSheet() {
  const sheet = document.getElementById("mobileSheet");
  const backdrop = document.getElementById("mobileSheetBackdrop");
  if (sheet) sheet.classList.remove("open");
  if (backdrop) backdrop.classList.remove("open");
}

function updateMobileSheet() {
  /* ★ Capability-mode rows (Chat/Studio) mirror the segmented dial — a
   * radio-style highlight, NOT a toggle. Reflect the live chatMode global. */
  const _mode = (typeof chatMode !== 'undefined' ? chatMode : 'chat');
  document.querySelectorAll('.mobile-mode-item').forEach(el => {
    el.classList.toggle('active', el.dataset.mode === _mode);
  });

  /* Sync each mobile sheet item's .active class with the desktop toggle state.
   * codeExec / memory are gone — the capability mode owns them now. */
  const map = {
    mobileTranslate:   "translateToggle",
    mobileBrowser:     "browserToggle",
    mobileImageGen:    "imageGenToggle",
    mobileHumanGuidance: "humanGuidanceToggle",
    mobileDesktop:     "desktopToggle",
    mobileSwarm:       "swarmToggle",
    mobileEndpoint:    "endpointToggle",
    mobileAutopilot:   "autopilotToggle"
  };
  let activeCount = 0;
  for (const [mobileId, desktopId] of Object.entries(map)) {
    const mobileEl = document.getElementById(mobileId);
    const desktopEl = document.getElementById(desktopId);
    if (!mobileEl || !desktopEl) continue;
    const isActive = desktopEl.classList.contains("active");
    mobileEl.classList.toggle("active", isActive);
    if (isActive) activeCount++;
  }
  /* Update the "more" button to show if any toggles are active */
  const moreBtn = document.getElementById("mobileMoreBtn");
  if (moreBtn) moreBtn.classList.toggle("has-active", activeCount > 0);
  /* Also update desktop submenu counts (they still exist in DOM) */
  if (typeof updateSubmenuCounts === "function") updateSubmenuCounts();
  /* Sync mobile depth section visibility + active state */
  updateMobileDepth();
  /* Sync the Context section — the compaction entry point (the desktop
   * context sphere is display:none below 900px, so this is the only mobile
   * access). Reflect live usage % + disable "compact now" while a task runs
   * (mirrors the desktop popover's busy guard), and hide "view history" when
   * this conversation has no compaction snapshots. */
  updateMobileContext();
}

/** Sync the mobile bottom-sheet Context section with live usage + state. */
function updateMobileContext() {
  const section = document.getElementById("mobileContextSection");
  if (!section) return;
  const conv = (typeof getActiveConv === "function") ? getActiveConv() : null;
  const summary = (typeof window.contextUsageSummary === "function")
    ? window.contextUsageSummary() : null;

  /* "Compact now" — disabled while a task is live (can't rewrite mid-turn). */
  const compactItem = document.getElementById("mobileCompactNow");
  const desc = document.getElementById("mobileCompactDesc");
  const busy = !!(conv && ((typeof activeStreams !== "undefined" && activeStreams &&
      typeof activeStreams.has === "function" && activeStreams.has(conv.id)) ||
      conv.activeTaskId));
  if (compactItem) {
    compactItem.classList.toggle("disabled", busy || !conv);
    if (desc) {
      if (busy) {
        desc.textContent = (typeof t === "function")
          ? t("compactNow.busy") : "A task is running — cannot compact";
      } else if (summary && summary.hasUsage) {
        /* Show the live percentage so the user sees WHY they'd compact. */
        desc.textContent = (typeof t === "function")
          ? t("mobile.compactUsage", { pct: summary.pct }) : (summary.pct + "% used");
      } else {
        desc.textContent = (typeof t === "function")
          ? t("mobile.compactDesc") : "Compact this conversation to free context";
      }
    }
  }

  /* "View history" — only meaningful when snapshots exist. */
  const histItem = document.getElementById("mobileCompactHistory");
  if (histItem) {
    const hasHistory = !!(summary && summary.compactions > 0);
    histItem.style.display = hasHistory ? "" : "none";
  }
}
if (typeof window !== "undefined") window.updateMobileContext = updateMobileContext;

/** Run manual compaction from the mobile sheet, then close it. Delegates to
 * the same closure the desktop context sphere uses (reload + re-render +
 * gauge drop + toast), so behaviour is identical across surfaces. */
function _mobileCompactNow() {
  const item = document.getElementById("mobileCompactNow");
  if (item && item.classList.contains("disabled")) return;
  const cid = (typeof activeConvId !== "undefined") ? activeConvId : null;
  if (!cid) return;
  closeMobileSheet();
  if (typeof window.runManualCompaction === "function") {
    window.runManualCompaction(cid);
  } else {
    console.warn("[mobileCompact] runManualCompaction not loaded");
  }
}
if (typeof window !== "undefined") window._mobileCompactNow = _mobileCompactNow;

/**
 * Sync the mobile bottom sheet depth bar with the desktop depth bar.
 * Shows/hides the section based on whether the model supports thinking depth.
 */
function updateMobileDepth() {
  const desktopBar = document.getElementById("thinkingDepthSection");
  const mobileSection = document.getElementById("mobileDepthSection");
  if (!mobileSection) return;
  /* Show mobile depth section when desktop depth bar has display set to 'flex' by JS
   * (on mobile, CSS hides it with display:none!important, but JS still sets .style.display) */
  const isVisible = desktopBar && (desktopBar.style.display === "flex" || desktopBar.style.display === "");
  mobileSection.style.display = isVisible ? "" : "none";
  if (!isVisible) return;
  /* Sync active button state */
  const activeDesktop = desktopBar.querySelector(".depth-btn.active");
  const activeDepth = activeDesktop ? activeDesktop.dataset.depth : "medium";
  mobileSection.querySelectorAll(".mobile-depth-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.depth === activeDepth);
  });
}

/* ── Reflow toolbar on window resize / orientation change ── */
window.addEventListener('resize', (function() {
  let tid;
  /* Remember the last drawer-vs-desktop state so we only act on a CROSSING
   * (a tablet rotating portrait↔landscape, or a resize past the breakpoint) —
   * not on every keyboard-driven resize event within the same layout class. */
  let _wasDrawer = (typeof isDrawerViewport === 'function') && isDrawerViewport();
  return function() {
    clearTimeout(tid);
    tid = setTimeout(function() {
      _scheduleReflow();
      const nowDrawer = isDrawerViewport();
      const sidebar = document.getElementById("sidebar");
      const bd = document.getElementById('sidebarBackdrop');
      if (nowDrawer && !_wasDrawer) {
        /* Crossed INTO the drawer viewport (e.g. landscape→portrait tablet):
         * the CSS just turned .sidebar into a fixed slide-over. If it was left
         * expanded from the desktop split it would now cover the chat with no
         * backdrop, so collapse it — same guard as initMobileLayout on load. */
        if (sidebar && !sidebar.classList.contains("collapsed")) {
          sidebar.classList.add("collapsed");
        }
        if (bd) bd.classList.remove('visible');
      } else if (!nowDrawer && _wasDrawer) {
        /* Crossed OUT to the pinned two-pane layout: drop the drawer backdrop
         * (and un-collapse so the desktop sidebar is visible again). */
        if (bd) bd.classList.remove('visible');
        if (sidebar) sidebar.classList.remove("collapsed");
      }
      _wasDrawer = nowDrawer;
    }, 120);
  };
})());

/* ── Mobile: swipe-to-open sidebar + swipe-to-close ── */
(function initMobileGestures() {
  if (!("ontouchstart" in window)) return;

  let touchStartX = 0, touchStartY = 0, touchDelta = 0, tracking = false, direction = null;
  const EDGE_WIDTH = 30;       // px from left edge to start tracking
  const SWIPE_THRESHOLD = 60;  // px to confirm a swipe

  /* A bottom sheet / portaled panel is a modal surface: a horizontal swipe on
   * it must NOT reach the sidebar-drawer gesture, otherwise the drawer opens
   * BEHIND the sheet. Covers #mobileSheet, #mobileFlowSheet, the portaled
   * timer/optimizer panels, and their shared backdrops. */
  function _isMobileOverlayOpen() {
    return !!document.querySelector(
      ".mobile-bottom-sheet.open, .mobile-panel-portaled.visible, " +
      ".mobile-panel-backdrop.open, .mobile-bottom-sheet-backdrop.open");
  }

  document.addEventListener("touchstart", function(e) {
    if (_isMobileOverlayOpen()) { tracking = false; return; }
    const t = e.touches[0];
    touchStartX = t.clientX;
    touchStartY = t.clientY;
    touchDelta = 0;
    direction = null;
    const sidebar = document.getElementById("sidebar");
    const isCollapsed = sidebar.classList.contains("collapsed");
    // Track: swipe from left edge to open, or swipe on open sidebar/backdrop to close
    if (isCollapsed && touchStartX < EDGE_WIDTH) {
      tracking = true;
    } else if (!isCollapsed) {
      tracking = true;
    }
  }, { passive: true });

  document.addEventListener("touchmove", function(e) {
    if (!tracking) return;
    const t = e.touches[0];
    const dx = t.clientX - touchStartX;
    const dy = t.clientY - touchStartY;
    // Lock direction on first significant move
    if (!direction) {
      if (Math.abs(dx) > 8 || Math.abs(dy) > 8) {
        direction = Math.abs(dx) > Math.abs(dy) ? "horizontal" : "vertical";
      }
    }
    if (direction === "horizontal") {
      touchDelta = dx;
    }
  }, { passive: true });

  document.addEventListener("touchend", function(e) {
    if (!tracking || direction !== "horizontal") {
      tracking = false;
      return;
    }
    const sidebar = document.getElementById("sidebar");
    const isCollapsed = sidebar.classList.contains("collapsed");
    if (isCollapsed && touchDelta > SWIPE_THRESHOLD) {
      // Swipe right from edge → open
      toggleSidebar();
    } else if (!isCollapsed && touchDelta < -SWIPE_THRESHOLD) {
      // Swipe left → close
      toggleSidebar();
    }
    tracking = false;
  }, { passive: true });

  // Mobile: close submenus/dropdowns when tapping outside
  document.addEventListener("click", function(e) {
    if (!isMobileViewport()) return;
    // Close open toolbar submenus
    document.querySelectorAll(".toolbar-submenu.open").forEach(sub => {
      if (!sub.contains(/** @type {Node} */ (e.target))) sub.classList.remove("open");
    });
    // Close preset dropdown
    const pw = document.querySelector(".preset-toggle-wrapper.open");
    if (pw && !pw.contains(/** @type {Node} */ (e.target))) pw.classList.remove("open");
  });
})();

/* ── Mobile: auto-collapse sidebar on conversation select ── */
(function patchMobileConvSelect() {
  // After the page loads, intercept conversation clicks
  document.addEventListener("click", function(e) {
    if (!isDrawerViewport()) return;
    const convItem = e.target.closest(".conv-item");
    if (convItem) {
      // Let the real click handler fire first, then close sidebar
      setTimeout(() => {
        const sidebar = document.getElementById("sidebar");
        if (sidebar && !sidebar.classList.contains("collapsed")) {
          toggleSidebar();
        }
      }, 150);
    }
  });
})();

/* ── Mobile: handle virtual keyboard resize via visualViewport API ── */
(function initMobileKeyboardHandler() {
  if (!window.visualViewport) return;
  /* On mobile, when the virtual keyboard opens the visual viewport shrinks.
   * We adjust body height to match, keeping the input area visible. */
  let lastHeight = 0;
  /* Track whether user was near bottom BEFORE keyboard starts closing.
   * We sample this on every resize so we know the state at keyboard-open time. */
  let _wasNearBottom = true;
  function onViewportResize() {
    if (!isMobileViewport()) return;
    const vv = window.visualViewport;
    const newH = vv.height;
    if (Math.abs(newH - lastHeight) < 1) return;
    const growing = newH > lastHeight;
    lastHeight = newH;
    /* Set explicit height on body to match the visual viewport */
    document.body.style.height = newH + 'px';
    if (!growing) {
      /* Keyboard opening (viewport shrinking) — record scroll state and
       * scroll textarea into view */
      _wasNearBottom = isNearBottom(200);
      const ta = document.getElementById('userInput');
      if (ta && document.activeElement === ta) {
        requestAnimationFrame(function() {
          ta.scrollIntoView({ block: 'end', behavior: 'smooth' });
        });
      }
    } else {
      /* ★ Keyboard closing (viewport growing) — the viewport height increases
       * but scrollTop stays the same, so chat appears to "jump to the middle".
       * Re-scroll to bottom if the user was near bottom before, or if there's
       * an active stream (user just sent a message and is watching the reply).
       * Force sync reflow first (void scrollHeight) so the layout reflects
       * the new body height, then scroll immediately + schedule a safety rAF. */
      if (_wasNearBottom || (typeof activeStreams !== 'undefined' && activeStreams.size > 0)) {
        var cc = document.getElementById('chatContainer');
        if (cc) {
          void cc.scrollHeight; // force reflow after body height change
          cc.scrollTop = cc.scrollHeight;
        }
        /* Safety: keyboard dismiss can fire multiple resize events as it
         * animates closed — schedule another scroll after layout settles. */
        requestAnimationFrame(function() {
          scrollToBottom(true);
        });
      }
    }
  }
  window.visualViewport.addEventListener('resize', onViewportResize);
  /* Reset on blur / keyboard dismiss */
  window.visualViewport.addEventListener('scroll', function() {
    if (!isMobileViewport()) return;
    document.body.style.height = window.visualViewport.height + 'px';
  });
})();

// Paperclip (Lucide) SVG used inline in the input-send hint where the
// {clip} token appears. 13px so it sits on the text baseline.
const _CLIP_SVG = '<svg class="input-hint-clip" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m16 6-8.414 8.586a2 2 0 0 0 2.829 2.829l8.414-8.586a4 4 0 1 0-5.657-5.657l-8.379 8.551a6 6 0 1 0 8.485 8.485l8.379-8.551"/></svg>';

// Turn a hint template containing the literal `{clip}` token into safe HTML:
// every non-token segment is HTML-escaped, the token becomes the inline SVG.
function _renderHintHtml(tmpl) {
  return String(tmpl).split('{clip}').map(escapeHtml).join(_CLIP_SVG);
}

function _inputSendHintText() {
  // Returns the footer hint TEMPLATE for the normal (non-image-gen) input mode,
  // honoring config.inputSendMode and the current i18n language. The returned
  // string contains a `{clip}` token; render via _renderHintHtml() into innerHTML.
  const m = (typeof config !== 'undefined' && config && config.inputSendMode) === 'ctrl_enter'
    ? 'ctrl_enter' : 'enter';
  try {
    if (typeof t === 'function') {
      return t(m === 'ctrl_enter' ? 'input.hintCtrlEnter' : 'input.hintEnter');
    }
  } catch (_) { /* fall through */ }
  return m === 'ctrl_enter'
    ? 'Ctrl+Enter send · Enter / Shift+Enter newline · {clip} or drop files'
    : 'Enter send · Ctrl+Enter / Shift+Enter newline · {clip} or drop files';
}

function refreshInputSendHint() {
  // Update the #inputHint footer if we are not in image-gen mode.
  try {
    if (typeof imageGenMode !== 'undefined' && imageGenMode) return;
    const hint = document.getElementById('inputHint');
    if (hint) hint.innerHTML = _renderHintHtml(_inputSendHintText());
  } catch (_) { /* noop */ }
}
if (typeof window !== 'undefined') window.refreshInputSendHint = refreshInputSendHint;

function _insertNewlineAtCursor(ta) {
  const start = ta.selectionStart;
  const end = ta.selectionEnd;
  ta.value = ta.value.substring(0, start) + '\n' + ta.value.substring(end);
  ta.selectionStart = ta.selectionEnd = start + 1;
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

function _getSendMode() {
  // 'enter' (default) — Enter sends, Ctrl+Enter newline
  // 'ctrl_enter'      — Ctrl+Enter sends, Enter newline
  // Shift+Enter always inserts a newline regardless of mode.
  const m = (typeof config !== 'undefined' && config && config.inputSendMode) || 'enter';
  return m === 'ctrl_enter' ? 'ctrl_enter' : 'enter';
}

function _doSendOrGenerate() {
  if (imageGenMode) { generateImageDirect(); return; }
  /* ★ Empty-send while streaming = "take over from here" arm gesture.
   * During streaming the composer button IS the Stop button, so pressing
   * Enter on an empty input is a free, non-conflicting gesture. If autopilot
   * is on we arm the in-flight task so the virtual user takes over when the
   * current reply finishes. A non-empty send still queues a real message
   * (which takes priority over autopilot), so this only fires when empty. */
  const _inp = document.getElementById("userInput");
  const _empty = !(_inp && _inp.value.trim())
    && (typeof pendingImages === 'undefined' || pendingImages.length === 0)
    && (typeof pendingPdfTexts === 'undefined' || pendingPdfTexts.length === 0);
  if (_empty) {
    const _conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
    const _streaming = _conv
      && ((typeof activeStreams !== 'undefined' && activeStreams.has(_conv.id))
          || !!_conv.activeTaskId);
    if (typeof autopilotEnabled !== 'undefined' && autopilotEnabled) {
      /* Empty-send = the explicit "hand it over to the virtual user" gesture.
       * Always ARM (enqueue the persistent, cancellable armed-marker) so the
       * pending sentinel shows in the queue bar and survives reload. */
      if (typeof _maybeArmAutopilot === 'function') _maybeArmAutopilot();
      /* If the conversation has already finished (no live task), also KICK so
       * the VU starts composing the next reply now — there is no end-of-turn
       * hook to fire otherwise. While streaming, the armed in-flight task's
       * hook handles the takeover at its natural stop. */
      if (!_streaming && typeof _kickAutopilot === 'function') _kickAutopilot();
    }
    return;  // never call sendMessage() with empty input
  }
  sendMessage();
}
