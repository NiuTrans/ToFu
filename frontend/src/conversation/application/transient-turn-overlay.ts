import { entityRecordValues } from '../domain/turn-store';
/**
 * Lifecycle-local turn overlays for work that is visible before it is durable.
 *
 * Durable TurnStore state is never mutated. Callers own when an overlay is
 * added/removed; ConversationSurface receives a shallow composed view so the
 * same turnId/blockId keyed renderer handles both live and settled revisions.
 */
import type { TurnRecord } from '../../api/conversation-sync.generated';
import type { TurnState } from '../domain/turn-store';
import type { TransientTurnRecord } from '../domain/transient-turn';

export interface TransientTurnOverlay {
  upsert(turn: TransientTurnRecord): void;
  remove(conversationId: string, turnId: string): boolean;
  get(conversationId: string, turnId: string): TransientTurnRecord | null;
  clear(conversationId: string): void;
  compose(state: TurnState): TurnState;
}

function durableTailOrdinal(state: TurnState, laneId: string): number {
  const ids = state.laneOrder[laneId];
  const ordinal = ids?.length ? Number(state.turnsById[ids[ids.length - 1]]?.ordinal) : NaN;
  return Number.isFinite(ordinal) ? ordinal : -Infinity;
}

export function createTransientTurnOverlay(): TransientTurnOverlay {
  const turnsByConversation = new Map<string, Map<string, TransientTurnRecord>>();

  const lane = (conversationId: string): Map<string, TransientTurnRecord> => {
    let turns = turnsByConversation.get(conversationId);
    if (!turns) {
      turns = new Map<string, TurnRecord>();
      turnsByConversation.set(conversationId, turns);
    }
    return turns;
  };

  return Object.freeze({
    upsert(turn: TransientTurnRecord): void {
      if (!turn.conversationId || !turn.turnId) {
        throw new Error('Transient turns require conversationId and turnId.');
      }
      lane(turn.conversationId).set(turn.turnId, turn);
    },
    remove(conversationId: string, turnId: string): boolean {
      const turns = turnsByConversation.get(conversationId);
      const removed = turns?.delete(turnId) ?? false;
      if (turns && turns.size === 0) turnsByConversation.delete(conversationId);
      return removed;
    },
    get(conversationId: string, turnId: string): TransientTurnRecord | null {
      return turnsByConversation.get(conversationId)?.get(turnId) ?? null;
    },
    clear(conversationId: string): void {
      turnsByConversation.delete(conversationId);
    },
    compose(state: TurnState): TurnState {
      const overlays = turnsByConversation.get(state.conversationId);
      if (!overlays?.size) return state;
      const turnsById = { ...state.turnsById };
      const laneOrder = Object.fromEntries(Object.entries(state.laneOrder)
        .map(([laneId, turnIds]) => [laneId, [...(turnIds ?? [])]]));
      const authoritativeTurnByPresentationId = new Map(
        entityRecordValues(state.turnsById).flatMap((turn) => (
          turn ? [[turn.presentationId || turn.turnId, turn.turnId] as const] : []
        )),
      );
      for (const turn of overlays.values()) {
        const authoritativeTurnId = authoritativeTurnByPresentationId.get(
          turn.presentationId || turn.turnId,
        );
        if (authoritativeTurnId && authoritativeTurnId !== turn.turnId) {
          continue;
        }
        const laneId = turn.laneId || 'main';
        if (!state.laneOrder[laneId]?.includes(turn.turnId)) {
          // Off-window overlays may only extend the lane. A swarm presentation
          // copy of history evicted by a tail snapshot would otherwise render
          // below the live Turn and read as the end of the conversation.
          if (Number(turn.ordinal) <= durableTailOrdinal(state, laneId)) continue;
          (laneOrder[laneId] ?? (laneOrder[laneId] = [])).push(turn.turnId);
        }
        turnsById[turn.turnId] = turn;
      }
      return { ...state, turnsById, laneOrder };
    },
  });
}
