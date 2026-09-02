/* ===== migrated source: core/frame_identity.js ===== */
/* Owner scoping for server-push frames. Unresolved or unscoped frames fail closed. */
function _frameIsOurs(userId) {
  const localUserId = runtimeScope._currentUserId;
  if (localUserId === undefined || localUserId === null
      || String(localUserId) === '') return false;
  if (userId === undefined || userId === null || String(userId) === '') {
    return false;
  }
  return String(userId) === String(localUserId);
}
runtimeScope._frameIsOurs = _frameIsOurs;
