import { orchestrationRegistry } from './registry';
type Callable = (...args: unknown[]) => unknown;
export interface OrchestrationStudioApiOptions extends Record<string, unknown> {
  open?: Callable;
  close?: Callable;
  refreshAuthoringContract?: Callable;
  loadDefinition?: Callable;
  toast?: Callable;
}
type StudioApiWindow = Window & {
  createOrchestrationStudioApi?: typeof createOrchestrationStudioApi;
};

/** Validated cross-feature Studio capability port. */
export function createOrchestrationStudioApi(
  options: OrchestrationStudioApiOptions = {},
) {
  const required = [
    'open', 'close', 'refreshAuthoringContract', 'loadDefinition', 'toast',
  ] as const;
  const missing = required.filter((name) => typeof options[name] !== 'function');
  if (missing.length) {
    throw new TypeError(
      `invalid orchestration Studio API; missing callable(s): ${missing.join(', ')}`,
    );
  }
  const call = (name: typeof required[number], ...args: unknown[]): unknown =>
    (options[name] as Callable)(...args);
  const open = (): unknown => call('open');
  const openDefinition = (definitionId: unknown): unknown => {
    const id = String(definitionId || '');
    if (!id) return open();
    // The blank-canvas bootstrap must not race an explicit stored read.
    call('open', { skipInitial: true });
    return call('loadDefinition', id);
  };
  return Object.freeze({
    open,
    openDefinition,
    close: (event: unknown, force: unknown) => call('close', event, force),
    refreshAuthoringContract: () => call('refreshAuthoringContract'),
    loadDefinition: (definitionId: unknown) =>
      call('loadDefinition', definitionId),
    toast: (message: unknown, isError: unknown, toastOptions: unknown) =>
      call('toast', message, isError, toastOptions),
  });
}

(orchestrationRegistry as unknown as StudioApiWindow).createOrchestrationStudioApi =
  createOrchestrationStudioApi;
