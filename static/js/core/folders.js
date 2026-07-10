/* ═══════════════════════════════════════════════════════════════════
   core/folders.js — extracted from core.js (split 2026-05-28)

   Folder CRUD: loadFolders, createFolder, updateFolder, deleteFolder, setConversationFolder, _migratePinnedToFolder.

   This file is concatenated by lib/js_bundler.py AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

async function loadFolders() {
  const list = await Api.folders.list();
  if (Array.isArray(list)) _folders = list;
  _foldersLoaded = true;
  /* ★ Trigger sidebar re-render so folder tabs appear immediately.
   *   On init, loadFolders() runs in parallel with loadConversationsFromServer().
   *   If conversations arrived first, the sidebar rendered with foldersReady=false
   *   (hiding foldered convs). Now that folders are ready, re-render to show them. */
  if (typeof renderConversationList === 'function') renderConversationList();
  return _folders;
}

async function createFolder(name, color) {
  const folder = await Api.folders.create(name, color);
  if (folder) _folders.push(folder);
  return folder;
}

async function updateFolder(folderId, updates) {
  const updated = await Api.folders.update(folderId, updates);
  if (updated) {
    const idx = _folders.findIndex(f => f.id === folderId);
    if (idx >= 0) Object.assign(_folders[idx], updated);
  }
  return updated;
}

async function deleteFolder(folderId) {
  const ok = await Api.folders.remove(folderId);
  if (!ok) return false;
  _folders = _folders.filter(f => f.id !== folderId);
  // Unassign conversations from deleted folder
  for (const c of conversations) {
    if (c.folderId === folderId) {
      c.folderId = null;
      syncConversationToServer(c).catch(() => {});
      // Also write-through to IDB so a refresh doesn't replay the stale folderId
      ConvCache.put(c);
    }
  }
  return true;
}

function setConversationFolder(convId, folderId) {
  const c = getConvById(convId);
  if (!c) return;
  c.folderId = folderId || null;
  saveConversations(null);  // null = metadata-only, don't bump updatedAt
  renderConversationList();
  /* ★ FIX: shell convs (0 local messages, _needsLoad=true) are skipped by
   *   syncConversationToServer's 0-message guard, so folderId never persists.
   *   Use the lightweight PATCH /settings endpoint instead — it only updates
   *   the settings JSON column without requiring messages.  This is also more
   *   efficient for folder assignment since we only need to change one field. */
  Api.conversations.patchSettings(convId, { folderId: c.folderId })
    .catch(e => console.warn('[setConversationFolder] PATCH failed:', e.message));
  /* ★ Write-through to the IDB cache: without this, a refresh would replay
   *   the cache's stale folderId in loadConversationMessages Phase-1, and
   *   Phase-2 won't overwrite because PATCH /settings doesn't bump updatedAt
   *   (so `cacheIsStale` evaluates false). No-op for shell convs (cache.put
   *   skips zero-message conversations). */
  ConvCache.put(c);
}

function getFolders() { return _folders; }
function getFolderById(id) { return _folders.find(f => f.id === id); }
function areFoldersLoaded() { return _foldersLoaded; }

/* ── Folder View Mode: when set, sidebar shows only this folder's conversations ── */
let _activeFolderId = null;
function getActiveFolderId() { return _activeFolderId; }
function setActiveFolderId(id) {
  _activeFolderId = id || null;
  renderConversationList();
}

function _convSorter(a, b) {
  /* ★ Active (streaming / generating) conversations float to top
   *   so they are never pushed out of view when other conversations update. */
  const aAct = (activeStreams.has(a.id) || a.activeTaskId) ? 1 : 0;
  const bAct = (activeStreams.has(b.id) || b.activeTaskId) ? 1 : 0;
  if (aAct !== bAct) return bAct - aAct;
  return (b.updatedAt || b.createdAt || 0) - (a.updatedAt || a.createdAt || 0);
}

/**
 * Auto-migrate pinned conversations to a "⭐ 置顶" folder.
 * Called once after both loadFolders() and loadConversationsFromServer() complete.
 * Creates the folder only if pinned convs exist and they aren't already in a folder.
 */
async function _migratePinnedToFolder() {
  const pinnedConvs = conversations.filter(c => c.pinned && !c.folderId);
  if (pinnedConvs.length === 0) return;

  // Check if "⭐ 置顶" folder already exists (from a previous migration)
  let starFolder = _folders.find(f => f.name === '⭐ 置顶');
  if (!starFolder) {
    starFolder = await createFolder('⭐ 置顶', '#f59e0b');
    if (!starFolder) { console.warn('[Folders] Failed to create migration folder'); return; }
  }

  for (const c of pinnedConvs) {
    c.folderId = starFolder.id;
    c.pinned = false;
    c.pinnedAt = 0;
    /* ★ Use PATCH /settings for reliability — syncConversationToServer
     *   skips shell convs with 0 local messages. */
    Api.conversations.patchSettings(c.id, { folderId: c.folderId, pinned: false, pinnedAt: 0 })
      .catch(e => console.warn('[Folders] Migration PATCH failed:', e.message));
    ConvCache.put(c);
  }
  saveConversations(null);
  renderConversationList();
  console.info('[Folders] Migrated %d pinned conversations to "⭐ 置顶" folder', pinnedConvs.length);
}
// Migrate legacy sessionStorage keys (chatui_* → tofu_*) once per page load.
// Keeps users who reload during the rename rollout from losing their active conv.
(function _migrateLegacyStorageKeys() {
  try {
    const _legacyMap = { 'chatui_activeConvId': 'tofu_activeConvId' };
    for (const [legacy, canonical] of Object.entries(_legacyMap)) {
      const v = sessionStorage.getItem(legacy);
      if (v != null && sessionStorage.getItem(canonical) == null) {
        sessionStorage.setItem(canonical, v);
        sessionStorage.removeItem(legacy);
      }
    }
  } catch (_e) { /* sessionStorage may be disabled — no-op */ }
})();

