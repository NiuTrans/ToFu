/* ═══════════════════════════════════════════════════════════════════
   core/translate_guard.js — frontend per-(conv, message) in-flight guard

   Mirrors the BACKEND guard (lib/translate/inflight.py). A translation for one
   message can be initiated from several independent frontend paths that race:

     • a manual "Translate" click (message_actions.translateMessage),
     • the auto-translate pipeline (_runTranslationPipeline, mode='auto'),
     • the page-load resume (_resumePendingTranslations),
     • the server safety-net's push 'done' frame (translation.js subscriber).

   Without a guard, a manual click racing the server safety-net (or two quick
   clicks) can both run a translate task and both render/commit the SAME
   message — the slower clobbering the faster. This is the frontend twin of
   the backend double-fire the in-flight guard already fixed server-side.

   The guard is keyed by the STABLE _msgId whenever the message has one
   (insert/truncation-drift-proof); callers without an id fall back to an
   index-derived key ('#idx:<n>'). A claim self-expires after a TTL so a tab
   that navigated away mid-translate can't wedge the message forever.

   Bundled by lib/js_bundler.py — symbols live on window scope. Loaded BEFORE
   translation.js / message_actions.js (which only CALL these at runtime).
   ═══════════════════════════════════════════════════════════════════ */

/* A claimed entry older than this is treated as stale and may be re-claimed —
 * comfortably longer than the client poll budget (~150s) so a still-legit
 * in-flight translate is never stolen. */
const _TRANSLATE_GUARD_TTL_MS = 180000;  // 3 min

/* key -> claimed-at epoch ms */
const _translateInflight = new Map();

function _translateGuardKey(convId, msgId, idx) {
  if (!convId) return '';
  if (msgId) return convId + '::' + msgId;
  if (idx !== null && idx !== undefined && idx !== -1) return convId + '::#idx:' + idx;
  return '';
}

/**
 * Atomically claim a translate slot for (convId, message).
 * @returns {boolean} true when the caller now OWNS the slot (must eventually
 *   call translateRelease), false when a live claim already exists (the caller
 *   must stand down and NOT start a duplicate translation). A missing key
 *   (no id and no usable index) degrades to always-allow (returns true).
 */
function translateClaim(convId, msgId, idx) {
  const key = _translateGuardKey(convId, msgId, idx);
  if (!key) return true;
  const now = Date.now();
  const prev = _translateInflight.get(key);
  if (prev !== undefined && (now - prev) < _TRANSLATE_GUARD_TTL_MS) {
    console.debug(`[TranslateGuard] ${key} already claimed ${((now - prev) / 1000).toFixed(0)}s ago — standing down`);
    return false;
  }
  _translateInflight.set(key, now);
  return true;
}

/** Release a previously-claimed slot. Idempotent / best-effort. */
function translateRelease(convId, msgId, idx) {
  const key = _translateGuardKey(convId, msgId, idx);
  if (!key) return;
  _translateInflight.delete(key);
}

/** Read-only probe: true iff a live (non-stale) claim exists. */
function translateInflight(convId, msgId, idx) {
  const key = _translateGuardKey(convId, msgId, idx);
  if (!key) return false;
  const prev = _translateInflight.get(key);
  return prev !== undefined && (Date.now() - prev) < _TRANSLATE_GUARD_TTL_MS;
}

if (typeof window !== 'undefined') {
  window.translateClaim = translateClaim;
  window.translateRelease = translateRelease;
  window.translateInflight = translateInflight;
}
