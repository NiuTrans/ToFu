import { describe, expect, it } from 'vitest';
import type { TurnContentSegment, TurnRecord } from '../../../api/conversation-sync.generated';
import { selectTerminalPresentationSegments } from './terminal-segment-presentation';
import { selectTurnBlocks } from './turn-blocks';

const prose = 'Actual critique\n\nRemaining issues are explicit.';
const prior = { type: 'text', blockId: 'text:terminal', text: `${prose}\n\n[PROGRESS: resolved=2 remaining=2]`, terminal: false, deliverable: false } as const;
const completion = { type: 'text', blockId: 'text:completion-19', text: prose, terminal: true, deliverable: true } as const;
const thinking = { type: 'thinking', blockId: 'thinking:1', text: 'Detailed reasoning', terminal: false } as const;
const tool = { type: 'tool_use', blockId: 'tool:1', id: 'call-1', name: 'read_file', input: {}, result: { status: 'completed' } } as const;
function makeTurn(status: TurnRecord['status'], segments: ReadonlyArray<TurnContentSegment>): TurnRecord {
  return { turnId: 'vu', presentationId: 'vu', conversationId: 'c', runId: 'run',
    createdAt: 1, updatedAt: 2, laneId: 'main', ordinal: 6, actor: 'virtual_user', status,
    kind: 'flow_node', projectionRevision: 1, projection: { content: prose, segments,
      toolRounds: [{ toolCallId: 'call-1', toolName: 'read_file', toolArgs: { path: 'source.ts' }, results: ['Rich result'] }] }, settlement: {} };
}
describe('canonical terminal presentation evidence', () => {
  it.each(['running', 'completed'] as const)('projects historical VU %s once without modifying durable carriers', status => {
    // Keep identical intermediate prose, reasoning and an independently rich
    // tool result even when the superseded owner lies before that tool.
    const segments: TurnContentSegment[] = [{ ...prior, blockId: 'text:intermediate' }, thinking, prior, tool, completion];
    const turn = makeTurn(status, segments);
    const before = JSON.stringify(turn);
    const blocks = selectTurnBlocks(turn);
    expect(blocks.filter(block => block.kind === 'text').map(block => block.blockId)).toEqual(['text:intermediate', 'text:completion-19']);
    expect(blocks.find(block => block.kind === 'thinking')).toBeDefined();
    const toolBlock = blocks.find(block => block.kind === 'tool');
    expect(toolBlock?.kind === 'tool' && toolBlock.round?.results).toEqual(['Rich result']);
    expect(toolBlock?.kind === 'tool' && toolBlock.round?.toolArgs).toEqual({ path: 'source.ts' });
    expect(JSON.stringify(turn)).toBe(before);
  });
  it.each([
    '[PROGRESS: resolved=2;remaining=2]', '[progress: resolved=2 remaining=2]',
    '[VERDICT: CONTINUE_WORKER]', '[VERDICT: BLOCKED]', '[VU: TASK_DONE]',
  ])('recognizes registered sanitizer equivalence: %s', token => {
    const segments = [{ ...prior, text: `${prose}\n\n\n${token}\n\n` }, completion];
    expect(selectTerminalPresentationSegments({ actor: 'virtual_user' }, segments)).toEqual([completion]);
  });
  it('handles embedded tokens with exact sanitation, not normalized prose comparison', () => {
    const segments = [{ ...prior, text: 'A [VERDICT: STOP] B' }, { ...completion, text: 'A  B' }];
    expect(selectTerminalPresentationSegments({ actor: 'virtual_user' }, segments)).toEqual([segments[1]]);
    const changed = [segments[0], { ...completion, text: 'A B' }];
    expect(selectTerminalPresentationSegments({ actor: 'virtual_user' }, changed)).toBe(changed);
  });
  it.each(['human', 'assistant', 'planner'] as const)('never sanitizes or collapses %s chronology', actor => {
    const segments = [prior, completion];
    expect(selectTerminalPresentationSegments({ actor }, segments)).toBe(segments);
  });
  it('preserves different answers, arbitrary repetition, live tails and non-owner prose', () => {
    for (const segments of [
      [prior, { ...completion, text: 'A different critique' }],
      [{ ...prior, text: prose }, completion],
      [{ ...prior, blockId: 'text:round-3' }, completion],
      [prior, { ...completion, terminal: false }],
      [prior, { ...completion, blockId: 'text:unrelated' }],
      [prior, { ...prior, blockId: 'text:intermediate' }, completion],
      [prior, completion, thinking],
    ]) expect(selectTerminalPresentationSegments({ actor: 'virtual_user' }, segments)).toBe(segments);
  });
});
