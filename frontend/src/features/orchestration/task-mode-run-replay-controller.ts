import { orchestrationRegistry } from './registry';
import {
  createOrchestrationCursorPoller,
  type CursorPollerOptions,
  type CursorPollFailure,
  type CursorPollRecovery,
  type TaskReplayPage,
} from './cursor-poller';
import type {
  OrchestrationRunOwner,
  OrchestrationRunSession,
} from './run-session';

type Port = Record<string, unknown>;
type OwnerContext = OrchestrationRunOwner & Record<string, unknown>;
interface RunReplayPage extends TaskReplayPage {
  run?: unknown;
  notFound?: unknown;
}
interface PollerPort {
  start(cursor?: number | null, context?: OwnerContext): unknown;
  stop(): unknown;
}

export interface TaskModeRunReplayControllerOptions
  extends Record<string, unknown> {
  session: OrchestrationRunSession;
  taskClient?: Port | (() => Port | null) | null;
  pause?: (context: OwnerContext | null) => boolean;
  pauseDelay?: number;
  interval?: number;
  retryBase?: number;
  retryMax?: number;
  maxFailures?: number;
  setTimeout?: (callback: () => void, delay: number) => number;
  clearTimeout?: (timer: number) => void;
  pollerFactory?: (
    options: CursorPollerOptions<OwnerContext, RunReplayPage>,
  ) => PollerPort;
}

type TaskModeRunReplayWindow = Window & {
  createTaskModeRunReplayController?:
    typeof createTaskModeRunReplayController;
};

/** Cursor replay, retry lifecycle and terminal snapshot handoff. */
export function createTaskModeRunReplayController(
  options: TaskModeRunReplayControllerOptions,
) {
  const { session } = options;
  const client = (): Port | null => {
    const value = typeof options.taskClient === 'function'
      ? options.taskClient() : options.taskClient;
    return value ?? null;
  };
  const call = (name: string, ...args: unknown[]): unknown => {
    const callback = options[name];
    return typeof callback === 'function'
      ? (callback as (...values: unknown[]) => unknown).apply(null, args)
      : undefined;
  };
  const pollerOptions: CursorPollerOptions<OwnerContext, RunReplayPage> = {
    request: (context, cursor) => {
      const taskClient = client();
      const operation = taskClient?.events;
      return context && typeof operation === 'function'
        ? (operation as (...args: unknown[]) => RunReplayPage | null
          | Promise<RunReplayPage | null>).call(
            taskClient, context.runId, cursor)
        : null;
    },
    accept: (context) => session.acceptsPoll(context),
    pause: options.pause,
    pauseDelay: options.pauseDelay ?? 1500,
    interval: options.interval ?? 800,
    retryBase: options.retryBase ?? 800,
    retryMax: options.retryMax ?? 6000,
    maxFailures: options.maxFailures ?? 12,
    setTimeout: options.setTimeout,
    clearTimeout: options.clearTimeout,
    retryable: (failure: CursorPollFailure<OwnerContext, RunReplayPage>) =>
      failure.response?.notFound !== true,
    onFailure: (failure: CursorPollFailure<OwnerContext, RunReplayPage>) => {
      if (failure.attempt === 1) {
        call('emit', 'connection', {
          state: 'reconnecting', detail: failure,
        });
      }
    },
    onRecovered: (recovery: CursorPollRecovery<OwnerContext>) => {
      call('emit', 'connection', {
        state: 'recovered', detail: recovery,
      });
    },
    onGiveUp: (failure: CursorPollFailure<OwnerContext, RunReplayPage>) => {
      if (failure.response?.notFound === true) {
        call('reset', {
          reason: 'missing', runId: failure.context?.runId,
        });
        return;
      }
      session.stopPolling();
      call('setBusy', false);
      call('emit', 'connection', { state: 'offline', detail: failure });
    },
    onResponse: (page, context) => {
      call('emit', 'replay', { page, owner: context });
      if (!page.replayComplete) return true;
      session.stopPolling(context);
      call('setBusy', false);
      if (!context) return false;
      const run = call('acceptedRun', page, context.runId);
      if (run) {
        call('emit', 'snapshot', {
          run, source: 'terminal-replay', renderFinal: true,
        });
      } else {
        call('readFinal', context.runId, context);
      }
      return false;
    },
    onConsumerError: (error: unknown) => {
      session.stopPolling();
      call('setBusy', false);
      call('emit', 'projection-error', { error });
    },
  };
  const poller = options.pollerFactory
    ? options.pollerFactory(pollerOptions)
    : createOrchestrationCursorPoller<OwnerContext, RunReplayPage>(
      pollerOptions);
  const start = (owner: OrchestrationRunOwner): boolean => {
    if (!session.startPolling(owner)) return false;
    call('setBusy', true);
    poller.start(0, owner as OwnerContext);
    return true;
  };
  return Object.freeze({ start, stop: () => { poller.stop(); } });
}

(orchestrationRegistry as unknown as TaskModeRunReplayWindow).createTaskModeRunReplayController =
  createTaskModeRunReplayController;
