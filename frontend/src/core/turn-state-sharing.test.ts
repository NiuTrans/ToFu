/** Immutable transition contract: unchanged facts keep identity; changed
 * collection members never mutate an older snapshot or queued replay. */
import { describe, expect, it, vi } from 'vitest';
import {
  createTurnState, createTurnStore, reduceTurnState, type TurnEvent, type TurnState,
} from './turn-state';
import type { ProjectionTurn } from './turn-projection';

function turn(id: string, ordinal: number, laneId = 'main'): ProjectionTurn {
  return {
    turnId: id, presentationId: id, conversationId: 'sharing', ordinal, laneId,
    actor: 'assistant', kind: 'reply', runId: '', status: 'running',
    currentAttemptId: `${id}-attempt`, projectionRevision: 1,
    projection: { content: 'before' }, settlement: {}, createdAt: 1, updatedAt: 1,
  };
}

function freeze<T>(value: T): T {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

function initial(): TurnState {
  return reduceTurnState(createTurnState('sharing'), {
    type: 'snapshot', snapshot: {
      conversationRevision: 1,
      turns: [turn('live', 1), turn('sibling', 2), turn('branch', 3, 'branch')],
      attempts: [{ attemptId: 'live-attempt', turnId: 'live', status: 'running', lastSeq: 1 }],
      turnWindow: { laneId: 'main', nextBeforeOrdinal: null, hasMore: false, totalTurns: 2 },
    },
  });
}

function event(turnId = 'live', seq = 2, extra: Record<string, unknown> = {}): TurnEvent {
  return {
    turnId, attemptId: `${turnId}-attempt`, seq, projectionRevision: seq,
    type: 'projection_updated', payload: { projection: { content: `after ${seq}` }, ...extra },
  } as TurnEvent;
}

const sharedCollections = [
  'laneOrder', 'historyByLane', 'queueItems', 'pendingEventsByTurn',
  'commandPending', 'liveRoundUsageByTurn',
] as const;

describe('structurally shared TurnState transitions', () => {
  it('streaming only changes its Turn and attempt, retaining unrelated collections', () => {
    const before = freeze(initial());
    const next = reduceTurnState(before, { type: 'event', event: event() });
    expect(next.turnsById.live?.projection.content).toBe('after 2');
    expect(before.turnsById.live?.projection.content).toBe('before');
    expect(next.turnsById.sibling).toBe(before.turnsById.sibling);
    expect(next.attemptsById['live-attempt']?.lastSeq).toBe(2);
    for (const key of sharedCollections) expect(next[key], key).toBe(before[key]);
  });

  it('transport, pressure and command feedback do not copy conversation history', () => {
    const before = freeze(initial());
    for (const action of [
      { type: 'transport', status: 'recovering' },
      { type: 'push_withheld', pushWithheld: true },
      { type: 'command_pending', turnId: 'live', operation: 'stop' },
    ] as const) {
      const next = reduceTurnState(before, action);
      expect(next.turnsById).toBe(before.turnsById);
      expect(next.attemptsById).toBe(before.attemptsById);
      expect(next.laneOrder).toBe(before.laneOrder);
      expect(next.queueItems).toBe(before.queueItems);
    }
    const pending = freeze(reduceTurnState(before, {
      type: 'command_pending', turnId: 'live', operation: 'stop',
    }));
    const failed = reduceTurnState(pending, { type: 'command_failed', turnId: 'live' });
    expect(failed.commandPending.live).toBeUndefined();
    expect(pending.commandPending.live).toBe('stop');
    expect(failed.turnsById).toBe(pending.turnsById);
  });

  it('duplicate, foreign, stale and gapped deliveries preserve state and recovery behavior', () => {
    const before = freeze(initial());
    const gap = vi.fn();
    const duplicate = event('live', 1);
    const foreign = { ...event(), attemptId: 'foreign-attempt' };
    const gapped = { ...event(), payload: {
      projectionPatch: { version: 2, baseRevision: 5, targetRevision: 6, operations: [] },
    } } as unknown as TurnEvent;
    for (const incoming of [duplicate, foreign, gapped]) {
      expect(reduceTurnState(before, { type: 'event', event: incoming }, {
        onProjectionGap: gap,
      })).toBe(before);
    }
    expect(gap).toHaveBeenCalledExactlyOnceWith('live');
    expect(reduceTurnState(before, { type: 'snapshot', snapshot: {
      conversationRevision: 0, deletedTurnIds: ['live'],
    } })).toBe(before);
    expect(reduceTurnState(before, { type: 'transport', status: before.transport })).toBe(before);
  });

  it('admitting and removing related Turns clones only affected lane arrays and history', () => {
    const before = freeze(initial());
    const next = freeze(reduceTurnState(before, { type: 'event', event: event('live', 2, {
      turns: [turn('new', 4)], attempts: [{ attemptId: 'new-attempt', turnId: 'new' }],
    }) }));
    expect(next.laneOrder.main).toEqual(['live', 'sibling', 'new']);
    expect(before.laneOrder.main).toEqual(['live', 'sibling']);
    expect(next.laneOrder.branch).toBe(before.laneOrder.branch);
    expect(next.historyByLane.main?.totalTurns).toBe(3);
    expect(before.historyByLane.main?.totalTurns).toBe(2);
    const removed = reduceTurnState(next, { type: 'command_response', response: {
      conversationRevision: 2, deletedTurnIds: ['sibling'],
    } });
    expect(removed.laneOrder.main).toEqual(['live', 'new']);
    expect(next.laneOrder.main).toEqual(['live', 'sibling', 'new']);
    expect(removed.laneOrder.branch).toBe(next.laneOrder.branch);
    expect(removed.historyByLane.main?.totalTurns).toBe(2);
  });

  it('preserves pending event order in old snapshots while replaying a hydrated Turn', () => {
    const before = freeze(reduceTurnState(initial(), { type: 'event', event: event('missing', 3) }));
    const queued = freeze(reduceTurnState(before, { type: 'event', event: event('missing', 2) }));
    expect(before.pendingEventsByTurn.missing?.map(item => item.seq)).toEqual([3]);
    expect(queued.pendingEventsByTurn.missing?.map(item => item.seq)).toEqual([3, 2]);
    const next = reduceTurnState(queued, { type: 'snapshot', snapshot: {
      conversationRevision: 2, turns: [turn('missing', 4)],
    } });
    expect(next.turnsById.missing?.projection.content).toBe('after 3');
    expect(next.pendingEventsByTurn.missing).toBeUndefined();
    expect(queued.pendingEventsByTurn.missing?.map(item => item.seq)).toEqual([3, 2]);
  });

  it('same-position metadata refresh keeps lane identity; reordering copies that lane', () => {
    const before = freeze(initial());
    const refreshed = freeze(reduceTurnState(before, { type: 'snapshot', snapshot: {
      conversationRevision: 2, turns: [{ ...turn('live', 1), projectionRevision: 2 }],
    } }));
    expect(refreshed.laneOrder).toBe(before.laneOrder);
    const reordered = reduceTurnState(refreshed, { type: 'snapshot', snapshot: {
      conversationRevision: 3, turns: [{ ...turn('live', 5), projectionRevision: 3 }],
    } });
    expect(reordered.laneOrder.main).toEqual(['sibling', 'live']);
    expect(reordered.laneOrder.branch).toBe(before.laneOrder.branch);
    expect(refreshed.laneOrder.main).toEqual(['live', 'sibling']);
  });

  it('queue acknowledgement preserves prior rows and applies only with an accepted event', () => {
    const before = freeze(reduceTurnState(initial(), { type: 'snapshot', snapshot: {
      conversationRevision: 2,
      queueItems: ['done', 'remaining'].map((queueId, index) => ({
        queueId, position: index + 1, priority: 0, timestamp: 1, kind: 'message', text: queueId,
      })),
    } }));
    const duplicate = reduceTurnState(before, { type: 'event', event: event('live', 1, {
      removedQueueIds: ['done'],
    }) });
    expect(duplicate).toBe(before);
    const next = reduceTurnState(before, { type: 'event', event: event('live', 2, {
      removedQueueIds: ['done'],
    }) });
    expect(next.queueItems.map(item => [item.queueId, item.position])).toEqual([['remaining', 1]]);
    expect(before.queueItems.map(item => [item.queueId, item.position]))
      .toEqual([['done', 1], ['remaining', 2]]);
  });

  it('an authoritative reset replaces history without mutating the previous graph', () => {
    const before = freeze(initial());
    const next = reduceTurnState(before, { type: 'snapshot', snapshot: {
      authoritativeFull: true, conversationRevision: 2, turns: [turn('replacement', 9)],
    } });
    expect(Object.keys(next.turnsById)).toEqual(['replacement']);
    expect(next.laneOrder.main).toEqual(['replacement']);
    expect(before.laneOrder.main).toEqual(['live', 'sibling']);
    expect(before.historyByLane.main?.totalTurns).toBe(2);
  });
});


describe('canonical change receipts', () => {
  it('covers every written/deleted Turn and attempt, including queued replay and reset', () => {
    const store = createTurnStore('sharing');
    const receipts: Array<{ turnIds: readonly string[]; attemptIds: readonly string[]; reset: boolean }> = [];
    store.subscribe((state, update) => {
      expect(update.changes).toBeDefined();
      const receipt = update.changes!;
      receipts.push(receipt);
      for (const [key, ids] of [['turnsById', receipt.turnIds], ['attemptsById', receipt.attemptIds]] as const) {
        const previous = update.previousState[key], current = state[key];
        for (const id of new Set([...Object.keys(previous), ...Object.keys(current)])) {
          if (previous[id] !== current[id]) expect(receipt.reset || ids.includes(id), `${key}:${id}`).toBe(true);
        }
      }
    });
    store.dispatch({ type: 'event', event: event() });
    store.dispatch({ type: 'snapshot', snapshot: { turns: [turn('live', 1), turn('sibling', 2)],
      attempts: [{ attemptId: 'live-attempt', turnId: 'live', lastSeq: 1, status: 'running' }] } });
    expect(receipts.at(-1)?.turnIds).toContain('live');
    store.dispatch({ type: 'event', event: event('live', 3, {
      turns: [turn('related', 3)], attempts: [{ attemptId: 'related-attempt', turnId: 'related' }],
    }) });
    expect(receipts.at(-1)?.turnIds).toEqual(expect.arrayContaining(['live', 'related']));
    store.dispatch({ type: 'event', event: event('live', 3) });
    expect(receipts.at(-1)).toEqual({ turnIds: [], attemptIds: [], reset: false });
    store.dispatch({ type: 'snapshot', snapshot: { deletedTurnIds: ['live'], conversationRevision: 1 } });
    store.dispatch({ type: 'command_pending', turnId: 'sibling', operation: 'delete' });
    expect(receipts.at(-1)?.turnIds).toEqual(['sibling']);
    store.dispatch({ type: 'snapshot', snapshot: { authoritativeFull: true, conversationRevision: 2,
      turns: [turn('restored', 1)], attempts: [] } });
    expect(receipts.at(-1)?.reset).toBe(true);
  });
});

it('places snapshot and restored Turns with the same stable ordinal ordering as full sort', () => {
  let state = createTurnState('sharing');
  const expected: string[] = [], ordinals = new Map<string, number>();
  const updates = Array.from({ length: 600 }, (_, index) => ({
    id: `ordered-${index % 70}`, ordinal: (index * 17) % 31,
  }));
  for (let index = 0; index < updates.length; index += 6) {
    const batch = updates.slice(index, index + 6);
    for (const row of batch) {
      if (!expected.includes(row.id)) expected.push(row.id);
      ordinals.set(row.id, row.ordinal);
      expected.sort((left, right) => ordinals.get(left)! - ordinals.get(right)!);
    }
    state = reduceTurnState(state, { type: 'snapshot', snapshot: {
      turns: batch.map(row => ({ ...turn(row.id, row.ordinal), projectionRevision: index + 1 })),
    } });
    expect(state.laneOrder.main).toEqual(expected);
  }
  const id = updates[0].id;
  state = reduceTurnState(state, { type: 'snapshot', snapshot: {
    deletedTurnIds: [id], turns: [{ ...turn(id, 0), projectionRevision: 999 }],
  } });
  expect(state.laneOrder.main?.filter(item => item === id)).toEqual([id]);
});
