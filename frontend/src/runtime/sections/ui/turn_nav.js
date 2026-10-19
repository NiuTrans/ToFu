/* ===== migrated source: ui/turn_nav.js ===== */
/* TurnStore/Surface bridge only; compact UI is owned by turn-navigation.ts. */
let _turnNavigation = null;
let _turnNavigationHost = null;

function _scrollTurnNode(turnId) {
  const inner = document.getElementById('chatInner');
  const node = inner && Array.from(inner.querySelectorAll('[data-turn-id]'))
    .find((item) => item.dataset.turnId === turnId);
  if (!node) return false;
  node.scrollIntoView({
    behavior: typeof prefersReducedMotion === 'function' && prefersReducedMotion()
      ? 'auto' : 'smooth',
    block: 'start',
  });
  return true;
}

function scrollToTurn(turnId) {
  const stableTurnId = typeof turnId === 'string' ? turnId : '';
  if (!stableTurnId) return;
  const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
  if (!conv) return;
  // Surface, not a DOM search or nav shadow window, owns revealing older Turns.
  const reveal = runtimeScope.ConversationSurfacePresentation?.revealTurn;
  if (typeof reveal === 'function' && reveal(conv.id, stableTurnId)) {
    _scrollTurnNode(stableTurnId);
    return;
  }
  if (_scrollTurnNode(stableTurnId)) return;
  runtimeScope.requestAuthoritativeConversationRender(conv.id, { forceScroll: false });
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (typeof getActiveConv === 'function' && getActiveConv()?.id !== conv.id) return;
      runtimeScope.ConversationSurfacePresentation?.revealTurn?.(conv.id, stableTurnId);
      if (!_scrollTurnNode(stableTurnId)) {
        console.warn('[turnNav] Turn node unavailable after convergence:', stableTurnId);
      }
    }));
  }
}

function buildTurnNav(conv) {
  const nav = document.getElementById('turnNav');
  if (nav !== _turnNavigationHost) {
    _turnNavigation?.destroy();
    _turnNavigationHost = nav;
    _turnNavigation = nav ? createTurnNavigation({
      host: nav,
      translate: (key, params) => t(key, params),
      scrollToTurn,
      directoryKey: (id) => runtimeScope.ConversationTurnStore.roundDirectoryKey(id),
      directory: async (id, offset, limit, anchor) => {
        const active = typeof getActiveConv === 'function' ? getActiveConv() : null;
        if (!active || active.id !== id) throw new Error('Conversation changed');
        return runtimeScope.ConversationTurnStore.loadConversationRoundDirectory(active, 'main', offset, limit, anchor);
      },
      hydrateRound: async (id, entry) => {
        const active = typeof getActiveConv === 'function' ? getActiveConv() : null;
        if (!active || active.id !== id) throw new Error('Conversation changed');
        await runtimeScope.ConversationTurnStore.hydrateConversationRound(active, 'main', entry);
      },

      loadEarlier: async (conversationId, beforeOrdinal) => {
        const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
        if (!conv || conv.id !== conversationId) return;
        await runtimeScope.ConversationTurnStore.loadConversationTurnPage(conv, 'main', beforeOrdinal, 64);
      },
    }) : null;
  }
  const conversationId = conv?._localOnly ? '' : conv?.id || '';
  const state = conversationId
    ? runtimeScope.ConversationTurnStore?.ensureRuntimeStore?.(conversationId)?.getState?.()
    : null;
  _turnNavigation?.update(conversationId, state || null);
}

function updateActiveTurn() {
  const container = typeof _getChatContainer === 'function'
    ? _getChatContainer() : document.getElementById('chatContainer');
  const inner = document.getElementById('chatInner');
  if (container && inner) _turnNavigation?.updateActive(container, inner);
}
