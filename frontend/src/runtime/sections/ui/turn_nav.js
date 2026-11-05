/* ===== migrated source: ui/turn_nav.js ===== */
/* TurnStore/Surface bridge only; compact UI is owned by turn-navigation.ts. */
let _turnNavigation = null;
let _turnNavigationHost = null;
let _turnNavigationConvId = '';
let _turnJumpEpoch = 0;

function scrollToTurn(turnId) {
  const stableTurnId = typeof turnId === 'string' ? turnId : '';
  if (!stableTurnId) return;
  const epoch = ++_turnJumpEpoch;
  const conversationId = typeof getActiveConv === 'function' ? getActiveConv()?.id : '';
  if (typeof conversationId !== 'string' || !conversationId) return;
  const reveal = runtimeScope.ConversationSurfacePresentation?.revealTurn;
  if (reveal?.(conversationId, stableTurnId)) return;
  runtimeScope.requestAuthoritativeConversationRender(conversationId, { forceScroll: false });
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (epoch !== _turnJumpEpoch || typeof getActiveConv !== 'function'
        || getActiveConv()?.id !== conversationId) return;
      if (!runtimeScope.ConversationSurfacePresentation?.revealTurn?.(conversationId, stableTurnId)) {
        console.warn('[turnNav] Turn node unavailable after convergence:', stableTurnId);
      }
    }));
  }
}

/* Open a conversation at one Turn (a sidebar search body hit): select it,
 * page the Turn in by its lane ordinal, then reveal it. A Turn that no longer
 * exists leaves the conversation open at its ordinary position. */
async function openConversationAtTurn(conversationId, turn) {
  const epoch = ++_turnJumpEpoch;
  await loadConversation(conversationId);
  const active = () => (epoch === _turnJumpEpoch && typeof getActiveConv === 'function'
    && getActiveConv()?.id === conversationId ? getActiveConv() : null);
  const conv = active();
  if (!conv) return;
  try {
    await runtimeScope.ConversationTurnStore.hydrateConversationRound(
      conv, 'main', { turnId: turn.turnId, ordinal: turn.ordinal });
  } catch (error) {
    console.warn('[turnNav] search match Turn unavailable:', error?.message || error);
    return;
  }
  if (active()) scrollToTurn(turn.turnId);
}

function buildTurnNav(conv) {
  const nav = document.getElementById('turnNav');
  const conversationId = conv?._localOnly ? '' : conv?.id || '';
  if (conversationId !== _turnNavigationConvId || nav !== _turnNavigationHost) _turnJumpEpoch++;
  _turnNavigationConvId = conversationId;
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
