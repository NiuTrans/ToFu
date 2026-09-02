/* ===== migrated source: core/current_user.js ===== */
/* Resolve the explicit storage owner before accepting server-push frames. */

let _currentUserIdResolved = false;
runtimeScope._currentUserId = null;

async function initCurrentUserId() {
  if (_currentUserIdResolved) return runtimeScope._currentUserId;
  try {
    const payload = await Api.users.me();
    const ownerId = Number(payload?.ownerId);
    if (payload?.authenticated !== true
        || !Number.isInteger(ownerId) || ownerId < 1) {
      throw new Error('users.me did not return an authenticated ownerId');
    }
    runtimeScope._currentUserId = ownerId;
    _currentUserIdResolved = true;
    if (typeof debugLog === 'function') {
      debugLog(`[current-user] owner resolved: ${ownerId}`, 'info');
    }
  } catch (error) {
    runtimeScope._currentUserId = null;
    if (typeof debugLog === 'function') {
      debugLog(`[current-user] owner unresolved; push remains blocked: ${error?.message || error}`,
               'warn');
    }
  }
  return runtimeScope._currentUserId;
}

function resetCurrentUserIdForTests() {
  _currentUserIdResolved = false;
  runtimeScope._currentUserId = null;
}

runtimeScope.initCurrentUserId = initCurrentUserId;
runtimeScope.resetCurrentUserIdForTests = resetCurrentUserIdForTests;
