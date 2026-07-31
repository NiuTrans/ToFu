/* ═══════════════════════════════════════════════════════════════════════════
   core/current_user.js — resolve THIS tab's tenant identity at boot
   ═══════════════════════════════════════════════════════════════════════════

   pt_679d064f68ac4dd6 (owner picked option B — fetch users/me on boot).

   WHY THIS EXISTS
   ---------------
   Four client-side multi-user gates read `window._currentUserId`:

     * core/conv_state_reducer.js::_frameIsOurs
     * core/cross_tab_sync.js::_onConvNotifyPush
     * core/cross_tab_sync.js::_onFoldersChangedPush
     * conv_sync_push.js::_onConvSyncPush

   Before this module NOTHING ever wrote that global, so every gate was
   structurally inert (`myUser === null` → accept-all). Correct for a
   personal install, but it means a multi-tenant deployment has no identity
   to compare a frame against. c6d1bd71 landed the SERVER half (the WS
   handshake resolves AuthContext.user_id and every notify frame is stamped);
   this module is the client half that makes the gates live.

   THE CONTRACT
   ------------
   `GET /api/v1/users/me` is PUBLIC (routes/api_v1/users.py:266) and already
   returns the three shapes we care about:

     multi-tenant login  → {authenticated:true,  user:{id, email, …}}
     personal install    → {authenticated:true,  user:null, principal:{…}}
     unauthenticated     → {authenticated:false, user:null}

   Only the first yields an identity. The other two resolve to `''` — every
   gate treats empty as "no identity established" and accepts all frames,
   which is the pre-commit behaviour preserved EXACTLY. A personal install
   therefore sees zero change.

   FAIL-OPEN, NOT FAIL-CLOSED
   --------------------------
   A boot-time network hiccup resolves to `''` (accept-all) rather than
   leaving the global undefined or throwing. Rejecting every frame because
   /users/me was briefly unreachable would brick the sidebar — strictly
   worse than briefly accepting a frame in a deployment that has no second
   tenant anyway.

   TYPE NOTE (do not "simplify" this)
   ----------------------------------
   The id is written VERBATIM — a numeric tenant id stays a number. The
   server's `_request_user_id()` / `task_user_id()` int-coerce numeric ids
   (`int(uid) if str(uid).isdigit() else uid`), so a frame can legitimately
   carry `userId: 7` while a different code path carries `'7'`. Every gate
   String()-normalizes BOTH sides for exactly that reason. Coercing here
   too would be a second, redundant normalization that could drift from the
   gates' rule.

   ORDERING
   --------
   main.js boot awaits this BEFORE wiring the push subscribers, so the very
   first frame is already evaluated against a resolved identity.
   ══════════════════════════════════════════════════════════════════════════ */

/* Resolution latch — the fetch runs at most once per page. A second call is
 * a no-op so a re-entrant boot path can never blank an already-resolved id. */
let _currentUserIdResolved = false;

/* Resolve this tab's tenant identity and publish it as
 * `window._currentUserId`. Always resolves (never rejects). Idempotent. */
async function initCurrentUserId() {
  if (_currentUserIdResolved) return window._currentUserId;
  /* Default only when nothing is resolved yet. A RE-ENTRANT call after a
   * transient failure must NOT blank a value another attempt may have
   * landed — blanking re-opens the accept-all window this module closes. */
  if (window._currentUserId === undefined) window._currentUserId = '';
  try {
    const data = await Api.users.me();
    /* `user` is non-null ONLY for a login-bound multi-tenant session. */
    const id = (data && data.user && data.user.id !== undefined
                && data.user.id !== null) ? data.user.id : '';
    window._currentUserId = id;
    /* Latch AFTER a successful probe only. Setting it before the await
     * would pin a transient network failure's '' forever: the tab would
     * never re-acquire its identity, and every gate would stay
     * accept-all for the rest of the page's life. Leaving it false on
     * the catch path lets a later trigger (push reconnect, visibility
     * resume, a manual retry) resolve the real id. */
    _currentUserIdResolved = true;
    if (typeof debugLog === 'function') {
      debugLog(id === ''
        ? '[current-user] no tenant identity (personal install) — sync gates stay open'
        : `[current-user] identity resolved: ${String(id)}`, 'info');
    }
  } catch (e) {
    /* Fail-open but RETRYABLE: '' keeps every gate accept-all (the
     * personal-install default) while leaving the latch open so the next
     * call can still resolve a real identity. */
    if (window._currentUserId === undefined) window._currentUserId = '';
    if (typeof debugLog === 'function') {
      debugLog(`[current-user] identity probe failed (staying unscoped, will retry): ${e && e.message}`,
               'warn');
    }
  }
  return window._currentUserId;
}

/* Test seam — lets a harness exercise the several response shapes without
 * reloading the module. NOT used by production code. */
function resetCurrentUserIdForTests() {
  _currentUserIdResolved = false;
}

if (typeof window !== 'undefined') {
  window.initCurrentUserId = initCurrentUserId;
  window.resetCurrentUserIdForTests = resetCurrentUserIdForTests;
}
