/**
 * Text, thinking, translation alternative and live-status DOM projections.
 * Owns deferred reasoning-body disclosure and its explicit disposal.
 * Entry points: renderBackgroundCompletion, renderTextBlock,
 * disposeThinkingBody, renderThinkingBlock, renderLiveStatusBlock.
 * Dependencies: conversation-sync.generated, conversation-view-model, ports,
 * stable-dom, icons, localized-copy.
 */
import { renderRichMarkdown, disposeRichMarkdown } from '../../../rich-response/markdown';
export { disposeRichMarkdown };
import type { BackgroundCommandCompletion } from '../../../api/conversation-sync.generated';
import {
  type ConversationBlockViewModel,
  type ConversationTurnViewModel,
} from '../../presentation/conversation-view-model';
import type { ClassicConversationRendererPorts } from './ports';
import { iconHtml } from '../../../icons';
import {
  directClassChild,
  setManagedHtml,
  setStreamingMarkdownHtml,
  setCompletedMarkdownHtml,
  resetManagedHtml,
  preserveDetailsOpen,
  endAnchoredScroller,
  type EndAnchoredScroller,
} from './stable-dom';
import {
  disclosureChevron,
  actionIcon,
} from './icons';
import { formatDuration, textFor } from './localized-copy';
import { updateAnswerNavigation } from './answer-navigation';

import { liveStatusPhase, presentLivePhaseLabel } from '../../presentation/live-phase';
import { swarmAgentPhaseLabel } from '../../presentation/swarm-agent-presentation';

export function renderBackgroundCompletion(
  node: HTMLElement,
  completion: BackgroundCommandCompletion,
  ports: ClassicConversationRendererPorts,
): void {
  const document = node.ownerDocument;
  let details = directClassChild<HTMLDetailsElement>(node, 'bgcmd-details');
  if (details?.dataset.commandId !== completion.commandId) {
    details = document.createElement('details');
    details.className = 'bgcmd-details';
    details.dataset.commandId = completion.commandId;
    node.replaceChildren(details);
  }
  // Queue rank/count changes are not new evidence. Retain native reader nodes
  // and replace text only when its value changes; unchanged selections survive.
  const part = (parent: HTMLElement, name: string, tag: string): HTMLElement => {
    let child = directClassChild<HTMLElement>(parent, name);
    if (!child) {
      child = document.createElement(tag);
      child.className = name;
      parent.appendChild(child);
    }
    return child;
  };
  const setText = (target: HTMLElement, value: string): void => {
    if (target.textContent !== value) target.textContent = value;
  };
  const summary = part(details, 'bgcmd-summary', 'summary');
  setText(part(summary, 'bgcmd-output-label', 'span'),
    `${textFor(ports, 'bgCommand.output', 'Command result')} · `);
  setText(part(summary, 'bgcmd-id', 'code'), completion.commandId.slice(0, 128));
  if (!directClassChild(summary, 'bgcmd-chevron')) {
    summary.appendChild(disclosureChevron(document, 'bgcmd-chevron'));
  }
  setText(part(details, 'bgcmd-command', 'pre'), completion.command.slice(0, 2000));
  setText(part(details, 'sw-card-raw-pre', 'pre'), completion.resultPreview.slice(0, 2000)
    || textFor(ports, 'bgCommand.noPayload', 'No result available.'));
  if (completion.resultTruncated || completion.resultPreview.length > 2000) {
    setText(part(details, 'bgcmd-note', 'p'),
      textFor(ports, 'bgCommand.truncated', 'Output preview truncated.'));
  } else directClassChild(details, 'bgcmd-note')?.remove();
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
    let label = directClassChild<HTMLElement>(node, 'bgcmd-label');
    if (!label) {
      label = node.ownerDocument.createElement('p');
      label.className = 'bgcmd-label';
      node.prepend(label);
    }
    const text = textFor(ports, 'bgCommand.freshTurn',
      'Background command finished · submitted as turn input');
    if (label.textContent !== text) label.textContent = text;
    return;
  }
  let body = directClassChild<HTMLElement>(node, 'md-content');
  if (!body) {
    body = node.ownerDocument.createElement('div');
    node.prepend(body);
  } else if (node.firstElementChild !== body
      && !body.previousElementSibling?.classList.contains('conversation-answer-label')) {
    // The answer rubric is allowed before prose. Moving a retained Markdown
    // body just to put it back would blur focused code/table controls on replay.
    node.prepend(body);
  }
  body.className = 'md-content';
  const setBodyHtml = block.terminal ? setCompletedMarkdownHtml : setStreamingMarkdownHtml;
  if (turn?.actor === 'human') disposeRichMarkdown(body);
  if (turn?.actor === 'human' || !renderRichMarkdown(body, block.markdown, block.terminal, ports, turn?.source.conversationId, turn?.turnId)) setBodyHtml(body, ports.renderSafeMarkdownHtml(
    block.displayMarkdown ?? block.markdown,
    {
      ...(turn?.actor === 'human' ? { escapeRawHtml: true } : {}),
      /* A non-terminal text block is the streaming hot path: skip hljs/KaTeX
       * per frame; the terminal re-render restores both. */
      ...(!block.terminal ? { streaming: true } : {}),
    },
  ));
  renderTranslationAlternative(node, block, ports);
  let answerLabel = directClassChild<HTMLElement>(node, 'conversation-answer-label');
  if (block.deliverable && turn?.role === 'assistant') {
    if (!answerLabel) {
      answerLabel = node.ownerDocument.createElement('div');
      answerLabel.className = 'conversation-answer-label';
    }
    updateAnswerNavigation(answerLabel, body, ports);
    if (answerLabel.nextElementSibling !== body) node.insertBefore(answerLabel, body);
  } else answerLabel?.remove();
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
  /** Newest-first reading: every opening rests at the end. */
  end: EndAnchoredScroller;
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
   * never towers over the answer. A grouped reply merges every round into
   * this one disclosure, so it re-enters 'active' each round: only an
   * automatic opening collapses; the reader's own toggle stays put. */
  const wasActive = details.dataset.state === 'active';
  if (block.terminal && wasActive && details.dataset.readerChoice !== 'true') details.open = false;
  details.dataset.state = block.terminal ? 'complete' : 'active';
  let summary = directClassChild<HTMLElement>(details, 'thinking-header');
  if (!summary) {
    summary = document.createElement('summary');
    const owner = details;
    summary.addEventListener('click', () => { owner.dataset.readerChoice = 'true'; });
    details.prepend(summary);
  }
  summary.className = 'thinking-header';
  let glyph = directClassChild<HTMLElement>(summary, 'thinking-icon');
  if (!glyph) {
    glyph = document.createElement('span');
    glyph.className = 'thinking-icon';
    glyph.setAttribute('aria-hidden', 'true');
    summary.prepend(glyph);
  }
  setManagedHtml(glyph, iconHtml('brain'));
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
  // The open body is a bounded scroller; keyboard readers need to reach it.
  if (content.tabIndex !== 0) content.tabIndex = 0;
  if (content.getAttribute('role') !== 'region') content.setAttribute('role', 'region');
  if (content.getAttribute('aria-label') !== labelText) content.setAttribute('aria-label', labelText);
  const scroller = content;
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
      end: endAnchoredScroller(scroller, body),
      render() {},
      dispose() {
        disclosure.removeEventListener('toggle', onToggle);
        next.end.dispose();
        next.render = () => {};
      },
    };
    const onToggle = (): void => {
      if (!disclosure.open) return;
      next.render();
      next.end.pin();
    };
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
    current.end.follow();
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

/* Client-side tick for the sparse wait phases. The backend only ships a new
 * elapsed frame on power-of-two heartbeats (RetryPhaseEventBudget), so the
 * visible counter would otherwise freeze between server frames. We re-anchor
 * on every real render and advance the number locally once per second. This
 * costs no network/server work: it only rewrites one text node in place. */
interface WaitTickState {
  /** Server-reported elapsed seconds at anchor time. */
  anchor: number;
  /** Client clock (ms) when the anchor frame was rendered. */
  at: number;
  prefix: string;
  suffix: string;
}
const waitTickStates = new WeakMap<HTMLElement, WaitTickState>();
/* Live-status node -> backend epoch-ms start of the call it reports. */
const elapsedTickStates = new WeakMap<HTMLElement, number>();
let waitTickTimer: ReturnType<typeof setInterval> | null = null;

function elapsedSince(startedAt: number): string {
  return formatDuration((Date.now() - startedAt) / 1000);
}

function waitTickEligible(block: Extract<ConversationBlockViewModel, { kind: 'live-status' }>): boolean {
  const value = block.value;
  return value.phase === 'waiting_model'
    && (value.detailKey === 'stream.phase.waitingForResponse'
      || value.detailKey === 'stream.phase.waitingQuotaReset'
      || value.detailKey === 'stream.phase.streamStalled')
    && typeof value.detailArgs?.elapsed === 'number'
    && Number.isFinite(value.detailArgs.elapsed);
}

function ensureWaitTickTimer(): void {
  if (waitTickTimer != null) return;
  waitTickTimer = setInterval(() => {
    /* The WeakMap is not iterable; scan live nodes each second. There is at
     * most one live-status block per active turn, so this stays trivial. */
    for (const node of Array.from(
      document.querySelectorAll<HTMLElement>('.conversation-block--live-status'),
    )) {
      const startedAt = elapsedTickStates.get(node);
      const clock = startedAt == null ? null : node.querySelector<HTMLElement>('.stream-phase-elapsed');
      if (clock && startedAt != null) {
        const elapsed = elapsedSince(startedAt);
        if (clock.textContent !== elapsed) clock.textContent = elapsed;
      }
      const state = waitTickStates.get(node);
      if (!state) continue;
      const target = node.querySelector<HTMLElement>('.stream-phase-text');
      if (!target) continue;
      const elapsed = state.anchor + Math.floor((Date.now() - state.at) / 1000);
      const next = state.prefix + elapsed + state.suffix;
      if (target.textContent !== next) target.textContent = next;
    }
  }, 1000);
  /* Do not keep the event loop alive just for the tick (browser no-op). */
  (waitTickTimer as unknown as { unref?: () => void }).unref?.();
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
  const goal = value.goalProgress;
  let label = presentLivePhaseLabel(liveStatusPhase(value),
    (key, fallback, args) => textFor(ports, key, fallback, args));
  if (goal && phase !== 'storage_wedged') {

    const roleKeys: Record<string, [string, string]> = {
      worker: ['stream.goal.role.worker', 'Worker'],
      planner: ['stream.goal.role.planner', 'Planner'],
      critic: ['stream.goal.role.critic', 'Critic'],
      virtual_user: ['stream.goal.role.virtualUser', 'Virtual user'],
    };
    const [key, fallback] = roleKeys[goal.role] ?? ['', goal.role];
    const role = textFor(ports, key, fallback);
    const context = textFor(ports,
      goal.rootSummary ? 'stream.goal.activeRoot' : 'stream.goal.activePhase',
      goal.rootSummary ? 'Run ongoing · Iteration {iteration} · {role}' : 'Iteration {iteration} · {role}',
      { iteration: goal.iteration, role });
    label = `${context}${label ? ' · ' + label : ''}`;
    if (goal.workers.length) label += ' · ' + goal.workers.map(worker =>
      worker.text + ' · ' + swarmAgentPhaseLabel(worker,
        (key, fallback, args) => textFor(ports, key, fallback, args))
      + (worker.latestTool ? ' · ' + textFor(ports, 'stream.goal.latestTool',
        'Latest tool: {tool}', { tool: worker.latestTool }) : '')
      + (worker.toolError ? ' · ' + textFor(ports,
        'stream.goal.toolError', 'Latest tool error: {error}', { error: worker.toolError }) : '')
    ).join('; ');
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
  if (waitTickEligible(block)) {
    const elapsed = Number(value.detailArgs!.elapsed);
    const anchor = String(elapsed);
    const at = label.indexOf(anchor);
    if (at >= 0) {
      waitTickStates.set(node, {
        anchor: elapsed,
        at: Date.now(),
        prefix: label.slice(0, at),
        suffix: label.slice(at + anchor.length),
      });
      ensureWaitTickTimer();
    } else {
      waitTickStates.delete(node);
    }
  } else {
    waitTickStates.delete(node);
  }
  if (phase === 'thinking_active' && value.thinkingLength != null) {
    const counter = document.createElement('span');
    counter.className = 'stream-phase-counter';
    counter.textContent = textFor(
      ports, 'stream.phase.chars', `${value.thinkingLength} chars`,
      { n: value.thinkingLength },
    );
    textNode.appendChild(counter);
  }
  const startedAt = value.startedAt;
  if (startedAt != null) {
    const clock = document.createElement('span');
    clock.className = 'stream-phase-ctx stream-phase-elapsed';
    /* A per-second rewrite inside the polite live region would be announced
     * every second; the phase text carries the announcement. */
    clock.setAttribute('aria-hidden', 'true');
    clock.textContent = elapsedSince(startedAt);
    textNode.appendChild(clock);
    elapsedTickStates.set(node, startedAt);
    ensureWaitTickTimer();
  } else {
    elapsedTickStates.delete(node);
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

/** Plain text only: recorded model IDs never cross the Markdown/HTML boundary. */
function renderTranslationModel(
  parent: HTMLElement,
  model: string | undefined,
  ports: ClassicConversationRendererPorts,
  className: string,
  visible: boolean,
): void {
  let badge = directClassChild<HTMLElement>(parent, className);
  if (!visible) { badge?.remove(); return; }
  if (!badge) {
    badge = parent.ownerDocument.createElement('span');
    badge.classList.add('bilingual-model', className);
    parent.appendChild(badge);
  }
  const label = model?.trim()
    ? textFor(ports, 'chat.translationModel', 'Translated by {model}', { model })
    : textFor(ports, 'chat.translationModelUnknown', 'Translation model not reported');
  badge.textContent = label;
  badge.title = label;
}

function renderTranslationAlternative(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'text' | 'thinking' }>,
  ports: ClassicConversationRendererPorts,
): void {
  let details = directClassChild<HTMLDetailsElement>(node, 'bilingual-block');

  // A translated answer needs attribution outside its closed Original fold.
  // With no alternate body (e.g. a reloaded translated input lacking its
  // original), retain the canonical receipt without inventing a disclosure.
  // The Turn's one translation receipt rides its answer or input; mid-turn
  // narration never repeats it under every paragraph.
  renderTranslationModel(node, block.kind === 'text' ? block.translationModel : undefined,
    ports, 'bilingual-attribution', block.kind === 'text'
      && (block.terminal || block.deliverable)
      && ((block.displayMode === 'translated' && Boolean(block.translatedMarkdown?.trim()))
        || Boolean(block.inputTranslationCompleted)));

  const attribution = directClassChild<HTMLElement>(node, 'bilingual-attribution');
  const answer = directClassChild<HTMLElement>(node, 'md-content');
  if (attribution && answer && answer.nextSibling !== attribution) answer.after(attribution);
  /* Only the deliverable answer carries the per-block original/translation
   * alternative. Mid-turn narration and thinking follow the turn-level
   * display mode — a per-block toggle there is timing-dependent noise. */
  if (block.kind !== 'text' || !block.deliverable
      || !block.translatedMarkdown?.trim()) {
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

  renderTranslationModel(summary, block.translationModel, ports, 'bilingual-model',
    alternativeMode === 'translated');
  let toggle = directClassChild<HTMLElement>(summary, 'bilingual-toggle');
  if (!toggle) {
    toggle = disclosureChevron(document, 'bilingual-toggle');
    summary.appendChild(toggle);
  } else if (!toggle.classList.contains('conversation-disclosure-chevron')) {
    const replacement = disclosureChevron(document, 'bilingual-toggle');
    toggle.replaceWith(replacement);
    toggle = replacement;
  }
  if (summary.lastElementChild !== toggle) summary.appendChild(toggle);
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
