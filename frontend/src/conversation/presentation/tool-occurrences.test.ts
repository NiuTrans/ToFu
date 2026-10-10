import { describe, expect, it } from 'vitest';
import { createToolOccurrenceMatcher } from './tool-occurrences';
import { richToolRoundsBySegment } from './view-model/segment-blocks';
import { resolveTimelineBatches } from '../ui/segment-timeline/batches';
import { computeExecutionBatches } from './tool-execution-groups';
import type { TurnRecord } from '../../api/conversation-sync.generated';

const rounds = [
  { toolCallId: 'repeat', attemptId: 'A', taskId: 'task-a', query: 'Old result' },
  { toolCallId: 'repeat', attemptId: 'B', taskId: 'task-b', query: 'Retry result' },
  { toolCallId: 'repeat', attemptId: 'B', taskId: 'task-b', query: 'Second retry result' },
];
describe('continuation timeline producer boundaries', () => {
  it('pairs a retained later segment with its own attempt after earlier segments were trimmed', () => {
    const resolve = createToolOccurrenceMatcher(rounds);
    expect(resolve('repeat', { attemptId: 'B' })).toBe(rounds[1]);
    expect(resolve('repeat', { attemptId: 'B' })).toBe(rounds[2]);
    expect(resolve('repeat', { attemptId: 'C' })).toBeUndefined();
    expect(resolve('repeat', { attemptId: 'A' })).toBe(rounds[0]);
  });
  it('keeps legacy unscoped occurrence order and numeric IDs', () => {
    const rows = [{ toolCallId: 7 }, { toolCallId: 7 }];
    const resolve = createToolOccurrenceMatcher(rows);
    expect(resolve('7')).toBe(rows[0]);
    expect(resolve(7)).toBe(rows[1]);
  });
  it('preserves embedded bodies without shifting the next scoped occurrence', () => {
    const embedded = { ...rounds[1], query: 'Embedded result' };
    const turn = { projection: { toolRounds: rounds, segments: [
      { type: 'tool_use', id: 'repeat', attemptId: 'B', _round: embedded },
      { type: 'tool_use', id: 'repeat', attemptId: 'B' },
    ] } } as unknown as TurnRecord;
    expect(richToolRoundsBySegment(turn)).toEqual([embedded, rounds[2]]);
  });
  it('uses the same pairing on the settled timeline', () => {
    const batches = resolveTimelineBatches([
      { type: 'tool_use', id: 'repeat', attemptId: 'B', llmRound: 0 },
    ], rounds, new Set(), false, { groupSegments: computeExecutionBatches });
    expect(batches[0].rounds).toEqual([rounds[1]]);
  });
});
