import { orchestrationRegistry } from './registry';
import type { createOrchestrationTaskRequestClient } from './task-request';

type Port = Record<string, unknown>;

export interface TaskModeControllerHubOptions extends Record<string, unknown> {
  contractSession?: Port | null;
}

type TaskModeControllerHubWindow = Window & {
  createOrchestrationTaskRequestClient?:
    typeof createOrchestrationTaskRequestClient;
  createTaskModeActionController?: (options: Port) => Port;
  createTaskModeCommandController?: (options: Port) => Port;
  createTaskModeRunController?: (options: Port) => Port;
  createTaskModeEventController?: (options: Port) => Port;
  createTaskModeControllerHub?: typeof createTaskModeControllerHub;
};

const record = (value: unknown): Port => value
  && typeof value === 'object' && !Array.isArray(value) ? value as Port : {};
const factory = (
  name: keyof TaskModeControllerHubWindow,
): ((options: Port) => Port) => {
  const candidate = (orchestrationRegistry as unknown as TaskModeControllerHubWindow)[name];
  if (typeof candidate !== 'function') {
    throw new Error(`Task Mode controller dependency is unavailable: ${name}`);
  }
  return candidate as (options: Port) => Port;
};

export function createTaskModeControllerHub(
  options: TaskModeControllerHubOptions = {},
) {
  let taskRequests: ReturnType<typeof createOrchestrationTaskRequestClient>
    | null = null;
  let actionController: Port | null = null;
  let commandController: Port | null = null;
  let runController: Port | null = null;
  let eventController: Port | null = null;
  const contracts = (): Port => {
    const session = options.contractSession;
    const snapshot = session?.snapshot;
    return typeof snapshot === 'function'
      ? record((snapshot as () => unknown).call(session)) : {};
  };
  const taskClient = (): ReturnType<
    typeof createOrchestrationTaskRequestClient> => {
    if (taskRequests) return taskRequests;
    const requestFactory = (orchestrationRegistry as unknown as TaskModeControllerHubWindow)
      .createOrchestrationTaskRequestClient;
    if (!requestFactory) {
      throw new Error('Orchestration task request owner is unavailable');
    }
    taskRequests = requestFactory({
      api: options.api,
      replayContract: () => contracts().replayContract || null,
      runtimeStartContract: () => contracts().runtimeStartContract || null,
      durableRunContract: () => contracts().durableRunContract || null,
      runContract: () => contracts().runContract || null,
    });
    return taskRequests;
  };
  const actions = (): Port => {
    if (actionController) return actionController;
    actionController = factory('createTaskModeActionController')({
      api: options.api,
      taskClient,
      confirm: options.confirm,
      report: options.report,
      mutationMessage: options.mutationMessage,
      mutationContract: () => contracts().mutationContract || null,
      resultError: options.resultError,
      translate: options.translate,
    });
    return actionController;
  };
  const events = (): Port => {
    if (eventController) return eventController;
    eventController = factory('createTaskModeEventController')({
      eventContract: () => contracts().eventContract || null,
      traceContract: () => contracts().traceContract || null,
      onGraph: options.onGraph,
      onInspector: options.onInspector,
      onTimeline: options.onTimeline,
      onGateOpened: options.onGateOpened,
      onGateClosed: options.onGateClosed,
      onLifecycle: options.onLifecycle,
    });
    return eventController;
  };
  const commands = (): Port => {
    if (commandController) return commandController;
    commandController = factory('createTaskModeCommandController')({
      actions,
      session: options.session,
      translate: options.translate,
      toast: options.toast,
      dismissGate: (requestId: unknown) => {
        const dismiss = events().dismissGate;
        return typeof dismiss === 'function'
          ? (dismiss as (id: unknown) => unknown).call(events(), requestId)
          : undefined;
      },
      reconcileRun: options.reconcileRun,
      refreshRuns: options.refreshRuns,
      openRun: options.openRun,
      deleteAccepted: options.deleteAccepted,
    });
    return commandController;
  };
  const run = (): Port => {
    if (runController) return runController;
    runController = factory('createTaskModeRunController')({
      session: options.session,
      taskClient,
      isTerminal: options.isTerminal,
      pause: options.pause,
      report: options.report,
      onTransition: options.onTransition,
    });
    return runController;
  };
  return Object.freeze({ taskClient, actions, commands, run, events });
}

(orchestrationRegistry as unknown as TaskModeControllerHubWindow).createTaskModeControllerHub =
  createTaskModeControllerHub;
