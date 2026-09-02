/**
 * Lifecycle-local target for composing a reply into a branch lane.
 *
 * This owner stores stable contract identities only. It has no DOM, transport,
 * transcript, or persistence dependency; the runtime adapter owns composer
 * chrome and submits through ConversationTurnStore.
 */
export interface BranchComposerTarget {
  conversationId: string;
  parentTurnId: string;
  laneId: string;
  title: string;
}

export interface BranchComposerSession {
  current(): BranchComposerTarget | null;
  open(target: BranchComposerTarget): BranchComposerTarget;
  close(): BranchComposerTarget | null;
  isActive(conversationId?: string): boolean;
}

export function createBranchComposerSession(): BranchComposerSession {
  let target: BranchComposerTarget | null = null;
  return Object.freeze({
    current(): BranchComposerTarget | null {
      return target;
    },
    open(next: BranchComposerTarget): BranchComposerTarget {
      if (!next.conversationId || !next.parentTurnId || !next.laneId) {
        throw new Error('Branch composer requires conversation, parent Turn, and lane IDs.');
      }
      target = { ...next };
      return target;
    },
    close(): BranchComposerTarget | null {
      const previous = target;
      target = null;
      return previous;
    },
    isActive(conversationId?: string): boolean {
      return Boolean(target && (!conversationId || target.conversationId === conversationId));
    },
  });
}
