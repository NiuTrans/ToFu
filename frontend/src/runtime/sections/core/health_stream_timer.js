/* ===== migrated source: core/health_stream_timer.js ===== */
/* Connection-health ports and a compatibility repaint request.
 * Transport liveness is owned by ConversationSyncCoordinator; live phase and
 * content are rendered from TurnState by ConversationSurface. */

function streamHealthSubscribe(listener) {
  if (typeof listener !== 'function') return () => {};
  return conversationConnectionHealth.subscribeAggregate(listener);
}

function streamHealthGet() {
  return conversationConnectionHealth.aggregate();
}

if (typeof window !== 'undefined') {
  runtimeScope.streamHealthSubscribe = streamHealthSubscribe;
  runtimeScope.streamHealthGet = streamHealthGet;
  if (typeof runtimeScope.initNetLatency === 'function') {
    queueMicrotask(runtimeScope.initNetLatency);
  }
}

async function _forceFinishDeadStream(convId) {
  const conv = conversations.find((item) => item?.id === convId);
  if (!conv) return;
  const attemptIds = runtimeScope.ConversationTurnRead?.activeAttemptIds?.(conv) || [];
  await Promise.allSettled(attemptIds.map((attemptId) =>
    runtimeScope.ConversationTurnStore.abortAttempt(attemptId)));
  await runtimeScope.ConversationTurnStore.hydrateConversation(conv);
}

function _probeAllStuckStreamsOnWake() {
  for (const conv of conversations) {
    if (!(runtimeScope.ConversationTurnRead?.activeAttemptIds?.(conv)?.length)) continue;
    runtimeScope.ConversationTurnStore.hydrateConversation(conv).catch((error) =>
      console.warn('[ConversationSync] wake hydration failed:', error));
  }
}

if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('pageshow', _probeAllStuckStreamsOnWake);
  window.addEventListener('online', _probeAllStuckStreamsOnWake);
}

function twUpdate(convId) {
  runtimeScope.requestAuthoritativeConversationRender?.(
    convId, { force: false, forceScroll: false },
  );
}
