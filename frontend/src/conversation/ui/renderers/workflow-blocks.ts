/**
 * Branch, queue and concluded goal-run DOM projections. Report loading remains
 * an injected port; queue and execution state stay authoritative.
 * Entry points: renderAutopilotRunNoticeBlock, renderBranchLaneHeader,
 * renderQueueStatusBlock, renderNativeQueueItem.
 * Dependencies: conversation-view-model, surface-contracts, ports, stable-dom,
 * localized-copy, text-blocks.
 */
import {
  type ConversationBlockViewModel,
  type ConversationQueueItemViewModel,
} from '../../presentation/conversation-view-model';
import type { ConversationSurfaceRenderers, ConversationQueueItemRenderContext } from '../surface-contracts';
import { createButton } from '../../../ui/button';
import type { ClassicConversationRendererPorts } from './ports';
import { directClassChild, preserveDetailsOpen } from './stable-dom';
import { textFor } from './localized-copy';
import { renderBackgroundCompletion } from './text-blocks';
import { renderGoalRecoveryNotice } from './goal-recovery-notice';

export function renderAutopilotRunNoticeBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'autopilot-run-notice' }>,
  ports: ClassicConversationRendererPorts,
): void {
  if (block.value.reason === 'worker_lost') {
    renderGoalRecoveryNotice(node, block.value, ports);
    return;
  }
  const labels: Record<Exclude<typeof block.value.reason, 'worker_lost'>, readonly [string, string]> = {
    yielded_to_human: ['autopilot.endedYielded',
      'Autopilot stood down — you sent a message, so it stopped here'],
    aborted_mid_vu: ['autopilot.endedAborted',
      'Autopilot stopped — the turn was cancelled while it was working'],
    superseded: ['autopilot.endedSuperseded',
      'Autopilot stood down — a newer turn took over this conversation'],
    budget_exhausted: ['autopilot.endedBudget',
      'Autopilot stopped early — it hit its turn budget (needs review)'],
    no_progress: ['autopilot.endedNoProgress',
      'Autopilot stopped early — it stopped making progress (needs review)'],
    stuck: ['autopilot.endedStuck',
      'Autopilot stopped early — it was repeating itself (needs review)'],
    stalled: ['autopilot.endedStalled',
      'Goal mode stopped — a full budget window passed with zero verified progress (needs review)'],
    vu_blocked: ['autopilot.endedBlocked',
      'Goal mode blocked — the verifier declared a genuine impasse (needs review)'],
  };
  const document = node.ownerDocument;
  const notice = document.createElement('div');
  notice.className = 'ap-run-notice';
  notice.dataset.apNotice = '1';
  notice.dataset.apRunId = block.value.runId;
  notice.dataset.apReason = block.value.reason;
  const label = document.createElement('span');
  label.className = 'ap-run-notice-label';
  const [key, fallback] = labels[block.value.reason];
  label.textContent = textFor(ports, key, fallback);
  notice.appendChild(label);
  /* The report panel shows for unsent replies (its historical home) and for
   * evidence-terminal reasons (stalled / vu_blocked), where the terminal
   * report IS the point. Content is the settings excerpt; when the durable
   * transition carries a longer full report, offer a lazy expander. */
  const reportReason = block.value.reason === 'stalled'
    || block.value.reason === 'vu_blocked';
  if ((block.value.unsent || reportReason) && block.value.content) {
    const details = document.createElement('details');
    details.className = 'ap-run-notice-unsent';
    details.open = preserveDetailsOpen(node, false);
    const summary = document.createElement('summary');
    summary.textContent = textFor(
      ports,
      block.value.unsent ? 'autopilot.unsentReply' : 'autopilot.runReport',
      block.value.unsent
        ? 'This reply was written but never sent to the conversation'
        : 'Run report',
    );
    const content = document.createElement('pre');
    content.className = 'ap-run-notice-text';
    content.textContent = block.value.content;
    details.append(summary, content);
    if (block.value.hasFullReport && ports.fetchAutopilotRunReport) {
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'ap-run-notice-full-report';
      more.textContent = textFor(
        ports, 'autopilot.showFullReport', 'Show full report');
      more.addEventListener('click', () => {
        if (!ports.fetchAutopilotRunReport) return;
        more.disabled = true;
        more.textContent = textFor(
          ports, 'autopilot.loadingFullReport', 'Loading full report…');
        ports.fetchAutopilotRunReport(block.value.runId)
          .then((result) => {
            const full = result && typeof result.report === 'string'
              ? result.report : '';
            if (full) {
              content.textContent = full;
              more.remove();
              return;
            }
            more.disabled = false;
            more.textContent = textFor(
              ports, 'autopilot.showFullReport', 'Show full report');
          })
          .catch(() => {
            more.disabled = false;
            more.textContent = textFor(
              ports, 'autopilot.showFullReport', 'Show full report');
          });
      });
      details.appendChild(more);
    }
    notice.appendChild(details);
  }
  node.className = 'conversation-block conversation-block--autopilot-run-notice';
  node.replaceChildren(notice);
}

export function renderBranchLaneHeader(
  node: HTMLElement,
  lane: Parameters<NonNullable<ConversationSurfaceRenderers['renderLaneHeader']>>[1],
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-lane-header branch-panel-header';
  node.replaceChildren();
  const toggle = node.ownerDocument.createElement('button');
  toggle.type = 'button';
  toggle.className = 'branch-panel-title conversation-branch-toggle';
  toggle.dataset.conversationAction = 'toggle-branch';
  toggle.setAttribute('aria-expanded', String(Boolean(lane.expanded)));
  const icon = node.ownerDocument.createElement('span');
  icon.className = 'branch-panel-icon';
  icon.textContent = lane.icon || '';
  const title = node.ownerDocument.createElement('span');
  title.className = 'branch-node-label';
  title.textContent = lane.title;
  const count = node.ownerDocument.createElement('span');
  count.className = 'branch-panel-count';
  count.textContent = textFor(ports, 'branch.userTurns',
    `${lane.humanTurnCount} user turns`, { n: lane.humanTurnCount });
  toggle.append(icon, title, count);
  node.appendChild(toggle);
  if (lane.live) {
    const stop = node.ownerDocument.createElement('button');
    stop.type = 'button';
    stop.className = 'branch-panel-stop';
    stop.dataset.conversationAction = 'stop-branch';
    stop.textContent = textFor(ports, 'branch.stop', 'Stop');
    node.appendChild(stop);
  }
  const remove = node.ownerDocument.createElement('button');
  remove.type = 'button';
  remove.className = 'branch-panel-delete';
  remove.dataset.conversationAction = 'delete-branch';
  remove.setAttribute('aria-label', textFor(
    ports, 'branch.deleteBranch', 'Delete branch',
  ));
  remove.textContent = textFor(ports, 'branch.delete', 'Delete');
  node.appendChild(remove);
}

/* Clock glyph — static SVG, no emoji/unicode icons (CLAUDE.md §3.4). */
const QUEUE_ITEM_ICON_SVG = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';

export function renderQueueStatusBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'queue-status' }>,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--queue-status';
  node.dataset.queueId = block.value.queueId;
  const status = node.ownerDocument.createElement('div');
  status.className = 'conversation-queue-status';
  const icon = node.ownerDocument.createElement('span');
  icon.className = 'queue-item-icon';
  icon.innerHTML = QUEUE_ITEM_ICON_SVG;
  const label = node.ownerDocument.createElement('span');
  label.className = 'conversation-queue-status__label';
  /* position 0 = the bound attempt proved this Turn is queued but its queue
   * row was not part of this read; say what is known without inventing a
   * position. */
  label.textContent = block.value.backgroundCommand
    ? textFor(ports, 'bgCommand.queued', 'Background command finished · delivery unconfirmed')
    : block.value.position < 1
    ? textFor(ports, 'queue.waitingChip', 'Queued · sends after this reply ends')
    : block.value.steerFallback
    ? textFor(ports, 'queue.steerFallbackChip',
      'Insert window closed · queued #{n}', { n: block.value.position })
    : `${textFor(ports, 'sidebar.queued', 'Queued')} #${
      block.value.position}`;
  if (block.value.steerFallback) {
    node.classList.add('conversation-block--queue-fallback');
    status.title = textFor(ports, 'queue.steerFallbackHint',
      'You chose "insert now", but this reply’s insert window had already '
      + 'closed; the message was queued and will be sent as a fresh turn '
      + 'after this reply ends.');
  }
  const cancel = node.ownerDocument.createElement('button');
  cancel.type = 'button';
  cancel.className = 'msg-action-btn queue-item-cancel';
  cancel.dataset.conversationAction = 'remove-queue';
  cancel.textContent = block.value.backgroundCommand
    ? textFor(ports, 'bgCommand.cancelDelivery', 'Cancel delivery')
    : textFor(ports, 'queue.cancelMsg', 'Cancel');
  status.append(icon, label, cancel);
  node.replaceChildren(status);
}

/** Shared status chrome only; each result remains in its Surface-owned queue row. */
function renderBackgroundQueueItem(
  node: HTMLElement,
  item: ConversationQueueItemViewModel,
  ports: ClassicConversationRendererPorts,
  context?: ConversationQueueItemRenderContext,
): void {
  const completion = item.source.backgroundCommand!;
  const { count, index } = context?.backgroundCommandGroup ?? { count: 1, index: 0 };
  const document = node.ownerDocument;
  if (!node.classList.contains('bgcmd-queue-item')) node.replaceChildren();
  node.className = 'conversation-queue-item message bgcmd-queue-item';
  node.dataset.bgcmdGroupEdge = count === 1 ? 'single'
    : index === 0 ? 'first' : index === count - 1 ? 'last' : 'middle';

  let header = directClassChild<HTMLElement>(node, 'bgcmd-queue-header');
  if (index === 0) {
    if (!header) {
      header = document.createElement('header');
      header.className = 'bgcmd-queue-header';
      const icon = document.createElement('span');
      icon.className = 'queue-item-icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.innerHTML = QUEUE_ITEM_ICON_SVG;
      const label = document.createElement('span');
      label.className = 'bgcmd-queue-label';
      header.append(icon, label);
      node.prepend(header);
    }
    const text = count > 1
      ? textFor(ports, 'bgCommand.queuedMany',
        '{count} background commands finished · delivery unconfirmed', { count })
      : textFor(ports, 'bgCommand.queued', 'Background command finished · delivery unconfirmed');
    const label = header.querySelector<HTMLElement>('.bgcmd-queue-label')!;
    if (label.textContent !== text) label.textContent = text;
  } else header?.remove();

  let row = directClassChild<HTMLElement>(node, 'bgcmd-queue-row');
  if (!row) {
    row = document.createElement('div');
    row.className = 'bgcmd-queue-row';
    const position = document.createElement('span');
    position.className = 'bgcmd-queue-position';
    const evidence = document.createElement('div');
    evidence.className = 'bgcmd-queue-evidence';
    const cancel = createButton({ label: '', variant: 'ghost', size: 'sm', wrap: true }, document);
    cancel.classList.add('bgcmd-queue-cancel');
    cancel.dataset.conversationAction = 'remove-queue';
    row.append(position, evidence, cancel);
    node.appendChild(row);
  }
  const position = row.querySelector<HTMLElement>('.bgcmd-queue-position')!;
  const positionText = item.position > 0 ? `#${item.position}` : '';
  if (position.textContent !== positionText) position.textContent = positionText;
  position.hidden = !positionText;
  const cancel = row.querySelector<HTMLButtonElement>('.bgcmd-queue-cancel')!;
  const cancelLabel = textFor(ports, 'bgCommand.cancelDelivery', 'Cancel delivery');
  if (cancel.textContent !== cancelLabel) cancel.textContent = cancelLabel;
  cancel.setAttribute('aria-label', `${cancelLabel}: ${completion.commandId}${positionText ? ` (${positionText})` : ''}`);
  renderBackgroundCompletion(row.querySelector<HTMLElement>('.bgcmd-queue-evidence')!, completion, ports);
}

export function renderNativeQueueItem(
  node: HTMLElement,
  item: ConversationQueueItemViewModel,
  ports: ClassicConversationRendererPorts,
  context?: ConversationQueueItemRenderContext,
): void {
  if (item.source.backgroundCommand) {
    renderBackgroundQueueItem(node, item, ports, context);
    return;
  }
  delete node.dataset.bgcmdGroupEdge;
  node.className = `conversation-queue-item message user-msg qsrc-${
    item.source.isPeerMessage
      ? (item.source.isPeerHuman ? 'operator' : 'agent')
      : (item.kind === 'workflow_step' ? 'workflow' : 'own')}${
    item.source.steerFallback ? ' queue-item--fallback' : ''}`;
  const document = node.ownerDocument;
  const header = document.createElement('header');
  header.className = 'message-header conversation-queue-item__header';
  const icon = document.createElement('span');
  icon.className = 'queue-item-icon';
  icon.innerHTML = QUEUE_ITEM_ICON_SVG;
  const label = document.createElement('span');
  label.className = 'message-role';
  label.textContent = item.source.steerFallback
    ? textFor(ports, 'queue.steerFallbackLabel',
      'Insert window closed · queued')
    : textFor(ports, 'sidebar.queued', 'Queued');
  if (item.source.steerFallback) {
    header.title = textFor(ports, 'queue.steerFallbackHint',
      'You chose "insert now", but this reply’s insert window had already '
      + 'closed; the message was queued and will be sent as a fresh turn '
      + 'after this reply ends.');
  }
  const number = document.createElement('span');
  number.className = 'queue-item-number';
  number.textContent = `#${item.position}`;
  header.append(icon, label, number);
  if (item.source.isPeerMessage && item.source.fromConv) {
    const source = document.createElement('span');
    source.className = 'queue-item-src queue-item-src-static';
    source.textContent = `${textFor(
      ports,
      item.source.isPeerHuman ? 'queue.fromOperator' : 'queue.fromConv',
      item.source.isPeerHuman ? 'from operator' : 'from',
    )} ${item.source.fromConv}`;
    header.appendChild(source);
  }
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'msg-action-btn queue-item-cancel';
  cancel.dataset.conversationAction = 'remove-queue';
  cancel.textContent = textFor(ports, 'queue.cancelMsg', 'Cancel');
  header.appendChild(cancel);
  const body = document.createElement('div');
  body.className = 'message-body conversation-queue-item__body';
  body.textContent = item.text || textFor(ports, 'queue.attachment', 'Attachment');
  if (item.text) body.title = item.text;
  const attachmentKinds = [
    item.source.hasImages ? 'img' : '',
    item.source.hasPdfs ? 'pdf' : '',
    item.source.hasAttachments ? 'media' : '',
    item.source.hasRefs ? 'ref' : '',
    item.source.hasQuotes ? 'quote' : '',
  ].filter(Boolean);
  if (attachmentKinds.length) {
    const attachments = document.createElement('span');
    attachments.className = 'queue-item-attachments';
    attachments.textContent = attachmentKinds.join(' · ');
    body.appendChild(attachments);
  }
  node.replaceChildren(header, body);
}
