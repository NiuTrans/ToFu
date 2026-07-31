/* eslint-disable */
/**
 * core/conv_disaster_recovery.js — console-invokable rescue trio.
 *
 * Extracted 2026-07-29 from static/js/core/conversations.js (pt_3879f00e
 * sub-part 2 slice 9). Zero cross-file callers: the three functions are
 * documented as "run from browser console" and reach each other only
 * inside this leaf.
 *
 * Load order: this leaf sits AFTER core/conv_apply_settings.js (needs
 * `_applySettingsToConv`) and BEFORE core/conversations.js in
 * ``lib.js_bundler._BUNDLE_FILES``. The three functions still reach
 * ``activeConvId`` / ``conversations`` / ``debugLog`` / ``Api`` /
 * ``saveConversations`` / ``_restoreConvToolState`` via bundle-level
 * window scope at CALL time — invocation happens from the console after
 * the whole bundle has loaded, so every bare-name resolves.
 *
 * ── Why this is its own leaf ────────────────────────────────────────────
 *
 * The trio is the last-resort disaster recovery path. It runs OUTSIDE the
 * hot cache-hydrate / stream / sync loop that conversations.js is built
 * around, and the only entry point is a human typing at devtools. Putting
 * it in a dedicated file lets us:
 *
 *   • measure it doesn't accidentally get called from live code (grep
 *     stays clean — this file is the only place `forceRecoverFromServer`
 *     is referenced outside comments and tests);
 *   • drive the RULES it embodies (adopt-only-if-server-longer,
 *     preserve-pinning-during-adopt, cross-invocation via console) from
 *     tests that instantiate the leaf directly rather than reproducing
 *     the entire loadConversationMessages harness;
 *   • keep the boot-critical conversations.js body focused on things
 *     that fire on every open, not this cold path.
 */

/**
 * Force-recover a conversation from server, ignoring local state.
 * Use when local messages appear truncated or missing.
 * Can be called from browser console: forceRecoverFromServer(convId)
 */
async function forceRecoverFromServer(convId) {
  convId = convId || activeConvId;
  if (!convId) { debugLog('[recover] No conversation ID', 'error'); return null; }
  const conv = conversations.find((c) => c.id === convId);
  if (!conv) { debugLog(`[recover] Conversation not found: ${convId}`, 'error'); return null; }
  try {
    const data = await Api.conversations.get(convId);
    if (!data) { debugLog('[recover] Server returned no data', 'error'); return null; }
    const serverMsgs = data.messages || [];
    const localMsgs = conv.messages || [];
    console.log(`[recover] Conv ${convId}: local has ${localMsgs.length} msgs, server has ${serverMsgs.length} msgs`);
    if (serverMsgs.length > localMsgs.length) {
      conv.messages = serverMsgs;
      conv.title = data.title || conv.title;
      conv.updatedAt = data.updatedAt || data.updated_at || conv.updatedAt;
      conv._serverMsgCount = serverMsgs.length;
      /* ★ Adopt the server rev so a later PUT carries the correct baseRev
       *   (this is a server GET — its rev is authoritative). */
      if (typeof data.rev === 'number') conv._serverRev = data.rev;
      conv._needsLoad = false;
      const keepPinned = conv.pinned, keepPinnedAt = conv.pinnedAt;
      _applySettingsToConv(conv, data.settings);
      conv.pinned = keepPinned; conv.pinnedAt = keepPinnedAt;
      saveConversations(convId);
      if (convId === activeConvId) {
        window.ConvView.replaceAll(conv.id, { forceScroll: false });
        if (typeof _restoreConvToolState === 'function') _restoreConvToolState(conv);
      }
      console.log(`[recover] ✅ Restored ${serverMsgs.length} messages (was ${localMsgs.length})`);
      return conv;
    } else {
      console.log(`[recover] ℹ️ Server has same or fewer messages — no recovery needed`);
      return conv;
    }
  } catch (e) {
    debugLog(`[recover] Failed: ${e.message}`, 'error');
    return null;
  }
}

/**
 * Audit all conversations for data loss: compare local message count vs server.
 * Run from console: auditConversations()
 */
async function auditConversations() {
  console.log('[audit] Checking all conversations for data loss...');
  const issues = [];
  for (const conv of conversations) {
    try {
      const data = await Api.conversations.get(conv.id);
      if (!data) continue;
      const serverCount = (data.messages || []).length;
      const localCount = (conv.messages || []).length;
      if (serverCount > localCount) {
        issues.push({ id: conv.id, title: conv.title, localCount, serverCount, diff: serverCount - localCount });
        console.warn(`[audit] ⚠️ "${conv.title}" — local: ${localCount}, server: ${serverCount} (+${serverCount - localCount} recoverable)`);
      }
    } catch (e) { /* skip */ }
  }
  if (issues.length === 0) {
    console.log('[audit] ✅ No data loss detected — all conversations match server');
  } else {
    console.log(`[audit] Found ${issues.length} conversation(s) with recoverable data:`);
    console.table(issues);
    console.log('[audit] Run forceRecoverFromServer("conv_id") to recover, or recoverAll() to fix all');
  }
  return issues;
}

/**
 * Batch recover all conversations that have more messages on server than locally.
 * Run from console: recoverAll()
 */
async function recoverAll() {
  const issues = await auditConversations();
  if (issues.length === 0) return;
  let recovered = 0;
  for (const issue of issues) {
    const result = await forceRecoverFromServer(issue.id);
    if (result) recovered++;
    await new Promise(r => setTimeout(r, 200)); /* small delay to not hammer server */
  }
  console.log(`[recoverAll] ✅ Recovered ${recovered}/${issues.length} conversations`);
}
