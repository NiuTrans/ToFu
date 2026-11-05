/**
 * Typed renderer, intent, window and scroll ports for ConversationSurface. No
 * runtime policy or browser state.
 */
import type {
  ConversationBlockViewModel,
  ConversationLaneViewModel,
  ConversationQueueItemViewModel,
  ConversationTurnViewModel,
  ConversationViewModel,
  PlanDecisionViewModel,
} from '../presentation/conversation-view-model';
import type { RoundOutlineTranslator } from './round-outline';

export interface ConversationIntent {
  type: string;
  conversationId: string;
  turnId?: string;
  blockId?: string;
  laneId?: string;
  queueId?: string;
  operation?: string;
  beforeOrdinal?: number;
  limit?: number;
  /** Direct source for Markdown/injection images without a positional index. */
  previewSource?: string;
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

/** Derived adjacency only; no synthetic queue identity or delivery state. */
export interface ConversationQueueItemRenderContext {
  backgroundCommandGroup?: { count: number; index: number };
}

export interface ConversationSurfaceRenderers {
  /** Release block-owned requests/listeners before DOM retirement. */
  disposeBlock?(node: HTMLElement): void;
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
    context?: ConversationQueueItemRenderContext,
  ): void;
}

export interface ScrollAnchorPort<Snapshot = unknown> {
  readonly following?: boolean;
  capture(root: HTMLElement): Snapshot;
  restore(root: HTMLElement, snapshot: Snapshot): void;
  followLatest?(root: HTMLElement, options?: { force?: boolean }): void;
  /** Exact navigation shares follow/layout cancellation and scoped geometry. */
  scrollToTurn?(root: HTMLElement, turnId: string, onScroll?: () => void): boolean;
  isNearBottom?(threshold?: number): boolean;

  suspendFollow?(): void;
  dispose?(): void;
}

export interface ConversationWindowOptions {
  /** A smaller value is allowed; the product hard ceiling remains 80. */
  maxTurns?: number;
  batchSize?: number;
  /** Blocks mounted per Turn before the window collapses the tail-ward cap. */
  maxBlocksPerTurn?: number;
  blockBatchSize?: number;
  label?(direction: 'earlier' | 'later', count: number): string;
  blockLabel?(count: number): string;

  historyStatusLabel?(state: 'loading' | 'failed'): string;

  historyRetryLabel?(): string;
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

  /** Physical window bottom is not necessarily the conversation tail. */
  canFollowLatest?(): boolean;
  scheduleAfterLayout?(callback: () => void): () => void;
}

export interface ConversationScrollSnapshot {
  following: boolean;
  scrollTop: number;
  anchorTurnId?: string;

  anchorBlockId?: string;
  anchorPresentationId?: string;
  anchorOffset?: number;
  /** Separate fallback geometry when the visible leaf is retired. */
  anchorTurnOffset?: number;
}

export interface ConversationSurfaceOptions extends ConversationSurfaceRenderers {
  /** Mounted Turn identities from the keyed commit, including window changes. */
  onCommit?(turnIds: ReadonlyArray<string>): void;
  onIntent?(intent: ConversationIntent): void | Promise<unknown>;
  /** Shared lightbox opener for Markdown images and injection-lane thumbnails. */
  onPreviewImage?(source: string): void;
  scrollAnchor?: ScrollAnchorPort;
  scrollViewport?: HTMLElement;
  viewport?: ConversationViewportOptions;
  windowing?: ConversationWindowOptions;

  roundOutlineLabel?: RoundOutlineTranslator;
  /** Localized label for the inline sync-degradation banner. */
  transportNoticeLabel?(
    state: 'recovering' | 'degraded' | 'offline',
  ): string;
  /** Main-lane tail range residency retired on this conversation's previous
   * mount; the first render reuses it while the same Turn is still newest. */
  retainedTail?: ConversationRetainedTail;
}

export interface ConversationRetainedTail {
  firstTurnId: string;
  lastTurnId: string;
}

export interface ConversationSurface {
  readonly root: HTMLElement;
  readonly windowState: ConversationWindowState;
  readonly mountedTurnIds: ReadonlyArray<string>;
  /** The main-lane tail range while residency has retired Turns above it. */
  readonly retainedTail: ConversationRetainedTail | null;
  render(viewModel: ConversationViewModel): void;
  showEarlier(): boolean;
  showLater(): boolean;

  /** Mount the bounded target window and align its Turn in the owned viewport. */
  revealTurn(turnId: string): boolean;
  isNearLatest(threshold?: number): boolean;
  followLatest(options?: { force?: boolean }): void;
  dispose(): void;
}
