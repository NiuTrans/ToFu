/* ===== migrated source: main/main_init_tasks.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   main init tasks — extracted from main.js (split 2026-05-28)

   initActiveTasks + _ensureNewest (the heavy startup-resume path).

   This file is concatenated by Vite's module graph BEFORE main.js so
   the boot IIFE can reference these symbols. Symbols share `window`
   scope — no imports / exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* Startup loads catalog metadata only. Authoritative Turn snapshots are
 * hydrated on demand by loadConversation(), so a large sidebar never becomes
 * a burst of one snapshot request per inactive conversation. Lifecycle
 * recovery is a backend verdict; this owner never infers transcript state or
 * dispatches a billable assistant Turn. */

// ── Init ──
async function initActiveTasks() {
  const folderLoad = (typeof loadFolders === 'function')
    ? Promise.resolve(loadFolders()).then(() => {
        if (typeof _migratePinnedToFolder === 'function') _migratePinnedToFolder();
      }).catch((error) => {
        console.warn('[initActiveTasks] folder load failed:', error?.message || error);
        if (typeof _scheduleFolderLoadRetry === 'function') _scheduleFolderLoadRetry();
      })
    : Promise.resolve();

  try {
    await loadConversationCatalog();
    const turnStore = runtimeScope.ConversationTurnStore;
    if (!turnStore || typeof turnStore.hydrateConversation !== 'function') {
      throw new Error('ConversationTurnStore failed to initialize');
    }
    /* Keep boot cost independent of catalog size. loadConversation() owns the
     * selected conversation's snapshot and every later switch follows that
     * same single-conversation path. Eagerly hydrating the full catalog made a
     * 500-row sidebar issue hundreds of multi-megabyte sync requests at once,
     * starving unrelated controls (including the Project modal). */
    _ensureNewest();
  } catch (error) {
    console.warn('[initActiveTasks] startup catalog initialization failed:',
                 error?.message || error);
    _ensureNewest();
  } finally {
    await folderLoad;
  }
}
function _ensureNewest() {
  if (activeConvId) {
    const activeConversation = getActiveConv();
    if (activeConversation && typeof convIsBusy === 'function'
        && convIsBusy(activeConversation)) showStreamingUIForConv(activeConvId);
    else {
      const c = activeConversation;
      if (c) runtimeScope.requestAuthoritativeConversationRender(c.id);
    }
    renderPendingQueueUI(activeConvId);
  }
}
