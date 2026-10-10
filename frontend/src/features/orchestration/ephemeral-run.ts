import { orchestrationRegistry } from './registry';
import {
  type OrchestrationActionLock,
  type OrchestrationActionOwner,
} from './action-lock';
import { type ContractRecord } from './contracts';
import {
  createOrchestrationCursorPoller,
  type TaskReplayPage,
} from './cursor-poller';
import {
  createOrchestrationRunSession,
} from './run-session';
import {
  createOrchestrationEphemeralAbortController,
  type EphemeralAbortPollContext,
} from './ephemeral-abort';
import { reportOrchestrationDiagnostic } from './diagnostic-report';

export interface EphemeralRunOptions {
  actionLock: OrchestrationActionLock;
  requests: {
    start(snapshot: unknown, input: unknown): Promise<ContractRecord>;
    poll(taskId: unknown, cursor: number): Promise<TaskReplayPage>;
    abort(taskId: unknown): Promise<ContractRecord>;
  };
  permits?: () => unknown | PromiseLike<unknown>;
  clearLog?: () => unknown;
  resetTrace?: () => unknown;
  renderEvent(event: unknown): unknown;
  mutationMessage(result: unknown, fallback: string): string;
  resultError(error: unknown, fallback: string): string;
  report?: (context: string, error: unknown) => unknown;
  log?: (html: string, className?: string) => unknown;
  translate?: (key: string) => string;
  escape?: (value: unknown) => unknown;
  icon?: (name: string) => unknown;
  pollDelay?: number | null;
  pollRetryBase?: number | null;
  pollRetryMax?: number | null;
  pollMaxFailures?: number | null;
  setTimeout?: (callback: () => void, delay: number) => number;
  clearTimeout?: (timer: number) => void;
}

type EphemeralRunWindow = Window & {
  createOrchestrationEphemeralRunController?:
    typeof createOrchestrationEphemeralRunController;
};

/** Ephemeral start/poll/abort ownership including late-start cleanup. */
export function createOrchestrationEphemeralRunController(
  options: EphemeralRunOptions,
) {
  const { actionLock, requests } = options;
  const runSession = createOrchestrationRunSession();
  let runAction: OrchestrationActionOwner | null = null;
  const translate = (key: string): string => options.translate
    ? options.translate(key) : key;
  const escape = (value: unknown): string => String(options.escape
    ? options.escape(value) : value == null ? '' : value);
  const icon = (name: string): string => String(
    options.icon ? options.icon(name) || '' : '');
  const log = (html: string, className?: string): void => {
    options.log?.(html, className);
  };
  const report = (context: string, error: unknown): void => {
    reportOrchestrationDiagnostic(options.report, context, error);
  };
  const releaseRunAction = (
    owner?: OrchestrationActionOwner | null,
  ): boolean => {
    const target = owner || runAction;
    const released = actionLock.release(target);
    if (released && runAction === target) runAction = null;
    return released;
  };

  const poller = createOrchestrationCursorPoller<
    EphemeralAbortPollContext, TaskReplayPage
  >({
    request: (context, cursor) => requests.poll(context?.taskId, cursor),
    accept: (context) => runSession.acceptsPoll(context),
    interval: options.pollDelay == null ? 700 : options.pollDelay,
    retryBase: options.pollRetryBase == null ? 800 : options.pollRetryBase,
    retryMax: options.pollRetryMax == null ? 6000 : options.pollRetryMax,
    maxFailures: options.pollMaxFailures == null ? 12 : options.pollMaxFailures,
    setTimeout: options.setTimeout,
    clearTimeout: options.clearTimeout,
    onFailure: (failure) => {
      if (failure.error && failure.attempt === 1) {
        report('poll', failure.error);
      }
      if (failure.attempt === 1) {
        log(`${icon('loop')} ${escape(translate(
          'orch.run.reconnecting'))}`, 'is-err');
      }
    },
    onRecovered: () => {
      log(`${icon('check')} ${escape(translate(
        'orch.run.reconnected'))}`, 'is-done');
    },
    onGiveUp: (failure) => {
      log(`${icon('warn')} ${escape(translate(
        'orch.run.offline'))}`, 'is-err');
      runSession.release(runSession.snapshot());
      releaseRunAction(failure.context?.action);
    },
    onResponse: (response, context) => {
      response.events.forEach(options.renderEvent);
      if (response.done) {
        runSession.release(runSession.snapshot());
        releaseRunAction(context?.action);
        return false;
      }
      return true;
    },
    onConsumerError: (error, context) => {
      report('poll-consumer', error);
      log(`${icon('warn')} ${escape(translate(
        'orch.run.pollFailed'))}`, 'is-err');
      runSession.release(runSession.snapshot());
      releaseRunAction(context?.action);
    },
  });

  const run = async (snapshot: unknown, input: unknown): Promise<boolean> => {
    if (actionLock.pending()) return false;
    const action = actionLock.acquire('run');
    runAction = action;
    const runOwner = runSession.begin(null);
    try {
      if (typeof options.permits === 'function' && !await options.permits()) {
        if (runSession.owns(runOwner, false)) releaseRunAction(action);
        return false;
      }
      if (!runSession.owns(runOwner, false)) {
        releaseRunAction(action);
        return false;
      }
      options.clearLog?.();
      options.resetTrace?.();
      log(`${icon('rocket')} ${escape(translate('orch.run.starting'))}`);
      const response = await requests.start(snapshot, input);
      if (!runSession.owns(runOwner, false)) {
        if (response.taskId) {
          try {
            const cleanup = await requests.abort(response.taskId);
            if (cleanup.cause) {
              report('late-start-cleanup', cleanup.cause);
            }
          } catch (cleanupError: unknown) {
            report('late-start-cleanup', cleanupError);
          }
        }
        releaseRunAction(action);
        return false;
      }
      if (response.cause) report('run-start', response.cause);
      if (!response.ok) {
        log(`${icon('warn')} ${escape(
          response.error || translate('orch.run.runFailed'))}`, 'is-err');
        releaseRunAction(action);
        return false;
      }
      const pollOwner = runSession.adopt(response.taskId, runOwner);
      if (!pollOwner || !runSession.startPolling(pollOwner)) {
        releaseRunAction(action);
        return false;
      }
      poller.start(0, {
        taskId: response.taskId,
        action,
        ...pollOwner,
      });
      return true;
    } catch (error: unknown) {
      if (!runSession.owns(runOwner, false)) {
        releaseRunAction(action);
        return false;
      }
      report('run-start', error);
      log(`${icon('warn')} ${escape(options.resultError(
        error, translate('orch.run.runFailed')))}`, 'is-err');
      releaseRunAction(action);
      return false;
    }
  };

  const abort = createOrchestrationEphemeralAbortController({
    session: runSession,
    poller,
    active: () => actionLock.pending('run'),
    currentAction: () => runAction,
    releaseAction: releaseRunAction,
    request: (taskId) => requests.abort(taskId),
    onRequested: () => log(`${icon('stop')} ${escape(
      translate('orch.run.abortRequested'))}`),
    onCause: (cause) => report('abort', cause),
    onRejected: (response) => log(`${icon('warn')} ${escape(
      options.mutationMessage(
        response, translate('orch.run.abortFailed')))}`, 'is-err'),
    onError: (error) => {
      report('abort', error);
      log(`${icon('warn')} ${escape(translate(
        'orch.run.abortFailed'))}`, 'is-err');
    },
  }).abort;

  return Object.freeze({ abort, run });
}

(orchestrationRegistry as unknown as EphemeralRunWindow).createOrchestrationEphemeralRunController =
  createOrchestrationEphemeralRunController;
