/**
 * Restart-interrupted Goal notice and one explicit recovery request.
 * Entry points: renderGoalRecoveryNotice, disposeGoalRecoveryNotice.
 * Dependencies: shared UI factories, injected authenticated resume port.
 * Durable execution/claim state remains server-owned; no polling or retries.
 */
import { createAlert } from '../../../ui/alert';
import { createButton } from '../../../ui/button';
import type { AutopilotRunNotice } from '../../presentation/view-model/types';
import type { ClassicConversationRendererPorts } from './ports';
import { textFor } from './localized-copy';

interface RecoveryNoticeState {
  value: AutopilotRunNotice;
  ports: ClassicConversationRendererPorts;
  phase: 'idle' | 'pending' | 'accepted' | 'refused' | 'unknown';
  error: string;
  button: HTMLButtonElement;
}
const notices = new WeakMap<HTMLElement, RecoveryNoticeState>();

export function disposeGoalRecoveryNotice(node: HTMLElement): void {
  // Detach the observer, never cancel a mutation which may already be accepted.
  notices.delete(node);
}

function paint(node: HTMLElement, state: RecoveryNoticeState): void {
  const text = (key: string, fallback: string) => textFor(state.ports, key, fallback);
  const bodies = {
    idle: ['goalRecovery.body', 'The service restarted during this Goal. Check saved progress and unfinished operations before continuing.'],
    pending: ['goalRecovery.checking', 'Checking recovery conditions…'],
    accepted: ['goalRecovery.accepted', 'Continuation accepted. Follow its progress in this conversation.'],
    refused: ['goalRecovery.refused', 'This Goal could not be resumed.'],
    unknown: ['goalRecovery.unknown', 'The response was not confirmed. The request may have been accepted; check the conversation before checking again.'],
  } as const;
  const [key, fallback] = bodies[state.phase];
  const focused = node.ownerDocument.activeElement === state.button;
  const alert = createAlert({
    variant: state.phase === 'accepted' ? 'info' : 'warning',
    title: text('goalRecovery.title', 'Goal interrupted by a service restart'),
    body: text(key, fallback),
  });
  alert.el.dataset.goalRecovery = state.phase;
  alert.el.dataset.apRunId = state.value.runId;
  if (state.error && state.phase === 'refused') {
    const detail = node.ownerDocument.createElement('div');
    detail.textContent = state.error;
    alert.el.appendChild(detail);
  }
  if (state.value.canRequestRecovery && state.ports.resumeGoalRun) {
    state.button.textContent = state.phase === 'pending'
      ? text('goalRecovery.checking', 'Checking recovery conditions…')
      : state.phase === 'accepted'
        ? text('goalRecovery.requestAccepted', 'Continuation accepted')
        : state.phase === 'unknown'
          ? text('goalRecovery.checkAgain', 'Check recovery again')
          : text('goalRecovery.resume', 'Check and resume');
    state.button.setAttribute('aria-disabled', String(
      state.phase === 'pending' || state.phase === 'accepted'));
    alert.el.appendChild(state.button);
  } else if (state.phase === 'idle') {
    const detail = node.ownerDocument.createElement('div');
    detail.textContent = text('goalRecovery.historical', 'This is an earlier interruption. Follow the latest turns in this conversation.');
    alert.el.appendChild(detail);
  }
  node.className = 'conversation-block conversation-block--autopilot-run-notice';
  node.replaceChildren(alert.el);
  if (focused && state.button.isConnected) state.button.focus();
}

async function requestRecovery(node: HTMLElement, state: RecoveryNoticeState): Promise<void> {
  if (notices.get(node) !== state || !state.value.canRequestRecovery
      || !state.ports.resumeGoalRun || state.phase === 'pending' || state.phase === 'accepted') return;
  state.phase = 'pending';
  state.error = '';
  paint(node, state);
  try {
    const result = await state.ports.resumeGoalRun(state.value.runId);
    if (notices.get(node) !== state) return;
    state.phase = result.ok && result.runId ? 'accepted'
      : result.status >= 400 && result.status < 500 ? 'refused' : 'unknown';
    state.error = result.error || '';
  } catch {
    if (notices.get(node) !== state) return;
    state.phase = 'unknown';
  }
  paint(node, state);
}

export function renderGoalRecoveryNotice(
  node: HTMLElement, value: AutopilotRunNotice, ports: ClassicConversationRendererPorts,
): void {
  let state = notices.get(node);
  if (!state || state.value.runId !== value.runId) {
    const next: RecoveryNoticeState = { value, ports, phase: 'idle', error: '',
      button: createButton({ label: '', variant: 'secondary', size: 'sm', wrap: true,
        onClick: () => { void requestRecovery(node, next); } }, node.ownerDocument) };
    state = next;
    notices.set(node, state);
  }
  state.value = value;
  state.ports = ports;
  paint(node, state);
}
