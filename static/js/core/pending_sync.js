/* ═══════════════════════════════════════════════════════════════════
   core/pending_sync.js — pending-sync retry cluster.

   Extracted from core/conversations.js (pt_3879f00e sub-part 2, slice 2):
   the 5-function pending-sync durability layer + its two state
   variables. Zero IIFE-load-time side effects — the poller is only
   started when markConvPendingSync fires, and only ever from a
   send-failure code path. Every external dependency (ConvCache,
   Api.health, activeStreams, conversations, renderConversationList,
   loadConversationMessages, syncConversationToServer) is read at
   CALL time via the shared bundle-level `window` scope.

   Symbols:
     markConvPendingSync(conv)              — mark trailing turn + start poller
     _clearPendingSyncMarkers(conv)         — clear all markers after confirmed PUT
     convHasPendingSync(conv)               — durable marker present?
     _startPendingSyncPolling()             — start the retry interval
     _flushPendingSyncs(trigger)            — attempt to sync every marked conv

   This file is concatenated into the core bundle BEFORE
   core/conversations.js (guarded by _BUNDLE_FILES ordering +
   tests/test_frontend_pending_sync_extracted.py).
   ═══════════════════════════════════════════════════════════════════ */

/* ═══════════════════════════════════════════════════════════════════
   Pending-sync durability (poor-network send failure)
   ───────────────────────────────────────────────────────────────────
   When a send POST fails on a poor network, sendMessage() marks the
   optimistic user message (+ the error bubble) with `_pendingSync` and
   calls markConvPendingSync(conv). That marker is a real message field,
   so ConvCache.put() persists it and it SURVIVES a page reload. A single
   best-effort rescue PUT is not enough — the network that failed the send
   will often fail that PUT too. This poller re-attempts the sync until it
   lands (or the message is gone), and the `online` / `visibilitychange`
   handlers kick it immediately on connectivity change.
   ═══════════════════════════════════════════════════════════════════ */

/** Mark a conversation's trailing turn as needing a server sync + persist to
 *  IndexedDB so it survives a reload, then start the retry poller. */
function markConvPendingSync(conv) {
  if (!conv || !conv.messages || conv.messages.length === 0) return;
  conv._pendingSyncAt = Date.now();
  /* Stamp the trailing messages (the just-failed turn) so the marker rides
   * the message row into IndexedDB — a conv-level flag would NOT survive
   * (the cache only persists whitelisted settings). Mark the tail user msg
   * and any trailing error assistant. */
  for (let i = conv.messages.length - 1; i >= 0 && i >= conv.messages.length - 2; i--) {
    const m = conv.messages[i];
    if (m) m._pendingSync = true;
  }
  try { ConvCache.put(conv); } catch (e) { console.debug(`[pendingSync] ConvCache.put failed: ${e && e.message}`); }
  _startPendingSyncPolling();
}
if (typeof window !== 'undefined') window.markConvPendingSync = markConvPendingSync;

/** Clear all pending-sync markers on a conv (called after a confirmed PUT). */
function _clearPendingSyncMarkers(conv) {
  if (!conv) return;
  let touched = false;
  if (conv._pendingSyncAt) { delete conv._pendingSyncAt; touched = true; }
  if (conv.messages) {
    for (const m of conv.messages) {
      if (m && m._pendingSync) { delete m._pendingSync; touched = true; }
    }
  }
  if (touched) {
    try { ConvCache.put(conv); } catch (e) { console.debug(`[pendingSync] ConvCache.put(clear) failed: ${e && e.message}`); }
  }
}
if (typeof window !== 'undefined') window._clearPendingSyncMarkers = _clearPendingSyncMarkers;

/** True if the conv still carries a durable pending-sync marker. */
function convHasPendingSync(conv) {
  if (!conv) return false;
  if (conv._pendingSyncAt) return true;
  return !!(conv.messages && conv.messages.some((m) => m && m._pendingSync));
}
if (typeof window !== 'undefined') window.convHasPendingSync = convHasPendingSync;

let _pendingSyncInterval = null;
const _PENDING_SYNC_POLL_MS = 12000; // retry cadence (background)
/** Start the retry poller if not already running. Stops itself when no conv
 *  has a pending-sync marker left. */
function _startPendingSyncPolling() {
  if (_pendingSyncInterval) return;
  _pendingSyncInterval = setInterval(() => { _flushPendingSyncs('poll'); }, _PENDING_SYNC_POLL_MS);
}
if (typeof window !== 'undefined') window._startPendingSyncPolling = _startPendingSyncPolling;

/** Attempt to sync every conv that carries a pending-sync marker. Runs on the
 *  poller, on the `online` event, and on `visibilitychange`. */
async function _flushPendingSyncs(trigger) {
  const pending = conversations.filter(convHasPendingSync);
  if (pending.length === 0) {
    if (_pendingSyncInterval) { clearInterval(_pendingSyncInterval); _pendingSyncInterval = null; }
    return 0;
  }
  /* Don't hammer a dead tunnel — only attempt when the server is reachable. */
  try {
    const h = await Api.health.check({ signal: AbortSignal.timeout(5000) });
    if (!h || !h.ok) return 0;
  } catch { return 0; }
  let synced = 0;
  for (const conv of pending) {
    /* Skip while a live stream owns the conv — finishStream will sync it. */
    if (activeStreams.has(conv.id)) continue;
    /* ★ Hydrate a shell before syncing. A conv reloaded as a metadata-only
     *   shell (messages:[] + _needsLoad, its pending tail known only from the
     *   conv-level _pendingSyncAt marker) cannot be synced directly:
     *   syncConversationToServer early-returns on 0 messages. Load its messages
     *   first — the cache path restores the durable _pendingSync tail from the
     *   IndexedDB messages store — then sync exactly as the loaded path does.
     *   If hydration can't materialise the tail (server unreachable AND cache
     *   miss), leave the marker so a later poll retries; never sync an empty
     *   shell (that would risk clobbering the server). */
    let target = conv;
    if ((!conv.messages || conv.messages.length === 0) && conv._needsLoad) {
      try {
        await loadConversationMessages(conv.id);
      } catch (e) {
        console.debug(`[pendingSync] hydrate failed for ${conv.id.slice(0,8)}: ${e && e.message}`);
      }
      target = conversations.find((c) => c.id === conv.id);
      /* Conv gone (deleted mid-flush), still empty (hydration failed), or the
       * marker cleared during hydration → skip; a later poll retries if needed. */
      if (!target || !target.messages || target.messages.length === 0) continue;
      if (!convHasPendingSync(target)) continue;
    }
    const ok = await syncConversationToServer(target);
    if (ok) synced++;
  }
  if (synced > 0) {
    console.info(`[pendingSync] ✅ Re-synced ${synced} pending conversation(s) — trigger=${trigger}`);
    if (typeof renderConversationList === 'function') renderConversationList();
  }
  /* Stop the poller once everything landed. */
  if (!conversations.some(convHasPendingSync) && _pendingSyncInterval) {
    clearInterval(_pendingSyncInterval); _pendingSyncInterval = null;
  }
  return synced;
}
if (typeof window !== 'undefined') window._flushPendingSyncs = _flushPendingSyncs;
