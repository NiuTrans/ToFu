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


describe('continuous history cursor isolation', () => {
  const tail = () => reduceTurnState(createTurnState('conversation'), {type: 'snapshot', snapshot: {
    turns: [turn('tail', 99)], turnWindow: {laneId: 'main', hasMore: true, nextBeforeOrdinal: 99, totalTurns: 100},
  }});
  it('hydrates detached ranks without crossing the gap or stealing the tail cursor', () => {
    const next = reduceTurnState(tail(), {type: 'snapshot', snapshot: {
      turns: [turn('old', 4)], laneId: 'main', beforeOrdinal: 5,
      nextBeforeOrdinal: 4, hasMore: false, totalTurns: 100, historyMode: 'detached',
    }});
    expect(next.turnsById.old).toBeTruthy();
    expect(next.historyByLane.main).toEqual({nextBeforeOrdinal: 99, hasMore: true, totalTurns: 100});
  });
  it('accepts only the continuous frontier and does not reset on a mismatched older page', () => {
    let next = reduceTurnState(tail(), {type: 'snapshot', snapshot: {
      turns: [turn('old', 4)], laneId: 'main', beforeOrdinal: 5,
      nextBeforeOrdinal: 4, hasMore: false, totalTurns: 100,
    }});
    expect(next.historyByLane.main?.nextBeforeOrdinal).toBe(99);
    next = reduceTurnState(next, {type: 'snapshot', snapshot: {
      turns: [turn('near', 98)], laneId: 'main', beforeOrdinal: 99,
      nextBeforeOrdinal: 98, hasMore: true, totalTurns: 100,
    }});
    expect(next.historyByLane.main?.nextBeforeOrdinal).toBe(98);
  });
});


describe('phase evidence after command acknowledgement', () => {
  const startup = {phase: 'working', detailKey: 'stream.phase.workerStarting'};
  const running = (revision: number) => turn('target', 1, {
    status: 'running', currentAttemptId: 'attempt', projectionRevision: revision,
    projection: {content: revision > 1 ? 'Started replying' : ''},
  });
  const acknowledged = () => {
    const state = reduceTurnState(createTurnState('conversation'), {type: 'snapshot', snapshot: {
      authoritativeFull: true, conversationRevision: 1, turns: [running(1)],
      attempts: [{attemptId: 'attempt', turnId: 'target', status: 'running', lastSeq: 1}],
      activeAttemptPhase: {turnId: 'target', attemptId: 'attempt', projectionRevision: 1, phase: startup},
    }});
    return reduceTurnState(state, {type: 'command_response', response: {
      conversationRevision: 2, turn: running(2),
    }});
  };
  const frame = (revision = 2, seq = 2, phase: unknown = null) => ({
    type: 'projection_updated' as const, conversationId: 'conversation', turnId: 'target', attemptId: 'attempt', seq,
    projectionRevision: revision,
    payload: {projection: {content: 'Started replying'}, phase},
  });

  it('consumes explicit phase null even when the ACK already installed its revision', () => {
    const state = acknowledged();
    expect(state.livePhase).toEqual(startup);
    const next = reduceTurnState(state, {type: 'event', event: frame()});
    expect(next.livePhase).toBeNull();
    expect(next.attemptsById.attempt?.lastSeq).toBe(2);
    expect(next.turnsById.target).toBe(state.turnsById.target);
  });
  it('accepts a same-revision stage change but never replays its consumed sequence', () => {
    const waiting = {phase: 'waiting_model', detail: 'Waiting for model'};
    const next = reduceTurnState(acknowledged(), {type: 'event', event: frame(2, 2, waiting)});
    expect(next.livePhase).toEqual(waiting);
    const duplicate = reduceTurnState(next, {type: 'event', event: frame(2, 2, startup)});
    expect(duplicate.livePhase).toEqual(waiting);
  });
  it('does not replace newer presentation with an older revision or foreign attempt', () => {
    const state = acknowledged();
    const old = reduceTurnState(state, {type: 'event', event: frame(1, 2)});
    expect(old.livePhase).toEqual(startup);
    const foreign = reduceTurnState(state, {type: 'event', event: {...frame(), attemptId: 'foreign'}});
    expect(foreign.livePhase).toEqual(startup);
  });
});
