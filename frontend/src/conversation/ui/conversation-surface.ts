/**
 * Keyed DOM owner for a conversation view model.
 *
 * This module owns every content-derived node below its root.  It has no
 * transport, persistence, retry, or settlement logic; renderers receive typed
 * blocks and emit interaction intents back to the application layer.
 */
import './conversation-surface.css';

import type {
  ConversationBlockViewModel,
  ConversationLaneViewModel,
  ConversationQueueItemViewModel,
  ConversationTurnViewModel,
  ConversationViewModel,
  PlanDecisionViewModel,
} from '../presentation/conversation-view-model';

export interface ConversationIntent {
  type: string;
  conversationId: string;
  turnId?: string;
  blockId?: string;
  laneId?: string;
  queueId?: string;
  operation?: string;
}

export interface ConversationBlockRenderContext {
  conversationId: string;
  lane: ConversationLaneViewModel;
  turn: ConversationTurnViewModel;
}

export interface ConversationTurnRenderContext {
  conversationId: string;
  lane: ConversationLaneViewModel;
}

export interface ConversationLaneRenderContext {
  conversationId: string;
}

export interface ConversationSurfaceRenderers {
  renderTurnAvatar?(
    node: HTMLElement,
    turn: ConversationTurnViewModel,
    context: ConversationTurnRenderContext,
  ): void;
  renderBlock?(
    node: HTMLElement,
    block: ConversationBlockViewModel,
    context: ConversationBlockRenderContext,
  ): void;
  renderTurnHeader?(
    node: HTMLElement,
    turn: ConversationTurnViewModel,
    context: ConversationTurnRenderContext,
  ): void;
  renderTurnActions?(
    node: HTMLElement,
    turn: ConversationTurnViewModel,
    context: ConversationTurnRenderContext,
  ): void;
  renderTurnFooter?(
    node: HTMLElement,
    turn: ConversationTurnViewModel,
    context: ConversationTurnRenderContext,
  ): void;
  renderTurnContextRail?(
    node: HTMLElement,
    block: Extract<ConversationBlockViewModel, { kind: 'context' }> | null,
    context: ConversationTurnRenderContext,
  ): void;
  renderPlanDecision?(
    node: HTMLElement,
    decision: PlanDecisionViewModel,
    context: ConversationTurnRenderContext,
  ): void;
  renderLaneHeader?(
    node: HTMLElement,
    lane: ConversationLaneViewModel,
    context: ConversationLaneRenderContext,
  ): void;
  renderQueueItem?(
    node: HTMLElement,
    item: ConversationQueueItemViewModel,
    conversationId: string,
  ): void;
}

export interface ScrollAnchorPort<Snapshot = unknown> {
  capture(root: HTMLElement): Snapshot;
  restore(root: HTMLElement, snapshot: Snapshot): void;
  followLatest?(root: HTMLElement): void;
  dispose?(): void;
}

export const CONVERSATION_DOM_WINDOW_MAX_TURNS = 80;
export const CONVERSATION_DOM_WINDOW_BATCH_TURNS = 20;

export interface ConversationWindowOptions {
  /** A smaller value is allowed; the product hard ceiling remains 80. */
  maxTurns?: number;
  batchSize?: number;
  label?(direction: 'earlier' | 'later', count: number): string;
}

export interface ConversationWindowState {
  start: number;
  end: number;
  total: number;
  maxTurns: number;
  batchSize: number;
}

export interface ConversationViewportOptions {
  nearBottomThreshold?: number;
  scheduleAfterLayout?(callback: () => void): () => void;
}

export interface ConversationScrollSnapshot {
  following: boolean;
  scrollTop: number;
  anchorTurnId?: string;
  anchorOffset?: number;
}

export interface ConversationSurfaceOptions extends ConversationSurfaceRenderers {
  onIntent?(intent: ConversationIntent): void;
  scrollAnchor?: ScrollAnchorPort;
  scrollViewport?: HTMLElement;
  viewport?: ConversationViewportOptions;
  windowing?: ConversationWindowOptions;
}

export interface ConversationSurface {
  readonly root: HTMLElement;
  readonly windowState: ConversationWindowState;
  render(viewModel: ConversationViewModel): void;
  showEarlier(): boolean;
  showLater(): boolean;
  followLatest(): void;
  dispose(): void;
}

function positiveInteger(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function conversationWindowOptions(
  options: ConversationWindowOptions | undefined,
): Required<Pick<ConversationWindowOptions, 'maxTurns' | 'batchSize'>> {
  const maxTurns = Math.min(
    positiveInteger(options?.maxTurns, CONVERSATION_DOM_WINDOW_MAX_TURNS),
    CONVERSATION_DOM_WINDOW_MAX_TURNS,
  );
  const batchSize = Math.min(
    positiveInteger(options?.batchSize, CONVERSATION_DOM_WINDOW_BATCH_TURNS),
    maxTurns,
    CONVERSATION_DOM_WINDOW_BATCH_TURNS,
  );
  return { maxTurns, batchSize };
}

/**
 * Own the user's follow/suspend latch and stable Turn anchor for one scroll
 * viewport. Durable Turns remain in the store; only DOM geometry lives here.
 */
export function createConversationViewportPort(
  viewport: HTMLElement,
  options: ConversationViewportOptions = {},
): ScrollAnchorPort<ConversationScrollSnapshot> & { readonly following: boolean } {
  const threshold = positiveInteger(options.nearBottomThreshold, 80);
  const scheduleAfterLayout = options.scheduleAfterLayout ?? ((callback) => {
    const view = viewport.ownerDocument.defaultView;
    if (view?.requestAnimationFrame) {
      const handle = view.requestAnimationFrame(callback);
      return () => view.cancelAnimationFrame(handle);
    }
    let cancelled = false;
    queueMicrotask(() => { if (!cancelled) callback(); });
    return () => { cancelled = true; };
  });
  let following = true;
  let lastScrollTop: number | null = null;
  let touchY: number | null = null;
  let cancelScheduled: (() => void) | null = null;
  let disposed = false;

  const nearBottom = (): boolean => (
    viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight <= threshold
  );
  const onWheel = (event: WheelEvent): void => {
    if (event.deltaY < 0) following = false;
  };
  const onTouchStart = (event: TouchEvent): void => {
    touchY = event.touches.item(0)?.clientY ?? null;
  };
  const onTouchMove = (event: TouchEvent): void => {
    const nextY = event.touches.item(0)?.clientY;
    if (touchY !== null && nextY !== undefined && nextY > touchY + 4) {
      following = false;
    }
    if (nextY !== undefined) touchY = nextY;
  };
  const onScroll = (): void => {
    const nextTop = viewport.scrollTop;
    if (nearBottom()) following = true;
    else if (lastScrollTop !== null && nextTop < lastScrollTop - 4) {
      following = false;
    }
    lastScrollTop = nextTop;
  };
  viewport.addEventListener('wheel', onWheel, { passive: true });
  viewport.addEventListener('touchstart', onTouchStart, { passive: true });
  viewport.addEventListener('touchmove', onTouchMove, { passive: true });
  viewport.addEventListener('scroll', onScroll, { passive: true });

  const writeBottom = (): void => {
    if (disposed || !following) return;
    viewport.scrollTop = viewport.scrollHeight;
    lastScrollTop = viewport.scrollTop;
  };
  const followLatest = (): void => {
    if (disposed) return;
    following = true;
    writeBottom();
    cancelScheduled?.();
    cancelScheduled = scheduleAfterLayout(() => {
      cancelScheduled = null;
      writeBottom();
    });
  };

  return {
    get following() { return following; },
    capture(root) {
      const viewportTop = viewport.getBoundingClientRect().top;
      const anchor = Array.from(
        root.querySelectorAll<HTMLElement>('[data-turn-id]'),
      ).find((node) => node.getBoundingClientRect().bottom > viewportTop + 1);
      return {
        following,
        scrollTop: viewport.scrollTop,
        ...(anchor?.dataset.turnId ? {
          anchorTurnId: anchor.dataset.turnId,
          anchorOffset: anchor.getBoundingClientRect().top - viewportTop,
        } : {}),
      };
    },
    restore(root, snapshot) {
      if (snapshot.following && following) {
        followLatest();
        return;
      }
      if (following || !snapshot.anchorTurnId) return;
      const anchor = Array.from(
        root.querySelectorAll<HTMLElement>('[data-turn-id]'),
      ).find((node) => node.dataset.turnId === snapshot.anchorTurnId);
      if (!anchor) return;
      const nextOffset = anchor.getBoundingClientRect().top
        - viewport.getBoundingClientRect().top;
      viewport.scrollTop += nextOffset - Number(snapshot.anchorOffset || 0);
      lastScrollTop = viewport.scrollTop;
    },
    followLatest() { followLatest(); },
    dispose() {
      if (disposed) return;
      disposed = true;
      cancelScheduled?.();
      cancelScheduled = null;
      viewport.removeEventListener('wheel', onWheel);
      viewport.removeEventListener('touchstart', onTouchStart);
      viewport.removeEventListener('touchmove', onTouchMove);
      viewport.removeEventListener('scroll', onScroll);
    },
  };
}

type ManagedBlockElement = HTMLElement & {
  _tofuBlock?: ConversationBlockViewModel;
  _tofuBlockRenderer?: ConversationSurfaceRenderers['renderBlock'];
};

type ManagedTurnAvatarElement = HTMLElement & {
  _tofuTurn?: ConversationTurnViewModel;
  _tofuTurnAvatarRenderer?: ConversationSurfaceRenderers['renderTurnAvatar'];
};

type ManagedTurnHeaderElement = HTMLElement & {
  _tofuTurn?: ConversationTurnViewModel;
  _tofuTurnHeaderRenderer?: ConversationSurfaceRenderers['renderTurnHeader'];
};

type ManagedTurnActionsElement = HTMLElement & {
  _tofuTurn?: ConversationTurnViewModel;
  _tofuTurnActionsRenderer?: ConversationSurfaceRenderers['renderTurnActions'];
};

type ManagedTurnFooterElement = HTMLElement & {
  _tofuTurn?: ConversationTurnViewModel;
  _tofuTurnFooterRenderer?: ConversationSurfaceRenderers['renderTurnFooter'];
};

type ManagedTurnContextRailElement = HTMLElement & {
  _tofuContextBlock?: Extract<ConversationBlockViewModel, { kind: 'context' }>;
  _tofuTurnContextRailRenderer?: ConversationSurfaceRenderers['renderTurnContextRail'];
  _tofuTurnContextRailRendered?: boolean;
};

type ManagedPlanDecisionElement = HTMLElement & {
  _tofuPlanDecision?: PlanDecisionViewModel;
  _tofuPlanDecisionRenderer?: ConversationSurfaceRenderers['renderPlanDecision'];
};

type ManagedTurnElement = HTMLElement;

type ManagedLaneHeaderElement = HTMLElement & {
  _tofuLane?: ConversationLaneViewModel;
  _tofuLaneHeaderRenderer?: ConversationSurfaceRenderers['renderLaneHeader'];
};

type ManagedQueueElement = HTMLElement & {
  _tofuQueueItem?: ConversationQueueItemViewModel;
  _tofuQueueRenderer?: ConversationSurfaceRenderers['renderQueueItem'];
};

function directPart(parent: Element, part: string): HTMLElement | null {
  return Array.from(parent.children).find(
    (node) => (node as HTMLElement).dataset.conversationPart === part,
  ) as HTMLElement | undefined ?? null;
}

function ensurePart(
  parent: HTMLElement,
  part: string,
  tagName = 'div',
): HTMLElement {
  const existing = directPart(parent, part);
  if (existing) {
    /* A part is a singleton slot. If an earlier transition left a duplicate,
     * drop the extras so the managed node is the only one that renders. */
    let kept = false;
    for (const node of Array.from(parent.children) as HTMLElement[]) {
      if (node.dataset.conversationPart !== part) continue;
      if (!kept && node === existing) { kept = true; continue; }
      node.remove();
    }
    return existing;
  }
  const node = parent.ownerDocument.createElement(tagName);
  node.dataset.conversationPart = part;
  node.className = `conversation-${part}`;
  parent.appendChild(node);
  return node;
}

function directKeyedChildren(
  parent: HTMLElement,
  key: 'turnId' | 'blockId' | 'laneId' | 'queueId',
): Map<string, HTMLElement> {
  const result = new Map<string, HTMLElement>();
  for (const child of Array.from(parent.children) as HTMLElement[]) {
    const value = child.dataset[key];
    if (value) result.set(value, child);
  }
  return result;
}

function placeAt(parent: HTMLElement, node: HTMLElement, index: number): void {
  const current = parent.children.item(index);
  if (current !== node) parent.insertBefore(node, current ?? null);
}

function hasRenderableContent(node: HTMLElement): boolean {
  return node.childElementCount > 0 || Boolean(node.textContent?.trim());
}

function jsonText(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? '';
  } catch {
    return String(value ?? '');
  }
}

/**
 * Compare JSON-like presentation values without allocating a serialized copy.
 *
 * Full conversation snapshots legitimately recreate contract objects. Object
 * identity alone would therefore re-run every block renderer even when its
 * visible value was unchanged. The depth and node budgets keep this check a
 * bounded UI optimization; exceeding either budget safely falls back to a
 * render instead of risking an unbounded walk on an unexpected payload.
 */
const STRUCTURED_COMPARE_MAX_DEPTH = 32;
const STRUCTURED_COMPARE_MAX_NODES = 4096;

interface StructuredCompareBudget {
  remaining: number;
}

function sameStructuredValue(
  previous: unknown,
  current: unknown,
  budget: StructuredCompareBudget,
  depth = 0,
): boolean {
  if (Object.is(previous, current)) return true;
  if (previous === null || current === null
      || typeof previous !== 'object' || typeof current !== 'object') {
    return false;
  }
  if (depth >= STRUCTURED_COMPARE_MAX_DEPTH || budget.remaining <= 0) {
    return false;
  }
  budget.remaining -= 1;
  const previousIsArray = Array.isArray(previous);
  const currentIsArray = Array.isArray(current);
  if (previousIsArray !== currentIsArray) return false;
  if (previousIsArray && currentIsArray) {
    if (previous.length !== current.length) return false;
    return previous.every((value, index) => sameStructuredValue(
      value, current[index], budget, depth + 1,
    ));
  }
  const previousPrototype = Object.getPrototypeOf(previous);
  const currentPrototype = Object.getPrototypeOf(current);
  if (previousPrototype !== currentPrototype
      || (previousPrototype !== Object.prototype && previousPrototype !== null)) {
    return false;
  }
  const previousRecord = previous as Record<string, unknown>;
  const currentRecord = current as Record<string, unknown>;
  const previousKeys = Object.keys(previousRecord);
  const currentKeys = Object.keys(currentRecord);
  if (previousKeys.length !== currentKeys.length) return false;
  return previousKeys.every((key) => (
    Object.prototype.hasOwnProperty.call(currentRecord, key)
    && sameStructuredValue(
      previousRecord[key], currentRecord[key], budget, depth + 1,
    )
  ));
}

function samePresentationValue(previous: unknown, current: unknown): boolean {
  return sameStructuredValue(previous, current, {
    remaining: STRUCTURED_COMPARE_MAX_NODES,
  });
}

function renderDefaultBlock(
  node: HTMLElement,
  block: ConversationBlockViewModel,
): void {
  node.replaceChildren();
  const document = node.ownerDocument;
  if (block.kind === 'text') {
    const body = document.createElement('div');
    body.className = 'conversation-block__markdown';
    body.textContent = block.markdown;
    node.appendChild(body);
    return;
  }
  if (block.kind === 'thinking') {
    const details = document.createElement('details');
    details.className = 'conversation-block__thinking';
    const summary = document.createElement('summary');
    summary.textContent = 'Thinking';
    const body = document.createElement('div');
    body.textContent = block.markdown;
    details.append(summary, body);
    node.appendChild(details);
    return;
  }
  if (block.kind === 'tool') {
    const details = document.createElement('details');
    details.className = 'conversation-block__tool';
    const summary = document.createElement('summary');
    summary.textContent = block.name || 'Tool';
    const input = document.createElement('pre');
    input.dataset.conversationPart = 'tool-input';
    input.textContent = jsonText(block.input);
    const result = document.createElement('pre');
    result.dataset.conversationPart = 'tool-result';
    result.textContent = jsonText(block.result);
    details.append(summary, input, result);
    node.appendChild(details);
    return;
  }
  if (block.kind === 'attachments') {
    const total = block.images.length + block.videos.length
      + block.pdfTexts.length + block.conversationReferences.length
      + block.replyQuotes.length;
    node.textContent = `${total} attachment${total === 1 ? '' : 's'}`;
    return;
  }
  if (block.kind === 'injections') {
    node.textContent = `${block.channel}: ${block.items.length}`;
    return;
  }
  if (block.kind === 'file-changes') {
    node.textContent = `${block.count} file change${block.count === 1 ? '' : 's'}`;
    return;
  }
  if (block.kind === 'provenance') {
    const labels = [
      block.value.memoryPrefetch ? 'memory' : '',
      block.value.preferencesApplied ? 'preferences' : '',
      block.value.preferencesLearned?.length ? 'learned' : '',
      block.value.relatedConversations ? 'related' : '',
      block.value.mcpLoginHint ? 'login' : '',
    ].filter(Boolean);
    node.textContent = labels.join(' · ');
    return;
  }
  if (block.kind === 'image-generation') {
    node.textContent = `${block.value.results.length} generated image result${
      block.value.results.length === 1 ? '' : 's'}`;
    return;
  }
  if (block.kind === 'proposed-plan') {
    const title = document.createElement('strong');
    title.textContent = 'Proposed Plan';
    const body = document.createElement('div');
    body.className = 'conversation-block__markdown';
    body.textContent = block.value.text;
    node.append(title, body);
    return;
  }
  if (block.kind === 'plan-execution') {
    const title = document.createElement('strong');
    title.textContent = block.value.contextMode === 'fresh'
      ? 'Executing plan · fresh task context'
      : 'Executing plan · current context';
    node.appendChild(title);
    return;
  }
  if (block.kind === 'artifacts') {
    node.textContent = `${block.artifacts.length} artifact${
      block.artifacts.length === 1 ? '' : 's'}`;
    return;
  }
  if (block.kind === 'autopilot-run-notice') {
    node.textContent = `Autopilot ended: ${block.value.reason}`;
    return;
  }
  if (block.kind === 'activity-event') {
    node.dataset.activityId = block.value.id;
    node.textContent = block.value.summary
      || [block.value.kind, block.value.status].filter(Boolean).join(' · ');
    return;
  }
  if (block.kind === 'live-status') {
    node.textContent = [block.value.label, block.value.detail]
      .filter(Boolean).join(' · ');
    return;
  }
}

function sameBlock(
  previous: ConversationBlockViewModel | undefined,
  current: ConversationBlockViewModel,
): boolean {
  if (!previous || previous.kind !== current.kind
      || previous.blockId !== current.blockId) return false;
  if (previous.kind === 'text' && current.kind === 'text') {
    return previous.markdown === current.markdown
      && previous.translatedMarkdown === current.translatedMarkdown
      && previous.displayMarkdown === current.displayMarkdown
      && previous.displayMode === current.displayMode
      && previous.deliverable === current.deliverable
      && previous.terminal === current.terminal
      && previous.resumable === current.resumable;
  }
  if (previous.kind === 'thinking' && current.kind === 'thinking') {
    return previous.markdown === current.markdown
      && previous.translatedMarkdown === current.translatedMarkdown
      && previous.displayMarkdown === current.displayMarkdown
      && previous.displayMode === current.displayMode
      && previous.terminal === current.terminal
      && previous.signature === current.signature;
  }
  if (previous.kind === 'proposed-plan' && current.kind === 'proposed-plan') {
    return previous.markdown === current.markdown
      && previous.translatedMarkdown === current.translatedMarkdown
      && previous.displayMarkdown === current.displayMarkdown
      && previous.displayMode === current.displayMode
      && previous.translationPending === current.translationPending
      && previous.translationStreaming === current.translationStreaming;
  }
  if (previous.kind === 'tool' && current.kind === 'tool') {
    return previous.toolCallId === current.toolCallId
      && previous.name === current.name
      && samePresentationValue(previous.input, current.input)
      && samePresentationValue(previous.result, current.result)
      && samePresentationValue(previous.round, current.round);
  }
  if (previous.kind === 'attachments' && current.kind === 'attachments') {
    return samePresentationValue(previous.images, current.images)
      && samePresentationValue(previous.videos, current.videos)
      && samePresentationValue(previous.pdfTexts, current.pdfTexts)
      && samePresentationValue(
        previous.conversationReferences, current.conversationReferences,
      )
      && samePresentationValue(previous.replyQuotes, current.replyQuotes);
  }
  if (previous.kind === 'injections' && current.kind === 'injections') {
    return previous.channel === current.channel
      && previous.anchorLlmRound === current.anchorLlmRound
      && samePresentationValue(previous.items, current.items);
  }
  if (previous.kind === 'file-changes' && current.kind === 'file-changes') {
    return previous.count === current.count
      && samePresentationValue(previous.files, current.files)
      && previous.state === current.state
      && previous.commandAvailable === current.commandAvailable
      && samePresentationValue(previous.error, current.error);
  }
  if (previous.kind === 'provenance' && current.kind === 'provenance') {
    return samePresentationValue(previous.value, current.value);
  }
  if (previous.kind === 'activity-event'
      && current.kind === 'activity-event') {
    return samePresentationValue(previous.value, current.value)
      && samePresentationValue(previous.terminalError, current.terminalError);
  }
  if (previous.kind === 'autopilot-run-notice'
      && current.kind === 'autopilot-run-notice') {
    return samePresentationValue(previous.value, current.value);
  }
  if (previous.kind === 'origin' && current.kind === 'origin') {
    return samePresentationValue(previous.value, current.value);
  }
  if (previous.kind === 'context' && current.kind === 'context') {
    return samePresentationValue(previous.value, current.value);
  }
  if (previous.kind === 'compaction' && current.kind === 'compaction') {
    return previous.summaryMarkdown === current.summaryMarkdown
      && samePresentationValue(previous.value, current.value);
  }
  if (previous.kind === 'image-generation'
      && current.kind === 'image-generation') {
    return samePresentationValue(previous.value, current.value);
  }
  if (previous.kind === 'plan-execution'
      && current.kind === 'plan-execution') {
    return samePresentationValue(previous.value, current.value);
  }
  if (previous.kind === 'artifacts' && current.kind === 'artifacts') {
    return samePresentationValue(previous.artifacts, current.artifacts);
  }
  if (previous.kind === 'live-status' && current.kind === 'live-status') {
    return samePresentationValue(previous.value, current.value);
  }
  return samePresentationValue(previous.source, current.source);
}

function turnTimestamp(
  turn: ConversationTurnViewModel,
): string | number | undefined {
  return turn.source?.projection?.timestamp ?? turn.source?.createdAt;
}

function sameFinish(
  previous: ConversationTurnViewModel['finish'],
  current: ConversationTurnViewModel['finish'],
): boolean {
  if (previous === current) return true;
  if (!previous || !current) return false;
  return previous.tone === current.tone
    && previous.label === current.label
    && previous.detail === current.detail
    && previous.errorKind === current.errorKind
    && previous.retryable === current.retryable
    && (previous.resumeOptions?.length ?? 0) === (current.resumeOptions?.length ?? 0)
    && (previous.resumeOptions ?? []).every((option, index) => {
      const next = current.resumeOptions?.[index];
      return option.operation === next?.operation && option.anchor === next?.anchor;
    });
}

function sameTurnHeader(
  previous: ConversationTurnViewModel | undefined,
  current: ConversationTurnViewModel,
): boolean {
  return Boolean(previous
    && previous.actor === current.actor
    && previous.role === current.role
    && previous.kind === current.kind
    && previous.status === current.status
    && previous.attemptId === current.attemptId
    && previous.commandPending === current.commandPending
    && Object.is(turnTimestamp(previous), turnTimestamp(current))
    && samePresentationValue(
      previous.metadata.orchestration, current.metadata.orchestration,
    )
    && samePresentationValue(previous.metadata.origin, current.metadata.origin)
    && sameFinish(previous.finish, current.finish));
}

function sameTurnAvatar(
  previous: ConversationTurnViewModel | undefined,
  current: ConversationTurnViewModel,
): boolean {
  return Boolean(previous
    && previous.actor === current.actor
    && previous.role === current.role
    && previous.kind === current.kind
    && samePresentationValue(previous.metadata.origin, current.metadata.origin));
}

function renderDefaultTurnAvatar(
  node: HTMLElement,
  turn: ConversationTurnViewModel,
): void {
  node.className = 'conversation-turn-avatar message-avatar';
  node.setAttribute('aria-hidden', 'true');
  const paths: Record<ConversationTurnViewModel['actor'], string> = {
    human: 'M8 2.25a5.75 5.75 0 1 0 0 11.5 5.75 5.75 0 0 0 0-11.5Z',
    assistant: 'M8 1.75 9.45 6.4 14 8l-4.55 1.6L8 14.25 6.55 9.6 2 8l4.55-1.6L8 1.75Z',
    planner: 'M8 2.25 13.75 8 8 13.75 2.25 8 8 2.25Z',
    critic: 'M8 1.75 13 3.8v3.7c0 3.1-2 5.45-5 6.75-3-1.3-5-3.65-5-6.75V3.8l5-2.05Z',
    virtual_user: 'M8 2.5a2.25 2.25 0 1 0 0 4.5 2.25 2.25 0 0 0 0-4.5Zm-4.25 10c.45-2.15 1.9-3.3 4.25-3.3s3.8 1.15 4.25 3.3',
  };
  const svg = node.ownerDocument.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  svg.setAttribute('fill', 'none');
  const path = node.ownerDocument.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', paths[turn.actor]);
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', '1.35');
  path.setAttribute('stroke-linecap', 'round');
  path.setAttribute('stroke-linejoin', 'round');
  svg.appendChild(path);
  node.replaceChildren(svg);
}

function sameTurnActions(
  previous: ConversationTurnViewModel | undefined,
  current: ConversationTurnViewModel,
): boolean {
  const previousActions = previous?.actions ?? [];
  const currentActions = current.actions ?? [];
  if (!previous || previousActions.length !== currentActions.length) return false;
  return previousActions.every((action, index) => {
    const next = currentActions[index];
    return action.action === next?.action
      && action.operation === next?.operation
      && action.disabled === next?.disabled;
  });
}

function sameTurnFooter(
  previous: ConversationTurnViewModel | undefined,
  current: ConversationTurnViewModel,
): boolean {
  const previousFallback = previous?.metadata.fallback;
  const currentFallback = current.metadata.fallback;
  const terminal = ['completed', 'interrupted', 'truncated', 'failed']
    .includes(current.status);
  return Boolean(previous
    && sameFinish(previous.finish, current.finish)
    && previous.status === current.status
    && previous.commandPending === current.commandPending
    && (!terminal || previous.projectionRevision === current.projectionRevision)
    && previous.metadata.model === current.metadata.model
    && previous.metadata.preset === current.metadata.preset
    && previous.metadata.translation.pending === current.metadata.translation.pending
    && previousFallback?.model === currentFallback?.model
    && previousFallback?.from === currentFallback?.from
    && previousFallback?.reason === currentFallback?.reason
    && previousFallback?.kind === currentFallback?.kind);
}

function renderDefaultTurnHeader(
  node: HTMLElement,
  turn: ConversationTurnViewModel,
): void {
  node.replaceChildren();
  const actor = node.ownerDocument.createElement('span');
  actor.className = 'conversation-turn__actor';
  actor.textContent = turn.actor;
  const status = node.ownerDocument.createElement('span');
  status.className = 'conversation-turn__status';
  status.textContent = turn.commandPending || turn.finish?.label || turn.status;
  node.append(actor, status);
}

function renderDefaultTurnActions(
  node: HTMLElement,
  turn: ConversationTurnViewModel,
): void {
  node.replaceChildren();
  for (const action of turn.actions ?? []) {
    const button = node.ownerDocument.createElement('button');
    button.type = 'button';
    button.className = `msg-action-btn conversation-action--${action.action}`;
    button.dataset.conversationAction = action.action;
    if (action.operation) button.dataset.operation = action.operation;
    button.disabled = action.disabled;
    button.textContent = action.action;
    node.appendChild(button);
  }
}

function renderDefaultTurnFooter(
  node: HTMLElement,
  turn: ConversationTurnViewModel,
): void {
  node.replaceChildren();
  if (turn.actor === 'human' || !turn.finish) return;
  const status = node.ownerDocument.createElement('span');
  status.className = `conversation-finish conversation-finish--${turn.finish.tone}`;
  status.textContent = [turn.finish.label, turn.finish.detail]
    .filter(Boolean).join(': ');
  node.appendChild(status);
}

function renderDefaultTurnContextRail(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'context' }> | null,
): void {
  node.className = 'conversation-turn-context-rail turn-ctx';
  node.replaceChildren();
  if (!block) {
    node.hidden = true;
    return;
  }
  const snapshot = block.value.snapshot;
  const summary = [snapshot.model, snapshot.depth]
    .filter((value): value is string => typeof value === 'string' && Boolean(value));
  node.textContent = summary.join(' · ');
  node.hidden = !node.textContent;
}

function samePlanDecision(
  previous: PlanDecisionViewModel | undefined,
  current: PlanDecisionViewModel,
): boolean {
  return Boolean(previous
    && previous.sourceTurnId === current.sourceTurnId
    && previous.sourceProjectionRevision === current.sourceProjectionRevision
    && previous.planId === current.planId
    && previous.pending === current.pending);
}

function renderDefaultPlanDecision(
  node: HTMLElement,
  decision: PlanDecisionViewModel,
): void {
  node.textContent = decision.pending ? 'Starting plan…' : 'Plan ready';
}

function sameLaneHeader(
  previous: ConversationLaneViewModel | undefined,
  current: ConversationLaneViewModel,
): boolean {
  return Boolean(previous
    && previous.title === current.title
    && previous.label === current.label
    && previous.icon === current.icon
    && previous.kind === current.kind
    && previous.anchorText === current.anchorText
    && previous.parentSelection === current.parentSelection
    && previous.expanded === current.expanded
    && previous.live === current.live
    && previous.humanTurnCount === current.humanTurnCount);
}

function renderDefaultLaneHeader(
  node: HTMLElement,
  lane: ConversationLaneViewModel,
): void {
  node.textContent = [lane.icon, lane.label || lane.title].filter(Boolean).join(' ');
}

function renderDefaultQueueItem(
  node: HTMLElement,
  item: ConversationQueueItemViewModel,
): void {
  node.textContent = item.text;
}

function sameQueueItem(
  previous: ConversationQueueItemViewModel | undefined,
  current: ConversationQueueItemViewModel,
): boolean {
  if (!previous) return false;
  return previous.queueId === current.queueId
    && previous.position === current.position
    && previous.kind === current.kind
    && previous.text === current.text
    && previous.source.priority === current.source.priority
    && previous.source.hasImages === current.source.hasImages
    && previous.source.hasPdfs === current.source.hasPdfs
    && previous.source.hasRefs === current.source.hasRefs
    && previous.source.hasQuotes === current.source.hasQuotes
    && previous.source.isPeerMessage === current.source.isPeerMessage
    && previous.source.isPeerHuman === current.source.isPeerHuman
    && previous.source.fromConv === current.source.fromConv;
}

/** Construct one lifecycle-bound owner for a conversation container. */
export function createConversationSurface(
  container: HTMLElement,
  options: ConversationSurfaceOptions = {},
): ConversationSurface {
  const root = container.ownerDocument.createElement('section');
  root.className = 'conversation-surface';
  root.dataset.conversationSurface = 'turn-store';
  container.appendChild(root);
  const windowOptions = conversationWindowOptions(options.windowing);
  const ownedScrollAnchor = !options.scrollAnchor && options.scrollViewport
    ? createConversationViewportPort(options.scrollViewport, options.viewport)
    : null;
  const scrollAnchor = options.scrollAnchor ?? ownedScrollAnchor;
  let disposed = false;
  let currentConversationId = '';
  let latestViewModel: ConversationViewModel | null = null;
  const laneWindows = new Map<string, {
    start: number;
    atTail: boolean;
    firstVisibleTurnId: string;
  }>();
  let laneBudgets = new Map<string, number>();
  let lanesById = new Map<string, ConversationLaneViewModel>();

  const emitIntent = (event: Event): void => {
    const target = event.target instanceof Element
      ? event.target.closest<HTMLElement>(
        '[data-conversation-action], [data-conversation-window-action]',
      ) : null;
    if (!target || !root.contains(target)) return;
    const windowAction = target.dataset.conversationWindowAction;
    if (windowAction === 'earlier' || windowAction === 'later') {
      event.preventDefault();
      moveWindow(windowAction, target.dataset.laneId || 'main');
      return;
    }
    const turnNode = target.closest<HTMLElement>('[data-turn-id]');
    const blockNode = target.closest<HTMLElement>('[data-block-id]');
    const laneNode = target.closest<HTMLElement>('[data-lane-id]');
    const queueNode = target.closest<HTMLElement>('[data-queue-id]');
    options.onIntent?.({
      type: target.dataset.conversationAction ?? '',
      conversationId: currentConversationId,
      ...(turnNode?.dataset.turnId ? { turnId: turnNode.dataset.turnId } : {}),
      ...(blockNode?.dataset.blockId ? { blockId: blockNode.dataset.blockId } : {}),
      ...(laneNode?.dataset.laneId ? { laneId: laneNode.dataset.laneId } : {}),
      ...(queueNode?.dataset.queueId ? { queueId: queueNode.dataset.queueId } : {}),
      ...(target.dataset.operation ? { operation: target.dataset.operation } : {}),
    });
  };
  root.addEventListener('click', emitIntent);

  const renderBlocks = (
    containerNode: HTMLElement,
    blocks: ReadonlyArray<ConversationBlockViewModel>,
    lane: ConversationLaneViewModel,
    turn: ConversationTurnViewModel,
  ): void => {
    const existing = directKeyedChildren(containerNode, 'blockId');
    blocks.forEach((block, index) => {
      let node = existing.get(block.blockId) as ManagedBlockElement | undefined;
      if (!node) {
        node = containerNode.ownerDocument.createElement('section') as ManagedBlockElement;
        node.dataset.blockId = block.blockId;
        node.className = 'conversation-block';
      }
      node.dataset.blockKind = block.kind;
      node.dataset.identitySource = block.identitySource;
      const renderer = options.renderBlock;
      if (!sameBlock(node._tofuBlock, block)
          || node._tofuBlockRenderer !== renderer) {
        (renderer ?? renderDefaultBlock)(node, block, {
          conversationId: currentConversationId,
          lane,
          turn,
        });
        node._tofuBlock = block;
        node._tofuBlockRenderer = renderer;
      }
      placeAt(containerNode, node, index);
      existing.delete(block.blockId);
    });
    for (const node of existing.values()) node.remove();
  };

  const renderLane = (
    laneContainer: HTMLElement,
    lane: ConversationLaneViewModel,
    includeHeader: boolean,
  ): void => {
    laneContainer.dataset.laneId = lane.laneId;
    laneContainer.dataset.laneKind = lane.kind;
    if (includeHeader) {
      laneContainer.className = 'conversation-lane conversation-lane--branch branch-panel';
      laneContainer.dataset.laneExpanded = String(Boolean(lane.expanded));
      laneContainer.dataset.laneLive = String(Boolean(lane.live));
    }
    if (includeHeader) {
      const header = ensurePart(
        laneContainer, 'lane-header', 'header',
      ) as ManagedLaneHeaderElement;
      const renderer = options.renderLaneHeader;
      if (!sameLaneHeader(header._tofuLane, lane)
          || header._tofuLaneHeaderRenderer !== renderer) {
        (renderer ?? renderDefaultLaneHeader)(header, lane, {
          conversationId: currentConversationId,
        });
        header._tofuLane = lane;
        header._tofuLaneHeaderRenderer = renderer;
      }
    }
    const windowState = currentLaneWindowState(lane);
    const visibleTurns = includeHeader && !lane.expanded ? [] : lane.turns.slice(
      windowState.start, windowState.end,
    );
    renderWindowControl(
      laneContainer,
      lane.laneId,
      'earlier',
      Math.min(windowState.batchSize, windowState.start),
    );
    const turnsContainer = ensurePart(laneContainer, 'lane-turns');
    turnsContainer.hidden = includeHeader && !lane.expanded;
    if (includeHeader) turnsContainer.className = 'conversation-lane-turns branch-messages';
    const existingTurns = directKeyedChildren(turnsContainer, 'turnId');
    visibleTurns.forEach((turn, index) => {
      let turnNode = existingTurns.get(turn.turnId) as ManagedTurnElement | undefined;
      if (!turnNode) {
        turnNode = turnsContainer.ownerDocument.createElement('article');
        turnNode.dataset.turnId = turn.turnId;
        turnNode.className = 'conversation-turn';
      }
      turnNode.dataset.turnStatus = turn.status;
      turnNode.dataset.turnRole = turn.role;
      turnNode.dataset.laneId = turn.laneId;
      turnNode.className = `conversation-turn message${
        turn.actor === 'human' ? ' user-msg' : ''}${
        turn.actor !== 'human'
          && (turn.status === 'failed' || turn.status === 'interrupted')
          ? ' turn-failed' : ''}`;

      const turnContext: ConversationTurnRenderContext = {
        conversationId: currentConversationId,
        lane,
      };
      const avatar = ensurePart(
        turnNode, 'turn-avatar',
      ) as ManagedTurnAvatarElement;
      const avatarRenderer = options.renderTurnAvatar;
      if (!sameTurnAvatar(avatar._tofuTurn, turn)
          || avatar._tofuTurnAvatarRenderer !== avatarRenderer) {
        (avatarRenderer ?? renderDefaultTurnAvatar)(avatar, turn, turnContext);
        avatar._tofuTurn = turn;
        avatar._tofuTurnAvatarRenderer = avatarRenderer;
      }

      const content = ensurePart(turnNode, 'turn-content');
      content.className = 'conversation-turn-content message-content';
      /* Adopt nodes created by the short-lived flat Surface schema. This makes
       * a hot asset swap converge without duplicating one Turn's content.
       * Collapse to a single node per part: if content already owns the part
       * (or a prior swap left more than one flat copy), keep exactly one and
       * drop the rest, otherwise a stray flat footer would persist beside the
       * managed one and render the telemetry strip twice. */
      for (const part of [
        'turn-header', 'turn-blocks', 'turn-plan-decision', 'turn-branches',
        'turn-actions', 'turn-footer',
      ]) {
        const flatNode = directPart(turnNode, part);
        if (flatNode) {
          const adopted = directPart(content, part);
          if (adopted && adopted !== flatNode) {
            flatNode.remove();
          } else {
            content.appendChild(flatNode);
          }
        }
        let surviving: HTMLElement | null = null;
        for (const node of Array.from(content.children) as HTMLElement[]) {
          if (node.dataset.conversationPart !== part) continue;
          if (surviving) node.remove();
          else surviving = node;
        }
      }
      const header = ensurePart(
        content, 'turn-header', 'header',
      ) as ManagedTurnHeaderElement;
      const headerRenderer = options.renderTurnHeader;
      if (!sameTurnHeader(header._tofuTurn, turn)
          || header._tofuTurnHeaderRenderer !== headerRenderer) {
        (headerRenderer ?? renderDefaultTurnHeader)(header, turn, turnContext);
        header._tofuTurn = turn;
        header._tofuTurnHeaderRenderer = headerRenderer;
      }
      const blocksContainer = ensurePart(content, 'turn-blocks');
      blocksContainer.className = 'conversation-turn-blocks message-body';
      renderBlocks(blocksContainer, turn.blocks, lane, turn);
      blocksContainer.hidden = turn.blocks.length === 0;
      const activePlanDecision = latestViewModel?.planDecision;
      let planDecisionNode = directPart(
        content, 'turn-plan-decision',
      ) as ManagedPlanDecisionElement | null;
      if (activePlanDecision?.sourceTurnId === turn.turnId) {
        planDecisionNode = planDecisionNode ?? ensurePart(
          content, 'turn-plan-decision', 'aside',
        ) as ManagedPlanDecisionElement;
        const planDecisionRenderer = options.renderPlanDecision;
        if (!samePlanDecision(
          planDecisionNode._tofuPlanDecision, activePlanDecision,
        ) || planDecisionNode._tofuPlanDecisionRenderer
            !== planDecisionRenderer) {
          (planDecisionRenderer ?? renderDefaultPlanDecision)(
            planDecisionNode,
            activePlanDecision,
            turnContext,
          );
          planDecisionNode._tofuPlanDecision = activePlanDecision;
          planDecisionNode._tofuPlanDecisionRenderer = planDecisionRenderer;
        }
      } else {
        planDecisionNode?.remove();
        planDecisionNode = null;
      }
      const actions = ensurePart(
        content, 'turn-actions',
      ) as ManagedTurnActionsElement;
      const actionsRenderer = options.renderTurnActions;
      if (!sameTurnActions(actions._tofuTurn, turn)
          || actions._tofuTurnActionsRenderer !== actionsRenderer) {
        (actionsRenderer ?? renderDefaultTurnActions)(actions, turn, turnContext);
        actions._tofuTurn = turn;
        actions._tofuTurnActionsRenderer = actionsRenderer;
      }
      actions.hidden = !hasRenderableContent(actions);
      const footer = ensurePart(
        content, 'turn-footer', 'footer',
      ) as ManagedTurnFooterElement;
      const footerRenderer = options.renderTurnFooter;
      if (!sameTurnFooter(footer._tofuTurn, turn)
          || footer._tofuTurnFooterRenderer !== footerRenderer) {
        (footerRenderer ?? renderDefaultTurnFooter)(footer, turn, turnContext);
        footer._tofuTurn = turn;
        footer._tofuTurnFooterRenderer = footerRenderer;
      }
      footer.hidden = !hasRenderableContent(footer);
      let branchesContainer = directPart(content, 'turn-branches');
      if (turn.branches.length) {
        branchesContainer = branchesContainer
          ?? ensurePart(content, 'turn-branches');
        const existingLanes = directKeyedChildren(branchesContainer, 'laneId');
        turn.branches.forEach((branch, branchIndex) => {
          let branchNode = existingLanes.get(branch.laneId);
          if (!branchNode) {
            branchNode = branchesContainer!.ownerDocument.createElement('section');
            branchNode.className = 'conversation-lane conversation-lane--branch';
            branchNode.dataset.laneId = branch.laneId;
          }
          renderLane(branchNode, branch, true);
          placeAt(branchesContainer!, branchNode, branchIndex);
          existingLanes.delete(branch.laneId);
        });
        for (const staleLane of existingLanes.values()) staleLane.remove();
      } else {
        branchesContainer?.remove();
        branchesContainer = null;
      }

      /* The content order mirrors the retained message contract. The plan
       * decision stays attached to its authorizing plan, while branch history
       * remains above the turn's action and finish shelves. */
      let contentPartIndex = 0;
      placeAt(content, header, contentPartIndex++);
      placeAt(content, blocksContainer, contentPartIndex++);
      if (planDecisionNode) placeAt(content, planDecisionNode, contentPartIndex++);
      if (branchesContainer) {
        placeAt(content, branchesContainer, contentPartIndex++);
      }
      placeAt(content, actions, contentPartIndex++);
      placeAt(content, footer, contentPartIndex++);

      const contextBlock = turn.blocks.find(
        (block): block is Extract<ConversationBlockViewModel, { kind: 'context' }> => (
          block.kind === 'context'
        ),
      ) ?? null;
      const contextRail = ensurePart(
        turnNode, 'turn-context-rail', 'aside',
      ) as ManagedTurnContextRailElement;
      const contextRailRenderer = options.renderTurnContextRail;
      if (!contextRail._tofuTurnContextRailRendered
          || contextRail._tofuContextBlock?.source !== contextBlock?.source
          || contextRail._tofuTurnContextRailRenderer !== contextRailRenderer) {
        (contextRailRenderer ?? renderDefaultTurnContextRail)(
          contextRail, contextBlock, turnContext,
        );
        contextRail._tofuContextBlock = contextBlock ?? undefined;
        contextRail._tofuTurnContextRailRenderer = contextRailRenderer;
        contextRail._tofuTurnContextRailRendered = true;
      }
      contextRail.hidden = !hasRenderableContent(contextRail);

      placeAt(turnNode, avatar, 0);
      placeAt(turnNode, content, 1);
      placeAt(turnNode, contextRail, 2);
      placeAt(turnsContainer, turnNode, index);
      existingTurns.delete(turn.turnId);
    });
    for (const staleTurn of existingTurns.values()) staleTurn.remove();
    renderWindowControl(
      laneContainer,
      lane.laneId,
      'later',
      Math.min(
        windowState.batchSize, windowState.total - windowState.end,
      ),
    );
    const laneWindow = laneWindows.get(lane.laneId);
    if (laneWindow) laneWindow.firstVisibleTurnId = visibleTurns[0]?.turnId ?? '';
  };

  const renderQueue = (
    queueContainer: HTMLElement,
    queue: ReadonlyArray<ConversationQueueItemViewModel>,
  ): void => {
    const existing = directKeyedChildren(queueContainer, 'queueId');
    queue.forEach((item, index) => {
      let node = existing.get(item.queueId) as ManagedQueueElement | undefined;
      if (!node) {
        node = queueContainer.ownerDocument.createElement('article');
        node.className = 'conversation-queue-item';
        node.dataset.queueId = item.queueId;
      }
      const renderer = options.renderQueueItem;
      if (!sameQueueItem(node._tofuQueueItem, item)
          || node._tofuQueueRenderer !== renderer) {
        (renderer ?? renderDefaultQueueItem)(node, item, currentConversationId);
        node._tofuQueueItem = item;
        node._tofuQueueRenderer = renderer;
      }
      node.dataset.queueId = item.queueId;
      placeAt(queueContainer, node, index);
      existing.delete(item.queueId);
    });
    for (const stale of existing.values()) stale.remove();
  };

  const maxWindowStart = (total: number, maxTurns: number): number => (
    Math.max(0, total - maxTurns)
  );

  const collectLanes = (viewModel: ConversationViewModel): {
    all: Map<string, ConversationLaneViewModel>;
    expanded: ConversationLaneViewModel[];
  } => {
    const all = new Map<string, ConversationLaneViewModel>();
    const expanded: ConversationLaneViewModel[] = [];
    const visit = (lane: ConversationLaneViewModel, main = false): void => {
      if (all.has(lane.laneId)) return;
      all.set(lane.laneId, lane);
      if (!main && !lane.expanded) return;
      expanded.push(lane);
      for (const turn of lane.turns) {
        for (const branch of turn.branches) visit(branch);
      }
    };
    visit(viewModel.mainLane, true);
    for (const lane of viewModel.orphanLanes) visit(lane);
    return { all, expanded };
  };

  const prepareLaneWindows = (
    viewModel: ConversationViewModel,
    reset: boolean,
  ): void => {
    const lanes = collectLanes(viewModel);
    lanesById = lanes.all;
    laneBudgets = new Map<string, number>();
    const laneCount = Math.max(1, lanes.expanded.length);
    const base = Math.floor(windowOptions.maxTurns / laneCount);
    let remainder = windowOptions.maxTurns % laneCount;
    for (const lane of lanes.expanded) {
      const budget = base + (remainder > 0 ? 1 : 0);
      if (remainder > 0) remainder -= 1;
      laneBudgets.set(lane.laneId, budget);
    }
    for (const [laneId, lane] of lanes.all) {
      const maxTurns = laneBudgets.get(laneId) ?? 0;
      const maximumStart = maxWindowStart(lane.turns.length, maxTurns);
      const previous = laneWindows.get(laneId);
      let start = previous?.start ?? maximumStart;
      const atTail = reset || !previous ? true : previous.atTail;
      if (atTail) start = maximumStart;
      else if (previous?.firstVisibleTurnId) {
        const anchoredIndex = lane.turns.findIndex(
          (turn) => turn.turnId === previous.firstVisibleTurnId,
        );
        if (anchoredIndex >= 0) start = anchoredIndex;
      }
      laneWindows.set(laneId, {
        start: Math.min(maximumStart, Math.max(0, start)),
        atTail,
        firstVisibleTurnId: previous?.firstVisibleTurnId ?? '',
      });
    }
    for (const laneId of [...laneWindows.keys()]) {
      if (!lanes.all.has(laneId)) laneWindows.delete(laneId);
    }
  };

  const currentLaneWindowState = (
    lane: ConversationLaneViewModel,
  ): ConversationWindowState => {
    const maxTurns = laneBudgets.get(lane.laneId) ?? 0;
    const laneWindow = laneWindows.get(lane.laneId) ?? {
      start: maxWindowStart(lane.turns.length, maxTurns),
      atTail: true,
      firstVisibleTurnId: '',
    };
    laneWindows.set(lane.laneId, laneWindow);
    const start = Math.min(
      laneWindow.start, maxWindowStart(lane.turns.length, maxTurns),
    );
    const batchSize = maxTurns > 0
      ? Math.min(windowOptions.batchSize, maxTurns) : 0;
    return {
      start,
      end: Math.min(lane.turns.length, start + maxTurns),
      total: lane.turns.length,
      maxTurns,
      batchSize,
    };
  };

  const currentWindowState = (): ConversationWindowState => (
    latestViewModel
      ? currentLaneWindowState(latestViewModel.mainLane)
      : {
        start: 0, end: 0, total: 0,
        maxTurns: windowOptions.maxTurns,
        batchSize: windowOptions.batchSize,
      }
  );

  const renderWindowControl = (
    laneNode: HTMLElement,
    laneId: string,
    direction: 'earlier' | 'later',
    count: number,
  ): void => {
    const control = ensurePart(
      laneNode, `turn-window-${direction}`, 'nav',
    );
    control.hidden = count === 0;
    control.dataset.windowDirection = direction;
    const button = ensurePart(
      control,
      'turn-window-button',
      'button',
    ) as HTMLButtonElement;
    button.type = 'button';
    button.disabled = count === 0;
    button.dataset.conversationWindowAction = direction;
    button.dataset.laneId = laneId;
    button.dataset.turnCount = String(count);
    button.textContent = options.windowing?.label?.(direction, count)
      ?? String(count);
  };

  const commit = (): void => {
    const viewModel = latestViewModel;
    if (!viewModel) return;
    const anchor = scrollAnchor?.capture(root);
    currentConversationId = viewModel.conversationId;
    root.dataset.conversationId = viewModel.conversationId;
    root.dataset.transport = viewModel.transport;
    root.dataset.conversationRevision = String(viewModel.conversationRevision);
    const windowState = currentWindowState();
    root.dataset.windowStart = String(windowState.start);
    root.dataset.windowEnd = String(windowState.end);
    root.dataset.windowTotal = String(windowState.total);
    root.dataset.windowMaxTurns = String(windowState.maxTurns);
    root.dataset.windowBatchSize = String(windowState.batchSize);
    const main = ensurePart(root, 'main-lane');
    main.className = 'conversation-lane conversation-lane--main';
    renderLane(main, viewModel.mainLane, false);
    const orphans = ensurePart(root, 'orphan-lanes');
    const existingOrphans = directKeyedChildren(orphans, 'laneId');
    viewModel.orphanLanes.forEach((lane, index) => {
      let laneNode = existingOrphans.get(lane.laneId);
      if (!laneNode) {
        laneNode = orphans.ownerDocument.createElement('section');
        laneNode.className = 'conversation-lane conversation-lane--orphan';
        laneNode.dataset.laneId = lane.laneId;
      }
      renderLane(laneNode, lane, true);
      placeAt(orphans, laneNode, index);
      existingOrphans.delete(lane.laneId);
    });
    for (const stale of existingOrphans.values()) stale.remove();
    renderQueue(ensurePart(root, 'queue'), viewModel.queue);
    root.dataset.domTurnCount = String(
      root.querySelectorAll('[data-turn-id]').length,
    );
    if (anchor !== undefined) scrollAnchor?.restore(root, anchor);
  };

  const moveWindow = (
    direction: 'earlier' | 'later',
    laneId = 'main',
  ): boolean => {
    const lane = lanesById.get(laneId);
    if (!latestViewModel || !lane) return false;
    const state = currentLaneWindowState(lane);
    if (state.maxTurns <= 0) return false;
    const laneWindow = laneWindows.get(laneId);
    if (!laneWindow) return false;
    const nextStart = Math.max(0, Math.min(
      maxWindowStart(state.total, state.maxTurns),
      laneWindow.start + (direction === 'earlier'
        ? -state.batchSize : state.batchSize),
    ));
    if (nextStart === laneWindow.start) return false;
    laneWindow.start = nextStart;
    laneWindow.atTail = nextStart === maxWindowStart(state.total, state.maxTurns);
    commit();
    return true;
  };

  return {
    root,
    get windowState() { return currentWindowState(); },
    render(viewModel) {
      if (disposed) throw new Error('ConversationSurface is disposed.');
      const previous = latestViewModel;
      const reset = !previous || previous.conversationId !== viewModel.conversationId;
      prepareLaneWindows(viewModel, reset);
      latestViewModel = viewModel;
      commit();
    },
    showEarlier() { return moveWindow('earlier', 'main'); },
    showLater() { return moveWindow('later', 'main'); },
    followLatest() { scrollAnchor?.followLatest?.(root); },
    dispose() {
      if (disposed) return;
      disposed = true;
      root.removeEventListener('click', emitIntent);
      ownedScrollAnchor?.dispose?.();
      root.remove();
    },
  };
}
