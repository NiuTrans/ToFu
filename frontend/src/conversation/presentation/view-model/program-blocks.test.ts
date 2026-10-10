/** Chronology and linear-work contracts for program parents. */
import { describe, expect, it } from 'vitest';
import type { TurnRecord, TurnToolRound } from '../../../api/conversation-sync.generated';
import type { ConversationBlockViewModel } from './types';
import { addProgramBlocks } from './program-blocks';

function tool(id: string, llmRound: number | undefined = 0): ConversationBlockViewModel {
  return { kind: 'tool', blockId: id, toolCallId: id, source: { llmRound }, round: { llmRound } } as unknown as ConversationBlockViewModel;
}

function text(id: string, llmRound: number | undefined = 0): ConversationBlockViewModel {
  return { kind: 'text', blockId: id, markdown: id, source: { llmRound } } as unknown as ConversationBlockViewModel;
}

function program(id: string, llmRound: number | undefined, childCallIds: string[] = []): TurnToolRound {
  return { _programSynthetic: true, _programCallId: id, llmRound, childCallIds } as TurnToolRound;
}

function project(blocks: ConversationBlockViewModel[], rounds: TurnToolRound[]): string[] {
  const turn = { turnId: 'turn', projection: { toolRounds: rounds } } as unknown as TurnRecord;
  const sourceBefore = JSON.stringify({ turn, blocks });
  const sourceBlocks = blocks.slice();
  addProgramBlocks(turn, blocks, new Map(), {});
  expect(JSON.stringify({ turn, blocks: sourceBlocks })).toBe(sourceBefore);
  expect(sourceBlocks.every(block => blocks.includes(block))).toBe(true);
  return blocks.map(block => block.blockId);
}

describe('program parent chronology', () => {
  it('anchors to the first durable child occurrence, independent of child-list order', () => {
    expect(project([tool('a', 4), tool('b', 0), text('answer')], [
      program('p', 0, ['missing', 'b', 'a']),
    ])).toEqual(['program:p', 'a', 'b', 'answer']);
  });

  it('chooses the earliest visible tool among all later rounds', () => {
    expect(project([tool('a'), tool('b'), text('answer')], [
      program('p', 0), { toolCallId: 'b' } as TurnToolRound, { toolCallId: 'a' } as TurnToolRound,
    ])).toEqual(['program:p', 'a', 'b', 'answer']);
  });

  it('keeps same-gap parent ordering and lets childless failures follow their own last parent', () => {
    expect(project([text('thought', 0), tool('child', 9)], [
      program('first', 0, ['child']), program('other', 1, ['child']), program('failure', 0),
    ])).toEqual(['thought', 'program:first', 'program:failure', 'program:other', 'child']);
  });

  it('does not replace a later same-round marker when a new parent anchors earlier', () => {
    expect(project([tool('early', 3), text('later', 0), text('answer', 2)], [
      program('early-parent', 0, ['early']), program('failure', 0),
    ])).toEqual(['program:early-parent', 'early', 'later', 'program:failure', 'answer']);
  });

  it('preserves null round fallback, duplicate identities and malformed child lists', () => {
    const first = program('same', undefined);
    const second = { ...program('same', undefined), childCallIds: ['missing', null, 1] } as unknown as TurnToolRound;
    expect(project([text('legacy', NaN)], [first, second])).toEqual([
      'legacy', 'program:same', 'program:same~2',
    ]);
  });

  it('reuses original blocks and skips their evidence when there are no programs', () => {
    const block = text('plain');
    Object.defineProperty(block, 'source', { get() { throw new Error('Unexpected program evidence scan'); } });
    const blocks = [block];
    addProgramBlocks({ turnId: 'turn', projection: { toolRounds: [] } } as unknown as TurnRecord, blocks, new Map(), {});
    expect(blocks[0]).toBe(block);
  });

  it('bounds suffix and round evidence reads linearly for thousands of childless parents', () => {
    let reads = 0;
    let roundReads = 0;
    const count = 2_000;
    const rounds = Array.from({ length: count }, (_, index) => {
      const round = program(String(index), index);
      Object.defineProperty(round, 'toolCallId', { get() { reads += 1; return undefined; } });
      Object.defineProperty(round, 'llmRound', { get() { roundReads += 1; return index; } });
      return round;
    });
    const blocks: ConversationBlockViewModel[] = [];
    addProgramBlocks({ turnId: 'turn', projection: { toolRounds: rounds } } as unknown as TurnRecord, blocks, new Map(), {});
    expect(blocks.map(block => block.blockId)).toEqual(rounds.map((_, index) => `program:${index}`));
    expect(reads).toBeLessThanOrEqual(count * 2);
    expect(roundReads).toBeLessThanOrEqual(count * 2);
  });
});
