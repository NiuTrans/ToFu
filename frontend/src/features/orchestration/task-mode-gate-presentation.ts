import { orchestrationRegistry } from './registry';
export interface TaskModeGateEvent extends Record<string, unknown> {
  mode?: unknown;
  prompt?: unknown;
  name?: unknown;
}

export interface TaskModeGatePresentationOptions {
  escape?: (value: unknown) => unknown;
  translate?: (key: string, params?: Record<string, unknown>) => unknown;
  icon?: (name: string) => unknown;
}

export interface TaskModeGateProjection {
  readonly gateIds: string[];
  readonly html: string;
}

type TaskModeGatePresentationWindow = Window & {
  createTaskModeGatePresentation?: typeof createTaskModeGatePresentation;
};

export function createTaskModeGatePresentation(
  options: TaskModeGatePresentationOptions = {},
) {
  const escape = (value: unknown): string => String(
    options.escape ? options.escape(value) : value == null ? '' : value);
  const translate = (key: string, params?: Record<string, unknown>): unknown =>
    options.translate ? options.translate(key, params) : key;
  const icon = (name: string): string => String(
    options.icon ? options.icon(name) || '' : '');
  const gateCard = (
    eventValue: TaskModeGateEvent = {},
    gateIndexValue: unknown = 0,
  ): string => {
    const event = eventValue ?? {};
    const gateIndex = typeof gateIndexValue === 'number' ? gateIndexValue : 0;
    const titleId = `tmGateTitle-${gateIndex}`;
    const promptId = `tmGatePrompt-${gateIndex}`;
    const prompt = event.prompt || (event.mode === 'approve'
      ? translate('orch.gate.approvePrompt')
      : translate('orch.gate.inputPrompt'));
    let html = `<div class="tm-gate-tag">${escape(
      translate('orch.gate.tag'))}</div><div class="tm-gate-head" id="${
      titleId}">${icon('person')} ${escape(event.name
      || translate('orch.gate.who'))}</div><div class="tm-gate-prompt" id="${
      promptId}">${escape(prompt)}</div><span class="orch-inline-action-state" data-orch-gate-state role="status" aria-live="polite" aria-atomic="true" hidden><span class="orch-inline-action-state-dot" aria-hidden="true"></span><span data-orch-gate-state-label></span></span>`;
    if (event.mode === 'approve') {
      html += `<div class="tm-gate-actions"><button type="button" class="tm-btn tm-btn-ok" data-tm-gate-decision="approve">${
        icon('check')} ${escape(translate('orch.gate.approve'))
        }</button><button type="button" class="tm-btn tm-btn-danger" data-tm-gate-decision="reject">${
        icon('reject')} ${escape(translate('orch.gate.reject'))}</button></div>`;
    } else {
      const placeholder = escape(translate('orch.gate.inputPlaceholder'));
      html += `<div class="tm-gate-actions tm-gate-input"><textarea class="tm-gate-field" data-tm-gate-input rows="3" aria-label="${
        placeholder}" placeholder="${placeholder}"></textarea><button type="button" class="tm-btn tm-btn-primary" data-tm-gate-send>${escape(
        translate('orch.gate.send'))}</button></div>`;
    }
    return `<div class="tm-gate-card" data-tm-gate-index="${gateIndex
      }" role="group" aria-labelledby="${titleId}" aria-describedby="${
      promptId}">${html}</div>`;
  };
  const project = (
    gatesValue: Record<string, TaskModeGateEvent> = {},
  ): Readonly<TaskModeGateProjection> => {
    const gates = gatesValue && typeof gatesValue === 'object'
      ? gatesValue : {};
    const gateIds = Object.keys(gates);
    return Object.freeze({
      gateIds,
      html: gateIds.map((requestId, index) =>
        gateCard(gates[requestId] ?? {}, index)).join(''),
    });
  };
  return Object.freeze({ gateCard, project });
}

(orchestrationRegistry as unknown as TaskModeGatePresentationWindow).createTaskModeGatePresentation =
  createTaskModeGatePresentation;
