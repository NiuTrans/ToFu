/* ═══════════════════════════════════════════════════════════════════════════
   core/identity_gate_tripwire.js — the watchdog for the multi-user gate

   WHY THIS IS ITS OWN FILE (the whole point — do not fold it back in)
   ------------------------------------------------------------------
   Three consumer gates (cross_tab_sync ×2, conv_sync_push) delegate their
   multi-user identity check to ``window._frameIsOurs``, defined in
   core/conv_state_reducer.js, and DELIBERATELY fail OPEN when it is absent:
   accepting a frame matches the pre-identity default, whereas fail-closed
   would brick cross-device sync — worse than briefly accepting a frame on a
   deployment that usually has one tenant.

   The predicate can only vanish via a BUILD-ORDER regression (the reducer
   moved into ``_DEFERRED_FILES``, or ordered after a consumer). Epic-E
   (pt_3879f00e2d2f4bc4 sub-part 3) proposes deferring cross_tab_sync.js,
   which is exactly that change.

   The tripwire USED to live inside conv_state_reducer.js — which made it
   structurally unable to fire on its own trigger condition. When the reducer
   is missing, the latch, the reporter, the flag reader AND the 60s probe
   timer that would ship it all vanish together (main.js guards the probe on
   ``typeof startSyncDriftProbe === 'function'``, another reducer symbol). So
   the runtime signal covered every cause of the degrade EXCEPT the only cause
   that exists. A guard that cannot fire on its primary trigger is the same
   "indistinguishable from working" failure this whole chain has been
   eliminating.

   Hence: this module owns the latch + the reporter + a SELF-OWNED flush, and
   depends on NOTHING it watches. It touches only ``window``, ``console``,
   ``setTimeout`` and (when present) ``Api`` / ``debugLog``. It must load
   BEFORE core/conv_state_reducer.js — enforced by
   tests/test_frontend_identity_gate_parity.py.

   TWO DELIVERY PATHS, ONE LATCH
   ----------------------------
     * Reducer PRESENT (normal): the 60s sync-drift probe reads
       ``identityGateDegraded()`` and piggybacks ``identityGateDegraded:true``
       onto the digest it already POSTs. Zero extra requests. The probe calls
       ``markIdentityGateReported()`` so this module's own flush stands down.
     * Reducer MISSING (the case that matters): no probe exists to piggyback
       on, so the flush below fires ONE standalone POST to the same endpoint
       with an empty digest list. The server reads the flag BEFORE validating
       ``digests`` precisely so this shape is accepted.

   The flush is a one-shot ``setTimeout``, not an interval: a build-order
   regression is a permanent property of the page, so one report per page load
   is the correct volume. Repeating it would be noise, and noise gets tuned
   out — which is how a signal dies.
   ══════════════════════════════════════════════════════════════════════════ */

/* Latched so a missing predicate — which fires on EVERY inbound frame —
 * warns once instead of flooding the console and burying its own signal. */
let _identityGateWarned = false;
let _identityGateSite = '';
let _identityGateReported = false;
let _identityGateFlushTimer = null;

/* Delay before the standalone flush. Long enough that the normal path (the
 * 60s drift probe, or an earlier-arriving frame) can claim the report first;
 * short enough that a broken page reports within one sitting. */
const _IDENTITY_GATE_FLUSH_MS = 15000;

/* Called by a consumer gate when ``window._frameIsOurs`` is unavailable. The
 * frame has ALREADY been accepted by then — this is pure telemetry and never
 * influences an accept/reject decision. */
function reportIdentityGateUnavailable(site) {
  if (_identityGateWarned) return;
  _identityGateWarned = true;
  _identityGateSite = String(site || 'unknown');
  const msg = '[conv-state] multi-user identity gate UNAVAILABLE at ' +
    _identityGateSite + ' — window._frameIsOurs is undefined, so frames are ' +
    'being accepted UNSCOPED. This is a build-order regression: ' +
    'core/conv_state_reducer.js must load before its consumers in ' +
    'lib/js_bundler.py _BUNDLE_FILES.';
  if (typeof console !== 'undefined' && console.warn) console.warn(msg);
  if (typeof debugLog === 'function') debugLog(msg, 'warn');
  _scheduleIdentityGateFlush();
}

/* Has the gate degraded to accept-all on this page? Read by the sync-drift
 * probe so the degrade rides its existing round-trip. */
function identityGateDegraded() { return _identityGateWarned; }

/* Which gate first saw the predicate missing (diagnostic only). */
function identityGateDegradedSite() { return _identityGateSite; }

/* The drift probe calls this once it has successfully carried the flag, so
 * the standalone flush below stands down and we never double-report. */
function markIdentityGateReported() {
  _identityGateReported = true;
  if (_identityGateFlushTimer !== null && typeof clearTimeout === 'function') {
    clearTimeout(_identityGateFlushTimer);
    _identityGateFlushTimer = null;
  }
}

/* Standalone delivery — the path that exists ONLY for the reducer-missing
 * case. Posts to the same endpoint the drift probe uses, with an empty digest
 * list (the server reads the flag before validating digests for exactly this
 * reason). Never throws; a transport failure leaves the console warning as
 * the last resort. */
async function flushIdentityGateDegraded() {
  if (!_identityGateWarned || _identityGateReported) return false;
  if (typeof Api === 'undefined' || !Api.conversations ||
      typeof Api.conversations.reportSyncDigest !== 'function') return false;
  _identityGateReported = true;   // claim BEFORE awaiting — no double-post
  try {
    await Api.conversations.reportSyncDigest([], {
      identityGateDegraded: true,
      identityGateSite: _identityGateSite,
    });
    return true;
  } catch (e) {
    if (typeof console !== 'undefined' && console.warn) {
      console.warn('[conv-state] identity-gate degrade report failed: %s',
                   e && e.message);
    }
    return false;
  }
}

function _scheduleIdentityGateFlush() {
  if (_identityGateFlushTimer !== null || typeof setTimeout !== 'function') return;
  _identityGateFlushTimer = setTimeout(() => {
    _identityGateFlushTimer = null;
    /* If the drift probe already carried the flag, this is a no-op. */
    try { flushIdentityGateDegraded(); } catch (e) { /* never throw into a timer */ }
  }, _IDENTITY_GATE_FLUSH_MS);
}

/* Test seam only — never called by production code. */
function resetIdentityGateWarnedForTests() {
  _identityGateWarned = false;
  _identityGateSite = '';
  _identityGateReported = false;
  if (_identityGateFlushTimer !== null && typeof clearTimeout === 'function') {
    clearTimeout(_identityGateFlushTimer);
  }
  _identityGateFlushTimer = null;
}

if (typeof window !== 'undefined') {
  window.reportIdentityGateUnavailable = reportIdentityGateUnavailable;
  window.identityGateDegraded = identityGateDegraded;
  window.identityGateDegradedSite = identityGateDegradedSite;
  window.markIdentityGateReported = markIdentityGateReported;
  window.flushIdentityGateDegraded = flushIdentityGateDegraded;
  window.resetIdentityGateWarnedForTests = resetIdentityGateWarnedForTests;
}
