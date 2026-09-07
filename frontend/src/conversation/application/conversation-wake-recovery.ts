/**
 * Responsibility: wake conversations with live attempts after page/network
 * activation through one bounded, lifecycle-owned application controller.
 * Entry point: createConversationWakeRecovery. Dependencies: the typed async
 * pool plus injected conversation/read/store/log ports.
 */

import {
  DEFAULT_ASYNC_POOL_CONCURRENCY,
  runWithConcurrency,
} from '../../core/async-pool';

export interface WakeRecoveryConversation {
  readonly id?: string;
}

export interface ConversationWakeRecoveryPorts<
  Conversation extends WakeRecoveryConversation,
> {
  readConversations(): readonly Conversation[];
  activeAttemptIds(conversation: Conversation): readonly unknown[];
  wakeConversation(conversation: Conversation): unknown | PromiseLike<unknown>;
  warn(error: unknown): void;
  readonly concurrency?: number;
}

export interface ConversationWakeEventTarget {
  addEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject,
    options?: boolean | AddEventListenerOptions,
  ): void;
  removeEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject,
    options?: boolean | EventListenerOptions,
  ): void;
}

export interface ConversationWakeRecoveryController {
  probe(): Promise<number>;
  start(target: ConversationWakeEventTarget): void;
  destroy(): void;
}

export function createConversationWakeRecovery<
  Conversation extends WakeRecoveryConversation,
>(
  ports: ConversationWakeRecoveryPorts<Conversation>,
): ConversationWakeRecoveryController {
  let eventTarget: ConversationWakeEventTarget | null = null;

  const probe = async (): Promise<number> => {
    const targets = ports.readConversations().filter(
      (conversation) => ports.activeAttemptIds(conversation).length > 0,
    );
    const result = await runWithConcurrency<Conversation>(
      targets,
      (conversation) => ports.wakeConversation(conversation),
      ports.concurrency ?? DEFAULT_ASYNC_POOL_CONCURRENCY,
    );
    for (const error of result.errors) ports.warn(error);
    return result.completed - result.errors.length;
  };

  const onWake = (): void => {
    void probe().catch((error: unknown) => ports.warn(error));
  };

  const destroy = (): void => {
    if (!eventTarget) return;
    const target = eventTarget;
    eventTarget = null;
    target.removeEventListener('pageshow', onWake);
    target.removeEventListener('online', onWake);
    target.removeEventListener('beforeunload', destroy);
  };

  const start = (target: ConversationWakeEventTarget): void => {
    if (eventTarget === target) return;
    destroy();
    eventTarget = target;
    target.addEventListener('pageshow', onWake);
    target.addEventListener('online', onWake);
    target.addEventListener('beforeunload', destroy, { once: true });
  };

  return Object.freeze({ probe, start, destroy });
}
