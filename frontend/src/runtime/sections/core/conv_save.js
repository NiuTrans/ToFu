/* ===== migrated source: core/conv_save.js ===== */
/* ─────────────────────────────────────────────────────────────────────────
 * core/conv_save.js — local catalog ordering and settings persistence.
 *
 * Local-persistence primitives:
 *
 *   saveConversations(changedConvId): in-memory sort + updatedAt bump,
 *     with the load-bearing flicker guard against active Turns so
 *     multiple simultaneous conversations don't compete for the top
 *     sort position every ~3s. Plus a 2-second-throttled sidebar
 *     refresh so the streaming conv bubbles to the top promptly.
 *
 * Bundle-scope invariants (mirror slices 5 / 6 / 9 / 11):
 *   * `conversations` / `convIsBusy` / `_convSorter` /
 *     `_broadcastToTabs` / `renderConversationList` /
 *     `renderConversationList` resolve from bundle scope at call time.
 * ───────────────────────────────────────────────────────────────────── */

function saveConversations(changedConvId) {
  const now = Date.now();
  if (changedConvId) {
    const c = conversations.find((x) => x.id === changedConvId);
    /* Do not bump updatedAt during periodic live-Turn refreshes.
     * When multiple conversations run simultaneously, each can refresh
     * catalog metadata. Bumping updatedAt each time makes
     * them compete for the top sort position, causing the sidebar to
     * flicker as conversations constantly swap order.
     * Only bump when the conversation is not busy; command acceptance and
     * authoritative settlement own real activity timestamps. */
    if (c && !(typeof convIsBusy === 'function' && convIsBusy(c))) c.updatedAt = now;
  }
  /* DB-first: in-memory array is truth for this tab, DB across tabs/sessions. */
  conversations.sort(_convSorter);
  /* Broadcasts are wake hints; the receiving tab reloads authoritative state. */
  if (typeof _broadcastToTabs === 'function') _broadcastToTabs("conv_saved", { convId: changedConvId });

  /* Throttled sidebar refresh while Turns are live.
   * During active work, saveConversations can be called repeatedly but
   * renderConversationList was NEVER called — so the sidebar sort order
   * and streaming dot were stale until the stream finished or user clicked
   * another conversation.  We now refresh the sidebar on a 2s throttle
   * so users see authoritative busy state promptly. */
  if (changedConvId && conversations.some(c =>
      typeof convIsBusy === 'function' && convIsBusy(c))) {
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
