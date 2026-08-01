/* ─────────────────────────────────────────────────────────────────────────
 * core/conv_save.js — extracted 2026-07-31 (pt_3879f00e sub-part 2
 * slice 13) from core/conversations.js.
 *
 * Local-persistence primitives:
 *
 *   saveConversations(changedConvId): in-memory sort + updatedAt bump,
 *     with the LOAD-BEARING flicker guard against active streams so
 *     multiple simultaneous streaming convs don't compete for the top
 *     sort position every ~3s. Plus a 2-second-throttled sidebar
 *     refresh so the streaming conv bubbles to the top promptly.
 *
 *   syncConversationToServerDebounced(conv, delayMs=1500): the
 *     debounced companion to syncConversationToServer, coalescing
 *     rapid settings toggles into one PUT so a slider-drag doesn't
 *     hammer the server.
 *
 * Bundle-scope invariants (mirror slices 5 / 6 / 9 / 11):
 *   * `conversations` / `activeStreams` / `_convSorter` /
 *     `_broadcastToTabs` / `renderConversationList` /
 *     `syncConversationToServer` all resolve from THIS file at CALL
 *     time via bundle-level window scope.
 * ───────────────────────────────────────────────────────────────────── */

function saveConversations(changedConvId) {
  const now = Date.now();
  if (changedConvId) {
    const c = conversations.find((x) => x.id === changedConvId);
    /* ── Don't bump updatedAt during periodic streaming saves ──
     * When multiple conversations stream simultaneously, each calls
     * saveConversations every ~3s.  Bumping updatedAt each time makes
     * them compete for the top sort position, causing the sidebar to
     * flicker as conversations constantly swap order.
     * Fix: only bump updatedAt when the conversation is NOT actively
     * streaming.  The timestamp is already set when the user sends a
     * message (before streaming starts) and again in finishStream()
     * (after activeStreams.delete, so the guard passes). */
    if (c && !activeStreams.has(changedConvId)) c.updatedAt = now;
  }
  /* ★ DB-first: in-memory array is truth for this tab, DB across tabs/sessions. */
  conversations.sort(_convSorter);
  /* cross_tab_sync.js is DEFERRED (Epic-E sub-part 3 slice A) — absent in
   * the pre-load window; typeof-guard so an early save can't ReferenceError.
   * A missed broadcast here only delays another tab's refresh until the
   * module lands (the reconcile poll + push notify cover the gap). */
  if (typeof _broadcastToTabs === 'function') _broadcastToTabs("conv_saved", { convId: changedConvId });

  /* ── Throttled sidebar refresh during streaming ──
   * During active streaming, saveConversations is called every ~3s but
   * renderConversationList was NEVER called — so the sidebar sort order
   * and streaming dot were stale until the stream finished or user clicked
   * another conversation.  We now refresh the sidebar on a 2s throttle
   * so users see the active conversation bubble to the top promptly. */
  if (changedConvId && activeStreams.size > 0) {
    const _now = Date.now();
    const _sc = /** @type {any} */ (saveConversations);
    if (!_sc._lastSidebarRefresh || _now - _sc._lastSidebarRefresh > 2000) {
      _sc._lastSidebarRefresh = _now;
      requestAnimationFrame(() => {
        if (typeof renderConversationList === 'function') renderConversationList();
      });
    }
  }
}

// ── Debounced sync: coalesces rapid settings toggles into one request ──
// finishStream() calls syncConversationToServer() directly (immediate).
// Settings/toggle changes call syncConversationToServerDebounced() which
// waits 1.5s for additional changes before firing.
const _syncDebounceTimers = new Map();  // convId → timeoutId
function syncConversationToServerDebounced(conv, delayMs = 1500) {
  const existing = _syncDebounceTimers.get(conv.id);
  if (existing) clearTimeout(existing);
  _syncDebounceTimers.set(conv.id, setTimeout(() => {
    _syncDebounceTimers.delete(conv.id);
    syncConversationToServer(conv);
  }, delayMs));
}
