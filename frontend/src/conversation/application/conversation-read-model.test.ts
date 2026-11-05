import { describe, expect, it } from 'vitest';
import { createTurnState } from '../domain/turn-store';
import { conversationCompactionArchiveIds } from './conversation-read-model';

function turn(turnId: string, projection: Record<string, unknown>, conversationId = 'conv') {
  return {
    turnId, presentationId: turnId, runId: '', conversationId, laneId: 'main', ordinal: 1,
    actor: 'assistant', kind: 'reply', status: 'completed', currentAttemptId: null,
    projectionRevision: 1, createdAt: 1, updatedAt: 1, settlement: {}, projection,
  } as never;
}

function timeline(...entries: Array<Record<string, unknown>>) {
  return {
    blockId: 'activity-timeline', version: 1,
    entries: entries.map((entry, index) => ({
      id: `row-${index}`, spanId: `row-${index}`, seq: index, occurredAt: index,
      kind: 'status', status: 'succeeded', severity: 'info', count: 1, ...entry,
    })),
  };
}

describe('conversationCompactionArchiveIds', () => {
  it('collects receipts and compaction Turns of this conversation once', () => {
    const state = createTurnState('conv');
    state.turnsById.auto = turn('auto', { activityTimeline: timeline(
      { reasonCode: 'context_compaction', status: 'running', archiveId: 'archive-auto' },
      { kind: 'tool' },
      { reasonCode: 'retrying', severity: 'warning', archiveId: 'not-a-receipt' },
    ) });
    state.turnsById.manual = turn('manual', {
      compaction: { blockId: 'compaction', archiveId: 'archive-manual' },
      activityTimeline: timeline({ reasonCode: 'context_compaction', archiveId: 'archive-auto' }),
    });
    state.turnsById.foreign = turn('foreign', {
      compaction: { blockId: 'compaction', archiveId: 'archive-elsewhere' },
    }, 'other-conversation');

    expect([...conversationCompactionArchiveIds(state)].sort())
      .toEqual(['archive-auto', 'archive-manual']);
    expect(conversationCompactionArchiveIds(null)).toEqual([]);
  });
});
