/**
 * Terminal unknown-usage continuation: one explicit human action runs the
 * read-only check and then confirms the server-proposed finite budget.
 * Entry: render/disposeUsageRecoveryNotice.
 * Dependencies: generated DTOs, injected renderer ports, shared UI factories.
 * No Turn writes, automatic authorization, old-request replay or Goal resume.
 *
 * The reader never types or interprets token/tool/time numbers: the server
 * proposes every limit, and the click itself is the authorization
 * (`confirmed: true` plus the exact previewed budget). Exact numbers stay in
 * a collapsed detail for operators; the visible sentence states the
 * consequences. Delivery uncertainty still reuses one frozen command.
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

type Phase = 'idle' | 'checking' | 'blocked' | 'check-failed'
  | 'changed' | 'submitting' | 'unknown' | 'refused' | 'accepted';
interface RecoveryState {
  binding: UsageRecoveryBinding;
  ports: ClassicConversationRendererPorts;
  api: UsageRecoveryPorts;
  phase: Phase;
  el: HTMLElement;
  continueButton: HTMLButtonElement;
  cancel: HTMLButtonElement;
  /** Only the read-only check; a submitted command is never aborted. */
  controller?: AbortController;
  observation?: ConversationUsageRecovery;
  /** Frozen only on explicit continuation; uncertain delivery always reuses it. */
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
      checking: ['usageRecovery.checking', 'Preparing to continue…'],
      submitting: ['usageRecovery.submitting', 'Starting the continuation…'],
      'check-failed': ['usageRecovery.checkFailed', 'The conversation could not be read. Nothing was started. Try again.'],
      changed: ['usageRecovery.changed', 'The conversation changed. Continue again to use what is saved now.'],
      unknown: ['usageRecovery.unknown', 'The response was not confirmed. The continuation may already have started; trying again reuses the same confirmed request.'],
      refused: ['usageRecovery.refused', 'The continuation was not accepted. Try again to use the current state.'],
      accepted: ['usageRecovery.accepted', 'Continuation started. Follow the new reply in this conversation; this does not confirm completion.'],
    };
    const blockedMessages = {
      not_applicable: ['usageRecovery.notApplicable', 'This record is no longer eligible for a new budget. Follow the latest replies in this conversation.'],
      operation_unconfirmed: ['usageRecovery.operationUnconfirmed', 'An earlier operation has an unconfirmed outcome. A new budget cannot be authorized while that outcome remains unresolved.'],
      configuration_unavailable: ['usageRecovery.configurationUnavailable', 'The model configuration is unavailable. Check the model connection in Settings, then continue again.'],
      ready: ['usageRecovery.checkFailed', 'The conversation could not be read. Nothing was started. Try again.'],
    } as const;
    const [key, fallback] = phase === 'blocked'
      ? blockedMessages[state.observation?.reason ?? 'ready'] : messages[phase];
    const alert = createAlert({
      variant: phase === 'accepted' ? 'info' : 'warning',
      title: text('usageRecovery.title', 'Continue generating'),
      body: text(key, fallback),
    });
    content.push(alert.el);
  }
  if (phase !== 'accepted') {
    for (const [key, fallback] of [
      ['usageRecovery.cost', 'Continuing may incur additional model and tool charges.'],
      ['usageRecovery.preserve', 'Everything generated so far and the earlier unconfirmed-usage record stay exactly as they are; nothing is filled in or settled.'],
      ['usageRecovery.scope', 'This starts a new reply in ordinary chat. It does not resend the earlier request or resume the earlier Goal or Flow.'],
    ]) {
      const line = document.createElement('div');
      line.textContent = text(key, fallback);
      content.push(line);
    }
  }
  const budget = state.command?.budget ?? state.observation?.budget;
  if (budget && ['checking', 'submitting', 'unknown'].includes(phase)) {
    // Exact limits remain inspectable, never a form the reader must judge.
    const numbers = document.createElement('details');
    numbers.dataset.usageRecoveryBudget = '';
    const summary = document.createElement('summary');
    summary.textContent = text('usageRecovery.budgetDetails', 'Limits chosen automatically');
    const value = document.createElement('div');
    value.textContent = textFor(state.ports, 'usageRecovery.budget',
      'Up to {maxTokens} tokens, {maxToolCalls} tool calls and {durationSeconds} seconds.', budget);
    numbers.append(summary, value);
    content.push(numbers);
  }
  const actions = document.createElement('div');
  actions.className = 'ui-row ui-row--wrap';
  const busy = phase === 'checking' || phase === 'submitting';
  /* These two blocks are permanent for this record: re-reading cannot change
   * them, so offering the action again would be the same dead control the
   * reader just came from. */
  const permanentlyBlocked = phase === 'blocked'
    && ['not_applicable', 'operation_unconfirmed'].includes(
      state.observation?.reason ?? '');
  state.continueButton.textContent = phase === 'unknown'
    ? text('usageRecovery.retry', 'Retry the same request')
    : phase === 'submitting'
      ? text('usageRecovery.submitting', 'Starting the continuation…')
      : phase === 'accepted'
        ? text('usageRecovery.requestAccepted', 'Continuation started')
        : text('usageRecovery.check', 'Continue generating');
  state.continueButton.setAttribute('aria-disabled', String(busy || phase === 'accepted'));
  if (!permanentlyBlocked) actions.append(state.continueButton);
  if (phase === 'checking') {
    state.cancel.textContent = text('usageRecovery.cancel', 'Cancel');
    actions.append(state.cancel);
  }
  content.push(actions);
  state.el.replaceChildren(...content);
  if (focused) {
    const target = focused.isConnected ? focused : state.continueButton;
    target.focus();
  }
}

async function deliver(node: HTMLElement, state: RecoveryState): Promise<void> {
  const command = state.command;
  if (!command) return;
  const { conversationId, turnId } = state.binding;
  state.phase = 'submitting';
  paint(state);
  try {
    const result = await state.api.recover(conversationId, turnId, command);
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
      // A definitive refusal consumed this authorization; the next attempt
      // must re-read the current source and propose its own budget.
      state.command = undefined;
      state.observation = undefined;
    }
  }
  if (!current(node, state)) return;
  paint(state);
  if (state.phase === 'accepted') state.api.onAccepted?.(conversationId);
}

/** One explicit gesture: read-only check, then the server's finite budget. */
async function startContinuation(node: HTMLElement, state: RecoveryState): Promise<void> {
  if (!current(node, state) || state.controller || state.command
      || ['checking', 'submitting', 'accepted'].includes(state.phase)) return;
  const controller = new AbortController();
  state.controller = controller;
  state.observation = undefined;
  state.phase = 'checking';
  paint(state);
  const { conversationId, turnId } = state.binding;
  let result: ConversationUsageRecovery | null = null;
  try {
    result = await state.api.inspect(conversationId, turnId, controller.signal);
  } catch {
    result = null;
  }
  if (!current(node, state) || controller.signal.aborted) return;
  state.controller = undefined;
  if (!result || result.ok !== true
      || result.conversationId !== conversationId || result.sourceTurnId !== turnId) {
    state.phase = 'check-failed';
    paint(state);
    return;
  }
  if (result.sourceProjectionRevision !== state.binding.projectionRevision) {
    state.phase = 'changed';
    paint(state);
    return;
  }
  state.observation = { ...result, budget: Object.freeze({ ...result.budget }) };
  if (result.state !== 'ready' || result.reason !== 'ready') {
    state.phase = 'blocked';
    paint(state);
    return;
  }
  if (controller.signal.aborted || !current(node, state)) return;
  state.command = Object.freeze({
    commandId: state.api.createCommandId(),
    expectedProjectionRevision: result.sourceProjectionRevision,
    confirmed: true,
    budget: Object.freeze({ ...result.budget }),
  });
  await deliver(node, state);
}

async function retryFrozen(node: HTMLElement, state: RecoveryState): Promise<void> {
  if (!current(node, state) || state.phase !== 'unknown' || !state.command) return;
  await deliver(node, state);
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
    const document = node.ownerDocument;
    const el = document.createElement('div');
    el.className = 'ui-stack ui-stack--tight ui-stack--bounded';
    const next: RecoveryState = {
      binding: { ...binding }, ports, api: ports.usageRecovery, phase: 'idle', el,
      continueButton: createButton({ label: '', variant: 'primary', size: 'sm', wrap: true,
        onClick: () => {
          if (next.phase === 'unknown') void retryFrozen(node, next);
          else void startContinuation(node, next);
        } }, document),
      cancel: createButton({ label: '', variant: 'ghost', size: 'sm', wrap: true,
        onClick: () => {
          if (!current(node, next) || next.phase !== 'checking') return;
          next.controller?.abort();
          next.controller = undefined;
          next.observation = undefined;
          next.phase = 'idle';
          paint(next);
          next.continueButton.focus();
        } }, document),
    };
    state = next;
    notices.set(node, state);
  }
  state.ports = ports;
  state.api = ports.usageRecovery;
  if (state.el.parentElement !== host) host.append(state.el);
  paint(state);
}
