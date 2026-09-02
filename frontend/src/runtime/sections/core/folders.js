/* ===== migrated source: core/folders.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   core/folders.js — extracted from core.js (split 2026-05-28)

   Folder CRUD: loadFolders, createFolder, updateFolder, deleteFolder, setConversationFolder, _migratePinnedToFolder.

   This file is concatenated by Vite's module graph AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* Bounded backoff retry for a failed FIRST folder load. loadFolders() has no
 * caller-side retry (it runs once at boot), so without this a transient fetch
 * failure permanently hides the folder rail until the next full page reload.
 * The chain is self-cancelling: each attempt is a no-op once _foldersLoaded is
 * true (a success — from this retry, a create, or a cross-device push refresh —
 * flips it and the next scheduled tick returns early). */
let _folderLoadRetryTimer = 0;
let _folderLoadRetryAttempt = 0;
const _FOLDER_LOAD_RETRY_DELAYS = [1500, 4000, 10000, 30000];
function _scheduleFolderLoadRetry() {
  if (_foldersLoaded) return;                       // already recovered
  if (_folderLoadRetryTimer) return;                // a retry is already pending
  const delay = _FOLDER_LOAD_RETRY_DELAYS[
    Math.min(_folderLoadRetryAttempt, _FOLDER_LOAD_RETRY_DELAYS.length - 1)];
  _folderLoadRetryTimer = setTimeout(() => {
    _folderLoadRetryTimer = 0;
    if (_foldersLoaded) return;
    _folderLoadRetryAttempt++;
    Promise.resolve(loadFolders()).catch(e =>
      console.warn('[loadFolders] retry failed:', e && e.message));
  }, delay);
}

async function loadFolders() {
  /* Api.folders.list() is best-effort ({onError:'null'}): a transient network
   * failure resolves to [] (empty array), NOT the real folder list. Adopting
   * that empty result would blank every folder tab on a flaky connection even
   * though the folders still exist server-side (the "folders missing on
   * desktop" symptom). Distinguish a genuine empty list (200 → []) from a
   * fetch failure by re-requesting with onError:'throw' when the first call
   * came back empty, so a real error is caught instead of masquerading as
   * "no folders". On error we keep whatever folders were already loaded. */
  let list = await Api.folders.list();
  if (Array.isArray(list) && list.length === 0) {
    try {
      const verified = await Api.get('/api/v1/folders');
      if (Array.isArray(verified)) list = verified;
    } catch (e) {
      console.warn('[loadFolders] fetch failed — keeping current folders:', e.message);
      if (_foldersLoaded) return _folders;   // preserve already-loaded tabs
      list = null;                            // first load ever failed → leave unloaded
      /* Self-heal: loadFolders() is called ONCE at boot (inside
       *   initActiveTasks' Promise.all) and the boot-reconnect loop only
       *   re-runs the CONVERSATION load — nothing re-fetches folders. So a
       *   failed first load would hide the folder rail for the whole session
       *   ("sidebar folder gone after refresh, only reappears on create").
       *   Schedule a bounded backoff retry so folders recover on their own
       *   once the transient fetch failure clears. Guarded by _foldersLoaded
       *   so a later success (or a concurrent retry) cancels the chain. */
      _scheduleFolderLoadRetry();
    }
  }
  if (Array.isArray(list)) _folders = list;
  else return _folders;   // fetch failed before first success — don't mark loaded
  _foldersLoaded = true;
  /* Trigger sidebar re-render so folder tabs appear immediately.
   *   On init, loadFolders() runs in parallel with loadConversationCatalog().
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
  /* INSTANT-UI (owner directive 2026-07-31, ): apply the
   *   rename/color change locally on the CLICK — the old code awaited the
   *   PATCH first, so the dialog stayed open and the tab kept its old name
   *   for a whole RTT. The PATCH then runs in the background; on failure the
   *   previous fields are restored and an error toast surfaces. */
  const idx = _folders.findIndex(f => f.id === folderId);
  if (idx < 0) return null;
  const prev = { ..._folders[idx] };
  Object.assign(_folders[idx], updates);
  if (typeof renderConversationList === 'function') renderConversationList();
  Api.folders.update(folderId, updates)
    .then(updated => {
      if (!updated) throw new Error('no response');
      const i = _folders.findIndex(f => f.id === folderId);
      if (i >= 0) Object.assign(_folders[i], updated);
      if (typeof renderConversationList === 'function') renderConversationList();
    })
    .catch(e => {
      console.warn('[updateFolder] PATCH failed — rolling back:', e && e.message);
      const i = _folders.findIndex(f => f.id === folderId);
      if (i >= 0) Object.assign(_folders[i], prev);
      if (typeof renderConversationList === 'function') renderConversationList();
      if (typeof showToast === 'function') showToast(t('folder.renameFailed'), 'error');
    });
  return _folders[idx];
}

async function deleteFolder(folderId) {
  /* INSTANT-UI (owner directive 2026-07-31, ): remove the
   *   folder tab AND unassign its conversations locally on the CLICK — the old
   *   code awaited the DELETE first, so the tab sat there for a whole RTT. The
   *   DELETE then runs in the background; the per-conversation server syncs
   *   only fire on success, and on failure the folder + every assignment is
   *   restored and an error toast surfaces. */
  const idx = _folders.findIndex(f => f.id === folderId);
  if (idx < 0) return false;
  const removed = _folders[idx];
  const unassigned = [];
  _folders = _folders.filter(f => f.id !== folderId);
  // Unassign conversations from the deleted folder — locally, NOW.
  for (const c of conversations) {
    if (c.folderId === folderId) {
      c.folderId = null;
      unassigned.push(c);
      // Also write-through to IDB so a refresh doesn't replay the stale folderId
      ConvCache.put(c);
    }
  }
  if (typeof renderConversationList === 'function') renderConversationList();
  Api.folders.remove(folderId)
    .then(ok => {
      if (!ok) throw new Error('delete rejected');
      for (const c of unassigned) persistConversationSettings(c).catch(() => {});
    })
    .catch(e => {
      console.warn('[deleteFolder] DELETE failed — rolling back:', e && e.message);
      /* Restore the folder at its original index (clamped) and every
       * conversation's assignment, so the sidebar returns to its pre-click
       * shape instead of silently losing the folder the server still has. */
      const at = Math.min(idx, _folders.length);
      _folders = [..._folders.slice(0, at), removed, ..._folders.slice(at)];
      for (const c of unassigned) {
        c.folderId = folderId;
        ConvCache.put(c);
      }
      if (typeof renderConversationList === 'function') renderConversationList();
      if (typeof showToast === 'function') showToast(t('folder.deleteFailed'), 'error');
    });
  return true;
}

function setConversationFolder(convId, folderId) {
  const c = getConvById(convId);
  if (!c) return;
  c.folderId = folderId || null;
  saveConversations(null);  // null = metadata-only, don't bump updatedAt
  renderConversationList();
  /* Folder assignment is settings-plane data. Persist it through the
   * lightweight PATCH endpoint; transcript state is never involved. */
  Api.conversations.patchSettings(convId, { folderId: c.folderId })
    .catch(e => console.warn('[setConversationFolder] PATCH failed:', e.message));
  /* Write through to the metadata cache so a refresh cannot replay a stale
   * folderId while the authoritative catalog request is in flight. */
  ConvCache.put(c);
}

function getFolders() { return _folders; }
function getFolderById(id) { return _folders.find(f => f.id === id); }
function areFoldersLoaded() { return _foldersLoaded; }

/* ── Folder View Mode: when set, sidebar shows only this folder's conversations ── */
let _activeFolderId = null;
/* Tracks the in-flight member fetch per folder so the sidebar can show a
 * loading affordance (C4) and so a genuine empty folder (totalCount 0) is
 * distinguished from "members not loaded yet". */
let _folderMembersLoading = null;   // folderId currently being fetched, or null
const _folderMembersLoaded = new Set();  // folderIds whose members were merged
function getActiveFolderId() { return _activeFolderId; }

/**
 * Fetch a folder's members from the server (resolved by real folderId,
 * independent of the top-N sidebar window) and INCREMENTALLY merge them into
 * the in-memory `conversations` array. Members that sort past the sidebar cap —
 * and were therefore never in the sidebar's top-N list — become visible here.
 *
 * The merge is delegated to mergeConversationCatalogRows so it reuses the id-keyed
 * catalog merge path: an already-present conversation keeps its lifecycle
 * metadata; a new member is added as a visibility-gate-passing shell.
 */
async function loadFolderMembers(id) {
  if (!id) return;
  _folderMembersLoading = id;
  if (typeof renderConversationList === 'function') renderConversationList();
  try {
    const env = typeof Api !== 'undefined' && Api.conversations && Api.conversations.listByFolder
      ? await Api.conversations.listByFolder(id) : null;
    const rows = env && Array.isArray(env.items) ? env.items : [];
    if (typeof mergeConversationCatalogRows === 'function') {
      mergeConversationCatalogRows(rows);
    }
    _folderMembersLoaded.add(id);
  } catch (e) {
    console.warn('[loadFolderMembers] fetch failed for folder=%s: %s', id, e && e.message);
  } finally {
    if (_folderMembersLoading === id) _folderMembersLoading = null;
    if (typeof renderConversationList === 'function') renderConversationList();
  }
}

function setActiveFolderId(id) {
  _activeFolderId = id || null;
  renderConversationList();
  /* Fetch the folder's real members on first entry so a folder whose members
   * all sort past the sidebar window still shows them. Fire-and-forget: the
   * synchronous render above shows whatever is already in memory + a loading
   * affordance; loadFolderMembers re-renders when the merge lands. */
  if (_activeFolderId && !_folderMembersLoaded.has(_activeFolderId)) {
    loadFolderMembers(_activeFolderId);
  }
}

function _convSorter(a, b) {
  /* Date group FIRST so sections never interleave; the helpers live next to
   * _convDateGroupKey in ui/conversation_list.js. */
  const ga = _convGroupRank(a);
  const gb = _convGroupRank(b);
  if (ga !== gb) return ga - gb;
  /* Active (streaming / generating) conversations float to the top OF
   *   THEIR DATE GROUP so they are never pushed out of view when other
   *   conversations update — without breaking group contiguity. */
  const aAct = typeof convIsBusy === 'function' && convIsBusy(a) ? 1 : 0;
  const bAct = typeof convIsBusy === 'function' && convIsBusy(b) ? 1 : 0;
  if (aAct !== bAct) return bAct - aAct;
  return (b.updatedAt || b.createdAt || 0) - (a.updatedAt || a.createdAt || 0);
}

/**
 * Auto-migrate pinned conversations to a "⭐ 置顶" folder.
 * Called once after both loadFolders() and loadConversationCatalog() complete.
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
    /* Pinned/folder migration is settings-plane data. */
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
