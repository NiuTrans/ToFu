/* ===== migrated source: core/folders.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   core/folders.js — extracted from core.js (split 2026-05-28)

   Folder CRUD: loadFolders, createFolder, updateFolder, deleteFolder, setConversationFolder, _migratePinnedToFolder.

   This file is concatenated by Vite's module graph AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* Bounded backoff retry for a failed FIRST folder load. Callers observe the
 * rejection for diagnostics, while this owner keeps the recovery lifecycle so
 * a transient fetch failure cannot hide the folder rail for the whole session.
 * The chain is self-cancelling: each attempt is a no-op once _foldersLoaded is
 * true (a success — from this retry, a create, or a cross-device push refresh —
 * flips it and the next scheduled tick returns early). */
let _folderLoadRetryTimer = 0;
let _folderLoadRetryAttempt = 0;
let _folderLoadFlight = null;
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

function _clearFolderLoadRetry() {
  if (_folderLoadRetryTimer) clearTimeout(_folderLoadRetryTimer);
  _folderLoadRetryTimer = 0;
  _folderLoadRetryAttempt = 0;
}

const _folderPresetMutations = createFolderPresetMutations({
  list: () => _folders,
  replace: rows => { _folders = rows; },
  create: (...args) => Api.folders.create(...args),
  update: (id, fields) => Api.folders.update(id, fields),

  remove: id => Api.folders.remove(id),
});

async function _loadFoldersOnce() {

  try {
    // Stale deliveries trigger a fresh read inside this same single flight.
    // Only a complete, current list may mark the catalog loaded.
    await _folderPresetMutations.loadConfirmed(() => Api.folders.list());
  } catch (e) {
    console.warn('[loadFolders] fetch failed — keeping current folders:', e.message);
    if (!_foldersLoaded) {
      /* First load failed: the sidebar stays fail-open and this existing
       * bounded chain heals it after connectivity returns. */
      _scheduleFolderLoadRetry();
    }
    throw e;
  }
  _foldersLoaded = true;
  _clearFolderLoadRetry();
  /* Trigger sidebar re-render so folder tabs appear immediately.
   *   On init, loadFolders() runs in parallel with loadConversationCatalog().
   *   If conversations arrived first, the sidebar rendered with foldersReady=false
   *   (hiding foldered convs). Now that folders are ready, re-render to show them. */
  if (typeof renderConversationList === 'function') renderConversationList();
  return _folders;
}

function loadFolders() {
  if (_folderLoadFlight) return _folderLoadFlight;
  const flight = _loadFoldersOnce();
  _folderLoadFlight = flight;
  const release = () => {
    if (_folderLoadFlight === flight) _folderLoadFlight = null;
  };
  flight.then(release, release);
  return flight;
}

function invalidateFolderProjection() {
  _folderPresetMutations.invalidate();
}

async function createFolder(name, color, projectPaths = [], readOnlyPaths = []) {
  const folder = await _folderPresetMutations.create(name, color, projectPaths, readOnlyPaths);
  // A create ACK confirms one record, not the complete initial catalog.
  if (!_foldersLoaded) {
    void loadFolders().catch(e => console.warn('[Folders] post-create refresh:', e.message));
  }
  renderConversationList();
  return folder;
}

/* Resolve the default project roots a folder contributes to a brand-new
 * conversation created inside it. A conversation created while a folder tab
 * is active inherits every bound root (the first is the primary project, the
 * rest become extra roots) so the user does not re-enter them per chat; an
 * empty list means the conversation starts unbound. Reads `projectPaths[]`
 * first and falls back to the legacy scalar `projectPath` for folders written
 * before the multi-root model. */
function folderDefaultProjectPaths(folderId) {
  const f = folderId ? getFolderById(folderId) : null;
  if (!f) return [];
  return folderPresetWorkspace(f).projectPaths;
}


function folderDefaultWorkspace(folderId) {
  return folderPresetWorkspace(folderId ? getFolderById(folderId) : null);
}

async function updateFolder(folderId, updates) {
  // The editor paints its local draft immediately. Only confirmed presets may
  // be inherited; failures must reach that editor, not disappear behind a toast.
  const updated = await _folderPresetMutations.update(folderId, updates);
  renderConversationList();
  return updated;
}

async function deleteFolder(folderId) {
  if (!getFolderById(folderId)) return false;
  // Keep the grouping and assignments intact while confirmation is pending.
  // A rejection reaches the dialog with no whole-record rollback race.
  await _folderPresetMutations.remove(folderId);
  for (const c of conversations) {
    if (c.folderId !== folderId) continue;
    c.folderId = null;
    ConvCache.put(c);
    Api.conversations.patchSettings(c.id, { folderId: null })
      .catch(e => console.warn('[deleteFolder] assignment sync failed:', e.message));
  }
  if (_activeFolderId === folderId) _activeFolderId = null;
  reconcileConversationCatalogMetadata(null);
  renderConversationList();
  return true;
}

function setConversationFolder(convId, folderId) {
  const c = getConvById(convId);
  if (!c) return;
  c.folderId = folderId || null;
  reconcileConversationCatalogMetadata(null);
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
  reconcileConversationCatalogMetadata(null);
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
