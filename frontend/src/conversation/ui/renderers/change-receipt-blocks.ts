/**
 * File-change, provenance and rollback receipt DOM projections. Rich
 * provenance remains an injected port.
 * Entry points: renderFileChangesBlock, renderProvenanceBlock,
 * renderRolledBackBlock.
 * Dependencies: conversation-view-model, ports, stable-dom, icons,
 * localized-copy.
 */
import {
  type ConversationBlockViewModel,
  type ConversationTurnViewModel,
} from '../../presentation/conversation-view-model';
import type { ClassicConversationRendererPorts } from './ports';
import {
  setManagedHtml,
  setManagedRichHtml,
  resetManagedHtml,
  preserveDetailsOpen,
} from './stable-dom';
import { disclosureChevron, lineSvgIcon } from './icons';
import { textFor } from './localized-copy';

/* Document glyph, matching the 16px stroke language of the other process rows. */
const FILE_GLYPH_PATHS = [
  'M4 1.5h4.5L11.5 4.5V13a1.5 1.5 0 0 1-1.5 1.5H4A1.5 1.5 0 0 1 2.5 13V3A1.5 1.5 0 0 1 4 1.5z',
  'M8.5 1.5V4.5h3',
] as const;

export function renderFileChangesBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'file-changes' }>,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  const previousDetails = node.querySelector<HTMLDetailsElement>(
    ':scope > details.conversation-file-changes',
  );
  node.className = 'conversation-block conversation-block--file-changes';
  const details = node.ownerDocument.createElement('details');
  details.className = `file-changes-bar conversation-file-changes${
    block.state === 'undone' ? ' fc-undone' : ''}`;
  details.open = previousDetails?.open ?? block.files.length <= 5;
  const summary = node.ownerDocument.createElement('summary');
  summary.className = 'fc-summary';
  const label = node.ownerDocument.createElement('span');
  label.className = 'fc-summary-text';
  const isUndone = block.state === 'undone';
  const count = block.truncated ? `${block.count}+` : block.count;
  label.textContent = textFor(
    ports,
    isUndone ? 'fileChanges.undone' : 'fileChanges.filesChanged',
    `${count} file${block.count === 1 ? '' : 's'} ${
      isUndone ? 'restored' : 'changed'}`,
    { n: count, s: block.count === 1 ? '' : 's' },
  );
  /* Process rows reserve the leading marker column for their glyph; without
   * one this summary's label lands in that empty column instead of on the
   * label baseline every sibling row uses. */
  const marker = node.ownerDocument.createElement('span');
  marker.className = 'fc-summary-icon';
  marker.setAttribute('aria-hidden', 'true');
  marker.append(lineSvgIcon(node.ownerDocument, FILE_GLYPH_PATHS));
  const chevronIcon = disclosureChevron(node.ownerDocument, 'fc-chevron');
  summary.append(marker, label, chevronIcon);
  const fileList = node.ownerDocument.createElement('div');
  fileList.className = 'fc-details';
  for (const file of block.files) {
    const path = typeof file.path === 'string' ? file.path : '';
    if (!path) continue;
    const row = node.ownerDocument.createElement('div');
    row.className = `fc-file${file.ok === false ? ' fc-file-err' : ''}${
      file.pending ? ' fc-file-pending' : ''}`;
    row.title = [file.root, path].filter(Boolean).join(':');
    const pathNode = node.ownerDocument.createElement('span');
    pathNode.className = 'fc-path';
    pathNode.textContent = `${file.root ? `${file.root}:` : ''}${path}`;
    const action = node.ownerDocument.createElement('span');
    action.className = 'fc-action';
    action.textContent = file.action || '';
    row.append(pathNode, action);
    fileList.appendChild(row);
  }
  const actions = node.ownerDocument.createElement('div');
  actions.className = 'fc-actions conversation-file-changes__actions';
  if (turn.status === 'completed' && block.commandAvailable) {
    const action = node.ownerDocument.createElement('button');
    action.type = 'button';
    action.addEventListener('click', (event) => {
      event.preventDefault();
    });
    if (block.state === 'applied') {
      action.className = 'fc-undo-btn';
      action.dataset.conversationAction = 'undo-turn-files';
      action.textContent = textFor(ports, 'fileChanges.undo', 'Undo');
      actions.append(action);
    } else if (block.state === 'undone') {
      action.className = 'fc-redo-btn';
      action.dataset.conversationAction = 'redo-turn-files';
      action.textContent = textFor(ports, 'fileChanges.redo', 'Redo');
      actions.append(action);
    } else if (block.state === 'undoing' || block.state === 'redoing') {
      action.className = 'fc-undo-btn';
      action.disabled = true;
      action.textContent = block.state === 'undoing' ? 'Undoing…' : 'Redoing…';
      actions.append(action);
    }
  }
  if (block.error != null) {
    const error = node.ownerDocument.createElement('span');
    error.className = 'conversation-file-changes__error';
    error.textContent = String(block.error);
    actions.append(error);
  }
  summary.append(actions);
  details.append(summary, fileList);
  node.replaceChildren(details);
}

export function renderProvenanceBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'provenance' }>,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--provenance';
  const richHtml = ports.renderProvenanceBlockHtml?.(block, turn) ?? '';
  if (richHtml) {
    node.dataset.rendererMode = 'rich';
    setManagedRichHtml(node, richHtml);
    return;
  }
  if (node.dataset.rendererMode !== 'fallback') {
    node.replaceChildren();
    resetManagedHtml(node);
    node.dataset.rendererMode = 'fallback';
  }
  const details = node.ownerDocument.createElement('details');
  details.className = 'sw-inbox-row sw-system-note-row sw-note-group conversation-provenance-fallback';
  details.open = preserveDetailsOpen(node, false);
  const summary = node.ownerDocument.createElement('summary');
  summary.textContent = textFor(ports, 'provenance.context', 'Turn context');
  const content = node.ownerDocument.createElement('pre');
  content.textContent = JSON.stringify(block.value, null, 2);
  details.append(summary, content);
  node.replaceChildren(details);
}

export function renderRolledBackBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'rolled-back' }>,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--rolled-back';
  const document = node.ownerDocument;
  const thinking = String(block.value.thinking ?? '').trim();
  const content = String(block.value.content ?? '').trim();
  const fragment = document.createDocumentFragment();
  /* Two disclosures share this block; preserve each variant's open state
   * independently across re-render. */
  const priorOpenByVariant = (variant: string): boolean | undefined =>
    node.querySelector<HTMLDetailsElement>(`details.${variant}`)?.open;
  if (thinking) {
    const disclosure = rolledBackDisclosure(
      document, ports, 'thinking-prior',
      textFor(ports, 'rolledBack.thinking', 'Earlier Thinking (rolled back)'),
      thinking,
    );
    disclosure.open = priorOpenByVariant('thinking-prior') ?? false;
    fragment.appendChild(disclosure);
  }
  if (content) {
    const disclosure = rolledBackDisclosure(
      document, ports, 'content-prior',
      textFor(ports, 'rolledBack.content', 'Interrupted Draft (rolled back)'),
      content,
    );
    disclosure.open = priorOpenByVariant('content-prior') ?? false;
    fragment.appendChild(disclosure);
  }
  node.replaceChildren(fragment);
}

/* Display-only disclosure for one rewound lane. Reuses the thinking-block
 * visual language; the -prior variants (dashed border, muted label) mark it
 * as history that will never rejoin the live stream. Collapsed by default. */
function rolledBackDisclosure(
  document: Document,
  ports: ClassicConversationRendererPorts,
  variantClass: string,
  labelText: string,
  markdown: string,
): HTMLDetailsElement {
  const details = document.createElement('details');
  details.className = `thinking-block ${variantClass}`;
  details.dataset.state = 'complete';
  const summary = document.createElement('summary');
  summary.className = 'thinking-header';
  const stateDot = document.createElement('span');
  stateDot.className = 'thinking-state-dot';
  stateDot.setAttribute('aria-hidden', 'true');
  const label = document.createElement('span');
  label.className = 'thinking-label';
  label.textContent = labelText;
  summary.append(stateDot, label, disclosureChevron(document, 'thinking-toggle'));
  const contentEl = document.createElement('div');
  contentEl.className = 'thinking-content';
  const body = document.createElement('div');
  body.className = 'thinking-text thinking-md md-content';
  setManagedHtml(body, ports.renderSafeMarkdownHtml(markdown));
  contentEl.appendChild(body);
  details.append(summary, contentEl);
  return details;
}
