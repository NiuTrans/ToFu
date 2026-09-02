/* ===== migrated source: main/main_translating_bubble.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   main translating bubble — extracted from main.js (split 2026-05-28)

   Translating-indicator bubble (shown while server pre-translates user message).

   This file is concatenated by Vite's module graph BEFORE main.js so
   the boot IIFE can reference these symbols. Symbols share `window`
   scope — no imports / exports needed.
   ═══════════════════════════════════════════════════════════════════ */


/**
 * Render pre-stream work as a transient Turn owned by ConversationSurface.
 *
 * Serves TWO phases on the SAME stable Turn so every send /
 * regenerate / edit exit path's existing `_removeTranslatingBubble()` tears it
 * down uniformly:
 *   • auto-translate on  → default label '翻译中…' (server pre-translates the
 *     user message before starting the agent);
 *   • auto-translate off → caller passes '连接中…' so the assistant side is NOT
 *     blank during the synchronous /api/chat/send POST (load → task-start).
 * Upgraded in place to the real streaming bubble once the POST returns a taskId.
 *
 * @param {string} [label] Status text. Defaults to the translating label.
 */
function _renderTranslatingBubble(label) {
  const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
  if (!conv || !runtimeScope.ConversationTransientTurns) return;
  _removeTranslatingBubble();
  const _label = label || t('sidebar.translating');
  const turnId = 'transient:send-preparation';
  _translatingTurnOwner = { conversationId: conv.id, turnId };
  runtimeScope.ConversationTransientTurns.upsert(
    conv,
    createTransientStatusTurn({
      conversationId: conv.id,
      turnId,
      phase: label ? 'connecting' : 'translating',
      label: _label,
    }),
  );
  const container = document.getElementById('chatContainer');
  if (container) container.scrollTop = container.scrollHeight;
}

let _translatingTurnOwner = null;

/* Targeted teardown: the overlay entry is keyed by conversation id and
 * outlives view switches, so removal must follow the SEND's conversation,
 * not whichever conversation happens to be active when the command
 * round trip settles. */
function _removeTranslatingBubble(conversationId) {
  const owner = _translatingTurnOwner;
  const targetConversationId = conversationId
    || (owner && owner.conversationId) || null;
  if (owner && (!conversationId || owner.conversationId === conversationId)) {
    _translatingTurnOwner = null;
  }
  if (!targetConversationId || !runtimeScope.ConversationTransientTurns) return;
  const conv = typeof conversations !== 'undefined'
    ? conversations.find(item => item?.id === targetConversationId) : null;
  if (conv) {
    runtimeScope.ConversationTransientTurns.remove(
      conv, 'transient:send-preparation',
    );
  }
}
