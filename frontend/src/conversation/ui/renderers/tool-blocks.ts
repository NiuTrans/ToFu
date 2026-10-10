/**
 * Tool, program and injection projections. Delegates rich markup to injected
 * ports and lazy full results to the existing lifecycle owner.
 * Entry points: renderToolBlock, renderProgramBlock, renderInjectionBlock.
 * Dependencies: conversation-view-model, program-execution-card,
 * tool-fallback-presentation,
 * lazy-tool-result, ports, stable-dom, localized-copy.
 */
import {
  type ConversationBlockViewModel,
  type ConversationTurnViewModel,
} from '../../presentation/conversation-view-model';
import { programExecutionCardHtml } from '../program-execution-card';

import { programChildParentCallId } from '../../presentation/tool-execution-groups';
import { buildToolFallbackCard } from '../../presentation/tool-fallback-presentation';
import {
  bindLazyToolResult,
  loadedToolResult,
  disposeLazyToolResult,
} from '../lazy-tool-result';
import type { ClassicConversationRendererPorts } from './ports';
import {
  setManagedRichHtml,
  resetManagedHtml,
  preserveDetailsOpen,
} from './stable-dom';
import { displayJson, textFor } from './localized-copy';


interface ToolBodyState { materialized: boolean; detach?: () => void }
const toolBodies = new WeakMap<HTMLElement, ToolBodyState>();

/** Retired by Surface unmount; no listeners or result bodies outlive the block. */
export function disposeToolBody(node: HTMLElement): void {
  toolBodies.get(node)?.detach?.();
  toolBodies.delete(node);
}

export function renderToolBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'tool' }>,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--tool';

  const parentId = programChildParentCallId(block.round);
  node.classList.toggle('ptool-parent-child', Boolean(parentId));
  if (parentId) node.dataset.parentToolCallId = parentId;
  else delete node.dataset.parentToolCallId;
  const reference = block.round?._snapshotToolResult;
  const loaded = loadedToolResult(node, reference);
  const bodyState = toolBodies.get(node) ?? { materialized: false };
  toolBodies.set(node, bodyState);
  const open = node.querySelector<HTMLDetailsElement>('.ptool-result-block')?.open;
  let displayedBlock = block;
  if (block.round) {
    const round = { ...block.round,
      _deferToolResultBody: !bodyState.materialized && !open };
    if (loaded !== undefined) {
      delete round._snapshotToolResult;
      round.toolContent = loaded;
    }
    displayedBlock = { ...block, round };
  }
  const richHtml = ports.renderToolBlockHtml?.(displayedBlock, turn) ?? '';
  if (richHtml) {
    node.dataset.rendererMode = 'rich';
    setManagedRichHtml(node, richHtml);
    bodyState.detach?.();
    bodyState.detach = undefined;
    const details = node.querySelector<HTMLDetailsElement>('.ptool-result-block');
    if (details?.querySelector('[data-deferred-tool-body]')) {
      const reveal = (): void => {
        if (!details.open) return;
        bodyState.materialized = true;
        renderToolBlock(node, block, turn, ports);
      };
      details.addEventListener('toggle', reveal);
      bodyState.detach = () => details.removeEventListener('toggle', reveal);
      if (details.open) reveal();
    }
    bindLazyToolResult(node, reference, () => renderToolBlock(node, block, turn, ports), {
      loading: () => ports.localizedText?.('common.loading', 'Loading…') ?? 'Loading…',
      retry: () => ports.localizedText?.('tool.resultRetry', 'Retry') ?? 'Retry',
      failure: stale => stale
        ? ports.localizedText?.('tool.resultStale', 'This message changed. Reopen the conversation.')
          ?? 'This message changed. Reopen the conversation.'
        : ports.localizedText?.('tool.resultLoadFailed', 'Could not load output. Please retry.')
          ?? 'Could not load output. Please retry.',
    }, ports.loadToolResult);
    return;
  }
  disposeLazyToolResult(node);
  disposeToolBody(node);
  const priorOpen = node.dataset.rendererMode === 'fallback'
    ? (node.querySelector('details')?.open ?? false)
    : false;
  node.dataset.rendererMode = 'fallback';
  resetManagedHtml(node);
  const status = block.result?.status || block.round?.status || '';
  const card = buildToolFallbackCard(node.ownerDocument, {
    title: typeof block.round?.query === 'string' && block.round.query.trim()
      ? block.round.query : block.name || block.round?.toolName || 'Tool',
    status: typeof status === 'string' ? status : String(status),
    sections: [
      { label: 'Input', text: displayJson(block.input) },
      {
        label: 'Result',
        text: typeof block.round?.toolContent === 'string'
          ? block.round.toolContent : displayJson(block.result?.content ?? block.result),
      },
    ],
  });
  card.open = priorOpen;
  node.replaceChildren(card);
}

export function renderProgramBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'program' }>,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--program';
  const richHtml = ports.renderToolBlockHtml?.(block, turn) ?? '';
  if (richHtml) {
    node.dataset.rendererMode = 'rich';
    setManagedRichHtml(node, richHtml);
    return;
  }
  node.dataset.rendererMode = 'fallback';
  setManagedRichHtml(node, programExecutionCardHtml(
    block.round, (key, fallback, values) => textFor(ports, key, fallback, values),
  ));
}

export function renderInjectionBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'injections' }>,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = `conversation-block conversation-block--injections conversation-injection--${
    block.channel}`;
  const richHtml = ports.renderInjectionBlockHtml?.(block, turn) ?? '';
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
  details.className = 'conversation-injection-fallback';
  details.open = preserveDetailsOpen(node, false);
  const summary = node.ownerDocument.createElement('summary');
  summary.textContent = `${block.channel} · ${block.items.length}`;
  const content = node.ownerDocument.createElement('pre');
  content.textContent = JSON.stringify(block.items[0] ?? {}, null, 2);
  details.append(summary, content);
  node.replaceChildren(details);
}
