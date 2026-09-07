import { orchestrationRegistry } from './registry';
type Run = Record<string, unknown>;
type TaskModeRunStoreWindow = Window & {
  createTaskModeRunStore?: typeof createTaskModeRunStore;
};

export function createTaskModeRunStore() {
  let runs: Run[] = [];
  let selected: Run | null = null;
  let loadError = false;
  let loadFailure: unknown = null;
  let refreshing = false;
  let refreshGeneration = 0;
  const discardedIds: Record<string, boolean> = Object.create(null) as
    Record<string, boolean>;
  let pageLimit = 0;
  let hasMore = false;
  let nextLimit: number | null = null;
  const run = (value: unknown): Run | null => value
    && typeof value === 'object' && !Array.isArray(value)
    ? { ...value as Run } : null;
  const index = (runId: unknown): number => runs.findIndex(
    (entry) => entry?.id === runId);
  const lifecycleRegresses = (current: Run | null, next: Run | null): boolean =>
    current?.terminal === true && (next?.terminal !== true
      || String(next.status || '') !== String(current.status || ''));
  const beginRefresh = () => {
    refreshing = true;
    refreshGeneration += 1;
    return Object.freeze({ generation: refreshGeneration });
  };
  const invalidateRefresh = (): number => {
    refreshGeneration += 1;
    refreshing = false;
    return refreshGeneration;
  };
  const commitRefresh = (
    owner: { generation?: unknown } | null | undefined,
    resultValue: unknown,
  ): boolean => {
    if (!owner || owner.generation !== refreshGeneration) return false;
    refreshing = false;
    const result = resultValue && typeof resultValue === 'object'
      ? resultValue as Run : null;
    if (!result || result.ok !== true || !Array.isArray(result.runs)) {
      loadError = true;
      loadFailure = result || null;
      return true;
    }
    loadError = false;
    loadFailure = null;
    pageLimit = Number(result.pageLimit || 0);
    hasMore = result.hasMore === true;
    nextLimit = Number(result.nextLimit || 0) || null;
    runs = result.runs.map(run).filter((entry): entry is Run =>
      Boolean(entry && !discardedIds[String(entry.id)]));
    if (selected?.id) {
      const selectedIndex = index(selected.id);
      if (selectedIndex < 0) runs.unshift({ ...selected });
      else runs[selectedIndex] = { ...runs[selectedIndex], ...selected };
    }
    return true;
  };
  const adopt = (runValue: unknown, activeRunId: unknown): boolean => {
    const snapshot = run(runValue);
    if (!snapshot?.id || snapshot.id !== activeRunId) return false;
    const position = index(snapshot.id);
    const existing = selected?.id === snapshot.id
      ? selected : position < 0 ? null : runs[position];
    if (lifecycleRegresses(existing, snapshot)) return false;
    delete discardedIds[String(snapshot.id)];
    selected = snapshot;
    if (position < 0) runs.unshift({ ...snapshot });
    else runs[position] = { ...runs[position], ...snapshot };
    return true;
  };
  const updateLifecycle = (
    runId: unknown, status: unknown, terminal?: unknown,
  ): boolean => {
    if (!runId || !status) return false;
    const patch: Run = { status };
    if (typeof terminal === 'boolean') patch.terminal = terminal;
    const position = index(runId);
    const existing = selected?.id === runId
      ? selected : position < 0 ? null : runs[position];
    if (lifecycleRegresses(existing, patch)) return false;
    if (position >= 0) runs[position] = { ...runs[position], ...patch };
    if (selected?.id === runId) selected = { ...selected, ...patch };
    return position >= 0 || selected?.id === runId;
  };
  const remove = (runId: unknown): boolean => {
    const before = runs.length;
    runs = runs.filter((entry) => entry?.id !== runId);
    if (selected?.id === runId) selected = null;
    return runs.length !== before;
  };
  const discard = (runIdValue: unknown): boolean => {
    const runId = String(runIdValue || '');
    if (!runId) return false;
    discardedIds[runId] = true;
    remove(runId);
    return true;
  };
  const snapshot = (activeRunId: unknown) => ({
    runs: runs.slice(), activeRunId: activeRunId || null,
    loadError, loadFailure, refreshing, pageLimit, hasMore, nextLimit,
  });
  return Object.freeze({
    beginRefresh, invalidateRefresh, commitRefresh, adopt, updateLifecycle,
    discard, remove,
    clearSelection: () => { selected = null; },
    selected: () => selected,
    hasRows: () => runs.length > 0,
    find: (runId: unknown) => {
      const position = index(runId);
      return position < 0 ? null : runs[position];
    },
    snapshot,
  });
}

(orchestrationRegistry as unknown as TaskModeRunStoreWindow).createTaskModeRunStore =
  createTaskModeRunStore;
