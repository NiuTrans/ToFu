import { orchestrationRegistry } from './registry';
import { orchestrationMutationMessage } from './mutation-result';
import { orchestrationResultError } from './result';
import { createTaskModeControllerHub } from './task-mode-controller-hub';

type Port = Record<string, unknown>;
type Callable = (...args: unknown[]) => unknown;

export interface TaskModeRootControllerOptions extends Record<string, unknown> {
  services: () => Port;
  contractSession: Port;
  session: Port;
  runStore: Port;
}

type TaskModeRootControllerWindow = Window & {
  createTaskModeRootController?: typeof createTaskModeRootController;
};

const operation = (port: Port, name: string): Callable | null =>
  typeof port[name] === 'function' ? port[name] as Callable : null;

export function createTaskModeRootController(
  options: TaskModeRootControllerOptions,
) {
  let hub: ReturnType<typeof createTaskModeControllerHub> | null = null;
  const services = (): Port => options.services();
  const invokeService = (name: string, ...args: unknown[]): unknown => {
    const service = services();
    return operation(service, name)?.apply(service, args);
  };
  const call = (name: string, ...args: unknown[]): unknown =>
    operation(options, name)?.apply(null, args);
  const apiClient = (): unknown => invokeService('api');
  const studioClient = (): unknown => invokeService('studio');
  const toast = (message: unknown, isError?: unknown): unknown =>
    invokeService('toast', message, isError);
  const reportTaskFailure = (context: string, value: unknown): boolean => {
    const result = value && typeof value === 'object'
      ? value as Port : {};
    if (result.ok !== false) return false;
    const cause = result.cause || {
      status: Number(result.status || result.httpStatus || 0),
      reason: String(result.reason || ''),
      error: orchestrationResultError(result, ''),
    };
    invokeService('reportError', 'TaskMode', context, cause);
    return true;
  };
  const ensure = () => {
    if (hub) return hub;
    hub = createTaskModeControllerHub({
      api: apiClient,
      contractSession: options.contractSession,
      session: options.session,
      confirm: (message: unknown, config: unknown) =>
        invokeService('confirm', message, config),
      report: reportTaskFailure,
      mutationMessage: (...args: unknown[]) => orchestrationMutationMessage(
        args[0], args[1] as Parameters<typeof orchestrationMutationMessage>[1],
        args[2], args[3]),
      resultError: (result: unknown, fallback: unknown) =>
        orchestrationResultError(result, String(fallback || '')),
      translate: (key: string, params?: unknown) => call('translate', key, params),
      toast,
      reconcileRun: (...args: unknown[]) => call('reconcileRun', ...args),
      refreshRuns: () => call('refreshRuns'),
      openRun: (runId: unknown) => call('openRun', runId),
      deleteAccepted: (runId: unknown) => {
        const run = ensure().run() as unknown as Port;
        if (operation(run, 'id')?.call(run) === runId) {
          operation(run, 'reset')?.call(
            run, { reason: 'deleted', runId });
          return;
        }
        operation(options.runStore, 'discard')?.call(options.runStore, runId);
        call('renderRunList');
      },
      isTerminal: (value: unknown) => call('isTerminal', value),
      pause: () => Boolean(invokeService('hidden')),
      onTransition: (transition: unknown) =>
        call('projectTransition', transition),
      onGraph: () => call('renderGraph'),
      onInspector: (_state: unknown, event: unknown) => {
        const entry = event && typeof event === 'object' ? event as Port : {};
        return call('renderInspector',
          entry.type === 'step_start' ? entry : null);
      },
      onTimeline: (event: unknown) => call('renderTimelineEvent', event),
      onGateOpened: () => call('presentPanel', 'inspector', 'human-gate'),
      onGateClosed: () => call('releasePanel', 'human-gate', 'run'),
      onLifecycle: (value: unknown) => {
        const lifecycle = value && typeof value === 'object'
          ? value as Port : {};
        return call('syncChip', lifecycle.status, lifecycle.done);
      },
    });
    return hub;
  };
  return Object.freeze({
    apiClient,
    studioClient,
    toast,
    reportTaskFailure,
    ensure,
    taskClient: () => ensure().taskClient(),
    actions: () => ensure().actions(),
    commands: () => ensure().commands(),
    run: () => ensure().run(),
    events: () => ensure().events(),
    current: () => hub,
    replace: (value: ReturnType<typeof createTaskModeControllerHub> | null) => {
      hub = value;
    },
  });
}

(orchestrationRegistry as unknown as TaskModeRootControllerWindow).createTaskModeRootController =
  createTaskModeRootController;
