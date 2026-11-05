/**
 * DOM-commit witnesses and bounded receive clocks for Turn perception receipts.
 *
 * The runtime supplies canonical state, active ownership and transport health;
 * notifyRenderedState accepts only the mounted renderer's commit. This module
 * retains identities/clocks, never conversation projections or event bodies.
 * PerceptionRecorder remains the sole paint, receipt queue and retry owner.
 */
import type {
  AttemptEvent,
  ConnectionHealth,
  ConversationSyncApi,
} from '../api/conversation-sync.generated';
import { entityRecordValues } from './entity-record';
import {
  createTurnPerceptionRecorder,
  type PerceptionAttemptIdentity,
} from './perception-recorder';
import type { TurnState } from './turn-state';

export interface TurnRenderObservationOptions {
  api: ConversationSyncApi;
  streamClientId?: string;
  /** Recheck windowing/unmount between DOM commit and the paint opportunity. */
  isTurnMounted?(conversationId: string, turnId: string): boolean;
  /** Stable across in-place commits, different after removal or replacement. */
  getTurnMountToken?(conversationId: string, turnId: string): string | number | null;
}

interface TurnRenderObservationState {
  readState(conversationId: string): TurnState | null;
  isActive(conversationId: string): boolean;
  readHealth(conversationId: string): ConnectionHealth | null | undefined;
  terminalStatuses: ReadonlySet<string>;
}

type ReceivedEventIdentity = Pick<AttemptEvent,
  'conversationId' | 'turnId' | 'attemptId' | 'projectionRevision'>;

/** Borrow the original options object so live host callbacks keep their binding. */
export function createTurnRenderObservations(
  options: TurnRenderObservationOptions,
  authority: TurnRenderObservationState,
) {
  const renderedTurns = new Map<string, PerceptionAttemptIdentity & {
    mountToken?: string | number | null;
  }>();
  const receivedEvents = new Map<string, {
    attemptId: string; revision: number; receivedAt?: number; publishedAt?: number;
  }>();
  let renderedConversationId = '';
  let renderedHealthSignature = '';
  const healthSignature = (conversationId: string): string => {
    const health = authority.readHealth(conversationId);
    return `${health?.state || ''}\u0000${health?.reason || ''}`;
  };
  const receiptKey = (conversationId: string, turnId: string): string =>
    `${conversationId}\u0000${turnId}`;

  const perceptionRecorder = createTurnPerceptionRecorder({
    api: options.api,
    clientId: options.streamClientId,
    getTurnMountToken: options.getTurnMountToken,
    isRendered(conversationId, identity) {
      const turn = renderedTurns.get(identity.turnId);
      const current = authority.readState(conversationId)?.turnsById[identity.turnId];
      return renderedConversationId === conversationId
        && authority.isActive(conversationId)
        && Boolean(options.isTurnMounted?.(conversationId, identity.turnId))
        && renderedHealthSignature === healthSignature(conversationId)
        && turn?.attemptId === identity.attemptId
        && turn?.projectionRevision === identity.projectionRevision
        && current?.currentAttemptId === identity.attemptId
        && Number(current?.projectionRevision) === identity.projectionRevision;
    },
  });

  const perceptionIdentity = (
    conversationId: string,
  ): PerceptionAttemptIdentity | null => {
    const state = authority.readState(conversationId);
    if (!state) return null;
    // Queued attempts without an executor task cannot own timing receipts.
    const attempts = entityRecordValues(state.attemptsById)
      .filter((attempt) => attempt?.attemptId && attempt.turnId && attempt.taskId
        && (attempt.status === 'pending' || attempt.status === 'running'))
      .sort((left, right) => {
        const leftLive = left?.status === 'pending' || left?.status === 'running';
        const rightLive = right?.status === 'pending' || right?.status === 'running';
        if (leftLive !== rightLive) return leftLive ? 1 : -1;
        return Number(left?.settledAt || left?.startedAt || left?.createdAt || 0)
          - Number(right?.settledAt || right?.startedAt || right?.createdAt || 0);
      });
    const attempt = attempts.at(-1);
    if (!attempt?.attemptId || !attempt.turnId) return null;
    return {
      attemptId: attempt.attemptId,
      turnId: attempt.turnId,
      projectionRevision: Number(
        state.turnsById[attempt.turnId]?.projectionRevision || 0,
      ),
    };
  };

  const clearRenderedConversation = (conversationId: string): void => {
    if (renderedConversationId === conversationId) {
      renderedTurns.clear();
      renderedConversationId = '';
    }
  };

  return {
    stageReceivedEvent(
      event: ReceivedEventIdentity, previousRevision: number,
      receivedAt?: number, serverPublishedAt?: number,
    ): string {
      const key = receiptKey(event.conversationId, event.turnId);
      if (previousRevision < Number(event.projectionRevision || 0)) {
        // Install before dispatch: a forced render may commit synchronously.
        receivedEvents.delete(key);
        receivedEvents.set(key, {
          attemptId: event.attemptId, revision: event.projectionRevision,
          receivedAt, publishedAt: serverPublishedAt,
        });
        while (receivedEvents.size > 64) {
          receivedEvents.delete(receivedEvents.keys().next().value!);
        }
      }
      return key;
    },
    discardReceivedEvent(key: string): void {
      receivedEvents.delete(key);
    },
    notifyRenderedState(
      conversationId: string, state: TurnState, mountedTurnIds: readonly string[],
    ): void {
      if (!authority.isActive(conversationId)
          || state.conversationId !== conversationId) return;
      if (renderedConversationId && renderedConversationId !== conversationId) {
        perceptionRecorder.invalidateRenderedConversation(renderedConversationId);
      }
      const mountedIds = new Set(mountedTurnIds.slice(-128));
      for (const [turnId, previous] of renderedTurns) {
        if (!mountedIds.has(turnId)
            || previous.mountToken !== options.getTurnMountToken?.(conversationId, turnId)) {
          perceptionRecorder.invalidateRenderedTurn(conversationId, previous);
        }
      }
      renderedConversationId = conversationId;
      renderedTurns.clear();

      renderedHealthSignature = healthSignature(conversationId);
      const liveAttemptId = perceptionIdentity(conversationId)?.attemptId;
      for (const turnId of mountedIds) {
        const turn = state.turnsById[turnId];
        if (!turn?.currentAttemptId) continue;
        const identity = {
          turnId, attemptId: turn.currentAttemptId,
          projectionRevision: Number(turn.projectionRevision || 0),
          mountToken: options.getTurnMountToken?.(conversationId, turnId),
        };
        renderedTurns.set(turnId, identity);
        const settled = authority.terminalStatuses.has(turn.status || '');
        const phase = !settled && liveAttemptId === identity.attemptId
          ? state.livePhase : null;
        if (!settled && !phase) continue;
        const received = receivedEvents.get(receiptKey(conversationId, turnId));
        const matches = received?.attemptId === identity.attemptId
          && received?.revision === identity.projectionRevision;
        perceptionRecorder.observeAttemptEvent({
          conversationId, ...identity,
          type: settled ? 'terminal_settlement' : 'projection_updated',
          payload: { phase },
        }, matches ? received?.receivedAt : undefined,
        matches ? received?.publishedAt : undefined);
      }
      const health = authority.readHealth(conversationId);
      if (health) perceptionRecorder.observeHealth(
        conversationId, health, perceptionIdentity(conversationId),
      );
    },
    invalidateConversation(conversationId: string): void {
      perceptionRecorder.invalidateRenderedConversation(conversationId);
      clearRenderedConversation(conversationId);
    },
    // Revoke paint evidence before the host disposes DOM. The host callback
    // may re-enter the runtime; receipt disposal follows that callback.
    clearRenderedConversation,
    disposeConversation(conversationId: string): void {
      perceptionRecorder.disposeConversation(conversationId);
      for (const key of receivedEvents.keys()) {
        if (key.startsWith(`${conversationId}\u0000`)) receivedEvents.delete(key);
      }
    },
  };
}
