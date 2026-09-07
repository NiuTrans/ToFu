/**
 * Responsibility: coordinate metadata-only conversation/folder startup and
 * converge the active conversation presentation without hydrating or
 * dispatching a Turn.
 * Entry point: createConversationStartup. Dependencies: injected catalog,
 * folder, Turn-runtime readiness, and presentation ports.
 */

export interface StartupConversationReference {
  readonly id: string;
}

export interface ConversationStartupPorts<
  Conversation extends StartupConversationReference,
> {
  loadConversationCatalog(): unknown | PromiseLike<unknown>;
  loadFolders?(): unknown | PromiseLike<unknown>;
  migratePinnedToFolder?(): void | PromiseLike<void>;
  scheduleFolderLoadRetry?(): void;
  hasTurnHydrator(): boolean;
  activeConversationId(): string | null;
  activeConversation(): Conversation | null;
  isConversationBusy(conversation: Conversation): boolean;
  showStreamingPresentation(conversationId: string): void;
  requestAuthoritativeRender(conversationId: string): void;
  renderPendingQueue(conversationId: string): void;
  warnFolderLoad(error: unknown): void;
  warnCatalogLoad(error: unknown): void;
}

export interface ConversationStartupController {
  initialize(): Promise<void>;
  ensureActivePresentation(): void;
}

export function createConversationStartup<
  Conversation extends StartupConversationReference,
>(
  ports: ConversationStartupPorts<Conversation>,
): ConversationStartupController {
  const ensureActivePresentation = (): void => {
    const conversationId = ports.activeConversationId();
    if (!conversationId) return;
    const conversation = ports.activeConversation();
    if (conversation && ports.isConversationBusy(conversation)) {
      ports.showStreamingPresentation(conversationId);
    } else if (conversation) {
      ports.requestAuthoritativeRender(conversation.id);
    }
    ports.renderPendingQueue(conversationId);
  };

  const startFolderLoad = (): Promise<void> => {
    if (!ports.loadFolders) return Promise.resolve();
    return Promise.resolve()
      .then(() => ports.loadFolders?.())
      .then(() => ports.migratePinnedToFolder?.())
      .catch((error: unknown) => {
        ports.warnFolderLoad(error);
        ports.scheduleFolderLoadRetry?.();
      });
  };

  const initialize = async (): Promise<void> => {
    const folderLoad = startFolderLoad();
    try {
      await ports.loadConversationCatalog();
      if (!ports.hasTurnHydrator()) {
        throw new Error('ConversationTurnStore failed to initialize');
      }
    } catch (error: unknown) {
      ports.warnCatalogLoad(error);
    }
    ensureActivePresentation();
    await folderLoad;
  };

  return Object.freeze({ initialize, ensureActivePresentation });
}
