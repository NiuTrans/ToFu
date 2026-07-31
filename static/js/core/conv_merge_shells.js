/* ═══════════════════════════════════════════════════════════════════
   pt_3879f00e sub-part 2 slice 7 — extracted 2026-07-28

   `_serverConvCount(sc)` + `mergeServerConvShells(serverConvs)` — the
   id-keyed sidebar shell-merge pair. Lifted from
   static/js/core/conversations.js (byte-identical bodies) and kept
   CONTIGUOUS in that order:

     * `test_frontend_folder_members_load.py` surgically extracts the
       region from the start of `_serverConvCount` to the end of
       `mergeServerConvShells` — the pair MUST remain contiguous in
       source order for that harness to keep working.

   BUNDLE ORDER: this leaf loads BEFORE conversations.js via
   `_BUNDLE_FILES`, so the two remaining call sites of
   `_serverConvCount` inside `loadConversationsFromServer` (and the
   THREE cross-file call sites of `mergeServerConvShells` —
   `folders.js` `loadFolderMembers` + `ui/conversation_list.js`
   infinite-scroll + `loadConversationsFromServer` itself) resolve
   the bare names at runtime via the shared bundle scope. The
   pair depends on `conversations` (module-level array in
   conversations.js) and `_applySettingsToConv` (conv_apply_settings.js)
   — both resolved AT CALL TIME via bundle scope, so leaf-before-
   conversations is safe.

   DEV-FALLBACK: matched `<script>` tag lives in index.html BEFORE
   conversations.js — see index.html and lib/js_bundler.py.
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Extract a conversation's message count from a server list row, tolerating
 * every key variant the backend may emit. The sidebar ?meta=1 path
 * (lib/conversations/meta_cache.py) sends `messageCount`; the default list
 * shape (routes/conversations.py::_conv_row_to_meta_dict) and the IDB cache
 * send `msgCount` / `msg_count`. This count feeds the SHELL VISIBILITY gate
 * (_needsLoad / _serverMsgCount → renderConversationList's filter), so reading
 * only one key would silently drop a real conversation from the sidebar when a
 * differently-shaped payload served it. Returns 0 when none are present.
 */
function _serverConvCount(sc) {
  if (!sc) return 0;
  const v = sc.messageCount != null ? sc.messageCount
    : (sc.msgCount != null ? sc.msgCount : sc.msg_count);
  return v || 0;
}


/**
 * Incrementally merge server metadata rows (from a folder-scoped or paginated
 * list query) into the in-memory `conversations` array, keyed by id.
 *
 * DISCIPLINE (the "never overwrite" invariant): a row that is ALREADY in
 * memory — because it's in the top-N sidebar window, is streaming, or has its
 * messages loaded — must NOT have its heavy/live fields clobbered by a
 * metadata-only row. We only FILL fields the local copy is missing:
 *   • messages / _serverRev / activeTaskId / _needsLoad → left untouched.
 *   • title / updatedAt / folderId → refreshed (cheap metadata, safe).
 *
 * A row NOT yet in memory is added as a SHELL with the same visibility-gate
 * fields the boot loader builds (_serverMsgCount from the count, _needsLoad
 * when the count is >0), so it passes renderConversationList's
 * `messages.length>0 || _serverMsgCount>0 || _needsLoad` filter instead of
 * silently hiding despite having been resolved by the folder query.
 *
 * Returns the number of NEW shells added (0 = every row was already known).
 */
function mergeServerConvShells(serverConvs) {
  if (!Array.isArray(serverConvs) || serverConvs.length === 0) return 0;
  const localMap = new Map(conversations.map((c) => [c.id, c]));
  let added = 0;
  for (const sc of serverConvs) {
    if (!sc || !sc.id) continue;
    const local = localMap.get(sc.id);
    const _scCount = _serverConvCount(sc);
    if (!local) {
      const nc = {
        id: sc.id,
        title: sc.title,
        messages: [],
        _serverMsgCount: _scCount,
        _needsLoad: _scCount > 0,
        createdAt: sc.createdAt,
        updatedAt: sc.updatedAt || sc.createdAt,
        activeTaskId: null,
      };
      _applySettingsToConv(nc, sc.settings);   // adopts folderId / pinned / etc.
      conversations.push(nc);
      added++;
    } else {
      /* Existing conv — refresh ONLY cheap metadata; never touch live/heavy
       * fields (messages, _serverRev, activeTaskId, _needsLoad). */
      if (sc.title) local.title = sc.title;
      const sT = sc.updatedAt || sc.createdAt || 0;
      if (sT && sT >= (local.updatedAt || 0)) local.updatedAt = sT;
      /* Keep _serverMsgCount at least as large as the server row reports so the
       * visibility gate stays satisfied, but never shrink it (a lagging list
       * snapshot mustn't rewind a count a fresher GET advanced). */
      if (_scCount > (local._serverMsgCount || 0)) local._serverMsgCount = _scCount;
      /* Adopt folderId from settings if the local copy doesn't have one yet
       * (mirrors setActiveFolderId's need to see members in the folder view). */
      if (sc.settings && sc.settings.folderId !== undefined && !local.folderId) {
        local.folderId = sc.settings.folderId;
      }
    }
  }
  return added;
}
