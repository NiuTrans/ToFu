import { orchestrationRegistry } from './registry';
import { createOrchestrationRunFilter } from './run-filter';
import { createTaskModeRunTime } from './task-mode-run-time';
import { createTaskModeListFocusController } from './task-mode-list-focus';
import { createTaskModeListPaging } from './task-mode-list-paging';
import { createTaskModeListErrorView } from './task-mode-list-error';
import { createTaskModeRunStatusPresentation } from './task-mode-run-status-presentation';
import { createTaskModeRunListPresentation } from './task-mode-list-presentation';

type Port = Record<string, unknown>;
type Run = Record<string, unknown>;

export interface TaskModeRunListViewOptions extends Record<string, unknown> {
  document?: Document;
  hostId?: string;
  filterHostId?: string;
  escape?: (value: unknown) => unknown;
  translate?: (key: string, params?: Record<string, unknown>) => unknown;
  icon?: (name: string) => unknown;
  onOpen?: (runId: unknown) => unknown;
  onLoadMore?: () => unknown;
  listFocus?: ReturnType<typeof createTaskModeListFocusController>;
  runTime?: ReturnType<typeof createTaskModeRunTime>;
  runFilter?: ReturnType<typeof createOrchestrationRunFilter>;
  paging?: ReturnType<typeof createTaskModeListPaging>;
  errorView?: ReturnType<typeof createTaskModeListErrorView>;
  runStatus?: ReturnType<typeof createTaskModeRunStatusPresentation>;
  presentation?: ReturnType<typeof createTaskModeRunListPresentation>;
}

type TaskModeRunListWindow = Window & {
  createTaskModeRunListView?: typeof createTaskModeRunListView;
};

export function createTaskModeRunListView(
  options: TaskModeRunListViewOptions = {},
) {
  let lastState: Run | null = null;
  let lastVisibleActiveRunId: unknown = null;
  const doc = (): Document => options.document ?? document;
  const escape = (value: unknown): string => String(options.escape
    ? options.escape(value) : value == null ? '' : value);
  const translate = (key: string, params?: Record<string, unknown>): unknown =>
    options.translate ? options.translate(key, params) : key;
  const icon = (name: string): string => String(options.icon
    ? options.icon(name) || '' : '');
  const host = (): HTMLElement | null => doc().getElementById(options.hostId
    || 'tmRunList');
  const filterHost = (): HTMLElement | null => doc().getElementById(
    options.filterHostId || 'tmRunFilters');
  const listFocus = options.listFocus ?? createTaskModeListFocusController(options);
  const runTime = options.runTime ?? createTaskModeRunTime(options);
  const runFilter = options.runFilter ?? createOrchestrationRunFilter({
    isTerminal: options.isTerminal as ((value: unknown) => boolean) | undefined,
    runContract: options.runContract,
  });
  const paging = options.paging ?? createTaskModeListPaging({
    escape, icon, onLoadMore: options.onLoadMore, translate,
  });
  const errorView = options.errorView ?? createTaskModeListErrorView({
    escape,
    failureMessage: options.failureMessage as ((value: unknown) => unknown) | undefined,
    icon,
    translate,
  });
  const runStatus = options.runStatus ?? createTaskModeRunStatusPresentation({
    escape,
    isTerminal: options.isTerminal as ((value: unknown) => unknown) | undefined,
    normalizeOutcome: options.normalizeOutcome as
      ((value: unknown) => Record<string, unknown> | null) | undefined,
    outcomeMessage: options.outcomeMessage as
      ((value: unknown, fallback: string) => unknown) | undefined,
    runContract: options.runContract,
    translate,
  });
  const presentation = options.presentation ?? createTaskModeRunListPresentation({
    escape,
    translate,
    richCopy: options.richCopy as ((value: unknown) => unknown) | undefined,
    icon: options.icon as ((name: string) => unknown) | undefined,
    runTime: runTime as unknown as Port,
    runStatus: runStatus as unknown as Port,
    paging: paging as unknown as Port,
    errorView: errorView as unknown as Port,
  });
  function setFilter(value: unknown): string {
    const before = runFilter.value();
    const next = runFilter.select(value);
    if (before !== next && lastState) render(lastState);
    return next;
  }
  const syncFilters = (runs: Run[]): void => {
    const filters = filterHost();
    if (!filters) return;
    const counts = runFilter.counts(runs) as Readonly<Record<string, number>>;
    filters.querySelectorAll('[data-tm-run-filter]').forEach((button) => {
      const name = button.getAttribute('data-tm-run-filter') || '';
      button.setAttribute('aria-pressed',
        name === runFilter.value() ? 'true' : 'false');
      const count = button.querySelector('[data-tm-filter-count]');
      if (count) count.textContent = String(counts[name] || 0);
      if (button.getAttribute('data-tm-filter-bound') === 'true') return;
      button.setAttribute('data-tm-filter-bound', 'true');
      button.addEventListener('click', () => { setFilter(name); });
    });
  };
  const reveal = (run: unknown): boolean => runFilter.reveal(
    run && typeof run === 'object' ? run as Run : null);
  const setBusy = (loadingValue: unknown, placeholderValue?: unknown): void => {
    const list = host();
    if (!list) return;
    const loading = Boolean(loadingValue);
    list.setAttribute('aria-busy', loading ? 'true' : 'false');
    if (loading && placeholderValue) {
      list.setAttribute('aria-live', 'polite');
      list.innerHTML = presentation.loadingMarkup();
    }
  };
  function render(stateValue: Run = {}): string {
    const state = stateValue ?? {};
    const list = host();
    if (!list) return '';
    const focusedRunId = listFocus.capture(list);
    lastState = state;
    const runs = Array.isArray(state.runs) ? state.runs as Run[] : [];
    const previousVisibleActiveRunId = lastVisibleActiveRunId;
    lastVisibleActiveRunId = null;
    syncFilters(runs);
    list.setAttribute('aria-busy', 'false');
    const visibleRuns = runFilter.apply(runs) as Run[];
    const projected = presentation.project(
      state, runs, visibleRuns, runFilter.value());
    list.setAttribute('aria-live', projected.ariaLive);
    list.innerHTML = projected.html;
    if (projected.kind !== 'rows') {
      listFocus.clear();
      if (projected.kind === 'filtered-empty') {
        list.querySelector('[data-tm-clear-filter]')?.addEventListener(
          'click', () => { setFilter('all'); });
        paging.bind(list);
      }
      return list.innerHTML;
    }
    list.querySelectorAll('[data-tm-run-index]').forEach((button) => {
      const run = visibleRuns[Number(button.getAttribute('data-tm-run-index'))];
      if (!run) return;
      button.addEventListener('click', () => { options.onOpen?.(run.id); });
    });
    paging.bind(list);
    const activeButton = list.querySelector<HTMLElement>('[aria-current="true"]');
    listFocus.restore(
      list, visibleRuns.map((run) => run.id), focusedRunId, activeButton);
    if (activeButton && state.activeRunId !== previousVisibleActiveRunId
        && typeof activeButton.scrollIntoView === 'function') {
      activeButton.scrollIntoView({ block: 'nearest' });
    }
    if (activeButton) lastVisibleActiveRunId = state.activeRunId;
    return list.innerHTML;
  }
  const syncChip = (hostId: string, runOrStatus: unknown): boolean => {
    const chip = doc().getElementById(hostId)?.querySelector<HTMLElement>('.tm-chip');
    if (!chip) return false;
    const state = runStatus.statusPresentation(runOrStatus);
    chip.className = `tm-chip tm-chip-${state.token}`;
    chip.title = String(state.title);
    chip.textContent = String(state.label);
    return true;
  };
  return {
    duration: runTime.duration,
    relativeTime: runTime.relativeTime,
    render,
    reveal,
    setBusy,
    setFilter,
    statusChip: runStatus.statusChip,
    statusLabel: runStatus.statusLabel,
    statusPresentation: runStatus.statusPresentation,
    syncChip,
  };
}

(orchestrationRegistry as unknown as TaskModeRunListWindow).createTaskModeRunListView =
  createTaskModeRunListView;
