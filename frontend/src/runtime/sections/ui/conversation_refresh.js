/* ===== migrated source: ui/conversation_refresh.js ===== */
/**
 * Refresh one conversation from the v3 turn/attempt authority.
 *
 * Executor task ids and task SSE are intentionally absent from this browser
 * boundary. Hydration reconnects live attempts using their durable cursors.
 */
async function refreshConversationRuntime(convId) {
  const conv = conversations.find((item) => item.id === convId);
  if (!conv) return null;
  const turnStore = runtimeScope.ConversationTurnStore;
  if (!turnStore || typeof turnStore.hydrateConversation !== 'function') {
    throw new Error('ConversationTurnStore failed to initialize');
  }
  try {
    return await turnStore.hydrateConversation(conv);
  } catch (error) {
    console.warn('[ConversationSync] refresh failed:', error?.message || error);
    if (typeof showToast === 'function') {
      showToast('Connection interrupted. Retrying conversation sync…', 'error');
    }
    throw error;
  }
}
if (typeof window !== 'undefined') {
  runtimeScope.refreshConversationRuntime = refreshConversationRuntime;
}
