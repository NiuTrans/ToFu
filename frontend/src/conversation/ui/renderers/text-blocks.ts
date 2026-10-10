/**
 * Text, thinking, translation alternative and live-status DOM projections.
 * Owns deferred reasoning-body disclosure and its explicit disposal.
 * Entry points: renderBackgroundCompletion, renderTextBlock,
 * disposeThinkingBody, renderThinkingBlock, renderLiveStatusBlock.
 * Dependencies: conversation-sync.generated, conversation-view-model, ports,
 * stable-dom, icons, localized-copy.
 */
import type { BackgroundCommandCompletion } from '../../../api/conversation-sync.generated';
import {
  type ConversationBlockViewModel,
  type ConversationTurnViewModel,
} from '../../presentation/conversation-view-model';
import type { ClassicConversationRendererPorts } from './ports';
import {
  directClassChild,
  setManagedHtml,
  setStreamingMarkdownHtml,
  setCompletedMarkdownHtml,
  resetManagedHtml,
  preserveDetailsOpen,
} from './stable-dom';
import {
  disclosureChevron,
  actionIcon,
} from './icons';
import { textFor } from './localized-copy';

export function renderBackgroundCompletion(
  node: HTMLElement,
  completion: BackgroundCommandCompletion,
  ports: ClassicConversationRendererPorts,
): void {
  const document = node.ownerDocument;
  const details = document.createElement('details');
  details.className = 'bgcmd-details';
  details.open = preserveDetailsOpen(node, false);
  const summary = document.createElement('summary');
  summary.className = 'bgcmd-summary';
  summary.append(`${textFor(ports, 'bgCommand.output', 'Command result')} · `);
  const commandId = document.createElement('code');
  commandId.className = 'bgcmd-id';
  commandId.textContent = completion.commandId.slice(0, 128);
  summary.appendChild(commandId);
  const command = document.createElement('pre');
  command.className = 'bgcmd-command';
  command.textContent = completion.command.slice(0, 2000);
  const output = document.createElement('pre');
  output.className = 'sw-card-raw-pre';
  output.textContent = completion.resultPreview.slice(0, 2000)
    || textFor(ports, 'bgCommand.noPayload', 'No result available.');
  details.append(summary, command, output);
  if (completion.resultTruncated || completion.resultPreview.length > 2000) {
    const note = document.createElement('p');
    note.className = 'bgcmd-note';
    note.textContent = textFor(ports, 'bgCommand.truncated', 'Output preview truncated.');
    details.append(note);
  }
  node.replaceChildren(details);
}

export function renderTextBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'text' }>,
  ports: ClassicConversationRendererPorts,
  turn?: ConversationTurnViewModel,
): void {
  node.className = 'conversation-block conversation-block--text message-body';
  node.dataset.deliverable = String(block.deliverable);
  node.dataset.terminal = String(block.terminal);
  const completion = turn?.source.projection.backgroundCommand;
  if (completion) {
    renderBackgroundCompletion(node, completion, ports);
    const label = node.ownerDocument.createElement('p');
    label.className = 'bgcmd-label';
    label.textContent = textFor(ports, 'bgCommand.freshTurn',
      'Background command finished · submitted as turn input');
    node.prepend(label);
    return;
  }
  let body = directClassChild<HTMLElement>(node, 'md-content');
  if (!body) {
    body = node.ownerDocument.createElement('div');
    node.prepend(body);
  } else if (node.firstElementChild !== body) {
    node.prepend(body);
  }
  body.className = 'md-content';
  const setBodyHtml = block.terminal ? setCompletedMarkdownHtml : setStreamingMarkdownHtml;
  setBodyHtml(body, ports.renderSafeMarkdownHtml(
    block.displayMarkdown ?? block.markdown,
    {
      ...(turn?.actor === 'human' ? { escapeRawHtml: true } : {}),
      /* A non-terminal text block is the streaming hot path: skip hljs/KaTeX
       * per frame; the terminal re-render restores both. */
      ...(!block.terminal ? { streaming: true } : {}),
    },
  ));
  renderTranslationAlternative(node, block, ports);
  /* Assistant text blocks carry their own copy affordance: the turn-level
   * Copy only holds the final answer (projection.content), so mid-turn
   * prose between tool calls would otherwise have no copy path at all.
   * Human bubbles are exempt: their single text block IS the turn content,
   * so the action-bar Copy covers it and the floating button only overlaps
   * the message text. */
  let copy = directClassChild<HTMLButtonElement>(node, 'conversation-block-copy');
  if (turn?.actor === 'human') {
    copy?.remove();
    return;
  }
  if (!copy) {
    copy = node.ownerDocument.createElement('button');
    copy.type = 'button';
    copy.dataset.conversationAction = 'copy-block';
    const icon = actionIcon(node.ownerDocument, 'copy');
    if (icon) copy.appendChild(icon);
  }
  copy.className = 'msg-action-btn conversation-block-copy'
    + ' conversation-action--copy-block';
  const copyLabel = textFor(ports, 'msgAction.copy', 'Copy');
  copy.title = copyLabel;
  copy.setAttribute('aria-label', copyLabel);
  if (node.lastElementChild !== copy) node.appendChild(copy);
}

/** Large completed folds own their parsing work only while the reader opens
 * them. Small folds stay eager; active streaming always keeps its live body.
 * State belongs to a keyed block node and is retired by the Surface. */
const DEFERRED_REASONING_MIN_CHARACTERS = 4096;

interface ThinkingBodyRenderState {
  details: HTMLDetailsElement;
  body: HTMLElement;
  appliedMarkdown: string | null;
  appliedStreaming: boolean | null;
  render(): void;
  dispose(): void;
}

const thinkingBodyRenderStates = new WeakMap<HTMLElement, ThinkingBodyRenderState>();

export function disposeThinkingBody(node: HTMLElement): void {
  thinkingBodyRenderStates.get(node)?.dispose();
  thinkingBodyRenderStates.delete(node);
}

export function renderThinkingBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'thinking' }>,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--thinking';
  const document = node.ownerDocument;
  let details = directClassChild<HTMLDetailsElement>(node, 'thinking-block');
  if (!details) {
    details = document.createElement('details');
    details.open = !block.terminal;
    node.prepend(details);
  } else if (node.firstElementChild !== details) {
    node.prepend(details);
  }
  details.className = 'thinking-block';
  /* Streaming blocks are created open; once the reasoning closes (next round
   * starts or the turn settles) collapse them again so finished thinking
   * never towers over the answer. A complete block the reader opened
   * manually never re-transitions through 'active', so it stays put. */
  const wasActive = details.dataset.state === 'active';
  if (block.terminal && wasActive) details.open = false;
  details.dataset.state = block.terminal ? 'complete' : 'active';
  let summary = directClassChild<HTMLElement>(details, 'thinking-header');
  if (!summary) {
    summary = document.createElement('summary');
    details.prepend(summary);
  }
  summary.className = 'thinking-header';
  let stateDot = directClassChild<HTMLElement>(summary, 'thinking-state-dot');
  if (!stateDot) {
    stateDot = document.createElement('span');
    stateDot.className = 'thinking-state-dot';
    stateDot.setAttribute('aria-hidden', 'true');
    summary.prepend(stateDot);
  }
  let label = directClassChild<HTMLElement>(summary, 'thinking-label');
  if (!label) {
    label = document.createElement('span');
    summary.appendChild(label);
  }
  label.className = 'thinking-label';
  const labelText = block.terminal
    ? textFor(ports, 'stream.thinking.done', 'Thinking Process')
    : textFor(ports, 'stream.thinking.active', 'Thinking…');
  label.textContent = labelText;
  if (block.terminal) label.removeAttribute('aria-live');
  else label.setAttribute('aria-live', 'polite');
  let toggle = directClassChild<HTMLElement>(summary, 'thinking-toggle');
  if (!toggle) {
    toggle = disclosureChevron(document, 'thinking-toggle');
    summary.appendChild(toggle);
  } else if (!toggle.classList.contains('conversation-disclosure-chevron')) {
    const replacement = disclosureChevron(document, 'thinking-toggle');
    toggle.replaceWith(replacement);
    toggle = replacement;
  }
  let content = directClassChild<HTMLElement>(details, 'thinking-content');
  if (!content) {
    content = document.createElement('div');
    details.appendChild(content);
  }
  content.className = 'thinking-content';
  let body = directClassChild<HTMLElement>(content, 'thinking-text');
  if (!body) {
    body = document.createElement('div');
    content.appendChild(body);
  }
  body.className = 'thinking-text thinking-md md-content';
  const markdown = block.displayMarkdown ?? block.markdown;
  const streaming = !block.terminal;
  let state = thinkingBodyRenderStates.get(node);
  if (!state || state.details !== details || state.body !== body) {
    disposeThinkingBody(node);
    const disclosure = details;
    const next: ThinkingBodyRenderState = {
      details, body, appliedMarkdown: null, appliedStreaming: null,
      render() {},
      dispose() {
        disclosure.removeEventListener('toggle', onToggle);
        next.render = () => {};
      },
    };
    const onToggle = (): void => { if (disclosure.open) next.render(); };
    disclosure.addEventListener('toggle', onToggle);
    state = next;
    thinkingBodyRenderStates.set(node, state);
  }
  const current = state;
  const target = body;
  current.render = () => {
    if (current.appliedMarkdown === markdown
        && current.appliedStreaming === streaming) return;
    const setBodyHtml = streaming ? setStreamingMarkdownHtml : setCompletedMarkdownHtml;
    setBodyHtml(target, ports.renderSafeMarkdownHtml(
      markdown, streaming ? { streaming: true } : undefined,
    ));
    current.appliedMarkdown = markdown;
    current.appliedStreaming = streaming;
    delete target.dataset.deferredReasoning;
  };
  if (block.terminal && !details.open
      && markdown.length > DEFERRED_REASONING_MIN_CHARACTERS) {
    // A closed fold may receive a newer projection or translation. Retain
    // unchanged materialized content, but never show stale text on reopening.
    if (current.appliedMarkdown !== markdown
        || current.appliedStreaming !== streaming) {
      target.replaceChildren();
      resetManagedHtml(target);
      current.appliedMarkdown = null;
      current.appliedStreaming = null;
      target.dataset.deferredReasoning = 'true';
    }
  } else {
    current.render();
  }
  renderTranslationAlternative(node, block, ports);
}

export function renderLiveStatusBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'live-status' }>,
  ports: ClassicConversationRendererPorts,
): void {
  const value = block.value;
  node.className = 'conversation-block conversation-block--live-status';
  node.setAttribute('role', 'status');
  node.setAttribute('aria-live', 'polite');
  node.setAttribute('aria-atomic', 'true');
  const document = node.ownerDocument;
  const phase = value.phase || 'working';
  const phaseFallback = phase === 'executor_preparing'
    ? "Task accepted; binding it to this server's task scheduler…"
    : phase === 'executor_queued'
      ? "Waiting in this server's AI task queue; syncing queue position and "
        + 'available slots (not model/API quota)…'
      : phase === 'worker_starting'
        ? 'Server execution slot acquired; starting the task…'
        : '';
  const localizedDetail = value.detailKey
    ? textFor(ports, value.detailKey,
      value.detail || value.label || phaseFallback,
      value.detailArgs)
    : '';
  let label = localizedDetail || value.detail || value.label;
  if (phase === 'warming') {
    label = textFor(ports, 'autopilot.warming', 'Autopilot is starting…');
  } else if (phase === 'thinking_active') {
    label = textFor(ports, 'stream.thinking.active', 'Thinking…');
  } else if (phase === 'llm_thinking' && !label) {
    label = textFor(ports, 'stream.phase.generatingResponse', 'Generating response…');
  } else if (phase === 'tool_exec' && value.tools?.length) {
    label = value.detail || value.tools.join(', ');
  } else if (phase === 'retrying' && !localizedDetail && !value.detail) {
    label = textFor(ports, 'stream.phase.retrying', 'Retrying…');
  } else if (phase === 'storage_wedged') {
    label = textFor(
      ports, 'stream.phase.storageWedged', 'Storage write stalled — retrying…',
    );
  } else if (phase === 'waiting' && !localizedDetail && !value.detail) {
    label = textFor(
      ports, 'stream.phase.waitingWorkerStatus',
      'Waiting for an execution status update…',
    );
  } else if (phase === 'responding' && !value.detail) {
    label = textFor(ports, 'autopilot.warming', 'Autopilot is responding…');
  }
  const modelRoute = value.modelRoute;
  if (modelRoute?.selectedModel && modelRoute.resolvedModel
      && modelRoute.selectedModel !== modelRoute.resolvedModel
      && value.detailKey !== 'stream.phase.modelRouted') {
    const routeLabel = textFor(
      ports,
      'stream.phase.modelRouted',
      'Model routing: {from} → {to} ({role}, {tier})',
      {
        from: modelRoute.selectedModel,
        to: modelRoute.resolvedModel,
        role: modelRoute.role,
        tier: modelRoute.tier,
      },
    );
    label = label ? `${routeLabel} · ${label}` : routeLabel;
  }

  if (phase === 'warming') {
    const status = document.createElement('div');
    status.className = 'stream-status';
    const pulse = document.createElement('div');
    pulse.className = 'pulse';
    status.append(pulse, label);
    node.replaceChildren(status);
    return;
  }

  const row = document.createElement('div');
  row.className = `stream-phase${
    phase === 'retrying' ? ' stream-phase-retrying' : ''}${
    phase === 'storage_wedged' ? ' stream-phase-wedged' : ''}${
    phase === 'thinking_active' ? ' stream-phase-thinking' : ''}`;
  const textNode = document.createElement('span');
  textNode.className = 'stream-phase-text';
  textNode.textContent = label;
  if (phase === 'thinking_active' && value.thinkingLength != null) {
    const counter = document.createElement('span');
    counter.className = 'stream-phase-counter';
    counter.textContent = textFor(
      ports, 'stream.phase.chars', `${value.thinkingLength} chars`,
      { n: value.thinkingLength },
    );
    textNode.appendChild(counter);
  }
  const dots = document.createElement('span');
  dots.className = 'stream-phase-dots';
  dots.append(
    document.createElement('span'),
    document.createElement('span'),
    document.createElement('span'),
  );
  for (const dot of Array.from(dots.children)) dot.textContent = '.';
  row.append(textNode, dots);
  node.replaceChildren(row);
}

function renderTranslationAlternative(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'text' | 'thinking' }>,
  ports: ClassicConversationRendererPorts,
): void {
  let details = directClassChild<HTMLDetailsElement>(node, 'bilingual-block');
  /* Only the deliverable answer carries the per-block original/translation
   * alternative. Mid-turn narration and thinking follow the turn-level
   * display mode — a per-block toggle there is timing-dependent noise. */
  if (block.kind !== 'text' || !block.deliverable
      || !block.translatedMarkdown) {
    details?.remove();
    return;
  }
  const alternative = block.displayMode === 'translated'
    ? block.markdown : block.translatedMarkdown;
  const alternativeMode = block.displayMode === 'translated'
    ? 'original' : 'translated';
  const document = node.ownerDocument;
  if (!details) {
    details = document.createElement('details');
    node.appendChild(details);
  }
  details.className = `bilingual-block bilingual-${alternativeMode}`;
  let summary = directClassChild<HTMLElement>(details, 'bilingual-header');
  if (!summary) {
    summary = document.createElement('summary');
    details.prepend(summary);
  }
  summary.className = 'bilingual-header';
  let label = directClassChild<HTMLElement>(summary, 'bilingual-label');
  if (!label) {
    label = document.createElement('span');
    summary.appendChild(label);
  }
  label.className = 'bilingual-label';
  label.textContent = alternativeMode === 'original'
    ? textFor(ports, 'chat.bilingualOriginal', 'Original')
    : textFor(ports, 'chat.bilingualTranslated', 'Translation');
  let toggle = directClassChild<HTMLElement>(summary, 'bilingual-toggle');
  if (!toggle) {
    toggle = disclosureChevron(document, 'bilingual-toggle');
    summary.appendChild(toggle);
  } else if (!toggle.classList.contains('conversation-disclosure-chevron')) {
    const replacement = disclosureChevron(document, 'bilingual-toggle');
    toggle.replaceWith(replacement);
  }
  let wrapper = directClassChild<HTMLElement>(details, 'bilingual-body');
  if (!wrapper) {
    wrapper = document.createElement('div');
    details.appendChild(wrapper);
  }
  wrapper.className = 'bilingual-body';
  let body = directClassChild<HTMLElement>(wrapper, 'md-content');
  if (!body) {
    body = document.createElement('div');
    wrapper.appendChild(body);
  }
  body.className = 'md-content';
  setManagedHtml(body, ports.renderSafeMarkdownHtml(alternative));
}
