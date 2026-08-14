import { orchestrationRegistry } from './registry';
import { createOrchestrationMutationCommand } from './mutation-command';
import { createTaskModeRunCommandController } from './task-mode-run-command-controller';

type Port = Record<string, unknown>;
export interface TaskModeCommandControllerOptions extends Record<string, unknown> {
  actions?: Port | (() => Port | null) | null;
  translate?: (key: string, params?: Record<string, unknown>) => unknown;
  session?: Port | null;
}
type TaskModeCommandWindow = Window & {
  createTaskModeCommandController?: typeof createTaskModeCommandController;
};

export function createTaskModeCommandController(
  options: TaskModeCommandControllerOptions = {},
) {
  const actions = (): Port | null => {
    const result = typeof options.actions === 'function'
      ? options.actions() : options.actions;
    return result ?? null;
  };
  const translate = (key: string, params?: Record<string, unknown>): string => {
    const value = options.translate ? options.translate(key, params) : key;
    return String(value == null ? key : value);
  };
  const call = (name: string, ...args: unknown[]): unknown => {
    const callback = options[name];
    return typeof callback === 'function'
      ? (callback as (...values: unknown[]) => unknown).apply(null, args)
      : undefined;
  };
  const toast = (
    keyOrMessage: unknown, isError: unknown, translated = false,
  ): void => {
    const message = translated ? keyOrMessage : translate(String(keyOrMessage));
    if (message) call('toast', message, Boolean(isError));
  };
  const failureMessage = (result: unknown, fallback: unknown): unknown => {
    const controller = actions();
    return controller && typeof controller.failureMessage === 'function'
      ? (controller.failureMessage as (...values: unknown[]) => unknown).call(
        controller, result, fallback) : fallback;
  };
  const captureOwner = (): unknown => {
    const snapshot = options.session?.snapshot;
    return typeof snapshot === 'function'
      ? (snapshot as () => unknown).call(options.session) : null;
  };
  const ownsOwner = (owner: unknown, runId?: unknown): boolean => {
    const owns = options.session?.owns;
    if (typeof owns !== 'function') return true;
    const owned = Boolean((owns as (...values: unknown[]) => unknown).call(
      options.session, owner, false));
    const id = options.session?.id;
    const current = typeof id === 'function'
      ? (id as () => unknown).call(options.session) : null;
    return owned && (!runId || current === String(runId));
  };
  const mutationCommand = createOrchestrationMutationCommand({ failureMessage });
  const runCommands = createTaskModeRunCommandController({
    actions, translate, call, toast, mutationCommand,
    captureOwner, ownsOwner,
  });
  const gate = async (
    method: string, requestId: unknown, value: unknown, successKey: string,
  ): Promise<boolean> => {
    if (!requestId) return false;
    const owner = captureOwner();
    const controller = actions();
    const operation = controller?.[method];
    const outcome = await mutationCommand.execute({
      context: method,
      fallback: translate('tm.toast.actionFailed'),
      request: () => typeof operation === 'function'
        ? (operation as (...values: unknown[]) => unknown).call(
          controller, requestId, value) : null,
    });
    if (!outcome.attempted) return false;
    if (!ownsOwner(owner)) return outcome.ok;
    if (!outcome.ok) {
      if (outcome.targetAbsent) call('dismissGate', requestId);
      toast(outcome.message, true, true);
      return false;
    }
    call('dismissGate', requestId);
    if (successKey) toast(successKey, false);
    return true;
  };
  const approveGate = (requestId: unknown, approved: unknown) => gate(
    'approveGate', requestId, Boolean(approved),
    approved ? 'orch.gate.approved' : 'orch.gate.rejected');
  const inputGate = (requestId: unknown, input: unknown): Promise<boolean> => {
    if (!requestId) return Promise.resolve(false);
    const source = input && typeof input === 'object'
      ? input as Port : null;
    const value = typeof input === 'string'
      ? input : source?.value != null ? String(source.value) : '';
    if (!value.trim()) {
      toast('orch.gate.enterResponse', true);
      return Promise.resolve(false);
    }
    return gate('inputGate', requestId, value, '');
  };
  return Object.freeze({
    approveGate, inputGate,
    abortRun: runCommands.abortRun,
    rerun: runCommands.rerun,
    deleteRun: runCommands.deleteRun,
  });
}

(orchestrationRegistry as unknown as TaskModeCommandWindow).createTaskModeCommandController =
  createTaskModeCommandController;
