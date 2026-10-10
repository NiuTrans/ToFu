/**
 * Pure ordered reads over normalized TurnState for retained feature adapters.
 *
 * This is the sole migration seam for features that need transcript facts but
 * do not own conversation state. Callers receive Turn records and projections,
 * never the positional compatibility message document.
 */
import type { TurnRecord } from '../../api/conversation-sync.generated';
import type { TurnState } from '../domain/turn-store';

export function orderedConversationTurns(
  state: TurnState | null | undefined,
  laneId = 'main',
): ReadonlyArray<TurnRecord> {
  if (!state) return [];
  return (state.laneOrder[laneId] ?? []).flatMap((turnId) => {
    const turn = state.turnsById[turnId];
    return turn ? [turn] : [];
  });
}

export function latestConversationTurn(
  state: TurnState | null | undefined,
  predicate: (turn: TurnRecord) => boolean = () => true,
  laneId = 'main',
): TurnRecord | null {
  const turns = orderedConversationTurns(state, laneId);
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    if (predicate(turns[index])) return turns[index];
  }
  return null;
}

export function conversationHasActor(
  state: TurnState | null | undefined,
  actor: TurnRecord['actor'],
  laneId = 'main',
): boolean {
  return orderedConversationTurns(state, laneId)
    .some((turn) => turn.actor === actor);
}

/** Active attempt ids across every lane, derived only from live Turns. */
export function activeConversationAttemptIds(
  state: TurnState | null | undefined,
): ReadonlyArray<string> {
  if (!state) return [];
  return [...new Set(Object.values(state.turnsById).flatMap((turn) => (
    turn && (turn.status === 'pending' || turn.status === 'running')
      && turn.currentAttemptId ? [turn.currentAttemptId] : []
  )))];
}

/** Active main-lane attempt, used by queue-vs-steer and abort commands. */
export function activeMainConversationAttemptId(
  state: TurnState | null | undefined,
): string | null {
  return latestConversationTurn(
    state,
    (turn) => (turn.status === 'pending' || turn.status === 'running')
      && Boolean(turn.currentAttemptId),
  )?.currentAttemptId ?? null;
}
