/**
 * Injection rounds: engine-authored `system_note` segments form their own
 * singleton batches at their wire position, never join the llmRound batch
 * they precede, and stay transparent to attempt-ordinal accounting.
 */
import { describe, expect, it } from 'vitest';

import { computeExecutionBatches } from './tool-execution-groups';

interface Seg {
  type: string;
  blockId?: string;
  llmRound?: number;
  text?: string;
  noteKind?: string;
  attemptId?: string;
}

const prose = (llmRound: number, blockId: string): Seg => ({
  type: 'text', llmRound, blockId, text: `prose-${blockId}`,
});
const tool = (llmRound: number, blockId: string): Seg => ({
  type: 'tool_use', llmRound, blockId,
});
const note = (
  llmRound: number, blockId: string, noteKind = 'context-injection',
): Seg => ({ type: 'system_note', llmRound, blockId, text: 'note', noteKind });

describe('computeExecutionBatches — injection-note rounds', () => {
  it('gives every note its own batch at its wire position', () => {
    const values = [
      prose(0, 't0'), tool(0, 'call0'),
      note(1, 'system-note:system-intervention-0'),
      prose(1, 't1'), tool(1, 'call1'),
    ];
    const batches = computeExecutionBatches(values, true);
    expect(batches.map((batch) => batch.baseKey)).toEqual([
      'L0', 'INJsystem-note:system-intervention-0', 'L1',
    ]);
    const injection = batches[1];
    expect(injection.injection).toBe(true);
    expect(injection.items).toHaveLength(1);
    expect(injection.llmRound).toBe(1);
    expect(batches[0].injection).toBeUndefined();
    expect(batches[2].injection).toBeUndefined();
  });

  it('keeps a turn-start note stack ahead of round 0, one batch per note, in entry order', () => {
    const values = [
      note(0, 'system-note:session-context-0', 'session-context'),
      note(0, 'system-note:context-injection-1'),
      note(0, 'system-note:context-injection-2'),
      prose(0, 't0'), tool(0, 'call0'),
    ];
    const batches = computeExecutionBatches(values, true);
    expect(batches.map((batch) => batch.baseKey)).toEqual([
      'INJsystem-note:session-context-0',
      'INJsystem-note:context-injection-1',
      'INJsystem-note:context-injection-2',
      'L0',
    ]);
    expect(batches.slice(0, 3).every((batch) => batch.injection === true)).toBe(true);
  });

  it('does not inflate attempt ordinals: a note pinned to round N never resets the attempt', () => {
    const values = [
      prose(0, 't0'), tool(0, 'call0'),
      note(0, 'system-note:context-injection-0'),
      prose(1, 't1'), tool(1, 'call1'),
    ];
    const batches = computeExecutionBatches(values, true);
    expect(batches.map((batch) => [batch.baseKey, batch.attemptOrdinal])).toEqual([
      ['L0', 1], ['INJsystem-note:context-injection-0', 1], ['L1', 1],
    ]);
    for (const batch of batches) expect(batch.totalAttempts).toBe(1);
  });

  it('keeps the legacy resume reset on ordinary batches when notes intervene', () => {
    const values = [
      prose(0, 't0'), tool(0, 'call0'),
      note(1, 'system-note:context-injection-0'),
      prose(1, 't1'),
      prose(0, 'retry-t0'), tool(0, 'retry-call0'),
    ];
    const batches = computeExecutionBatches(values, true);
    const ordinary = batches.filter((batch) => !batch.injection);
    expect(ordinary.map((batch) => batch.attemptOrdinal)).toEqual([1, 1, 2]);
    expect(ordinary[0].totalAttempts).toBe(2);
    expect(batches[1].injection).toBe(true);
    // The note precedes the round-1 batch of attempt 1 and inherits it.
    expect(batches[1].attemptOrdinal).toBe(1);
  });

  it('scopes injection notes to their recorded attempt via attemptId', () => {
    const withScope = (seg: Seg, attemptId: string): Seg => ({ ...seg, attemptId });
    const values = [
      withScope(prose(0, 't0'), 'a1'), withScope(tool(0, 'call0'), 'a1'),
      withScope(note(1, 'system-note:context-injection-0'), 'a2'),
      withScope(prose(1, 't1'), 'a2'), withScope(tool(1, 'call1'), 'a2'),
    ];
    const batches = computeExecutionBatches(values, true);
    const injection = batches[1];
    expect(injection.injection).toBe(true);
    expect(injection.scope).toBe('a2');
    expect(injection.attemptOrdinal).toBe(2);
    expect(injection.totalAttempts).toBe(2);
  });

  it('falls back to a positional key for legacy notes without blockId', () => {
    const values = [prose(0, 't0'), note(1, undefined as unknown as string), note(1, undefined as unknown as string)];
    const batches = computeExecutionBatches(values, true);
    expect(batches.map((batch) => batch.baseKey)).toEqual([
      'L0', 'INJposition-1', 'INJposition-2',
    ]);
  });

  it('leaves tool-round batching untouched (no system_note values)', () => {
    const rounds = [
      { toolCallId: 'c0', llmRound: 0 },
      { toolCallId: 'c1', llmRound: 0 },
      { toolCallId: 'c2', llmRound: 1 },
    ];
    const batches = computeExecutionBatches(rounds, true);
    expect(batches.map((batch) => [batch.baseKey, batch.items.length])).toEqual([
      ['L0', 2], ['L1', 1],
    ]);
    expect(batches.every((batch) => !batch.injection)).toBe(true);
  });
});


describe('seamless logical continuation', () => {
  it.each([
    [[0, 1, 0, 1], [1, 2, 3, 4]],
    [[0, 4, 0, 1], [1, 5, 6, 7]],
    [[0, 1, 2, 3], [1, 2, 3, 4]],
  ])('folds legacy resets but preserves cumulative counters and gaps', (raw, expected) => {
    const rows = raw.map((llmRound, i) => ({
      llmRound, roundNum: i + 1, toolCallId: `call-${i}`,
      attemptId: i < 2 ? 'old' : 'new',
    }));
    const before = JSON.stringify(rows);
    const batches = computeExecutionBatches(rows, true);
    expect(batches.map(b => b.displayRoundNumber)).toEqual(expected);
    expect(new Set(batches.map(b => b.key)).size).toBe(4);
    expect(JSON.stringify(rows)).toBe(before);
  });

  it('continues legacy tool-only counters across producing scopes', () => {
    const rows = [
      {roundNum: 1, attemptId: 'old', toolCallId: 'a'},
      {roundNum: 2, attemptId: 'old', toolCallId: 'b'},
      {roundNum: 1, attemptId: 'new', toolCallId: 'c'},
    ];
    expect(computeExecutionBatches(rows, false).map(b => b.displayRoundNumber))
      .toEqual([1, 2, 3]);
  });
});
