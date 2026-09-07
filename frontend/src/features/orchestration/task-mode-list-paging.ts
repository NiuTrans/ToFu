import { orchestrationRegistry } from './registry';
export interface TaskModeListPagingOptions {
  escape?: (value: unknown) => unknown;
  translate?: (key: string, params?: Record<string, unknown>) => unknown;
  icon?: (name: string) => unknown;
  onLoadMore?: () => unknown;
}

type TaskModeListPagingWindow = Window & {
  createTaskModeListPaging?: typeof createTaskModeListPaging;
};

export function createTaskModeListPaging(options: TaskModeListPagingOptions = {}) {
  const escape = (value: unknown): string => String(
    options.escape ? options.escape(value) : value == null ? '' : value);
  const translate = (key: string, params?: Record<string, unknown>): unknown =>
    options.translate ? options.translate(key, params) : key;
  const icon = (name: string): string => String(options.icon
    ? options.icon(name) || '' : '');
  const markup = (
    stateValue: Record<string, unknown> = {},
    visibleCount = 0,
  ): string => {
    const state = stateValue ?? {};
    if (state.hasMore && state.nextLimit) {
      return `<div class="tm-run-page"><button type="button" class="tm-btn tm-run-more" data-tm-load-more>${
        icon('chevronDown')} ${escape(translate('tm.list.loadMore'))}</button></div>`;
    }
    return state.hasMore
      ? `<div class="tm-run-page tm-run-limit">${escape(translate(
        'tm.list.limitReached', { n: state.pageLimit || visibleCount }))}</div>`
      : '';
  };
  const bind = (root: Element | null): boolean => {
    const loadMore = root?.querySelector<HTMLButtonElement>('[data-tm-load-more]');
    if (!loadMore) return false;
    loadMore.addEventListener('click', () => {
      loadMore.disabled = true;
      options.onLoadMore?.();
    });
    return true;
  };
  return Object.freeze({ bind, markup });
}

(orchestrationRegistry as unknown as TaskModeListPagingWindow).createTaskModeListPaging =
  createTaskModeListPaging;
