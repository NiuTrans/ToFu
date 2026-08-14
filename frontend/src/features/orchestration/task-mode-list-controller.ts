import { orchestrationRegistry } from './registry';
import {
  createOrchestrationRequestReader,
  type OrchestrationRequestReader,
} from './request-reader';

type Port = Record<string, unknown>;

export interface TaskModeRunListControllerOptions extends Record<string, unknown> {
  view?: Port | null;
  store: Port;
  client?: Port | (() => Port | null) | null;
  activeRunId?: unknown | (() => unknown);
  createView?: () => Port;
  projectActionState?: (action: { pending: boolean; name: string }) => unknown;
  report?: (context: string, result: unknown) => unknown;
  reader?: OrchestrationRequestReader;
}

type TaskModeRunListControllerWindow = Window & {
  createTaskModeRunListController?: typeof createTaskModeRunListController;
};

const invoke = (port: Port, name: string, ...args: unknown[]): unknown => {
  const fn = port[name];
  return typeof fn === 'function'
    ? (fn as (...values: unknown[]) => unknown).apply(port, args) : undefined;
};

export function createTaskModeRunListController(
  options: TaskModeRunListControllerOptions,
) {
  let view = options.view ?? null;
  let retainedLimit = 0;
  let pending: Promise<boolean> | null = null;
  let pendingLimit: number | undefined | null = null;
  const reader = options.reader ?? createOrchestrationRequestReader({
    client: options.client,
    report: options.report,
  });
  const activeRunId = (): unknown => typeof options.activeRunId === 'function'
    ? options.activeRunId() : options.activeRunId;
  const ensureView = (): Port => {
    if (view) return view;
    view = options.createView ? options.createView() : options.view ?? null;
    if (!view) throw new Error('Task Mode run list view is unavailable');
    return view;
  };
  const snapshot = (): Port => invoke(
    options.store, 'snapshot', activeRunId()) as Port;
  const setBusy = (loading: unknown, showPlaceholder?: unknown): unknown => {
    const projected = invoke(ensureView(), 'setBusy', loading, showPlaceholder);
    options.projectActionState?.({ pending: Boolean(loading), name: 'refresh' });
    return projected;
  };
  const render = (): unknown => invoke(ensureView(), 'render', snapshot());
  const performRefresh = async (requestedLimit?: number): Promise<boolean> => {
    const refreshOwner = invoke(options.store, 'beginRefresh');
    setBusy(true, !invoke(options.store, 'hasRows'));
    if (requestedLimit) retainedLimit = requestedLimit;
    const result = await reader.read('list', ['', '', requestedLimit]);
    if (!invoke(options.store, 'commitRefresh', refreshOwner, result)) return false;
    reader.report('list', result);
    const response = result;
    if (response?.ok === true && response.pageLimit) {
      retainedLimit = Number(response.pageLimit);
    }
    setBusy(false);
    render();
    return true;
  };
  const refresh = (limitValue?: unknown): Promise<boolean> => {
    const currentLimit = snapshot().pageLimit;
    const requestedLimit = Number(
      limitValue || retainedLimit || currentLimit || 0) || undefined;
    if (pending && pendingLimit === requestedLimit) return pending;
    const request = performRefresh(requestedLimit);
    pending = request;
    pendingLimit = requestedLimit;
    const release = (): void => {
      if (pending !== request) return;
      pending = null;
      pendingLimit = null;
    };
    request.then(release, release);
    return request;
  };
  const loadMore = (): Promise<boolean> => {
    const nextLimit = snapshot().nextLimit;
    return nextLimit ? refresh(nextLimit) : Promise.resolve(false);
  };
  const invalidate = (): boolean => {
    invoke(options.store, 'invalidateRefresh');
    pending = null;
    pendingLimit = null;
    setBusy(false);
    return true;
  };
  return Object.freeze({
    view: ensureView,
    setBusy,
    render,
    refresh,
    loadMore,
    invalidate,
    statusChip: (value: unknown) => invoke(ensureView(), 'statusChip', value),
    statusLabel: (value: unknown) => invoke(ensureView(), 'statusLabel', value),
    relativeTime: (value: unknown) => invoke(ensureView(), 'relativeTime', value),
    duration: (value: unknown) => invoke(ensureView(), 'duration', value),
  });
}

(orchestrationRegistry as unknown as TaskModeRunListControllerWindow).createTaskModeRunListController =
  createTaskModeRunListController;
