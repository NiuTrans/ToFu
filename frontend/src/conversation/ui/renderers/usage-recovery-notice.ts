/**
 * Terminal unknown-usage recovery: a read-only check followed by explicit human
 * confirmation of one new budget. Entry: render/disposeUsageRecoveryNotice.
 * Dependencies: generated DTOs, injected renderer ports, shared UI factories.
 * No Turn writes, automatic authorization, old-request replay or Goal resume.
 */
import type { ConversationUsageRecovery, RecoverUsageRequest } from '../../../api/conversation-sync.generated';
import { apiFailure } from '../../../api/errors';
import { createAlert, createButton } from '../../../ui';
import type { ClassicConversationRendererPorts, UsageRecoveryPorts } from './ports';
import { textFor } from './localized-copy';

export interface UsageRecoveryBinding {
  conversationId: string;
  turnId: string;
  projectionRevision: number;
}

type Phase = 'idle' | 'checking' | 'ready' | 'blocked' | 'check-failed'
  | 'changed' | 'submitting' | 'unknown' | 'refused' | 'accepted';
interface RecoveryState {
  binding: UsageRecoveryBinding;
  ports: ClassicConversationRendererPorts;
  api: UsageRecoveryPorts;
  phase: Phase;
  el: HTMLElement;
  check: HTMLButtonElement;
  confirm: HTMLButtonElement;
  cancel: HTMLButtonElement;
  controller?: AbortController;
  observation?: ConversationUsageRecovery;
  /** Frozen only on explicit confirmation; uncertain delivery always reuses it. */
  command?: RecoverUsageRequest;
}
const notices = new WeakMap<HTMLElement, RecoveryState>();

export function disposeUsageRecoveryNotice(node: HTMLElement): void {
  const state = notices.get(node);
  notices.delete(node);
  state?.controller?.abort();
  state?.el.remove();
  // Never cancel a mutation which may already have been accepted.
}

function current(node: HTMLElement, state: RecoveryState): boolean {
  return notices.get(node) === state && node.isConnected;
}

function paint(state: RecoveryState): void {
  const document = state.el.ownerDocument;
  const focused = state.el.contains(document.activeElement)
    ? document.activeElement as HTMLElement : null;
  const text = (key: string, fallback: string) => textFor(state.ports, key, fallback);
  const phase = state.phase;
  state.el.dataset.usageRecovery = phase;
  state.el.setAttribute('aria-busy', String(phase === 'checking' || phase === 'submitting'));
  const content: Node[] = [];
  if (phase !== 'idle') {
    const messages: Record<Exclude<Phase, 'idle' | 'blocked'>, [string, string]> = {
      checking: ['usageRecovery.checking', 'Checking whether a new budget can be authorized…'],
      ready: ['usageRecovery.review', 'Review the new budget before continuing.'],
      'check-failed': ['usageRecovery.checkFailed', 'The check could not be completed. No new budget was authorized. Try again.'],
      changed: ['usageRecovery.changed', 'The conversation changed. Check again before confirming a new budget.'],
      submitting: ['usageRecovery.submitting', 'Submitting the confirmed budget…'],
      unknown: ['usageRecovery.unknown', 'The response was not confirmed. The request may already have been accepted. Retrying sends only the same confirmed request, not another authorization.'],
      refused: ['usageRecovery.refused', 'The request was not accepted. Check the current conditions again before confirming a new budget.'],
      accepted: ['usageRecovery.accepted', 'Continuation accepted. Follow the new reply in this conversation; this does not confirm completion.'],
    };
    const blockedMessages = {
      not_applicable: ['usageRecovery.notApplicable', 'This record is no longer eligible for a new budget. Follow the latest replies in this conversation.'],
      operation_unconfirmed: ['usageRecovery.operationUnconfirmed', 'An earlier operation has an unconfirmed outcome. A new budget cannot be authorized while that outcome remains unresolved.'],
      configuration_unavailable: ['usageRecovery.configurationUnavailable', 'The model configuration is unavailable. Check the model connection in Settings, then check again.'],
      ready: ['usageRecovery.checkFailed', 'The check could not be completed. No new budget was authorized. Try again.'],
    } as const;
    const [key, fallback] = phase === 'blocked'
      ? blockedMessages[state.observation?.reason ?? 'ready'] : messages[phase];
    const alert = createAlert({
      variant: phase === 'accepted' ? 'info' : 'warning',
      title: text('usageRecovery.title', 'Continue with a new budget'),
      body: text(key, fallback),
    });
    content.push(alert.el);
  }
  const budget = state.command?.budget ?? state.observation?.budget;
  if (budget && ['ready', 'submitting', 'unknown'].includes(phase)) {
    const numbers = document.createElement('div');
    numbers.dataset.usageRecoveryBudget = '';
    numbers.textContent = textFor(state.ports, 'usageRecovery.budget',
      'New budget: up to {maxTokens} tokens, {maxToolCalls} tool calls and {durationSeconds} seconds.', budget);
    content.push(numbers);
    for (const [key, fallback] of [
      ['usageRecovery.cost', 'Continuing may incur additional model and tool charges.'],
      ['usageRecovery.preserve', 'The earlier unknown-usage record and existing output will be preserved. This does not fill in or settle the missing usage.'],
      ['usageRecovery.scope', 'This starts a new reply in ordinary chat. It does not resend the earlier request or resume the earlier Goal or Flow.'],
    ]) {
      const line = document.createElement('div');
      line.textContent = text(key, fallback);
      content.push(line);
    }
  }
  const actions = document.createElement('div');
  actions.className = 'ui-row ui-row--wrap';
  if (['idle', 'checking', 'blocked', 'check-failed', 'changed', 'refused'].includes(phase)) {
    state.check.textContent = phase === 'checking'
      ? text('usageRecovery.checking', 'Checking whether a new budget can be authorized…')
      : text('usageRecovery.check', 'Check and continue');
    state.check.setAttribute('aria-disabled', String(phase === 'checking'));
    actions.append(state.check);
  }
  if (['ready', 'submitting', 'unknown', 'accepted'].includes(phase)) {
    state.confirm.textContent = phase === 'unknown'
      ? text('usageRecovery.retry', 'Retry the same request')
      : phase === 'submitting'
        ? text('usageRecovery.submitting', 'Submitting the confirmed budget…')
        : phase === 'accepted'
          ? text('usageRecovery.requestAccepted', 'Continuation accepted')
          : text('usageRecovery.confirm', 'Confirm and continue');
    state.confirm.setAttribute('aria-disabled', String(phase === 'submitting' || phase === 'accepted'));
    actions.append(state.confirm);
  }
  if (phase === 'checking' || phase === 'ready') {
    state.cancel.textContent = text('usageRecovery.cancel', 'Cancel');
    actions.append(state.cancel);
  }
  content.push(actions);
  state.el.replaceChildren(...content);
  if (focused) {
    const target = focused.isConnected ? focused
      : actions.contains(state.confirm) ? state.confirm : state.check;
    target.focus();
  }
}

async function check(node: HTMLElement, state: RecoveryState): Promise<void> {
  if (!current(node, state) || state.controller || state.command
      || ['ready', 'submitting', 'accepted'].includes(state.phase)) return;
  const controller = new AbortController();
  state.controller = controller;
  state.observation = undefined;
  state.phase = 'checking';
  paint(state);
  const { conversationId, turnId } = state.binding;
  try {
    const result = await state.api.inspect(conversationId, turnId, controller.signal);
    if (!current(node, state) || controller.signal.aborted) return;
    if (result.ok !== true || result.conversationId !== conversationId || result.sourceTurnId !== turnId) {
      throw new Error('Recovery check identity mismatch');
    }
    if (result.sourceProjectionRevision !== state.binding.projectionRevision) {
      state.phase = 'changed';
    } else {
      state.observation = { ...result, budget: Object.freeze({ ...result.budget }) };
      state.phase = result.state === 'ready' && result.reason === 'ready' ? 'ready' : 'blocked';
    }
  } catch {
    if (!current(node, state) || controller.signal.aborted) return;
    state.phase = 'check-failed';
  } finally {
    if (current(node, state) && state.controller === controller) {
      state.controller = undefined;
      paint(state);
    }
  }
}

async function confirm(node: HTMLElement, state: RecoveryState): Promise<void> {
  if (!current(node, state) || !['ready', 'unknown'].includes(state.phase)) return;
  if (!state.command) {
    const observation = state.observation;
    if (!observation || observation.sourceProjectionRevision !== state.binding.projectionRevision) return;
    state.command = Object.freeze({
      commandId: state.api.createCommandId(),
      expectedProjectionRevision: observation.sourceProjectionRevision,
      confirmed: true,
      budget: Object.freeze({ ...observation.budget }),
    });
  }
  state.phase = 'submitting';
  paint(state);
  const { conversationId, turnId } = state.binding;
  try {
    const result = await state.api.recover(conversationId, turnId, state.command);
    if (!current(node, state)) return;
    if (result.ok !== true || result.conversationId !== conversationId) {
      throw new Error('Recovery acknowledgement identity mismatch');
    }
    state.phase = 'accepted';
  } catch (error) {
    if (!current(node, state)) return;
    const { status } = apiFailure(error);
    const refused = status >= 400 && status < 500 && ![408, 429].includes(status);
    state.phase = refused ? 'refused' : 'unknown';
    if (refused) {
      state.command = undefined;
      state.observation = undefined;
    }
  }
  if (!current(node, state)) return;
  paint(state);
  if (state.phase === 'accepted') state.api.onAccepted?.(conversationId);
}

/** Reuses mounted controls through receipt repaint; revision changes invalidate
 * an unconfirmed check, never rewrite a possibly accepted command. */
export function renderUsageRecoveryNotice(
  node: HTMLElement,
  host: HTMLElement | null,
  binding: UsageRecoveryBinding | undefined,
  ports: ClassicConversationRendererPorts,
): void {
  if (!host || !binding || !ports.usageRecovery) {
    disposeUsageRecoveryNotice(node);
    return;
  }
  let state = notices.get(node);
  if (state && (state.binding.conversationId !== binding.conversationId
      || state.binding.turnId !== binding.turnId
      || (!state.command && state.binding.projectionRevision !== binding.projectionRevision))) {
    disposeUsageRecoveryNotice(node);
    state = undefined;
  }
  if (!state) {
    const el = node.ownerDocument.createElement('div');
    el.className = 'ui-stack ui-stack--tight ui-stack--bounded';
    const next: RecoveryState = {
      binding: { ...binding }, ports, api: ports.usageRecovery, phase: 'idle', el,
      check: createButton({ label: '', size: 'sm', wrap: true,
        onClick: () => { void check(node, next); } }, node.ownerDocument),
      confirm: createButton({ label: '', variant: 'primary', size: 'sm', wrap: true,
        onClick: () => { void confirm(node, next); } }, node.ownerDocument),
      cancel: createButton({ label: '', variant: 'ghost', size: 'sm', wrap: true,
        onClick: () => {
          if (!current(node, next) || !['checking', 'ready'].includes(next.phase)) return;
          next.controller?.abort();
          next.controller = undefined;
          next.observation = undefined;
          next.phase = 'idle';
          paint(next);
          next.check.focus();
        } }, node.ownerDocument),
    };
    state = next;
    notices.set(node, state);
  }
  state.ports = ports;
  state.api = ports.usageRecovery;
  if (state.el.parentElement !== host) host.append(state.el);
  paint(state);
}
