/**
 * Responsibility: refresh one catalog conversation through the authoritative
 * Turn runtime without owning catalog, transport, presentation, or retry state.
 * Entry point: createConversationRefresh. Dependencies: injected ports only.
 */

export interface ConversationHydrator<Conversation, Result> {
  hydrateConversation(conversation: Conversation): Promise<Result>;
}

export interface ConversationRefreshDependencies<Conversation, Result> {
  findConversation(conversationId: string): Conversation | null | undefined;
  resolveHydrator(): ConversationHydrator<Conversation, Result> | null | undefined;
  reportFailure?(error: unknown): void;
}

export type ConversationRefresh<Result> = (
  conversationId: string,
) => Promise<Result | null>;

export function createConversationRefresh<Conversation, Result = unknown>(
  dependencies: ConversationRefreshDependencies<Conversation, Result>,
): ConversationRefresh<Result> {
  return async (conversationId: string): Promise<Result | null> => {
    const conversation = dependencies.findConversation(conversationId);
    if (conversation == null) return null;

    const hydrator = dependencies.resolveHydrator();
    if (!hydrator || typeof hydrator.hydrateConversation !== 'function') {
      throw new Error('ConversationTurnStore failed to initialize');
    }

    try {
      return await hydrator.hydrateConversation(conversation);
    } catch (error) {
      try {
        dependencies.reportFailure?.(error);
      } catch {
        // Presentation diagnostics cannot replace the authoritative failure.
      }
      throw error;
    }
  };
}
