import { orchestrationRegistry } from './registry';
import { type OrchestrationActionOwner } from './action-lock';
import { type OrchestrationCursorPoller } from './cursor-poller';
import { reportOrchestrationDiagnostic } from './diagnostic-report';
import { createOrchestrationMutationCommand } from './mutation-command';
import {
  type OrchestrationRunOwner,
  type OrchestrationRunSession,
} from './run-session';

export interface EphemeralAbortPollContext extends Record<string, unknown>,
  OrchestrationRunOwner {
  taskId: unknown;
  action: OrchestrationActionOwner | null;
}

export interface EphemeralAbortOptions {
  session: OrchestrationRunSession;
  poller: OrchestrationCursorPoller<EphemeralAbortPollContext>;
  active(): boolean;
  currentAction(): OrchestrationActionOwner | null;
  releaseAction(action: OrchestrationActionOwner | null): unknown;
  request(taskId: unknown): unknown | PromiseLike<unknown>;
  onRequested?(): unknown;
  onCause?(cause: unknown): unknown;
  onRejected?(result: unknown): unknown;
  onError?(error: unknown): unknown;
}

type EphemeralAbortWindow = Window & {
  createOrchestrationEphemeralAbortController?:
    typeof createOrchestrationEphemeralAbortController;
};

/** Pause a live poll while aborting and resume it when abort is unconfirmed. */
export function createOrchestrationEphemeralAbortController(
  options: EphemeralAbortOptions,
) {
  const { session, poller } = options;
  let pending = false;
  const mutationCommand = createOrchestrationMutationCommand();

  const notify = (
    name: 'onRequested' | 'onCause' | 'onRejected' | 'onError',
    value?: unknown,
  ): void => {
    const callback = options[name] as
      ((value?: unknown) => unknown) | undefined;
    try {
      callback?.(value);
    } catch (notificationError: unknown) {
      if (name === 'onError') return;
      reportOrchestrationDiagnostic(options.onError, notificationError);
    }
  };

  const finish = (action: OrchestrationActionOwner | null): void => {
    session.invalidate();
    options.releaseAction(action);
  };
  const resume = (
    taskId: unknown,
    owner: OrchestrationRunOwner,
    cursor: number,
    action: OrchestrationActionOwner | null,
  ): boolean => {
    const pollOwner = session.beginRead(owner);
    if (!pollOwner || !session.startPolling(pollOwner)) {
      options.releaseAction(action);
      return false;
    }
    poller.start(cursor, { taskId, action, ...pollOwner });
    return true;
  };

  const abort = async (): Promise<boolean> => {
    if (pending) return false;
    const taskId = session.id();
    if (!options.active() && !taskId) return false;
    const action = options.currentAction();
    notify('onRequested');
    if (!taskId) {
      session.invalidate();
      poller.stop();
      options.releaseAction(action);
      return true;
    }

    const owner = session.snapshot();
    const cursor = poller.cursor();
    pending = true;
    session.stopPolling(owner);
    poller.stop();
    try {
      const outcome = await mutationCommand.execute({
        context: 'abort', acceptAbsent: true,
        request: () => options.request(taskId),
      });
      const response = outcome.result;
      const result = response && typeof response === 'object'
        ? response as Record<string, unknown> : {};
      if (result.cause) notify('onCause', result.cause);
      if (!outcome.mutation) {
        resume(taskId, owner, cursor, action);
        notify('onError', outcome.cause
          || new TypeError('missing abort mutation'));
        return false;
      }
      if (outcome.satisfied) finish(action);
      else resume(taskId, owner, cursor, action);
      if (!outcome.satisfied) notify('onRejected', response);
      return outcome.satisfied;
    } catch (error: unknown) {
      resume(taskId, owner, cursor, action);
      notify('onError', error);
      return false;
    } finally {
      pending = false;
    }
  };

  return Object.freeze({ abort });
}

(orchestrationRegistry as unknown as EphemeralAbortWindow).createOrchestrationEphemeralAbortController =
  createOrchestrationEphemeralAbortController;
