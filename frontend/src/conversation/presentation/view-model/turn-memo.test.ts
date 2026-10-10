/** Immutable Turn memo hits avoid terminal evidence work; revision changes repaint. */
import { describe, expect, it } from 'vitest';
import type { TurnRecord } from '../../../api/conversation-sync.generated';
import { createTurnState } from '../../domain/turn-store';
import { selectConversationViewModel } from '../conversation-view-model';

describe('settled Turn presentation memo', () => {
  it('reuses terminal presentation without reading error evidence on warm hits', () => {
    let errorReads = 0;
    const settlement = {
      outcome: 'failed', cause: 'generation_error',
      get error() {
        errorReads += 1;
        return { kind: 'provider_error', message: 'Provider temporarily unavailable', retryable: true };
      },
    };
    const turn = {
      turnId: 'failed', laneId: 'main', actor: 'assistant', status: 'failed',
      projectionRevision: 1, projection: {}, settlement,
    } as unknown as TurnRecord;
    const state = createTurnState('memo-contract');
    state.turnsById[turn.turnId] = turn;
    state.laneOrder.main = [turn.turnId];
    const initial = selectConversationViewModel(state).mainLane.turns[0];
    expect(initial.finish?.detail).toBe('Provider temporarily unavailable');
    const coldReads = errorReads;
    expect(coldReads).toBeGreaterThan(0);
    for (let frame = 0; frame < 50; frame += 1) {
      expect(selectConversationViewModel(state).mainLane.turns[0]).toBe(initial);
    }
    expect(errorReads).toBe(coldReads);
    state.turnsById[turn.turnId] = {
      ...turn, projectionRevision: 2,
      settlement: { outcome: 'failed', error: { kind: 'provider_error', message: 'New diagnosis' } },
    };
    const updated = selectConversationViewModel(state).mainLane.turns[0];
    expect(updated).not.toBe(initial);
    expect(updated.finish?.detail).toBe('New diagnosis');
  });
});
