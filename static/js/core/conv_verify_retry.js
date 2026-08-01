/* ─────────────────────────────────────────────────────────────────────────
 * core/conv_verify_retry.js — extracted 2026-07-31 (pt_3879f00e sub-part
 * 2 slice 11) from core/conversations.js.
 *
 * Cache-verify self-heal retry cluster: when a client cache-first paint
 * cannot be verified against the server on the first try (server GET
 * failed / user still tapping), retry with a bounded backoff so a
 * transient blip does not leave the OPEN conv perpetually flagged as
 * "cache known stale". The cap keeps the retry from becoming a
 * background thrash.
 *
 * Bundle-scope invariants (mirror slices 5 / 6 / 9):
 *   * `_verifyActiveConvFromServer` is still declared in conversations.js
 *     and resolved from THIS file at CALL time via bundle-level window
 *     scope — the file order in `_BUNDLE_FILES` puts THIS leaf BEFORE
 *     conversations.js, and the typeof guard makes the reference safe
 *     when hot-reloaded out of order.
 *   * `activeConvId` / `conversations` / `activeStreams` / `_editingMsgIdx`
 *     / `_setCacheVerifying` all resolve identically via bundle-level
 *     window scope.
 *
 * The bounded default backoff (4s / 12s) is exported as a const so the
 * intent is textually reviewable in one place — an accidental unbounded
 * retry is a hot production hazard, so the halt condition
 * (`attempt >= delays.length`) is kept load-bearing and syntactically
 * visible.
 * ───────────────────────────────────────────────────────────────────── */

const _CONV_VERIFY_RETRY_DELAYS_DEFAULT = [4000, 12000];
const _convVerifyRetryTimers = {};

function _convVerifyRetryDelays() {
  /* Test seam: a harness may shorten the backoff via a window override. */
  const d = (typeof window !== 'undefined') ? window._CONV_VERIFY_RETRY_DELAYS : null;
  return (Array.isArray(d) && d.length) ? d : _CONV_VERIFY_RETRY_DELAYS_DEFAULT;
}

function _clearConvVerifyRetryTimer(convId) {
  /* Cancel any pending self-heal retry for this conv — used when the server
   * verify itself lands successfully via the eager path (loadConversation-
   * Messages), so the bounded backoff never re-fires against fresh data. */
  clearTimeout(_convVerifyRetryTimers[convId]);
  delete _convVerifyRetryTimers[convId];
}

function _scheduleConvVerifyRetry(convId) {
  if (convId !== activeConvId) return;   /* only the OPEN conv self-heals in place */
  if (typeof _verifyActiveConvFromServer !== 'function') return;
  const conv = conversations.find((c) => c.id === convId);
  if (!conv) return;
  const delays = _convVerifyRetryDelays();
  const attempt = conv._verifyRetryCount || 0;
  if (attempt >= delays.length) return;
  clearTimeout(_convVerifyRetryTimers[convId]);
  _convVerifyRetryTimers[convId] = setTimeout(() => {
    delete _convVerifyRetryTimers[convId];
    const c = conversations.find((x) => x.id === convId);
    if (!c || convId !== activeConvId) return;
    if (activeStreams.has(convId) || _editingMsgIdx !== null || c.activeTaskId) return;
    c._verifyRetryCount = attempt + 1;
    Promise.resolve(_verifyActiveConvFromServer(convId)).then((adopted) => {
      if (adopted !== null && adopted !== undefined) {
        /* Verify landed (with or without changes) — the paint is server-true. */
        c._verifyRetryCount = 0;
        delete c._cacheKnownStale;
        _setCacheVerifying(convId, false);
      } else {
        _scheduleConvVerifyRetry(convId);
      }
    }).catch(() => _scheduleConvVerifyRetry(convId));
  }, delays[attempt]);
}
