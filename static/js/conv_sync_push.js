/* ═══════════════════════════════════════════════════════════════════
   conv_sync_push.js — server→client "history was rewritten" alignment

   THE GAP THIS CLOSES
   -------------------
   When the backend reconcile rewrites a conversation's persisted history
   (ghost-tail delete / superseded-error-husk collapse — see
   routes/conversations.py::_persist_reconcile), it now emits

       push_event('conv', <convId>, {kind:'history_rewrite', rev:<new>})

   Before this module, that verdict reached the client ONLY on a manual
   refresh (a fresh GET) — the "I must refresh to sync the correct state"
   pain. This subscriber applies the rewrite in place the instant it lands.

   WHY NOT REUSE _verifyActiveConvFromServer (the 'notify' path)
   ------------------------------------------------------------
   That handler is deliberately KEEP-LONGER: it adopts only when the server
   is longer/has-more (a new turn / trailing grow) and never shrinks local
   (guards the two-writer truncation race). A reconcile rewrite is the exact
   opposite — it SHORTENS (removes a ghost tail / collapses a husk). So a
   keep-longer adopt would IGNORE it. This channel therefore does an
   UNCONDITIONAL adopt of the authoritative server copy, gated only by rev
   monotonicity (never apply an older/equal rev) and the live-task guard
   (never yank a conversation with an in-flight local stream).

   Bundled as a top-level module (registered in lib/js_bundler.py
   _BUNDLE_FILES) and wired from main.js boot AFTER push.js defines
   pushSubscribe — mirrors _wireConvSyncPush in core/cross_tab_sync.js.
   ═══════════════════════════════════════════════════════════════════ */

/* Highest history_rewrite rev already applied per conv — dedupes a burst of
 *   frames (e.g. two tabs' GETs both scheduling a persist) so we refetch at
 *   most once per genuinely-newer rev. Separate from `_serverRev` (the CAS base
 *   for PUTs) because a history_rewrite adopt is unconditional, not keep-longer;
 *   collapsing the two would let a keep-longer no-op suppress a real rewrite. */
const _historyRewriteAppliedRev = new Map();

/* Fetch the authoritative server copy and adopt it UNCONDITIONALLY into the
 *   in-memory conv + the IndexedDB paint cache, re-rendering if it's open.
 *   Unlike the keep-longer notify path, this REPLACES conv.messages wholesale
 *   (a reconcile can only ever remove settled ghost/husk turns the client has
 *   no unsynced edits to). Returns nothing; best-effort, never throws. */
async function _applyHistoryRewrite(convId, frameRev) {
  const conv = conversations.find((c) => c.id === convId);
  /* Unknown conv (not loaded on this device) → nothing to align; the next
   *   open will GET the already-reconciled row. */
  if (!conv) return;

  /* Live-task guard: never replace a conversation that has an in-flight local
   *   stream — its trailing placeholder is the live target, and the backend
   *   reconcile is itself gated off live-task convs, so a history_rewrite for
   *   one should not exist; ignore defensively. */
  if (conv.activeTaskId || (typeof activeStreams !== "undefined" && activeStreams.has(convId))) {
    return;
  }

  /* rev-gate: skip an older/equal rewrite we've already applied. A frame with
   *   rev 0 (server couldn't read it back) still passes once — treat unknown as
   *   "apply once" rather than suppress a real change. */
  if (typeof frameRev === "number" && frameRev > 0) {
    const applied = _historyRewriteAppliedRev.get(convId);
    if (typeof applied === "number" && frameRev <= applied) return;
  }

  let data;
  try {
    data = await Api.conversations.get(convId);
  } catch (e) {
    console.warn("[conv-sync] history_rewrite GET failed for %s: %s", convId.slice(0, 8), e && e.message);
    return;
  }
  if (!data) return;

  /* Re-check the live-task guard AFTER the round-trip: a send that started
   *   while the GET was in flight would be wiped by the wholesale replace
   *   below (pt_3cd6cd48). The rewrite is re-offered on the next frame. */
  if (conv.activeTaskId || (typeof activeStreams !== "undefined" && activeStreams.has(convId))) {
    return;
  }

  const serverMsgs = data.messages || [];
  const localLen = (conv.messages || []).length;

  /* Adopt the authoritative (possibly shorter) message list. */
  conv.messages = serverMsgs;
  conv.title = data.title || conv.title;
  conv.updatedAt = data.updatedAt || data.updated_at || conv.updatedAt;
  conv._serverMsgCount = serverMsgs.length;
  conv._needsLoad = false;
  const keepPinned = conv.pinned, keepPinnedAt = conv.pinnedAt;
  if (typeof _applySettingsToConv === "function") _applySettingsToConv(conv, data.settings);
  conv.pinned = keepPinned; conv.pinnedAt = keepPinnedAt;
  /* Advance the CAS base too so a subsequent keep-longer notify for the same
   *   rev collapses to a no-op. */
  if (typeof data.rev === "number") conv._serverRev = data.rev;

  const appliedRev = (typeof data.rev === "number") ? data.rev
    : (typeof frameRev === "number" ? frameRev : 0);
  if (appliedRev) _historyRewriteAppliedRev.set(convId, appliedRev);

  if (typeof saveConversations === "function") saveConversations(convId);
  try { ConvCache.put(conv); } catch (e) { console.debug("[conv-sync] cache put skipped: %s", e && e.message); }

  if (typeof activeConvId !== "undefined" && activeConvId === convId) {
    window.ConvView.replaceAll(convId, { forceScroll: false });
    if (typeof _restoreConvToolState === "function") _restoreConvToolState(conv);
  } else if (typeof renderConversationList === "function") {
    /* Not open — the shortened count / title may affect the sidebar preview. */
    renderConversationList();
  }
  console.info("[conv-sync] applied history_rewrite conv=%s rev=%s (%d→%d msgs)",
    convId.slice(0, 8), String(appliedRev), localLen, serverMsgs.length);
}

function _onConvSyncPush(frame) {
  try {
    if (!frame || frame.kind !== "history_rewrite") return;
    /* Multi-user gate (forward-safe): DELEGATES to the single implementation
     *   in core/conv_state_reducer.js::_frameIsOurs — never re-implement the
     *   normalization rules here. Fail-OPEN when unavailable (accepting the
     *   frame matches today's pre-identity behaviour; fail-closed would
     *   silently kill history_rewrite alignment), but REPORT the miss so the
     *   degraded state is not indistinguishable from a working gate. */
    if (typeof window._frameIsOurs === "function") {
      if (!window._frameIsOurs(frame.userId)) return;
    } else if (typeof window.reportIdentityGateUnavailable === "function") {
      window.reportIdentityGateUnavailable("_onConvSyncPush");
    }
    const convId = frame.convId || frame.taskId;   // push_event uses taskId as the conv id
    if (!convId) return;
    _applyHistoryRewrite(convId, (typeof frame.rev === "number") ? frame.rev : null);
  } catch (e) {
    console.warn("[conv-sync] _onConvSyncPush error: %s", e && e.message);
  }
}

/* Wired from main.js boot AFTER push.js defines pushSubscribe (this file is a
 *   top-level module bundled before main.js; do NOT subscribe at load time when
 *   pushSubscribe may be undefined). Idempotent. Subscribes the 'conv' channel
 *   with the '*' wildcard so ONE subscription covers every conversation — the
 *   backend delivers a per-conv push_event('conv', convId, ...) to '*' too. */
let _convSyncPushChannelWired = false;
function _wireConvHistoryRewritePush() {
  if (_convSyncPushChannelWired) return;
  if (typeof pushSubscribe !== "function") return;
  _convSyncPushChannelWired = true;
  pushSubscribe("conv", "*", _onConvSyncPush);
  if (typeof debugLog === "function") {
    debugLog("[conv-sync] ✓ history_rewrite push subscription wired", "info");
  }
}
if (typeof window !== "undefined") {
  window._wireConvHistoryRewritePush = _wireConvHistoryRewritePush;
  window._onConvSyncPush = _onConvSyncPush;          // exposed for tests
  window._applyHistoryRewrite = _applyHistoryRewrite; // exposed for tests
}
