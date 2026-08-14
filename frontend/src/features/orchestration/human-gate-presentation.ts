import { orchestrationRegistry } from './registry';
import { record } from './contracts';
import {
  orchestrationRequestLimitPolicy,
  type OrchestrationRequestLimitPolicy,
} from './request-limits';

export interface HumanGateProjection {
  readonly requestId: string;
  readonly row: HTMLDivElement;
  readonly input: HTMLTextAreaElement | null;
  readonly approve: HTMLButtonElement | null;
  readonly reject: HTMLButtonElement | null;
  readonly send: HTMLButtonElement | null;
}

export interface HumanGatePresentationOptions {
  document?: Document;
  translate?: (key: string, params?: Record<string, unknown>) => string;
  icon?: (name: string) => unknown;
  limitPolicy?: OrchestrationRequestLimitPolicy | unknown;
  requestLimits?: unknown;
}

type HumanGatePresentationWindow = Window & {
  createOrchestrationHumanGatePresentation?:
    typeof createOrchestrationHumanGatePresentation;
};

/** Safe Studio human-gate DOM projection without request ownership. */
export function createOrchestrationHumanGatePresentation(
  options: HumanGatePresentationOptions = {},
) {
  const limitPolicy = orchestrationRequestLimitPolicy(
    options.limitPolicy || options.requestLimits);
  const owner = (): Document => options.document ?? document;
  const translate = (
    key: string,
    params?: Record<string, unknown>,
  ): string => options.translate ? options.translate(key, params) : key;
  const icon = (name: string): string => String(
    options.icon ? options.icon(name) || '' : '');
  const build = (eventValue: unknown): HumanGateProjection => {
    const event = record(eventValue) ?? {};
    const doc = owner();
    const requestId = String(event.request_id || '');
    const row = doc.createElement('div');
    row.className = 'orch-run-line orch-human-gate';
    row.id = `orchHumanGate-${requestId}`;
    const iconBox = doc.createElement('span');
    iconBox.className = 'orch-human-icon';
    iconBox.innerHTML = icon('person');
    row.appendChild(iconBox);
    row.appendChild(doc.createTextNode(' '));
    const name = doc.createElement('b');
    name.textContent = String(event.name || translate('orch.gate.who'));
    row.appendChild(name);
    row.appendChild(doc.createTextNode(` — ${String(event.prompt
      || translate(event.mode === 'approve'
        ? 'orch.gate.approvePrompt' : 'orch.gate.inputPrompt'))}`));

    const actions = doc.createElement('div');
    actions.className = 'orch-human-actions';
    const actionState = doc.createElement('span');
    actionState.className = 'orch-inline-action-state';
    actionState.setAttribute('data-orch-gate-state', '');
    actionState.setAttribute('role', 'status');
    actionState.setAttribute('aria-live', 'polite');
    actionState.setAttribute('aria-atomic', 'true');
    actionState.hidden = true;
    const stateDot = doc.createElement('span');
    stateDot.className = 'orch-inline-action-state-dot';
    stateDot.setAttribute('aria-hidden', 'true');
    const stateLabel = doc.createElement('span');
    stateLabel.setAttribute('data-orch-gate-state-label', '');
    actionState.appendChild(stateDot);
    actionState.appendChild(stateLabel);

    let input: HTMLTextAreaElement | null = null;
    let approve: HTMLButtonElement | null = null;
    let reject: HTMLButtonElement | null = null;
    let send: HTMLButtonElement | null = null;
    if (event.mode === 'approve') {
      approve = doc.createElement('button');
      approve.type = 'button';
      approve.className = 'orch-btn orch-btn-run';
      approve.textContent = translate('orch.gate.approve');
      reject = doc.createElement('button');
      reject.type = 'button';
      reject.className = 'orch-btn orch-btn-danger';
      reject.textContent = translate('orch.gate.reject');
      actions.appendChild(approve);
      actions.appendChild(reject);
    } else {
      actions.classList.add('orch-human-input');
      input = doc.createElement('textarea');
      input.className = 'orch-input';
      input.rows = 3;
      input.placeholder = translate('orch.gate.inputPlaceholder');
      input.setAttribute('aria-label', translate('orch.gate.inputPlaceholder'));
      limitPolicy.applyHumanInput(input);
      send = doc.createElement('button');
      send.type = 'button';
      send.className = 'orch-btn orch-btn-primary';
      send.textContent = translate('orch.gate.send');
      actions.appendChild(input);
      actions.appendChild(send);
    }
    row.appendChild(actionState);
    row.appendChild(actions);
    return Object.freeze({ requestId, row, input, approve, reject, send });
  };

  return Object.freeze({ build });
}

(orchestrationRegistry as unknown as HumanGatePresentationWindow)
  .createOrchestrationHumanGatePresentation =
    createOrchestrationHumanGatePresentation;
