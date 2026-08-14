import { orchestrationRegistry } from './registry';
import { createOrchestrationKeyedActionLock } from './action-lock';
import { projectOrchestrationActionState } from './action-state-view';
import { reportOrchestrationDiagnostic } from './diagnostic-report';

type Run = Record<string, unknown>;
export interface TaskModeRunTitleOptions {
  document?: Document; titleId?: string;
  escape?: (value: unknown) => unknown;
  translate?: (key: string, params?: Record<string, unknown>) => unknown;
  icon?: (name: string) => unknown;
  report?: (context: string, error: unknown) => unknown;
  statusChip?: (value: unknown) => unknown;
  isTerminal?: (run: unknown) => unknown;
  onRetry?: (runId: string) => unknown;
  onEdit?: (definitionId: unknown) => unknown;
  onDelete?: (runId: unknown) => unknown;
  onRerun?: (run: Run) => unknown;
  onAbort?: (runId: unknown) => unknown;
}
type TaskModeRunTitleWindow = Window & {
  createTaskModeRunTitleView?: typeof createTaskModeRunTitleView;
};

export function createTaskModeRunTitleView(options: TaskModeRunTitleOptions = {}) {
  let currentRunId = '';
  const actions = createOrchestrationKeyedActionLock();
  const doc = (): Document => options.document ?? document;
  const escape = (value: unknown): string => String(options.escape
    ? options.escape(value) : value == null ? '' : value);
  const translate = (key: string, params?: Record<string, unknown>): unknown =>
    options.translate ? options.translate(key, params) : key;
  const icon = (name: string): string => String(options.icon
    ? options.icon(name) || '' : '');
  const statusChip = (status: unknown): string => String(options.statusChip
    ? options.statusChip(status) || '' : '');
  const terminal = (run: unknown): boolean => Boolean(options.isTerminal
    ? options.isTerminal(run) : false);
  const setActionState = (head: Element | null, value: unknown): boolean => {
    if (!head) return false;
    const action = value && typeof value === 'object'
      ? value as Record<string, unknown> : {};
    const keys: Record<string, string> = {
      edit: 'tm.busy.edit', delete: 'tm.busy.delete', rerun: 'tm.busy.rerun',
      abort: 'tm.busy.abort', load: 'tm.loading',
    };
    return projectOrchestrationActionState({
      busyTargets: [head],
      controls: head.querySelectorAll('button') as unknown as
        ArrayLike<{ disabled: boolean }>,
      status: head.querySelector('[data-tm-action-state]') as
        (Element & { hidden: boolean }) | null,
      label: head.querySelector('[data-tm-action-state-label]'),
      statusText: String(translate(keys[String(action.name || '')]
        || 'tm.busy.action')),
    }, action).pending;
  };
  const bindAction = (
    head: Element, selector: string, actionName: string,
    callback: (() => unknown) | undefined, runId: string,
  ): void => {
    const button = head.querySelector(selector);
    if (!button || typeof callback !== 'function') return;
    button.addEventListener('click', () => {
      if (head.getAttribute('aria-busy') === 'true') return;
      const owner = actions.acquire(runId, actionName);
      if (!owner) return;
      setActionState(head, actions.snapshot(runId));
      let result: unknown;
      try { result = callback(); } catch (error: unknown) {
        result = Promise.reject(error);
      }
      Promise.resolve(result).catch((error: unknown) => {
        reportOrchestrationDiagnostic(
          options.report, 'title action', error);
      }).then(() => {
        actions.release(owner);
        if (currentRunId === runId) {
          setActionState(doc().getElementById(options.titleId || 'tmRunTitle'),
            actions.snapshot(runId));
        }
      });
    });
  };
  const render = (
    runValue: Run | null, emptyKey?: string, stateValue: Run = {},
  ): string => {
    const state = stateValue ?? {};
    const head = doc().getElementById(options.titleId || 'tmRunTitle');
    if (!head) return '';
    if (!runValue) {
      const retryId = String(state.retryId || '');
      const emptyMessage = state.message == null
        ? translate(emptyKey || 'tm.runNotFound') : state.message;
      currentRunId = retryId;
      const retry = retryId && typeof options.onRetry === 'function'
        ? ` <button type="button" class="tm-btn" data-tm-title-retry>${icon('loop')} <span>${escape(translate('tm.btn.retry'))}</span></button>` : '';
      head.innerHTML = `<div class="tm-empty"><span role="status" aria-live="polite" aria-atomic="true">${escape(emptyMessage)}</span>${retry}</div>`;
      setActionState(head, { pending: Boolean(state.busy), name: 'load' });
      bindAction(head, '[data-tm-title-retry]', 'load',
        () => options.onRetry?.(retryId), retryId);
      return head.innerHTML;
    }
    const run = runValue;
    const editButton = run.orch_id
      ? `<button type="button" class="tm-btn tm-btn-ghost" data-tm-title-edit title="${escape(translate('tm.btn.editStudio'))}" aria-label="${escape(translate('tm.btn.editStudio'))}">${icon('layout')} <span>${escape(translate('tm.btn.editStudio'))}</span></button>` : '';
    const actionButton = terminal(run)
      ? `<button type="button" class="tm-btn tm-btn-ghost" data-tm-title-rerun title="${escape(translate('tm.btn.rerun'))}" aria-label="${escape(translate('tm.btn.rerun'))}">${icon('loop')} <span>${escape(translate('tm.btn.rerun'))}</span></button><button type="button" class="tm-btn tm-btn-ghost tm-btn-danger" data-tm-title-delete title="${escape(translate('tm.delete.confirmTitle'))}" aria-label="${escape(translate('tm.btn.delete'))}">${icon('reject')} <span>${escape(translate('tm.btn.delete'))}</span></button>`
      : `<button type="button" class="tm-btn tm-btn-ghost" data-tm-title-abort title="${escape(translate('tm.abort.confirmTitle'))}" aria-label="${escape(translate('tm.btn.abort'))}">${icon('stop')} <span>${escape(translate('tm.btn.abort'))}</span></button>`;
    const runId = String(run.id == null ? '' : run.id);
    currentRunId = runId;
    const input = String(run.input == null ? '' : run.input);
    const context = `${runId ? `<span class="tm-title-run-id" title="${escape(runId)}"><span class="tm-title-context-label">${escape(translate('tm.run.id'))}</span><code>${escape(runId)}</code></span>` : ''}${input ? `<span class="tm-title-input"><span class="tm-title-context-label">${escape(translate('tm.run.input'))}</span><span class="tm-title-input-text">${escape(input.slice(0, 300))}</span>${input.length > 300 ? `<span class="tm-text-trunc">${escape(translate('tm.run.inputTruncated'))}</span>` : ''}</span>` : ''}`;
    head.innerHTML = `<div class="tm-title-row"><span class="tm-title-summary" role="status" aria-live="polite" aria-atomic="true"><span class="tm-title-name" role="heading" aria-level="2">${escape(run.name || translate('tm.unnamedFlow'))}</span>${statusChip(run)}</span><span class="tm-title-spacer"></span><span class="tm-action-state" data-tm-action-state role="status" aria-live="polite" aria-atomic="true" hidden><span class="tm-action-state-dot" aria-hidden="true"></span><span data-tm-action-state-label></span></span>${editButton}${actionButton}</div>${context ? `<div class="tm-title-context">${context}</div>` : ''}`;
    setActionState(head, actions.snapshot(runId));
    bindAction(head, '[data-tm-title-edit]', 'edit',
      () => options.onEdit?.(run.orch_id), runId);
    bindAction(head, '[data-tm-title-delete]', 'delete',
      () => options.onDelete?.(run.id), runId);
    bindAction(head, '[data-tm-title-rerun]', 'rerun',
      () => options.onRerun?.(run), runId);
    bindAction(head, '[data-tm-title-abort]', 'abort',
      () => options.onAbort?.(run.id), runId);
    return head.innerHTML;
  };
  return { render };
}
(orchestrationRegistry as unknown as TaskModeRunTitleWindow).createTaskModeRunTitleView =
  createTaskModeRunTitleView;
