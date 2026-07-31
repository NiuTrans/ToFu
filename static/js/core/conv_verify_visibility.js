/* eslint-disable */
/**
 * core/conv_verify_visibility.js — the cache-verify visibility pair.
 *
 * Extracted 2026-07-29 from static/js/core/conversations.js (pt_3879f00e
 * sub-part 2 slice 10). Two small, pure predicates that TOGETHER carry
 * the cache-verification visibility contract:
 *
 *   _setCacheVerifying(convId, on)
 *     — Pure DOM decoration. Toggles the `.chat-cache-verifying` class on
 *       #chatInner ONLY when convId is the active conv. Called from the
 *       hydration paths whenever a known-stale IndexedDB copy is painted
 *       optimistically while a background server GET is in flight; the
 *       provisional content is dimmed so the transient pre-correction
 *       render reads as "being checked", not as truth. Cleared the
 *       moment Phase-2 reconciles or the fetch settles. No state
 *       mutation, no persistence.
 *
 *   _openConvMayHoldOrphanGhost(conv, convId)
 *     — Pure boolean predicate. Decides whether a warm-open conv may
 *       still be carrying a client-minted, never-reconciled trailing
 *       empty-assistant placeholder ("ghost" bubble left behind when a
 *       task's stream dropped before its first token). Returns true only
 *       for the SPECIFIC shape (assistant / no content / no thinking /
 *       no toolRounds / no finishReason / no error) AND when no live
 *       stream owns the conv. NEVER truncates: the return value only
 *       tells the caller to re-verify against the server, so the
 *       authoritative reconcile in routes/conversations.py →
 *       lib/conversations/reconcile.py can decide whether the ghost is
 *       real. Avoids the banned frontend-lifecycle-inference pattern
 *       (client truncating history on its own verdict).
 *
 * Load order: this leaf sits BEFORE core/conversations.js in
 * ``lib.js_bundler._BUNDLE_FILES``. Both functions still reach the free
 * names `activeConvId` and `activeStreams` via bundle-level window scope
 * at CALL time (conversations.js exports them as top-level bindings
 * that bundle-concat makes visible on window). The DOM getElementById
 * call is typeof-guarded so the leaf still parses inside a node harness.
 *
 * ── Why this specific extraction ────────────────────────────────────────
 *
 * These are the two smallest pure helpers left in the cache-verify path.
 * Extracting them:
 *
 *   • lets the ghost-detection RULE be tested directly rather than
 *     through the ~750L loadConversationMessages harness (a NEUTER on
 *     the ghost shape can only bite when the predicate is drivable in
 *     isolation);
 *   • separates concerns — the visibility toggle is a DOM decoration,
 *     the ghost predicate is a data-shape verdict; they were adjacent
 *     in conversations.js purely by history, not by cohesion;
 *   • shrinks the boot-critical file by ~40 useful lines;
 *   • keeps the two `_setCacheVerifying` / `_openConvMayHoldOrphanGhost`
 *     symbols on window scope for the ~11 remaining call sites (9 for
 *     the visibility toggle, 2 for the ghost predicate) — bundle-concat
 *     resolves them at call time.
 *
 * The bounded self-heal retry cluster (`_convVerifyRetryDelays`,
 * `_scheduleConvVerifyRetry`, `_CONV_VERIFY_RETRY_DELAYS_DEFAULT`,
 * `_convVerifyRetryTimers`) is DELIBERATELY LEFT IN CONVERSATIONS.JS —
 * it calls `_verifyActiveConvFromServer` which lives in the still-
 * unextracted stream/verify path and has deep test-seam plumbing
 * (`window._CONV_VERIFY_RETRY_DELAYS`) that's exercised by an existing
 * dedicated suite. Moving it now would require reproducing that test
 * seam without touching the seam's callers, which is out of scope for a
 * one-slice extraction.
 */

/**
 * Toggle the "verifying" visual state on the chat viewport. Used when a
 * known-stale IndexedDB copy is painted optimistically while a background
 * server GET is in flight to correct it: the provisional content is dimmed so
 * the transient pre-correction render reads as "being checked", not as truth.
 * Cleared (on=false) the moment Phase-2 reconciles or the fetch settles.
 * Pure DOM decoration — no effect on state / persistence.
 */
function _setCacheVerifying(convId, on) {
  if (convId !== activeConvId) return;
  const inner = (typeof document !== 'undefined')
    ? document.getElementById('chatInner') : null;
  if (!inner) return;
  if (on) inner.classList.add('chat-cache-verifying');
  else inner.classList.remove('chat-cache-verifying');
}

/* An OPEN conversation held in memory may still carry a client-minted,
 * never-reconciled trailing empty-assistant placeholder (an orphaned "ghost"
 * bubble left behind when a task's stream dropped before its first token).
 * The backend GET path reconciles these away (routes/conversations.py →
 * lib/conversations/reconcile.py), but the warm-open early-return below would
 * skip that fetch entirely, so the ghost renders forever until a hard refresh.
 *
 * This predicate ONLY decides whether to re-verify against the server — it
 * NEVER decides the ghost is dead or splices it. When it returns true we fall
 * through to Phase 2 and adopt whatever the server says exists (if the server
 * still lists the placeholder, e.g. a live task it owns, it stays). That keeps
 * the server authoritative and avoids the banned frontend-lifecycle-inference
 * pattern (the client truncating history on its own verdict). A genuinely-live
 * stream is excluded via activeStreams so a legitimate "Preparing…" pre-first-
 * token bubble is never disturbed. */
function _openConvMayHoldOrphanGhost(conv, convId) {
  if (!conv || activeStreams.has(convId)) return false;
  const msgs = conv.messages;
  if (!msgs || msgs.length === 0) return false;
  const tail = msgs[msgs.length - 1];
  return !!tail && tail.role === 'assistant'
    && !(tail.content && tail.content.length)
    && !(tail.thinking && tail.thinking.length)
    && !(tail.toolRounds && tail.toolRounds.length)
    && !tail.finishReason
    && !tail.error;
}
