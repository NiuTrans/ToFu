import { orchestrationRegistry } from './registry';
export interface TaskModeListErrorOptions {
  escape?: (value: unknown) => unknown;
  translate?: (key: string) => unknown;
  icon?: (name: string) => unknown;
  failureMessage?: (failure: unknown) => unknown;
}

type TaskModeListErrorWindow = Window & {
  createTaskModeListErrorView?: typeof createTaskModeListErrorView;
};

export function createTaskModeListErrorView(
  options: TaskModeListErrorOptions = {},
) {
  const escape = (value: unknown): string => String(
    options.escape ? options.escape(value) : value == null ? '' : value);
  const translate = (key: string): unknown => options.translate
    ? options.translate(key) : key;
  const icon = (name: string): string => String(options.icon
    ? options.icon(name) || '' : '');
  const failureMessage = (failureValue: unknown): string => {
    if (typeof options.failureMessage === 'function') {
      return String(options.failureMessage(failureValue) || '');
    }
    const failure = failureValue && typeof failureValue === 'object'
      ? failureValue as Record<string, unknown> : {};
    return typeof failure.error === 'string' ? failure.error
      : typeof failure.reason === 'string' ? failure.reason : '';
  };
  const markup = (
    stateValue: Record<string, unknown> = {},
    cachedValue: unknown = false,
  ): string => {
    const state = stateValue ?? {};
    const cached = Boolean(cachedValue);
    const message = failureMessage(state.loadFailure);
    const refreshing = Boolean(state.refreshing);
    return `<div class="${cached ? 'tm-list-warning' : 'tm-state tm-state-err'
      }" role="status">${icon('warn')}<div class="tm-state-copy"><div class="tm-state-title">${escape(
      translate(cached ? 'tm.err.cachedTitle' : 'tm.err.title'))
      }</div><div class="tm-state-sub">${escape(translate(
      cached ? 'tm.err.cachedSub' : 'tm.err.sub'))}</div>${message
      ? `<div class="tm-state-reason">${escape(message)}</div>` : ''
      }</div><button type="button" class="tm-btn tm-state-btn" data-tm-action="refresh-runs"${
      refreshing ? ' disabled aria-disabled="true"' : ''}>${icon('loop')} ${escape(
      translate('tm.btn.retry'))}</button></div>`;
  };
  return Object.freeze({ markup });
}

(orchestrationRegistry as unknown as TaskModeListErrorWindow).createTaskModeListErrorView =
  createTaskModeListErrorView;
