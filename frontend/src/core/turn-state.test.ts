import {describe, it, expect} from 'vitest';
import {createTurnState, reduceTurnState} from './turn-state';
import type {ProjectionTurn} from './turn-projection';

const turn = (
  turnId: string, ordinal: number, extra: Partial<ProjectionTurn> = {},
): ProjectionTurn => ({
  turnId, presentationId: turnId, conversationId: 'conversation', ordinal,
  laneId: 'main', actor: 'assistant', kind: 'reply', runId: '',
  status: 'failed', projectionRevision: 1, settlement: {},
  createdAt: 1, updatedAt: 1,
  projection: {content: '', toolRounds: [{toolCallId: 'saved', status: 'done'}]},
  ...extra,
});
const initial = () => reduceTurnState(createTurnState('conversation'), {
  type: 'snapshot', snapshot: {conversationRevision: 10,
    turns: [turn('target', 1), turn('later', 2)]},
});

describe('seamless continuation acknowledgement', () => {
  it('keeps later turns on click and on command rejection', () => {
    const pending = reduceTurnState(initial(), {type: 'command_pending', turnId: 'target', operation: 'continue'});
    expect(pending.laneOrder.main).toEqual(['target', 'later']);
    const rejected = reduceTurnState(pending, {type: 'command_failed', turnId: 'target'});
    expect(rejected.laneOrder.main).toEqual(['target', 'later']);
  });
  it('removes the accepted tail and keeps the target checkpoint atomically', () => {
    const next = reduceTurnState(initial(), {type: 'command_response', response: {
      conversationRevision: 11, turn: turn('target', 1, {status: 'pending', projectionRevision: 2}),
      deletedTurnIds: ['later'],
    }});
    expect(next.laneOrder.main).toEqual(['target']);
    expect(next.turnsById.later).toBeUndefined();
    expect(next.turnsById.target?.projection?.toolRounds).toHaveLength(1);
    const stale = reduceTurnState(next, {type: 'command_response', response: {
      conversationRevision: 10, turn: turn('later', 2),
      deletedTurnIds: ['target'],
    }});
    expect(stale.laneOrder.main).toEqual(['target']);
    expect(stale.turnsById.target).toBeDefined();
  });
  it('accepts equal-revision acknowledgement after sync already advanced', () => {
    const next = reduceTurnState(initial(), {type: 'command_response', response: {
      conversationRevision: 10, deletedTurnIds: ['later'],
    }});
    expect(next.laneOrder.main).toEqual(['target']);
  });
});
