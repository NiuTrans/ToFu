/**
 * Origin and context fold/rail DOM projections. Reads typed metadata and
 * injected safe fragments; no durable state. The context facts themselves
 * are rendered by the single rail owner, ../turn-context-rail.
 * Entry points: renderOriginBlock, renderContextBlock, renderContextRail.
 * Dependencies: conversation-view-model, ports, localized-copy, turn-chrome,
 * turn-context-rail.
 */
import type { ConversationBlockViewModel } from '../../presentation/conversation-view-model';
import type { ClassicConversationRendererPorts } from './ports';
import { textFor } from './localized-copy';
import { initiatorRoleLabel } from './turn-chrome';
import { renderTurnContextRailHtml } from '../turn-context-rail';

export function renderOriginBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'origin' }>,
  ports: ClassicConversationRendererPorts,
): void {
  const origin = block.value;
  const document = node.ownerDocument;
  node.className = 'conversation-block conversation-block--origin';
  node.replaceChildren();
  if (origin.initiator === 'peer' || origin.initiator === 'operator') {
    const banner = document.createElement('div');
    banner.className = `peer-msg-banner${
      origin.initiator === 'operator' ? ' peer-msg-banner-operator' : ''}`;
    const label = document.createElement('span');
    label.className = 'peer-msg-text';
    label.textContent = textFor(
      ports,
      origin.initiator === 'operator' ? 'peer.operatorBanner' : 'peer.messageBanner',
      origin.initiator === 'operator'
        ? 'Message from the project operator' : 'Message from a peer conversation',
    );
    if (origin.sourceConversationId) {
      const source = document.createElement('button');
      source.type = 'button';
      source.className = 'peer-msg-from conversation-origin-link';
      source.dataset.conversationAction = 'open-conversation';
      source.dataset.operation = origin.sourceConversationId;
      source.textContent = `conv ${origin.sourceConversationId.slice(0, 8)}`;
      label.append(' ', source);
    }
    banner.appendChild(label);
    node.appendChild(banner);
    return;
  }
  if (origin.initiator === 'brain' && origin.brain) {
    const card = document.createElement('div');
    card.className = 'brain-dispatch-card';
    const heading = document.createElement('div');
    heading.className = 'bdc-head';
    const title = document.createElement('span');
    title.className = 'bdc-title-label';
    title.textContent = textFor(ports, 'brain.dispatchTitle', 'Brain dispatch');
    heading.appendChild(title);
    if (origin.brain.answered) {
      const answered = document.createElement('span');
      answered.className = 'bdc-answered';
      answered.textContent = textFor(
        ports, 'brain.answeredChip', 'carries a human answer',
      );
      heading.appendChild(answered);
    }
    if (origin.brain.epicId) {
      const epicId = document.createElement('span');
      epicId.className = 'bdc-epic-id';
      epicId.textContent = origin.brain.epicId;
      heading.appendChild(epicId);
    }
    card.appendChild(heading);
    if (origin.brain.epicTitle) {
      const epic = document.createElement('button');
      epic.type = 'button';
      epic.className = 'bdc-epic-title conversation-origin-link';
      epic.dataset.conversationAction = 'open-project-brain';
      epic.textContent = origin.brain.epicTitle;
      card.appendChild(epic);
    }
    const facts = [
      ['brain.fromLabel', 'From', origin.brain.originatorTitle
        || origin.brain.originatorConv],
      ['brain.methodLabel', 'Method', origin.brain.method],
      ['brain.reasonLabel', 'Why me', origin.brain.route],
    ] as const;
    const metadata = document.createElement('div');
    metadata.className = 'bdc-meta';
    for (const [key, fallback, value] of facts) {
      if (!value) continue;
      const item = document.createElement('span');
      item.className = 'bdc-meta-item';
      const label = document.createElement('span');
      label.className = 'bdc-meta-label';
      label.textContent = textFor(ports, key, fallback);
      item.append(label, String(value));
      metadata.appendChild(item);
    }
    if (metadata.childElementCount) card.appendChild(metadata);
    node.appendChild(card);
    return;
  }
  if (origin.initiator !== 'human' && origin.initiator !== 'autopilot') {
    const banner = document.createElement('div');
    banner.className = 'proactive-banner';
    const label = document.createElement('span');
    label.className = 'pb-text';
    label.textContent = initiatorRoleLabel(origin);
    banner.appendChild(label);
    node.appendChild(banner);
  }
}

type ContextBlock = Extract<ConversationBlockViewModel, { kind: 'context' }>;

/* The retained host adds catalog-aware services (model names, brand marks,
 * the live branch) through renderTurnContextParts; without it the same typed
 * renderer paints every persisted fact with the injected copy. */
function contextParts(
  block: ContextBlock,
  ports: ClassicConversationRendererPorts,
): { fold: string; rail: string } | null {
  const rendered = ports.renderTurnContextParts?.(block);
  if (rendered !== undefined) return rendered;
  return renderTurnContextRailHtml(block.value.snapshot, {
    text: (key, fallback, values) => textFor(ports, key, fallback, values),
  });
}

function trustedPartInnerHtml(
  document: Document,
  html: string,
  wrapperClass: string,
): string {
  if (!html) return '';
  const holder = document.createElement('div');
  holder.innerHTML = html;
  const wrapper = holder.firstElementChild;
  return wrapper?.classList.contains(wrapperClass)
    ? wrapper.innerHTML : html;
}

export function renderContextBlock(
  node: HTMLElement,
  block: ContextBlock,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--context';
  node.innerHTML = contextParts(block, ports)?.fold ?? '';
  node.hidden = !node.childElementCount && !node.textContent?.trim();
}

export function renderContextRail(
  node: HTMLElement,
  block: ContextBlock | null,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-turn-context-rail turn-ctx';
  node.replaceChildren();
  if (!block) {
    node.hidden = true;
    return;
  }
  node.innerHTML = trustedPartInnerHtml(
    node.ownerDocument, contextParts(block, ports)?.rail ?? '', 'turn-ctx',
  );
  node.hidden = !node.childElementCount && !node.textContent?.trim();
}
