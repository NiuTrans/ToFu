/* ===== migrated source: core/translate_guard.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   core/translate_guard.js — frontend per-Turn translation claim

   Mirrors the BACKEND guard (lib/translate/inflight.py). A translation for one
   message can be initiated from several independent frontend paths that race:

     • a manual Turn translation intent (_runManualTurnTranslation),
     • the auto-translate pipeline (_runTranslationPipeline, mode='auto'),
     • the page-load resume (_resumePendingTranslations),
     • the server safety-net's push 'done' frame (translation.js subscriber).

   Without a guard, a manual click racing the server safety-net (or two quick
   clicks) can both run a translate task and both render/commit the SAME
   message — the slower clobbering the faster. This is the frontend twin of
   the backend double-fire the in-flight guard already fixed server-side.

   The guard is keyed only by the authoritative (conversationId, turnId)
   identity. A claim self-expires after a TTL so a tab that navigated away
   mid-translate cannot wedge the Turn forever.

   Bundled by Vite's module graph and registered before translation.js, whose
   manual and automatic paths both claim this owner at runtime.
   ═══════════════════════════════════════════════════════════════════ */

/* A claimed entry older than this is treated as stale and may be re-claimed —
 * comfortably longer than the client poll budget (~150s) so a still-legit
 * in-flight translate is never stolen. */
const _TRANSLATE_GUARD_TTL_MS = 180000;  // 3 min

/* key -> claimed-at epoch ms */
const _translateInflight = new Map();

function _translateGuardKey(convId, turnId) {
  return convId && turnId ? convId + '::' + turnId : '';
}

/**
 * Atomically claim a translate slot for (conversationId, turnId).
 * @returns {boolean} true when the caller now OWNS the slot (must eventually
 *   call translateRelease), false when a live claim already exists (the caller
 *   must stand down and NOT start a duplicate translation). A missing key
 *   degrades to always-allow only for a missing identity.
 */
function translateClaim(convId, turnId) {
  const key = _translateGuardKey(convId, turnId);
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
function translateRelease(convId, turnId) {
  const key = _translateGuardKey(convId, turnId);
  if (!key) return;
  _translateInflight.delete(key);
}

/** Read-only probe: true iff a live (non-stale) claim exists. */
function translateInflight(convId, turnId) {
  const key = _translateGuardKey(convId, turnId);
  if (!key) return false;
  const prev = _translateInflight.get(key);
  return prev !== undefined && (Date.now() - prev) < _TRANSLATE_GUARD_TTL_MS;
}

if (typeof window !== 'undefined') {
  runtimeScope.translateClaim = translateClaim;
  runtimeScope.translateRelease = translateRelease;
  runtimeScope.translateInflight = translateInflight;
}
