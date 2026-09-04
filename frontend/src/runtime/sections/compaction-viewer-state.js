/* ===== migrated source: compaction-viewer-state.js ===== */
/* Responsibility: compose typed bounded compaction-history state with the
 * retained endpoint and context-bar ports. Policy lives in
 * frontend/src/core/compaction-history-state.ts. */

const CompactionHistoryState = createCompactionHistoryState({
  list: (conversationId) => Api.compactions.list(conversationId),
  onRefresh: () => {
    if (typeof runtimeScope.updateContextBar === 'function') {
      runtimeScope.updateContextBar();
    }
  },
});

function getCompactionHistory(conversationId) {
  return CompactionHistoryState.get(conversationId);
}

function loadCompactionHistory(conversationId) {
  return CompactionHistoryState.refresh(conversationId);
}
