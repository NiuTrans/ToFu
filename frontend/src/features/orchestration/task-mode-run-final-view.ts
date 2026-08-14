import { orchestrationRegistry } from './registry';
import { projectOrchestrationFinalResult } from './outcome-result';

export interface TaskModeRunFinalOptions {
  document?: Document; finalId?: string;
  escape?: (value: unknown) => unknown;
  translate?: (key: string, params?: Record<string, unknown>) => unknown;
  projectFinal?: (value: unknown) => ReturnType<typeof projectOrchestrationFinalResult>;
  outcomeContract?: unknown;
}
type TaskModeRunFinalWindow = Window & {
  createTaskModeRunFinalView?: typeof createTaskModeRunFinalView;
};
export function createTaskModeRunFinalView(options: TaskModeRunFinalOptions = {}) {
  const doc = (): Document => options.document ?? document;
  const escape = (value: unknown): string => String(options.escape
    ? options.escape(value) : value == null ? '' : value);
  const translate = (key: string, params?: Record<string, unknown>): unknown =>
    options.translate ? options.translate(key, params) : key;
  const project = (value: unknown) => options.projectFinal
    ? options.projectFinal(value)
    : projectOrchestrationFinalResult(value, translate, options.outcomeContract);
  const clear = (): void => {
    const final = doc().getElementById(options.finalId || 'tmFinal');
    if (!final) return;
    final.style.display = 'none';
    final.setAttribute('aria-hidden', 'true');
    final.innerHTML = '';
  };
  const render = (run: unknown): string => {
    const final = doc().getElementById(options.finalId || 'tmFinal');
    if (!final) return '';
    final.setAttribute('role', 'region');
    final.setAttribute('aria-label', String(translate('tm.final.result')));
    final.tabIndex = 0;
    const projection = project(run || {});
    if (!projection.finalText && !projection.message) { clear(); return ''; }
    const sections: string[] = [];
    if (projection.finalText) sections.push(
      `<div class="tm-final-section"><div class="tm-final-label">${escape(translate(projection.partial ? 'tm.final.partial' : 'tm.final.result'))}${projection.finalTruncated ? ` <span class="tm-text-trunc">${escape(translate('tm.final.truncated'))}</span>` : ''}</div><pre class="tm-final-pre">${escape(projection.finalText)}</pre></div>`);
    if (projection.message) sections.push(
      `<div class="tm-final-section"><div class="tm-final-label">${escape(translate(projection.reasonKey))}${projection.messageTruncated ? ` <span class="tm-text-trunc">${escape(translate('tm.final.truncated'))}</span>` : ''}</div><pre class="tm-final-pre tm-final-error tm-final-${escape(projection.outcome.category)}">${escape(projection.message)}</pre></div>`);
    final.style.display = '';
    final.setAttribute('aria-hidden', 'false');
    final.innerHTML = sections.join('');
    return final.innerHTML;
  };
  return { clear, render };
}
(orchestrationRegistry as unknown as TaskModeRunFinalWindow).createTaskModeRunFinalView =
  createTaskModeRunFinalView;
