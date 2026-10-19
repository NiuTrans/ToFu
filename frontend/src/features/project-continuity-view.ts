/** Small view composition helpers shared only by coordination drawer surfaces. */
import { createField, createAlert, createButton, type ButtonOptions } from '../ui';
export const createContinuityButton = (options: ButtonOptions) => createButton({ ...options, wrap: true });
import { t } from '../i18n';
import type { ProjectDispatchPorts } from './project-dispatch';
import type { ProjectScopeChangeSource } from '../api/project-continuity.generated';
export const text = (value: string, tag = 'p') => {
  const node = document.createElement(tag); node.textContent = value;
  node.className = 'ui-text-block'; return node;
};
export const stack = (tag = 'div', card = false) => {
  const node = document.createElement(tag); node.className = card ? 'ui-card ui-stack ui-stack--bounded' : 'ui-stack ui-stack--bounded'; return node;
};
export function field(host: HTMLElement, name: string, label: string, options: {
  value?: string; number?: boolean; choices?: { value: string; label: string }[];
} = {}) {
  const handle = createField({ label, value: options.value, inputType: options.number ? 'number' : 'text', choices: options.choices });
  const control = handle.control as HTMLInputElement | HTMLSelectElement;
  control.name = name; host.append(handle.el); return control;
}
export function sourceField(host: HTMLElement, humanTurns: ProjectDispatchPorts['humanTurns'], conversationId: string, name: string) {
  const turns = humanTurns().filter(turn => turn.actor === 'human' && turn.projectionRevision > 0).slice(-128);
  const control = field(host, name, t('projectDispatch.sourceTurn'), { choices: [
    { value: '', label: t('projectContinuity.selectSource') },
    ...turns.map(turn => ({ value: turn.turnId,
      label: `${turn.turnId} · r${turn.projectionRevision}${turn.promptPreview ? ' · ' + turn.promptPreview : ''}` })),
  ] });
  return { control, read(): ProjectScopeChangeSource | null {
    const known = turns.find(turn => turn.turnId === control.value);
    const current = humanTurns().find(turn => turn.actor === 'human' && turn.turnId === known?.turnId
      && turn.projectionRevision === known.projectionRevision);
    return current ? { conversationId, turnId: current.turnId, projectionRevision: current.projectionRevision } : null;
  } };
}
export function showError(host: HTMLElement, reason: unknown) {
  host.replaceChildren(createAlert({ variant: 'error', title: t('projectCoordination.error'),
    body: reason instanceof Error ? reason.message : String(reason) }).el);
}
export function positiveInteger(control: HTMLInputElement | HTMLSelectElement, max: number): number | null {
  const n = Number(control.value); return Number.isSafeInteger(n) && n > 0 && n <= max ? n : null;
}
