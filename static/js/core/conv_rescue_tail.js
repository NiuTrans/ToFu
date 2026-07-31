/* eslint-disable */
/**
 * core/conv_rescue_tail.js — the server-is-not-authoritative verdict.
 *
 * Extracted 2026-07-29 from static/js/core/conversations.js (pt_3879f00e
 * sub-part 2 slice 8). ONE call site remaining, inside
 * loadConversationMessages: when a server reply holds FEWER messages
 * than the client does, this function decides whether that shortfall is
 * a legitimate server delete (empty verdict → normal overwrite) or the
 * signature of a lost-race whole-blob write on the backend (non-empty
 * verdict → keep local, push back).
 *
 * Load order: this leaf sits BEFORE core/conversations.js in
 * ``lib.js_bundler._BUNDLE_FILES`` so the surviving CALL SITE inside
 * ``loadConversationMessages`` resolves via bundle-level window scope
 * at call time (bundle-concat exposes bare-name functions on window).
 *
 * ── _rescuableLocalTail: the server-is-not-authoritative decision ──────────
 *
 * A server reply holding FEWER messages than the client does is NOT proof the
 * client is stale. A backend whole-blob writer that lost a race can erase a row
 * that was already committed (measured: 13 autopilot appends, 8 survivors), and
 * at that moment the local copy is the ONLY place the message still exists —
 * overwriting is what destroys it for good.
 *
 * Extracted as a pure function on purpose: the caller is a ~35k-char async
 * function wired to the DOM, ConvView and ~19 helpers, so the decision could
 * only be guarded by reproducing all of that. A pure seam lets the rule itself
 * be driven directly, which is the difference between a guard that tests the
 * product and one that tests a hand-copied twin of it.
 *
 * Returns the locally-held rows that the server is missing AND that look
 * persisted (they carry an identity: _msgId or _isVirtualUser). A half-built
 * optimistic draft has no id, so it is NOT rescuable and the normal overwrite
 * proceeds — otherwise "keep everything" would strand drafts the server has
 * legitimately never seen.
 */
function _rescuableLocalTail(localMsgs, serverMsgs) {
  if (!Array.isArray(localMsgs) || !Array.isArray(serverMsgs)) return [];
  if (localMsgs.length <= serverMsgs.length) return [];
  return localMsgs.slice(serverMsgs.length)
    .filter(m => m && (m._msgId || m._isVirtualUser));
}
