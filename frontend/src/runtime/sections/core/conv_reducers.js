/* ===== migrated source: core/conv_reducers.js ===== */
/* Pure conversation queries shared by chat and tool-result views.
 *
 * Transcript reconciliation does not belong here. Conversation Sync v3 owns
 * message identity and projection; this module only resolves presentation
 * settings and catalog identities.
 */

function convAutoTranslate(conv) {
  if (conv && conv.autoTranslate !== undefined) return !!conv.autoTranslate;
  if (typeof autoTranslate !== 'undefined' && autoTranslate !== undefined) {
    return !!autoTranslate;
  }
  return false;
}

function _convFindById(conversationId) {
  if (!conversationId || !Array.isArray(conversations)) return null;
  const exact = conversations.find((conversation) =>
    conversation?.id === conversationId);
  if (exact) return exact;
  const prefixMatches = conversations.filter((conversation) =>
    conversation?.id?.startsWith(conversationId));
  return prefixMatches.length === 1 ? prefixMatches[0] : null;
}

function convTitleById(conversationId) {
  if (!conversationId) return '';
  const conversation = _convFindById(conversationId);
  const title = String(conversation?.title || '').trim();
  if (title) return title;
  return typeof t === 'function' ? t('toast.untitledConv') : 'Untitled chat';
}

function convFullIdById(conversationId) {
  return String(_convFindById(conversationId)?.id || '');
}

if (typeof window !== 'undefined') {
  runtimeScope.convAutoTranslate = convAutoTranslate;
  runtimeScope.convTitleById = convTitleById;
  runtimeScope.convFullIdById = convFullIdById;
}
