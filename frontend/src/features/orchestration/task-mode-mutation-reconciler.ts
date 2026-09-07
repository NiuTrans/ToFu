import { orchestrationRegistry } from './registry';
type Port = Record<string, unknown>;
export interface TaskModeMutationReconcilerOptions extends Record<string, unknown> {
  store?: Port | (() => Port | null) | null;
  activeRunId?: unknown | (() => unknown);
}
type TaskModeMutationReconcilerWindow = Window & {
  createTaskModeMutationReconciler?: typeof createTaskModeMutationReconciler;
};
export function createTaskModeMutationReconciler(
  options: TaskModeMutationReconcilerOptions = {},
) {
  const store = (): Port | null => {
    const result = typeof options.store === 'function'
      ? options.store() : options.store;
    return result ?? null;
  };
  const activeRunId = (): unknown => typeof options.activeRunId === 'function'
    ? options.activeRunId() : options.activeRunId;
  const call = (name: string, ...args: unknown[]): unknown => {
    const fn = options[name];
    return typeof fn === 'function'
      ? (fn as (...values: unknown[]) => unknown).apply(null, args) : undefined;
  };
  const reconcile = (mutationValue: unknown, runId: unknown): boolean => {
    const mutation = mutationValue && typeof mutationValue === 'object'
      ? mutationValue as Port : null;
    if (!mutation || !runId) return false;
    const runStore = store();
    if (mutation.targetExists === false) {
      if (activeRunId() === runId && typeof options.resetRun === 'function') {
        call('resetRun', runId);
      } else {
        const discard = runStore?.discard as
          ((id: unknown) => unknown) | undefined;
        discard?.call(runStore, runId);
        call('renderList');
      }
      return true;
    }
    let lifecycleConflict = false;
    if (mutation.resourceStatus && runStore) {
      const update = runStore.updateLifecycle as
        ((...args: unknown[]) => unknown) | undefined;
      const projected = update?.call(runStore, runId, mutation.resourceStatus,
        mutation.resourceTerminal);
      lifecycleConflict = projected === false;
      if (projected) {
        call('renderList');
        const selected = typeof runStore.selected === 'function'
          ? (runStore.selected as () => unknown).call(runStore) : null;
        if (activeRunId() === runId && selected) call('renderTitle', selected);
      }
    }
    const lifecycleUnknown = Boolean(mutation.resourceStatus)
      && typeof mutation.resourceTerminal !== 'boolean';
    if (!mutation.reconcileRequired && !lifecycleUnknown
        && !lifecycleConflict) return false;
    call('refreshRuns');
    if (activeRunId() === runId) call('resyncRun', runId);
    return true;
  };
  return Object.freeze({ reconcile });
}

(orchestrationRegistry as unknown as TaskModeMutationReconcilerWindow).createTaskModeMutationReconciler =
  createTaskModeMutationReconciler;
