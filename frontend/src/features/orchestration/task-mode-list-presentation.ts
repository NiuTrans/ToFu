import { orchestrationRegistry } from './registry';
type PresentationPort = Record<string, unknown>;
type RunRecord = Record<string, unknown>;

export interface TaskModeRunListPresentationOptions {
  runTime: PresentationPort;
  runStatus: PresentationPort;
  paging: PresentationPort;
  errorView: PresentationPort;
  escape?: (value: unknown) => unknown;
  translate?: (key: string, params?: Record<string, unknown>) => unknown;
  richCopy?: (value: unknown) => unknown;
  icon?: (name: string) => unknown;
}

type TaskModeRunListPresentationWindow = Window & {
  createTaskModeRunListPresentation?: typeof createTaskModeRunListPresentation;
};

const call = (port: PresentationPort, name: string, ...args: unknown[]): unknown => {
  const fn = port[name];
  return typeof fn === 'function'
    ? (fn as (...values: unknown[]) => unknown).apply(port, args) : undefined;
};

export function createTaskModeRunListPresentation(
  options: TaskModeRunListPresentationOptions,
) {
  const escape = (value: unknown): string => String(options.escape
    ? options.escape(value) : value == null ? '' : value);
  const translate = (key: string, params?: Record<string, unknown>): unknown =>
    options.translate ? options.translate(key, params) : key;
  const richCopy = (value: unknown): string => String(options.richCopy
    ? options.richCopy(value) : escape(value));
  const icon = (name: string): string => String(options.icon
    ? options.icon(name) || '' : '');
  const loadingMarkup = (): string => `<div class="tm-loading"><span class="tm-spin"></span>${
    escape(translate('tm.loading'))}</div>`;
  const emptyMarkup = (): string => `<div class="tm-state">${icon('rocket')
    }<div class="tm-state-title">${escape(translate('tm.empty.title'))
    }</div><div class="tm-state-sub">${richCopy(translate('tm.empty.sub'))
    }</div><button type="button" class="tm-btn tm-btn-primary tm-state-btn" data-tm-action="open-studio">${
    icon('layout')} ${escape(translate('tm.btn.openStudio'))}</button></div>`;
  const errorMarkup = (state: RunRecord, cached: boolean): string => String(
    call(options.errorView, 'markup', state, cached) || '');
  const pagingMarkup = (state: RunRecord, count: number): string => String(
    call(options.paging, 'markup', state, count) || '');
  const filteredEmptyMarkup = (
    state: RunRecord, runCount: number, filterValue: unknown,
  ): string => `${state.loadError ? errorMarkup(state, true) : ''
    }<div class="tm-state tm-filter-empty"><div class="tm-state-title">${escape(
    translate(filterValue === 'active'
      ? 'tm.filter.emptyActive' : 'tm.filter.emptyFinished'))
    }</div><button type="button" class="tm-btn tm-state-btn" data-tm-clear-filter>${escape(
    translate('tm.filter.showAll'))}</button>${pagingMarkup(state, runCount)}</div>`;
  const rowsMarkup = (state: RunRecord, visibleRuns: RunRecord[]): string =>
    `${state.loadError ? errorMarkup(state, true) : ''}${visibleRuns.map(
      (run, index) => {
        const active = run.id === state.activeRunId;
        const live = call(options.runStatus, 'isTerminal', run)
          ? '' : ' tm-run-live';
        const elapsed = call(options.runTime, 'duration', run);
        return `<button type="button" class="tm-run${active ? ' is-active' : ''
          }${live}" data-tm-run-index="${index}"${active
          ? ' aria-current="true"' : ''}><div class="tm-run-top"><span class="tm-run-name">${escape(
          run.name || translate('tm.unnamedFlow'))}</span>${String(call(
          options.runStatus, 'statusChip', run) || '')}</div><div class="tm-run-meta">${escape(
          call(options.runTime, 'relativeTime', run.created_at))}${elapsed
          ? `<span class="tm-run-dur">${escape(elapsed)}</span>` : ''
          }</div></button>`;
      }).join('')}${pagingMarkup(state, visibleRuns.length)}`;
  const project = (
    stateValue: RunRecord = {}, runsValue: RunRecord[] = [],
    visibleRunsValue: RunRecord[] = [], filterValue?: unknown,
  ) => {
    const state = stateValue ?? {};
    const runs = Array.isArray(runsValue) ? runsValue : [];
    const visibleRuns = Array.isArray(visibleRunsValue) ? visibleRunsValue : [];
    if (state.loadError && !runs.length) return Object.freeze({
      kind: 'error', ariaLive: 'polite', html: errorMarkup(state, false),
    });
    if (!runs.length) return Object.freeze({
      kind: 'empty', ariaLive: 'polite', html: emptyMarkup(),
    });
    if (!visibleRuns.length) return Object.freeze({
      kind: 'filtered-empty', ariaLive: 'polite',
      html: filteredEmptyMarkup(state, runs.length, filterValue),
    });
    return Object.freeze({
      kind: 'rows', ariaLive: 'off', html: rowsMarkup(state, visibleRuns),
    });
  };
  return Object.freeze({ loadingMarkup, project });
}

(orchestrationRegistry as unknown as TaskModeRunListPresentationWindow).createTaskModeRunListPresentation =
  createTaskModeRunListPresentation;
