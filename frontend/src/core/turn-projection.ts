/**
 * Conversation-shell reconciliation from authoritative TurnState.
 *
 * This module deliberately projects only catalog/lifecycle metadata. Turn
 * content remains normalized in TurnStore and is selected directly by
 * ConversationSurface; no parallel positional transcript document exists.
 */
import type {
  ConversationQueueItem,
  TurnRecord,
} from '../api/conversation-sync.generated';

type JsonRecord = Record<string, unknown>;

/** The generated wire DTO is also the normalized store's durable turn shape. */
export type ProjectionTurn = TurnRecord;

export interface LiveRoundUsage {
  attemptId: string;
  value: JsonRecord;
}

export interface TurnProjectionState {
  conversationRevision?: number;
  transport?: string;
  turnsById: Record<string, ProjectionTurn | undefined>;
  laneOrder: Record<string, string[] | undefined>;
  historyByLane?: Record<string, { totalTurns: number } | undefined>;
  commandPending: Record<string, string | null | undefined>;
  liveRoundUsageByTurn: Record<string, LiveRoundUsage | undefined>;
  queueItems?: ReadonlyArray<ConversationQueueItem>;
  livePhase?: unknown;
}

export interface ProjectedConversation extends JsonRecord {
  id?: string;
  _turnProjectionFingerprint?: string;
  _turnTransport?: string;
  _serverRev?: number;
  _serverTurnCount?: number;
  _turnSnapshotRequired?: boolean;
  _finishingStream?: boolean;
  _finishingAttemptId?: string | null;
}

export interface ApplyTurnProjectionInput {
  conversation: ProjectedConversation;
  state: TurnProjectionState;
  active?: boolean;
  domStale?: boolean;
  persist?(conversation: ProjectedConversation): void;
  replaceAll?(conversation: ProjectedConversation): void;
  buildNavigation?(conversation: ProjectedConversation): void;
  renderConversationList?(): void;
  updateSendButton?(): void;
}

function livePhaseFingerprint(phase: unknown): string {
  if (!phase || typeof phase !== 'object') return '';
  const value = phase as JsonRecord;
  const detailArgs = value.detailArgs && typeof value.detailArgs === 'object'
    ? value.detailArgs as JsonRecord : {};
  return [
    value.phase,
    value.detailKey,
    detailArgs.reasonKey,
    value.seq,
  ].map((item) => String(item ?? '')).join(':');
}

export function projectionFingerprint(state: TurnProjectionState): string {
  const turns = Object.keys(state.laneOrder).sort().flatMap((laneId) =>
    (state.laneOrder[laneId] ?? []).map((turnId) => {
      const turn = state.turnsById[turnId];
      const liveUsage = state.liveRoundUsageByTurn?.[turnId];
      const live = liveUsage && liveUsage.attemptId === turn?.currentAttemptId
        ? liveUsage.value : undefined;
      return `${laneId}:${turn?.turnId}:${turn?.projectionRevision}:${turn?.status}:`
        + `${turn?.currentAttemptId || ''}:${state.commandPending[turnId] || ''}:`
        + `${liveUsage?.attemptId || ''}:${live?.round || ''}:`
        + `${live?.tokensIn || ''}:${live?.tokensOut || ''}:`
        + `${live?.model || ''}:${live?.tag || ''}`;
    }),
  ).join('|');
  // Phase itself remains owned by TurnState.  Only its sidebar-visible shape
  // participates in the invalidation key so phase transitions repaint catalog
  // status without materializing another mutable phase document.
  return `${turns}|phase:${livePhaseFingerprint(state.livePhase)}`;
}

/** Reconcile lifecycle metadata without materializing a second transcript. */
export function applyTurnStateProjection(input: ApplyTurnProjectionInput): boolean {
  const { conversation, state } = input;
  const fingerprint = projectionFingerprint(state);

  const incomingRevision = Number(state.conversationRevision || 0);
  if (incomingRevision > Number(conversation._serverRev || 0)) {
    conversation._serverRev = incomingRevision;
  }
  conversation._serverTurnCount = state.historyByLane?.main?.totalTurns
    ?? state.laneOrder.main?.length ?? 0;

  if (conversation._turnProjectionFingerprint === fingerprint) {
    const transportChanged = conversation._turnTransport !== state.transport;
    conversation._turnTransport = state.transport;
    if (transportChanged) {
      input.renderConversationList?.();
      input.updateSendButton?.();
    }
    if (input.domStale && input.active) input.replaceAll?.(conversation);
    return false;
  }

  conversation._turnProjectionFingerprint = fingerprint;
  conversation._turnTransport = state.transport;
  conversation._turnSnapshotRequired = false;

  input.persist?.(conversation);
  if (input.active) {
    input.replaceAll?.(conversation);
    input.buildNavigation?.(conversation);
  }
  input.renderConversationList?.();
  input.updateSendButton?.();
  return true;
}
