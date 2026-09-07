/** Inline decision renderer for one executable proposed plan.
 *
 * ConversationSurface supplies the source Turn's owned mount immediately
 * after its plan blocks. This component owns only that mount's controls and
 * briefly disables them while the exact execute command is being accepted.
 */
import type {
  PlanDecisionViewModel,
} from '../presentation/conversation-view-model';

export type PlanExecutionContextMode = 'current' | 'fresh';

export interface PlanDecisionBarCopy {
  title: string;
  description: string;
  continueDiscussion: string;
  executeCurrent: string;
  executeFresh: string;
  executing: string;
  freshHint: string;
}

export type PlanDecisionBarCopyKey =
  | 'plan.readyTitle'
  | 'plan.readyDescription'
  | 'plan.continueDiscussion'
  | 'plan.executeCurrent'
  | 'plan.executeFresh'
  | 'plan.executing'
  | 'plan.freshHint';

export interface PlanDecisionBarOptions {
  copy?(): PlanDecisionBarCopy;
  translate?(key: PlanDecisionBarCopyKey): string;
  onContinueDiscussion?(
    conversationId: string,
    decision: PlanDecisionViewModel,
  ): void;
  onExecute(
    conversationId: string,
    decision: PlanDecisionViewModel,
    contextMode: PlanExecutionContextMode,
  ): Promise<void>;
  onError?(error: unknown, conversationId: string): void;
}

export interface PlanDecisionBar {
  activateConversation(conversationId: string | null): void;
  render(
    node: HTMLElement,
    conversationId: string,
    decision: PlanDecisionViewModel,
  ): void;
  dispose(): void;
}

interface ConversationPlanDecision {
  conversationId: string;
  decision: PlanDecisionViewModel;
}

interface PlanDecisionSubmission {
  token: number;
  conversationId: string;
  decisionKey: string;
  contextMode: PlanExecutionContextMode;
}

function defaultCopy(
  translate?: PlanDecisionBarOptions['translate'],
): PlanDecisionBarCopy {
  const localized = (key: PlanDecisionBarCopyKey, fallback: string): string => {
    const value = translate?.(key);
    return value && value !== key ? value : fallback;
  };
  return {
    title: localized('plan.readyTitle', 'Your plan is ready'),
    description: localized(
      'plan.readyDescription',
      'Keep refining, or choose the context used for execution.',
    ),
    continueDiscussion: localized(
      'plan.continueDiscussion', 'Keep refining',
    ),
    executeCurrent: localized(
      'plan.executeCurrent', 'Run in this chat',
    ),
    executeFresh: localized(
      'plan.executeFresh', 'Run in a new task',
    ),
    executing: localized('plan.executing', 'Starting…'),
    freshHint: localized(
      'plan.freshHint',
      'Starts from the accepted plan only. This chat remains unchanged.',
    ),
  };
}

/** Create one stable decision bar; callers provide all ambient browser ports. */
export function createPlanDecisionBar(
  options: PlanDecisionBarOptions,
): PlanDecisionBar {
  let root: HTMLElement | null = null;
  let activeConversationId: string | null = null;
  let current: ConversationPlanDecision | null = null;
  let submission: PlanDecisionSubmission | null = null;
  let nextSubmissionToken = 0;

  const decisionKey = (decision: PlanDecisionViewModel): string => JSON.stringify([
    decision.sourceTurnId,
    decision.planId,
    decision.sourceProjectionRevision,
  ]);

  const currentSubmission = (): PlanDecisionSubmission | null => {
    if (!current || !submission) return null;
    return submission.conversationId === current.conversationId
        && submission.decisionKey === decisionKey(current.decision)
      ? submission : null;
  };

  const attachRoot = (node: HTMLElement): void => {
    if (root === node) return;
    root?.removeEventListener('click', onClick);
    root = node;
    root.className = 'plan-decision-bar';
    root.dataset.planDecisionBar = 'true';
    root.setAttribute('role', 'region');
    root.setAttribute('aria-live', 'polite');
    root.addEventListener('click', onClick);
  };

  const actionButton = (
    document: Document,
    action: 'continue' | PlanExecutionContextMode,
    label: string,
    className: string,
    copyKey: PlanDecisionBarCopyKey,
  ): HTMLButtonElement => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    button.dataset.planDecisionAction = action;
    button.dataset.i18n = copyKey;
    button.disabled = Boolean(currentSubmission() || current?.decision.pending);
    button.textContent = label;
    return button;
  };

  const paint = (): void => {
    const node = root;
    if (!node) return;
    if (!current) {
      node.hidden = true;
      node.replaceChildren();
      return;
    }
    node.hidden = false;
    const document = node.ownerDocument;
    const copy = options.copy?.() ?? defaultCopy(options.translate);
    const activeSubmission = currentSubmission();
    const pending = Boolean(activeSubmission || current.decision.pending);
    node.dataset.conversationId = current.conversationId;
    node.dataset.planId = current.decision.planId;
    node.dataset.pending = String(pending);
    node.setAttribute('aria-label', copy.title);
    node.dataset.i18nAriaLabel = 'plan.readyTitle';
    node.setAttribute('aria-busy', String(pending));

    const indicator = document.createElement('span');
    indicator.className = 'plan-decision-indicator';
    indicator.setAttribute('aria-hidden', 'true');
    const indicatorIcon = document.createElement('span');
    indicatorIcon.className = 'plan-decision-indicator-icon';
    if (!pending) {
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('viewBox', '0 0 16 16');
      svg.setAttribute('width', '16');
      svg.setAttribute('height', '16');
      svg.setAttribute('fill', 'none');
      svg.setAttribute('aria-hidden', 'true');
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'm3.25 8.2 3 3 6.5-6.5');
      path.setAttribute('stroke', 'currentColor');
      path.setAttribute('stroke-width', '1.7');
      path.setAttribute('stroke-linecap', 'round');
      path.setAttribute('stroke-linejoin', 'round');
      svg.appendChild(path);
      indicatorIcon.appendChild(svg);
    }
    indicator.appendChild(indicatorIcon);

    const message = document.createElement('div');
    message.className = 'plan-decision-copy';
    const title = document.createElement('strong');
    title.className = 'plan-decision-title';
    title.dataset.i18n = 'plan.readyTitle';
    title.textContent = copy.title;
    const description = document.createElement('span');
    description.className = 'plan-decision-description';
    description.dataset.i18n = 'plan.readyDescription';
    description.textContent = copy.description;
    message.append(title, description);

    const actions = document.createElement('div');
    actions.className = 'plan-decision-actions';
    const continueButton = actionButton(
      document, 'continue', copy.continueDiscussion,
      'plan-decision-button plan-decision-button--quiet',
      'plan.continueDiscussion',
    );
    const currentButton = actionButton(
      document, 'current',
      activeSubmission?.contextMode === 'current'
        ? copy.executing : copy.executeCurrent,
      'plan-decision-button plan-decision-button--primary',
      activeSubmission?.contextMode === 'current'
        ? 'plan.executing' : 'plan.executeCurrent',
    );
    const freshButton = actionButton(
      document, 'fresh',
      activeSubmission?.contextMode === 'fresh'
        ? copy.executing : copy.executeFresh,
      'plan-decision-button plan-decision-button--secondary',
      activeSubmission?.contextMode === 'fresh'
        ? 'plan.executing' : 'plan.executeFresh',
    );
    freshButton.title = copy.freshHint;
    freshButton.dataset.i18nTitle = 'plan.freshHint';
    freshButton.setAttribute(
      'aria-label', `${copy.executeFresh}. ${copy.freshHint}`,
    );
    freshButton.dataset.i18nAriaLabel = 'plan.executeFreshAria';
    actions.append(continueButton, freshButton, currentButton);
    node.replaceChildren(indicator, message, actions);
  };

  async function onClick(event: Event): Promise<void> {
    const target = event.target instanceof Element
      ? event.target.closest<HTMLElement>('[data-plan-decision-action]') : null;
    const action = target?.dataset.planDecisionAction;
    if (!current || currentSubmission() || current.decision.pending || !action) return;
    if (current.conversationId !== activeConversationId) {
      current = null;
      submission = null;
      paint();
      return;
    }
    const accepted = current;
    if (action === 'continue') {
      options.onContinueDiscussion?.(
        accepted.conversationId,
        accepted.decision,
      );
      return;
    }
    if (action !== 'current' && action !== 'fresh') return;
    const token = ++nextSubmissionToken;
    submission = {
      token,
      conversationId: accepted.conversationId,
      decisionKey: decisionKey(accepted.decision),
      contextMode: action,
    };
    paint();
    try {
      await options.onExecute(
        accepted.conversationId,
        accepted.decision,
        action,
      );
    } catch (error) {
      options.onError?.(error, accepted.conversationId);
    } finally {
      if (submission?.token === token) {
        submission = null;
        paint();
      }
    }
  }

  return {
    activateConversation(conversationId) {
      if (conversationId === activeConversationId) return;
      if (root) {
        root.hidden = true;
        root.replaceChildren();
        root.removeEventListener('click', onClick);
        root = null;
      }
      activeConversationId = conversationId;
      current = null;
      submission = null;
    },
    render(node, conversationId, decision) {
      if (conversationId !== activeConversationId) {
        node.hidden = true;
        node.replaceChildren();
        return;
      }
      attachRoot(node);
      const previousKey = current ? decisionKey(current.decision) : '';
      current = { conversationId, decision };
      if (decisionKey(decision) !== previousKey) submission = null;
      paint();
    },
    dispose() {
      root?.removeEventListener('click', onClick);
      root?.remove();
      root = null;
      activeConversationId = null;
      current = null;
      submission = null;
    },
  };
}
