import { orchestrationRegistry } from './registry';
import type { createOrchestrationMutationCommand } from './mutation-command';
import { projectOrchestrationDurableStartOutcome } from './durable-start-outcome';

type Port = Record<string, unknown>;
export interface TaskModeRunCommandPorts {
  actions(): Port | null;
  translate(key: string, params?: Record<string, unknown>): string;
  call(name: string, ...args: unknown[]): unknown;
  toast(value: unknown, isError: unknown, translated?: boolean): void;
  mutationCommand: ReturnType<typeof createOrchestrationMutationCommand>;
  captureOwner?: () => unknown;
  ownsOwner?: (owner: unknown, runId?: unknown) => boolean;
}
type TaskModeRunCommandWindow = Window & {
  createTaskModeRunCommandController?:
    typeof createTaskModeRunCommandController;
};

/** Durable abort/rerun/delete commands over shared mutation surface ports. */
export function createTaskModeRunCommandController(
  ports: TaskModeRunCommandPorts,
) {
  const captureOwner = (): unknown => ports.captureOwner?.() ?? null;
  const ownsOwner = (owner: unknown, runId?: unknown): boolean =>
    !ports.ownsOwner || ports.ownsOwner(owner, runId);
  const abortRun = async (runId: unknown): Promise<boolean> => {
    if (!runId) return false;
    const owner = captureOwner();
    const actions = ports.actions();
    const outcome = await ports.mutationCommand.execute({
      context: 'abortRun',
      fallback: ports.translate('tm.toast.abortFailed'),
      acceptAbsent: true,
      request: () => actions && typeof actions.abortRun === 'function'
        ? (actions.abortRun as (...values: unknown[]) => unknown).call(
          actions, runId, {
            message: ports.translate('tm.abort.confirm'),
            options: {
              title: ports.translate('tm.abort.confirmTitle'),
              okText: ports.translate('tm.btn.abort'), danger: true,
            },
          }) : null,
    });
    if (!outcome.attempted) return false;
    if (outcome.mutation) {
      ports.call('reconcileRun', outcome.mutation, runId);
    }
    if (!outcome.satisfied) {
      if (ownsOwner(owner, runId)) ports.toast(outcome.message, true, true);
      return false;
    }
    if (ownsOwner(owner, runId)) ports.toast('tm.toast.abort', false);
    return true;
  };

  const rerun = async (runValue: unknown): Promise<boolean> => {
    const run = runValue && typeof runValue === 'object'
      ? runValue as Port : null;
    if (!run?.id || !run.definition) return false;
    const sourceOwner = captureOwner();
    const actions = ports.actions();
    const result = actions && typeof actions.rerun === 'function'
      ? await (actions.rerun as (value: unknown) => unknown).call(actions, run)
      : null;
    if (!result) return false;
    const outcome = projectOrchestrationDurableStartOutcome(result);
    if (!ownsOwner(sourceOwner, run.id)) {
      if (outcome.targetRunId) ports.call('refreshRuns');
      return false;
    }
    if (!outcome.targetRunId) {
      ports.toast(
        outcome.error || ports.translate('tm.toast.rerunFailed'), true, true);
      return false;
    }
    const refresh = Promise.resolve(ports.call('refreshRuns'));
    const opening = Promise.resolve(ports.call('openRun', outcome.targetRunId));
    const targetOwner = captureOwner();
    const opened = await opening;
    await refresh;
    if (!ownsOwner(targetOwner, outcome.targetRunId)) return false;
    if (!opened) {
      ports.toast('tm.toast.rerunOpenFailed', true);
      return false;
    }
    if (!outcome.accepted) {
      ports.toast(
        outcome.error || ports.translate('tm.toast.rerunFailed'), true, true);
      return false;
    }
    ports.toast('tm.toast.rerun', false);
    return true;
  };

  const deleteRun = async (runId: unknown): Promise<boolean> => {
    if (!runId) return false;
    const owner = captureOwner();
    const actions = ports.actions();
    const outcome = await ports.mutationCommand.execute({
      context: 'deleteRun',
      fallback: ports.translate('tm.toast.deleteFailed'),
      acceptAbsent: true,
      request: () => actions && typeof actions.deleteRun === 'function'
        ? (actions.deleteRun as (...values: unknown[]) => unknown).call(
          actions, runId, {
            message: ports.translate('tm.delete.confirm'),
            options: {
              title: ports.translate('tm.delete.confirmTitle'),
              okText: ports.translate('tm.btn.delete'), danger: true,
            },
          }) : null,
    });
    if (!outcome.attempted) return false;
    if (!outcome.satisfied) {
      if (outcome.mutation) {
        ports.call('reconcileRun', outcome.mutation, runId);
      }
      if (ownsOwner(owner, runId)) {
        ports.toast(outcome.message, true, true);
      }
      return false;
    }
    ports.call('deleteAccepted', runId);
    ports.call('refreshRuns');
    return true;
  };

  return Object.freeze({ abortRun, rerun, deleteRun });
}

(orchestrationRegistry as unknown as TaskModeRunCommandWindow).createTaskModeRunCommandController =
  createTaskModeRunCommandController;
