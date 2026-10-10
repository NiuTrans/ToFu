/**
 * Proposed-plan and plan-execution DOM projections. Markdown is injected;
 * decisions remain application commands.
 * Entry points: renderProposedPlanBlock, renderPlanExecutionBlock.
 * Dependencies: conversation-view-model, ports, stable-dom, icons,
 * localized-copy.
 */
import type { ConversationBlockViewModel } from '../../presentation/conversation-view-model';
import type { ClassicConversationRendererPorts } from './ports';
import { preserveDetailsOpen } from './stable-dom';
import { lineSvgIcon } from './icons';
import { textFor } from './localized-copy';

export function renderProposedPlanBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'proposed-plan' }>,
  ports: ClassicConversationRendererPorts,
): void {
  const document = node.ownerDocument;
  node.className = 'conversation-block conversation-block--proposed-plan';
  const card = document.createElement('article');
  card.className = 'plan-card';
  const header = document.createElement('header');
  header.className = 'plan-card-head';
  const icon = document.createElement('span');
  icon.className = 'plan-card-icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.appendChild(lineSvgIcon(document, [
    'M4 2.75h8a1 1 0 0 1 1 1v8.5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-8.5a1 1 0 0 1 1-1Z',
    'M5.5 6h5M5.5 8.5h5M5.5 11h3',
  ]));
  const title = document.createElement('span');
  title.className = 'plan-card-title';
  title.dataset.i18n = 'plan.cardTitle';
  title.textContent = textFor(ports, 'plan.cardTitle', 'Proposed Plan');
  header.append(icon, title);
  if (block.translationPending) {
    const status = document.createElement('span');
    status.className = 'plan-card-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    const statusDot = document.createElement('span');
    statusDot.className = 'plan-card-status-dot';
    statusDot.setAttribute('aria-hidden', 'true');
    const statusText = document.createElement('span');
    statusText.dataset.i18n = 'plan.translationPending';
    statusText.textContent = textFor(
      ports, 'plan.translationPending', 'Translating…',
    );
    status.append(statusDot, statusText);
    header.appendChild(status);
  }
  const body = document.createElement('div');
  body.className = 'plan-card-body md-content';
  body.dataset.translationState = block.translationStreaming
    ? 'streaming' : block.displayMode;
  body.innerHTML = ports.renderSafeMarkdownHtml(block.displayMarkdown);
  card.dataset.translationState = body.dataset.translationState;
  card.append(header, body);
  node.replaceChildren(card);
}

export function renderPlanExecutionBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'plan-execution' }>,
  ports: ClassicConversationRendererPorts,
): void {
  const document = node.ownerDocument;
  node.className = 'conversation-block conversation-block--plan-execution';
  const details = document.createElement('details');
  details.className = 'plan-execution-card';
  details.open = preserveDetailsOpen(node, false);
  const summary = document.createElement('summary');
  summary.className = 'plan-execution-head';
  const title = document.createElement('span');
  title.className = 'plan-execution-title';
  title.textContent = textFor(
    ports, 'plan.executionTitle', 'Executing accepted plan',
  );
  const context = document.createElement('span');
  context.className = 'plan-execution-context';
  context.textContent = block.value.contextMode === 'fresh'
    ? textFor(ports, 'plan.contextFresh', 'Fresh task context')
    : textFor(ports, 'plan.contextCurrent', 'Current context');
  summary.append(title, context);
  const body = document.createElement('div');
  body.className = 'plan-execution-body md-content';
  body.innerHTML = ports.renderSafeMarkdownHtml(block.value.planText);
  details.append(summary, body);
  node.replaceChildren(details);
}
