/**
 * Responsibility: reconcile a local conversation-metadata change with the
 * catalog order, cross-tab invalidation, and bounded sidebar presentation.
 * Entry point: createConversationCatalogReconciler. Dependencies: injected
 * catalog, clock, busy-state, invalidation, and animation-frame ports.
 */

export interface MutableCatalogConversation {
  readonly id: string;
  updatedAt?: number;
}

export interface ConversationCatalogReconcilerPorts<
  Conversation extends MutableCatalogConversation,
> {
  readConversations(): Conversation[];
  isConversationBusy(conversation: Conversation): boolean;
  compareConversations(left: Conversation, right: Conversation): number;
  publishCatalogInvalidation(conversationId: string | null): void;
  requestSidebarRender(render: () => void): void;
  renderSidebar(): void;
  now(): number;
}

export type ReconcileConversationCatalog = (
  changedConversationId: string | null,
) => void;

const SIDEBAR_REFRESH_INTERVAL_MS = 2_000;

export function createConversationCatalogReconciler<
  Conversation extends MutableCatalogConversation,
>(
  ports: ConversationCatalogReconcilerPorts<Conversation>,
): ReconcileConversationCatalog {
  let lastSidebarRefreshAt: number | null = null;
  let sidebarRenderPending = false;

  return (changedConversationId: string | null): void => {
    const conversations = ports.readConversations();
    const now = ports.now();
    if (changedConversationId) {
      const changedConversation = conversations.find(
        (conversation) => conversation.id === changedConversationId,
      );
      // Periodic live-Turn metadata refresh must not reorder simultaneous
      // streams. Command acceptance and settlement own activity timestamps.
      if (changedConversation
          && !ports.isConversationBusy(changedConversation)) {
        changedConversation.updatedAt = now;
      }
    }

    conversations.sort(ports.compareConversations);
    ports.publishCatalogInvalidation(changedConversationId);

    if (!changedConversationId || sidebarRenderPending
        || !conversations.some(ports.isConversationBusy)
        || (lastSidebarRefreshAt !== null
          && now - lastSidebarRefreshAt <= SIDEBAR_REFRESH_INTERVAL_MS)) {
      return;
    }
    lastSidebarRefreshAt = now;
    sidebarRenderPending = true;
    ports.requestSidebarRender(() => {
      sidebarRenderPending = false;
      ports.renderSidebar();
    });
  };
}
