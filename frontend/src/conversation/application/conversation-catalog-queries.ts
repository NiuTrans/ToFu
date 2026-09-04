/**
 * Responsibility: answer pure presentation/settings queries over an injected
 * conversation catalog without owning catalog state or localization.
 * Entry points: resolveConversationAutoTranslate, findConversationById,
 * conversationTitleById, and conversationFullIdById. Dependencies: none.
 */

export interface ConversationCatalogEntry {
  readonly id?: unknown;
  readonly title?: unknown;
  readonly autoTranslate?: unknown;
}

function entries(catalog: unknown): readonly ConversationCatalogEntry[] {
  return Array.isArray(catalog)
    ? catalog as readonly ConversationCatalogEntry[] : [];
}

export function resolveConversationAutoTranslate(
  conversation: ConversationCatalogEntry | null | undefined,
  defaultValue: unknown,
): boolean {
  if (conversation?.autoTranslate !== undefined) {
    return Boolean(conversation.autoTranslate);
  }
  return defaultValue !== undefined ? Boolean(defaultValue) : false;
}

export function findConversationById(
  catalog: unknown,
  conversationId: unknown,
): ConversationCatalogEntry | null {
  if (!conversationId) return null;
  const catalogEntries = entries(catalog);
  const exact = catalogEntries.find(
    (conversation) => conversation?.id === conversationId,
  );
  if (exact) return exact;
  const prefix = String(conversationId);
  const prefixMatches = catalogEntries.filter(
    (conversation) => typeof conversation?.id === 'string'
      && conversation.id.startsWith(prefix),
  );
  return prefixMatches.length === 1 ? prefixMatches[0] : null;
}

export function conversationTitleById(
  catalog: unknown,
  conversationId: unknown,
  untitledLabel = 'Untitled chat',
): string {
  if (!conversationId) return '';
  const conversation = findConversationById(catalog, conversationId);
  const title = String(conversation?.title || '').trim();
  return title || untitledLabel;
}

export function conversationFullIdById(
  catalog: unknown,
  conversationId: unknown,
): string {
  return String(findConversationById(catalog, conversationId)?.id || '');
}
