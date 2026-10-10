/**
 * Responsibility: own the single transient Turn shown while a send command is
 * being translated or accepted, including conversation-keyed teardown.
 * Entry point: createSendPreparationOverlayController. Dependencies: the
 * transient-Turn store plus injected catalog, translation, and scroll ports.
 */

import { createTransientStatusTurn } from './transient-status-turn';
import type { TransientTurnRecord } from '../domain/transient-turn';

export const SEND_PREPARATION_TURN_ID = 'transient:send-preparation';

export interface SendPreparationConversation {
  id: string;
}

export interface SendPreparationTransientTurns<
  Conversation extends SendPreparationConversation,
> {
  upsert(conversation: Conversation, turn: TransientTurnRecord): unknown;
  remove(conversation: Conversation, turnId: string): unknown;
}

export interface SendPreparationOverlayDependencies<
  Conversation extends SendPreparationConversation,
> {
  getActiveConversation(): Conversation | null | undefined;
  findConversation(conversationId: string): Conversation | null | undefined;
  resolveTransientTurns():
    | SendPreparationTransientTurns<Conversation>
    | null
    | undefined;
  translateTranslatingLabel(): string;
  scrollToLatest?(): void;
}

export interface SendPreparationOverlayController {
  show(label?: string): boolean;
  remove(conversationId?: string): boolean;
  destroy(): void;
}

export function createSendPreparationOverlayController<
  Conversation extends SendPreparationConversation,
>(
  dependencies: SendPreparationOverlayDependencies<Conversation>,
): SendPreparationOverlayController {
  let ownerConversationId: string | null = null;

  const remove = (conversationId?: string): boolean => {
    const targetConversationId = conversationId || ownerConversationId;
    if (ownerConversationId
        && (!conversationId || ownerConversationId === conversationId)) {
      ownerConversationId = null;
    }
    if (!targetConversationId) return false;

    const transientTurns = dependencies.resolveTransientTurns();
    const conversation = dependencies.findConversation(targetConversationId);
    if (!transientTurns || !conversation) return false;
    transientTurns.remove(conversation, SEND_PREPARATION_TURN_ID);
    return true;
  };

  const show = (label?: string): boolean => {
    const conversation = dependencies.getActiveConversation();
    const transientTurns = dependencies.resolveTransientTurns();
    if (!conversation || !transientTurns) return false;

    remove();
    const visibleLabel = label || dependencies.translateTranslatingLabel();
    transientTurns.upsert(
      conversation,
      createTransientStatusTurn({
        conversationId: conversation.id,
        turnId: SEND_PREPARATION_TURN_ID,
        phase: label ? 'connecting' : 'translating',
        label: visibleLabel,
      }),
    );
    ownerConversationId = conversation.id;
    try {
      dependencies.scrollToLatest?.();
    } catch {
      // Scroll presentation cannot invalidate the transient Turn command.
    }
    return true;
  };

  const destroy = (): void => {
    remove();
  };

  return Object.freeze({ show, remove, destroy });
}
