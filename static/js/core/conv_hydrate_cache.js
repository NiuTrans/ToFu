/* ═══════════════════════════════════════════════════════════════════
   pt_3879f00e sub-part 2 slice 6 — extracted 2026-07-28

   `hydrateSidebarFromCache()` — the cold-boot cache-first sidebar paint.
   Lifted from static/js/core/conversations.js. Byte-identical body.

   The function reads the IndexedDB `ConvCache` (full-list mirror first,
   then opened-conv metas) and seeds `conversations` with lightweight
   shells BEFORE the server round-trip, so first paint shows real
   conversations with zero network dependency — critical on a flaky
   tunnel/mobile network.

   BUNDLE ORDER: this leaf loads BEFORE conversations.js via
   `_BUNDLE_FILES`, so downstream reads of the bare name
   `hydrateSidebarFromCache` inside main.js's bootstrap resolve at
   runtime via the shared bundle scope. The extraction depends on
   helpers that live in OTHER leaves loaded even earlier
   (`_serverConvCount` in conversations.js, `_applySettingsToConv` in
   conv_apply_settings.js, `_startPendingSyncPolling` +
   `_flushPendingSyncs` in pending_sync.js, `_convSorter` in
   conv_reducers.js, `renderConversationList` global) — bare-name reads
   resolved via the shared bundle scope from each of those.

   DEV-FALLBACK: matched `<script>` tag lives in index.html BEFORE
   conversations.js — see index.html and lib/js_bundler.py.
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Paint the sidebar from the IndexedDB cache BEFORE (or without) a server
 * round-trip, so first paint shows real conversations with zero network
 * dependency — critical on a flaky tunnel/mobile network.
 *
 * Honesty note: `ConvCache.put()` only stores conversations that have
 * messages, so this reflects conversations OPENED on this device, NOT the
 * full server list. It is a best-effort head-start; the server list (when it
 * arrives) is the source of truth and reconciles these shells in-place via
 * the id-keyed merge in loadConversationsFromServer(). A cached-only conv the
 * server does not confirm (e.g. deleted elsewhere) is pruned by that merge's
 * existing empty-local-only sweep. The boot path never put()s the meta-only
 * server shells, so the cache never learns about un-opened conversations —
 * acceptable by design.
 *
 * @returns {Promise<number>} count of shells added from cache
 */
async function hydrateSidebarFromCache() {
  try {
    if (typeof ConvCache === 'undefined' || !ConvCache.isAvailable()) return 0;
    /* ★ Prefer the FULL-LIST sidebar mirror (putSidebarList) — it holds EVERY
     *   conversation the server last reported, so the sidebar paints complete
     *   on a cold boot instead of only the handful opened on this device.
     *   Fall back to getAllMeta (opened-conv metas) when the mirror is empty
     *   (first run before any full load, or a v2→v3 upgrade). Both shapes carry
     *   id/title/updatedAt/settings + a message-count key, which is all the
     *   shell builder below needs. The mirror additionally carries `rev`, which
     *   we adopt as the CAS base so the id-keyed merge treats the shell as
     *   known (skew-proof staleness) rather than re-pulling every body. */
    let metas = [];
    let fromFullList = false;
    if (ConvCache.getSidebarList) {
      metas = await ConvCache.getSidebarList();
      fromFullList = !!(metas && metas.length);
    }
    if (!fromFullList) {
      metas = await ConvCache.getAllMeta();
    }
    if (!metas || !metas.length) return 0;
    const known = new Set(conversations.map(c => c.id));
    let added = 0;
    let pendingShells = 0;
    for (const m of metas) {
      if (!m.id || known.has(m.id)) continue;
      const _mCount = _serverConvCount(m);
      const nc = {
        id: m.id,
        title: m.title || 'Untitled',
        messages: [],
        _serverMsgCount: _mCount,
        _needsLoad: _mCount > 0,
        _fromCache: true,
        createdAt: m.createdAt || m.updatedAt || m.cachedAt || Date.now(),
        updatedAt: m.updatedAt || m.cachedAt || Date.now(),
        activeTaskId: null,
      };
      /* Adopt the mirror's CAS base rev when present (full-list rows carry it;
       * opened-conv metas do not) so loadConversationsFromServer's id-keyed
       * merge trusts the monotonic rev signal for this shell instead of a
       * skew-prone wall-clock, and a first PUT sends a matching baseRev. */
      if (typeof m.rev === 'number') nc._serverRev = m.rev;
      _applySettingsToConv(nc, m.settings);
      /* ★ Poor-network durability: restore the conv-level pending-sync marker
       *   from the cached meta so the flush poller can SEE a stranded pending
       *   tail on a shell that hasn't loaded its messages yet. Without this the
       *   message a failed poor-network send rescued into IndexedDB is invisible
       *   to _flushPendingSyncs (which reads conv.messages) until the user
       *   happens to open that exact conversation — silent data loss, one layer
       *   up from the message-level marker. */
      if (m.settings && m.settings._pendingSyncAt) {
        nc._pendingSyncAt = m.settings._pendingSyncAt;
        pendingShells++;
      }
      conversations.push(nc);
      added++;
    }
    if (added) {
      conversations.sort(_convSorter);
      if (typeof renderConversationList === 'function') renderConversationList();
      console.log(`[hydrateSidebarFromCache] painted ${added} cached conversation(s) before server load`);
    }
    /* Kick the retry poller if any hydrated shell carries a stranded pending
     * tail — it will hydrate + re-sync them (see _flushPendingSyncs). */
    if (pendingShells > 0) {
      console.info(`[hydrateSidebarFromCache] ${pendingShells} shell(s) carry a pending-sync tail — starting flush poller`);
      _startPendingSyncPolling();
      _flushPendingSyncs('cache_hydrate');
    }
    return added;
  } catch (e) {
    debugLog(`hydrateSidebarFromCache failed: ${e.message}`, 'warn');
    return 0;
  }
}
