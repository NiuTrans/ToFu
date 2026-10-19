/**
 * Tool, program and injection projections. Delegates rich markup to injected
 * ports and lazy full results to the existing lifecycle owner.
 * Entry points: renderToolBlock, renderProgramBlock, renderInjectionBlock.
 * Dependencies: conversation-view-model, program-execution-card,
 * tool-fallback-presentation,
 * lazy-tool-result, ports, stable-dom, localized-copy.
 */
import { renderRichResponse, disposeRichResponse } from '../../../rich-response/renderer';

import { projectRichToolResult } from '../../../rich-response/tool-result';
import {
  type ConversationBlockViewModel,
  type ConversationTurnViewModel,
} from '../../presentation/conversation-view-model';
import { programExecutionCardHtml } from '../program-execution-card';

import { isNativeProgramGateway } from '../../presentation/program-gateway-presentation';

import { programChildParentCallId } from '../../presentation/tool-execution-groups';
import { buildToolFallbackCard } from '../../presentation/tool-fallback-presentation';
import {
  bindLazyToolResult,
  loadedToolResult,

  loadedToolResultResults,
  disposeLazyToolResult,
} from '../lazy-tool-result';
import type { ClassicConversationRendererPorts } from './ports';

import type { ChoicePorts } from '../../../rich-response/choices';
import {
  setManagedRichHtml,
  resetManagedHtml,
  preserveDetailsOpen,
} from './stable-dom';
import { displayJson, textFor } from './localized-copy';
import { mountToolDiffReview } from '../tool-diff-review';
import { renderToolEffectNotice } from './tool-effect-notice';
import { toolEffectNotice } from '../../presentation/tool-effect-presentation';


interface ToolBodyState { materialized: boolean; detach?: () => void; rich?: HTMLElement; choices?: ChoicePorts; choiceScope?: string }
const toolBodies = new WeakMap<HTMLElement, ToolBodyState>();

/** Retired by Surface unmount; no listeners or result bodies outlive the block. */
export function disposeToolBody(node: HTMLElement): void {

  const rich = toolBodies.get(node)?.rich;
  if (rich) disposeRichResponse(rich);
  toolBodies.get(node)?.detach?.();
  toolBodies.delete(node);
}

function renderToolBlockContent(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'tool' }>,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--tool';

  if (isNativeProgramGateway(block.round ?? { toolName: block.name })) {
    const reference = block.round?._snapshotToolResult;
    const loaded = loadedToolResult(node, reference);
    const round = { ...block.round, toolName: block.name, toolArgs: block.input,
      status: block.round?.status ?? block.result?.status, result: block.result };
    if (loaded !== undefined) { delete round._snapshotToolResult; round.toolContent = loaded; }
    node.classList.remove('ptool-parent-child'); delete node.dataset.parentToolCallId;
    toolBodies.get(node)?.detach?.();
    if (toolBodies.get(node)?.rich) disposeRichResponse(toolBodies.get(node)!.rich!);
    toolBodies.delete(node);
    setManagedRichHtml(node, programExecutionCardHtml(round,
      (key, fallback, values) => textFor(ports, key, fallback, values)));
    bindLazyToolResult(node, reference, () => renderToolBlock(node, block, turn, ports), {
      loading: () => textFor(ports, 'common.loading', 'Loading…'),
      retry: () => textFor(ports, 'tool.resultRetry', 'Retry'),
      failure: stale => textFor(ports, stale ? 'tool.resultStale' : 'tool.resultLoadFailed',
        stale ? 'This message changed. Reopen the conversation.' : 'Could not load output. Please retry.'),
    }, ports.loadToolResult);
    return;
  }

  const parentId = programChildParentCallId(block.round);
  node.classList.toggle('ptool-parent-child', Boolean(parentId));
  if (parentId) node.dataset.parentToolCallId = parentId;
  else delete node.dataset.parentToolCallId;
  const reference = block.round?._snapshotToolResult;
  const loaded = loadedToolResult(node, reference);
  const bodyState = toolBodies.get(node) ?? { materialized: false };
  toolBodies.set(node, bodyState);

  const choiceConversationId = turn.source?.conversationId;
  const choiceScope = JSON.stringify([choiceConversationId, turn.turnId]);
  if (bodyState.choiceScope !== choiceScope) {
    bodyState.choices = choiceConversationId && turn.turnId ? ports.choiceInteractions?.(choiceConversationId, turn.turnId) : undefined;
    bodyState.choiceScope = choiceScope;
    if (bodyState.rich) disposeRichResponse(bodyState.rich);
  }
  if (block.round?.toolName === 'ask_human' && ['choice','batch'].includes(String(block.round.guidanceType ?? ''))
      && block.round.guidanceId && bodyState.choices) {
    disposeLazyToolResult(node); bodyState.detach?.(); bodyState.detach = undefined;
    bodyState.rich ??= node.ownerDocument.createElement('div');
    if (node.childNodes.length !== 1 || node.firstChild !== bodyState.rich) node.replaceChildren(bodyState.rich);
    resetManagedHtml(node);
    node.dataset.rendererMode = 'native-choice';
    renderRichResponse(bodyState.rich, { version: 1, title: ports.localizedText?.('rich.question', 'Question') ?? 'Question',
      blocks: [{ type: block.round.guidanceType === 'choice' ? 'choice_prompt' : 'form_prompt', id: 'human-choice', title: ports.localizedText?.('rich.question', 'Question') ?? 'Question', interactionId: block.round.guidanceId }] },
      { conversationId: turn.source.conversationId, choices: bodyState.choices,
        interactionSnapshot: JSON.stringify([turn.status, block.round.status, block.round.guidanceResponse]), text: (key, fallback) => ports.localizedText?.(key, fallback) ?? fallback });
    return;
  }

  const mountRich = (): void => {
    const results = Array.isArray(block.round?.results) ? block.round.results.slice(0, 16) : [];
    const value = projectRichToolResult([loaded, block.result, block.round?.result, block.round?.toolContent, ...results], String(block.round?.toolName ?? ''));
    if (value === null) { if (bodyState.rich) { disposeRichResponse(bodyState.rich); bodyState.rich.remove(); bodyState.rich = undefined; } return; }
    bodyState.rich ??= node.ownerDocument.createElement('div');
    bodyState.rich.dataset.toolRichResponse = 'true';
    renderRichResponse(bodyState.rich,value,{conversationId:turn.source.conversationId,surfaceContext:turn.source.conversationId && turn.turnId ? {conversationId:turn.source.conversationId,turnId:turn.turnId} : undefined,choices:bodyState.choices,text:(key,fallback) => ports.localizedText?.(key,fallback) ?? fallback});
    node.append(bodyState.rich);
  };
  bodyState.rich?.remove();
  const open = node.querySelector<HTMLDetailsElement>('.ptool-result-block')?.open;
  let displayedBlock = block;
  if (block.round) {
    const round = { ...block.round,
      _deferToolResultBody: !bodyState.materialized && !open };
    if (loaded !== undefined) {
      delete round._snapshotToolResult;
      round.toolContent = loaded;

      const results = loadedToolResultResults(node, reference);
      if (results !== undefined) round.results = results;
      // Commands retain one native disclosure after hydration; this marker is
      // display-only and is never written to the authoritative round.
      if (round.toolName === 'run_command' || round.toolName === 'code_exec') {
        Object.assign(round, { _displayToolResultReference: reference });
      }
    }
    displayedBlock = { ...block, round };
  }
  const richHtml = ports.renderToolBlockHtml?.(displayedBlock, turn) ?? '';
  if (richHtml) {
    node.dataset.rendererMode = 'rich';
    setManagedRichHtml(node, richHtml);
    mountToolDiffReview(node, displayedBlock, turn,
      (key, fallback) => ports.localizedText?.(key, fallback) ?? fallback);
    bodyState.detach?.();

    mountRich();
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
  bodyState.detach?.();
  const priorOpen = node.dataset.rendererMode === 'fallback'
    ? (node.querySelector('details')?.open ?? false)
    : false;
  node.dataset.rendererMode = 'fallback';
  resetManagedHtml(node);
  const status = block.result?.status || block.round?.status || '';
  const card = buildToolFallbackCard(node.ownerDocument, {

    resourceRound: block.round,
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

  mountRich();
}

function renderProgramBlockContent(
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

export function renderToolBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'tool' }>,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  renderToolBlockContent(node, block, turn, ports);
  renderToolEffectNotice(node, block.effectNotice === undefined
    ? toolEffectNotice(block.round?.executionEffect, turn.status, block.round?.status) : block.effectNotice,
    ports, block.round?.executionEffect);
}

export function renderProgramBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'program' }>,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  renderProgramBlockContent(node, block, turn, ports);
  renderToolEffectNotice(node, block.effectNotice === undefined
    ? toolEffectNotice(block.round?.executionEffect, turn.status, block.round?.status) : block.effectNotice,
    ports, block.round?.executionEffect);
}
