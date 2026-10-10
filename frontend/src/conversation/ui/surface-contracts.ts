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
  /** Direct image source for injection-lane thumbnails (no positional index). */
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
  ): void;
}

export interface ScrollAnchorPort<Snapshot = unknown> {
  capture(root: HTMLElement): Snapshot;
  restore(root: HTMLElement, snapshot: Snapshot): void;
  followLatest?(root: HTMLElement): void;

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
  /** Lightbox opener for injection-lane (steer/peer) image thumbnails. */
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
}

export interface ConversationSurface {
  readonly root: HTMLElement;
  readonly windowState: ConversationWindowState;
  render(viewModel: ConversationViewModel): void;
  showEarlier(): boolean;
  showLater(): boolean;

  revealTurn(turnId: string): boolean;
  followLatest(): void;
  dispose(): void;
}
