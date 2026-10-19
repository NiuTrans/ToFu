/** Chronology and linear-work contracts for program parents. */
import { describe, expect, it } from 'vitest';
import type { TurnRecord, TurnToolRound } from '../../../api/conversation-sync.generated';
import type { ConversationBlockViewModel } from './types';
import { addProgramBlocks } from './program-blocks';
import { selectTurnBlocks } from './turn-blocks';

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

  it('uses the earliest later tool only when its own round has no authored anchor', () => {
    expect(project([tool('a'), tool('b'), text('answer')], [
      program('p', 8), { toolCallId: 'b' } as TurnToolRound, { toolCallId: 'a' } as TurnToolRound,
    ])).toEqual(['program:p', 'a', 'b', 'answer']);
  });

  it('keeps a gatewayless childless parent in its authored round before later thinking and prose', () => {
    expect(project([text('thought', 11), text('next-thought', 12), text('narration', 12), tool('read', 12)], [
      program('lookup', 11), { toolCallId: 'read', llmRound: 12 } as TurnToolRound,
    ])).toEqual(['thought', 'program:lookup', 'next-thought', 'narration', 'read']);
  });

  it('does not borrow a same-numbered round from another attempt or task', () => {
    const own = { ...text('own-thought', 11), source: { llmRound: 11, attemptId: 'a', taskId: 'task-a' } };
    const resumed = { ...text('resumed-thought', 11), source: { llmRound: 11, attemptId: 'b', taskId: 'task-b' } };
    expect(project([own, resumed, tool('later', 12)], [
      { ...program('lookup', 11), attemptId: 'a', taskId: 'task-a' },
      { toolCallId: 'later', llmRound: 12 } as TurnToolRound,
    ])).toEqual(['own-thought', 'program:lookup', 'resumed-thought', 'later']);
  });

  it.each([
    [{ attemptId: 'a' }, { attemptId: 'a', taskId: 'task-a' }],
    [{ attemptId: 'a', taskId: 'task-a' }, { attemptId: 'a' }],
    [{ taskId: 'task-a' }, { attemptId: 'a', taskId: 'task-a' }],
    [{ attemptId: 'a', taskId: 'task-a' }, { taskId: 'task-a' }],
    [{}, { attemptId: 'a', taskId: 'task-a' }],
    [{ attemptId: 'a', taskId: 'task-a' }, {}],
  ])('anchors a uniquely compatible partially stamped round (%j, %j)', (authoredScope, programScope) => {
    const thought = { ...text('thought', 11), source: { llmRound: 11, ...authoredScope } };
    expect(project([thought, text('next-thought', 12), tool('later', 12)], [
      { ...program('lookup', 11), ...programScope },
      { toolCallId: 'later', llmRound: 12 } as TurnToolRound,
    ])).toEqual(['thought', 'program:lookup', 'next-thought', 'later']);
  });

  it('does not infer an owner from ambiguously scoped round anchors', () => {
    const thoughts = ['a', 'b'].map(attemptId => ({
      ...text(`thought-${attemptId}`, 11), source: { llmRound: 11, attemptId },
    }));
    expect(project([...thoughts, text('next-thought', 12), tool('later', 12)], [
      program('lookup', 11), { toolCallId: 'later', llmRound: 12 } as TurnToolRound,
    ])).toEqual(['thought-a', 'thought-b', 'next-thought', 'program:lookup', 'later']);
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

const gatewaySegment = (attemptId?: string) => ({
  type: 'tool_use', blockId: `gateway:${attemptId ?? 'unknown'}`, id: 'reused',
  name: 'execute_program', llmRound: 11, input: {}, attemptId,
  _round: { toolName: 'execute_program', toolCallId: 'reused', llmRound: 11, attemptId },
});
const thoughtSegment = (attemptId: string) => ({
  type: 'thinking', blockId: `thought:${attemptId}`, llmRound: 11, text: attemptId, attemptId,
});
function selectorTurn(segments: unknown[], toolRounds: unknown[]): TurnRecord {
  return { turnId: 'turn', actor: 'assistant', status: 'completed',
    projection: { segments, toolRounds } } as unknown as TurnRecord;
}

it('keeps ordered gateway round evidence authoritative over a greedily paired rich body', () => {
  const thoughts = [11, 12].map(llmRound => ({
    type: 'thinking', blockId: `thought:${llmRound}`, text: String(llmRound), llmRound,
  }));
  const gateway = (llmRound: number) => ({
    type: 'tool_use', blockId: `gateway:${llmRound}`, name: 'execute_program',
    id: 'reused', llmRound, input: {},
  });
  const rounds = [11, 12].flatMap(llmRound => [
    { toolCallId: 'reused', toolName: 'execute_program', llmRound },
    { ...program('reused', llmRound) },
  ]);
  const before = selectorTurn([thoughts[0], thoughts[1], gateway(12)], rounds);
  const after = selectorTurn([thoughts[0], gateway(11), thoughts[1], gateway(12)], rounds);
  for (const turn of [before, after]) {
    expect(selectTurnBlocks(turn, {}).map(block => block.blockId)).toEqual([
      'thought:11', 'program:reused', 'thought:12', 'program:reused~2',
    ]);
  }
});

it('preserves same-round parent order when only the later gateway has materialized', () => {
  const thought = thoughtSegment('a');
  const next = { type: 'thinking', blockId: 'next', text: 'next', llmRound: 12, attemptId: 'a' };
  const rounds = ['first', 'second'].map(id => ({ ...program(id, 11), attemptId: 'a' }));
  const gateway = (id: string) => ({ ...gatewaySegment('a'), id, blockId: `gateway:${id}`, _round: undefined });
  for (const ids of [[], ['second'], ['first'], ['first', 'second']]) {
    const segments = [thought, ...ids.map(gateway), next];
    expect(selectTurnBlocks(selectorTurn(segments, rounds), {}).map(block => block.blockId)).toEqual([
      'thought:a', 'program:first', 'program:second', 'next',
    ]);
  }
});

it('keeps program IDs with their owners as earlier gateway segments materialize', () => {
  const rounds = ['a', 'b'].map(attemptId => ({ ...program('reused', 11), attemptId }));
  const before = selectorTurn([thoughtSegment('a'), thoughtSegment('b'), gatewaySegment('b')], rounds);
  const after = selectorTurn([thoughtSegment('a'), gatewaySegment('a'), thoughtSegment('b'), gatewaySegment('b')], rounds);
  const original = JSON.stringify([before, after]);
  const identities = (turn: TurnRecord) => selectTurnBlocks(turn).filter(block => block.kind === 'program')
    .map(block => [block.round.attemptId, block.blockId]);
  expect(identities(before)).toEqual([['a', 'program:reused'], ['b', 'program:reused~2']]);
  expect(identities(after)).toEqual(identities(before));
  expect(selectTurnBlocks(after).map(block => block.blockId)).toEqual([
    'thought:a', 'program:reused', 'thought:b', 'program:reused~2',
  ]);
  expect(JSON.stringify([before, after])).toBe(original);
});

it('does not let an earlier unscoped gateway consume the later scoped owner', () => {
  const owner = { ...program('reused', 11), attemptId: 'b', taskId: 'task-b' };
  const turn = selectorTurn([gatewaySegment(), thoughtSegment('b'),
    { ...gatewaySegment('b'), taskId: 'task-b' }], [owner]);
  expect(selectTurnBlocks(turn).map(block => [block.kind, block.blockId])).toEqual([
    ['tool', 'gateway:unknown'], ['thinking', 'thought:b'], ['program', 'program:reused'],
  ]);
});

it('keeps equally supported gateway occurrences visible instead of choosing the first', () => {
  const turn = selectorTurn([gatewaySegment('b'), { ...gatewaySegment('b'), blockId: 'gateway:other' }],
    [{ ...program('reused', 11), attemptId: 'b' }]);
  expect(selectTurnBlocks(turn).map(block => block.kind)).toEqual(['tool', 'tool', 'program']);
});

it.each([false, true])('bounds selector work for a large legacy gateway history (parents: %s)', withParents => {
  let reads = 0;
  const count = 1_000;
  const segments: unknown[] = [];
  const rounds: TurnToolRound[] = [];
  for (let index = 0; index < count; index++) {
    const attemptId = `attempt-${index}`;
    const gateway = { ...gatewaySegment(attemptId), name: 'execute_tools' };
    segments.push(gateway);
    const round = { ...gateway._round, toolName: 'execute_tools' } as TurnToolRound;
    Object.defineProperty(round, '_programSynthetic', { get() { reads++; return false; } });
    rounds.push(round);
    if (withParents) {
      const parent = { ...program('reused', 11), attemptId };
      Object.defineProperty(parent, '_programSynthetic', { get() { reads++; return true; } });
      rounds.push(parent);
    }
  }
  const blocks = selectTurnBlocks(selectorTurn(segments, rounds));
  expect(blocks.filter(block => block.kind === 'program')).toHaveLength(withParents ? count : 0);
  expect(blocks.filter(block => block.kind === 'tool')).toHaveLength(0);
  expect(reads).toBeLessThanOrEqual(count * 12);
  console.info(`program selector: ${count} legacy gateways, parents=${withParents}, ${reads} ownership reads`);
});
