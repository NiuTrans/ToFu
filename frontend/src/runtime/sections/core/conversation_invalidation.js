/* ===== migrated source: core/conversation_invalidation.js ===== */
/* Cross-tab and cross-device invalidation for Conversation Sync v3.
 *
 * Notify, push, and BroadcastChannel frames are wake hints only. They may
 * dispose a deleted conversation or invalidate a coordinator; transcript
 * state is replaced exclusively by the v3 snapshot/event reducer.
 */

let _syncChannel = null;
try {
  _syncChannel = new BroadcastChannel('tofu_conversation_invalidation');
  if (typeof /** @type {any} */ (_syncChannel).unref === 'function') {
    /** @type {any} */ (_syncChannel).unref();
  }
  _syncChannel.onmessage = (event) => {
    if (event.data?.sourceTab !== TAB_ID) _handleCrossTabMsg(event.data || {});
  };
} catch (_error) { /* BroadcastChannel is an optional acceleration. */ }

function _applyRemoteConvDeleted(conversationId) {
  if (!conversationId) return;
  runtimeScope.ConversationTurnStore?.disposeConversation?.(conversationId);
  try {
    if (typeof ConvCache !== 'undefined') ConvCache.remove?.(conversationId);
  }
  catch (_error) { /* cache is best-effort */ }
  const index = conversations.findIndex((item) => item?.id === conversationId);
  if (index < 0) return;
  conversations.splice(index, 1);
  if (activeConvId === conversationId) {
    if (conversations.length > 0) {
      loadConversation(conversations[Math.min(index, conversations.length - 1)].id);
    } else {
      newChat();
    }
  } else {
    renderConversationList();
  }
}

const _BOOT_LOAD_LEASE_MS = 45000;
function _bootLoadHeld() {
  const acquiredAt = Number(runtimeScope._bootLoadInFlight || 0);
  if (!acquiredAt) return false;
  if (Date.now() - acquiredAt <= _BOOT_LOAD_LEASE_MS) return true;
  runtimeScope._bootLoadInFlight = 0;
  return false;
}
function _acquireBootLoad() {
  if (_bootLoadHeld()) return false;
  runtimeScope._bootLoadInFlight = Date.now();
  return true;
}
function _releaseBootLoad() {
  runtimeScope._bootLoadInFlight = 0;
}
runtimeScope._bootLoadInFlight = 0;
runtimeScope._acquireBootLoad = _acquireBootLoad;
runtimeScope._releaseBootLoad = _releaseBootLoad;
runtimeScope._isBootLoadHeld = _bootLoadHeld;

const _conversationCatalogRevisionGate = createConversationCatalogRevisionGate({
  readRevision(conversationId) {
    const conversation = conversations.find((item) => item?.id === conversationId);
    if (!conversation) return null;
    const stateRevision = runtimeScope.ConversationTurnRead
      ?.state?.(conversation)?.conversationRevision;
    const shellRevision = conversation._serverRev;
    return Math.max(
      Number.isSafeInteger(stateRevision) ? stateRevision : -1,
      Number.isSafeInteger(shellRevision) ? shellRevision : -1,
    );
  },
  refreshCatalog: () => loadConversationCatalog(),
  isVisible: () => document.visibilityState === 'visible',
  warn: (message) => debugLog(`[conversation-catalog] ${message}`, 'warn'),
});
retainedCompositionLifecycle.add(() => _conversationCatalogRevisionGate.destroy());

function _scheduleConvListRefresh(conversationId, revision) {
  _conversationCatalogRevisionGate.schedule(conversationId, revision);
}

function _onConvNotifyPush(frame) {
  if (!frame || (frame.type !== 'conv_changed'
      && frame.type !== 'conv_deleted')) return;
  if (!_frameIsOurs(frame.userId)) return;
  const conversationId = frame.convId;
  if (!conversationId) return;
  if (frame.type === 'conv_deleted') {
    _applyRemoteConvDeleted(conversationId);
    return;
  }
  const conversation = conversations.find((item) => item?.id === conversationId);
  if (conversation) {
    if (Number.isSafeInteger(frame.rev) && frame.rev > 0
        && _conversationCatalogRevisionGate.reached(conversationId, frame.rev)) return;
    runtimeScope.ConversationTurnStore?.invalidateConversation?.(conversationId);
  }
  _scheduleConvListRefresh(conversationId, frame.rev);
}

function _onConversationInvalidation(frame) {
  try {
    const invalidation = decodeConversationInvalidation(frame);
    if (!_frameIsOurs(invalidation.userId)) return;
    runtimeScope.ConversationTurnStore?.invalidateConversation?.(
      invalidation.conversationId,
      invalidation.cursorHint,
    );
    if (!conversations.some((item) =>
      item?.id === invalidation.conversationId)) {
      _scheduleConvListRefresh();
    }
  } catch (error) {
    debugLog(`[conversation-sync] invalid invalidation: ${error?.message || error}`,
             'warn');
  }
}
runtimeScope._onConversationInvalidation = _onConversationInvalidation;

let _foldersRefreshTimer = 0;
function _scheduleFoldersRefresh() {
  clearTimeout(_foldersRefreshTimer);
  _foldersRefreshTimer = setTimeout(() => {
    if (document.visibilityState !== 'visible'
        || typeof loadFolders !== 'function') return;
    void Promise.resolve(loadFolders()).catch((error) =>
      debugLog(`[folders] refresh failed: ${error?.message || error}`, 'warn'));
  }, 150);
}

function _onFoldersChangedPush(frame) {
  if (!frame || frame.type !== 'folders_changed') return;
  if (!_frameIsOurs(frame.userId)) return;
  const deletedFolderId = frame.deletedFolderId;
  if (deletedFolderId) {
    for (const conversation of conversations) {
      if (conversation?.folderId === deletedFolderId) conversation.folderId = null;
    }
    const folders = typeof getFolders === 'function' ? getFolders() : null;
    if (Array.isArray(folders)) {
      const index = folders.findIndex((folder) => folder?.id === deletedFolderId);
      if (index >= 0) folders.splice(index, 1);
    }
    renderConversationList();
  }
  _scheduleFoldersRefresh();
}
runtimeScope._onFoldersChangedPush = _onFoldersChangedPush;

function _broadcastToTabs(type, extra) {
  if (!_syncChannel) return;
  try {
    _syncChannel.postMessage({ type, sourceTab: TAB_ID, ...(extra || {}) });
  } catch (error) {
    debugLog(`[conversation-sync] broadcast failed: ${error?.message || error}`,
             'warn');
  }
}

function _handleCrossTabMsg(message) {
  if (!message) return;
  if (message.type === 'conv_deleted') {
    _applyRemoteConvDeleted(message.convId);
    return;
  }
  if (message.convId) {
    runtimeScope.ConversationTurnStore?.invalidateConversation?.(message.convId);
  }
  if (message.type === 'conv_saved' || message.type === 'conv_restored') {
    _scheduleConvListRefresh(message.convId, null);
  }
}

function _activeConversationWake() {
  if (!activeConvId) return Promise.resolve(null);
  const conversation = conversations.find((item) => item?.id === activeConvId);
  if (!conversation) return Promise.resolve(null);
  return runtimeScope.ConversationTurnStore?.wakeConversation?.(conversation)
    || Promise.resolve(null);
}

function _revalidateOnResume(trigger) {
  if (!_acquireBootLoad()) return false;
  const identityRefresh = typeof initCurrentUserId === 'function'
    ? initCurrentUserId() : Promise.resolve(null);
  void Promise.resolve(identityRefresh)
    .then(() => loadConversationCatalog())
    .then(() => _activeConversationWake())
    .catch((error) =>
      debugLog(`[conversation-sync] ${trigger} recovery failed: ${error?.message || error}`,
               'warn'))
    .finally(_releaseBootLoad);
  return true;
}
runtimeScope._revalidateOnResume = _revalidateOnResume;

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  _revalidateOnResume('visibilitychange');
});

window.addEventListener('online', () => {
  _revalidateOnResume('online');
});

async function _recoverOfflineConversations() {
  const targets = conversations.filter((conversation) =>
    conversation?.id === activeConvId
    || (runtimeScope.ConversationTurnRead?.activeAttemptIds?.(conversation)?.length || 0) > 0
    || ['degraded', 'offline'].includes(
      conversation?._conversationSyncHealth?.state,
    ));
  const result = await runWithConcurrency(
    targets,
    (conversation) =>
      runtimeScope.ConversationTurnStore.wakeConversation(conversation),
    DEFAULT_ASYNC_POOL_CONCURRENCY,
  );
  return result.completed - result.errors.length;
}

const _RECONCILE_MS_PUSH_UP = 300000;
const _RECONCILE_MS_PUSH_DOWN = 25000;
function _reconcileIntervalMs() {
  return typeof pushIsConnected === 'function' && pushIsConnected()
    ? _RECONCILE_MS_PUSH_UP : _RECONCILE_MS_PUSH_DOWN;
}
function _crossDeviceReconcile() {
  if (document.visibilityState !== 'visible') return false;
  return _revalidateOnResume('periodic');
}

let _reconcileTimer = 0;
let _reconcileDueAt = 0;
function _scheduleNextReconcile(shortenOnly = false) {
  const base = _reconcileIntervalMs();
  const delay = Math.round(base * (0.8 + Math.random() * 0.4));
  const dueAt = Date.now() + delay;
  if (_reconcileTimer) {
    if (shortenOnly && _reconcileDueAt <= dueAt) return;
    clearTimeout(_reconcileTimer);
  }
  _reconcileDueAt = dueAt;
  _reconcileTimer = setTimeout(() => {
    _reconcileTimer = 0;
    _reconcileDueAt = 0;
    _crossDeviceReconcile();
    _scheduleNextReconcile();
  }, delay);
}
_scheduleNextReconcile();

let _convSyncPushWired = false;
function _wireConvSyncPush() {
  if (_convSyncPushWired || typeof pushSubscribe !== 'function') return;
  _convSyncPushWired = true;
  pushSubscribe('notify', '*', (frame) => {
    if (frame?.type === 'conversation.invalidated') {
      _onConversationInvalidation(frame);
    } else if (frame?.type === 'conv_changed' || frame?.type === 'conv_deleted') {
      _onConvNotifyPush(frame);
    } else if (frame?.type === 'folders_changed') {
      _onFoldersChangedPush(frame);
    }
  });
  if (typeof pushOnReconnect === 'function') {
    pushOnReconnect(() => _revalidateOnResume('push-reconnect'));
  }
  if (typeof pushOnLatency === 'function') {
    pushOnLatency((reading) => {
      if (reading?.connected === false) _scheduleNextReconcile(true);
    });
  }
}
runtimeScope._wireConvSyncPush = _wireConvSyncPush;
