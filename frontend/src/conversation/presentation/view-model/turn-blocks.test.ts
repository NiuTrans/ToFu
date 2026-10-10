/** Saved terminal text survives a checkpoint ending at a tool. */
import { describe, expect, it } from 'vitest';
import type { TurnRecord } from '../../../api/conversation-sync.generated';
import { selectTurnBlocks } from './turn-blocks';
function turn(status: TurnRecord['status'] = 'completed'): TurnRecord {
  return { turnId: 'oversized', actor: 'assistant', status, laneId: 'main',
    projection: { content: 'Saved final answer', segments: [
      { type: 'text', blockId: 'narration', text: 'Waiting for verification' },
      { type: 'tool_use', blockId: 'poll', id: 'poll', name: 'poll', input: {} },
    ] } } as unknown as TurnRecord;
}
describe('terminal answer with an older checkpoint', () => {
  it.each(['completed', 'interrupted', 'truncated', 'failed'] as const)(
    'shows saved %s content after tools without changing authority', status => {
      const source = turn(status), before = JSON.stringify(source);
      const blocks = selectTurnBlocks(source);
      expect(blocks.map(block => block.kind)).toEqual(['text', 'tool', 'text']);
      expect(blocks.at(-1)).toMatchObject({ blockId: 'text:terminal', displayMarkdown: 'Saved final answer' });
      expect(JSON.stringify(source)).toBe(before);
      expect(selectTurnBlocks(source)).toBe(blocks);
    });
  it('preserves existing deliverable identity and translation without duplication', () => {
    const source = turn();
    source.projection.segments = [...source.projection.segments!, { type: 'text', blockId: 'answer', text: 'Stored',
      deliverable: true, translatedText: '保存的最终回复' }];
    const blocks = selectTurnBlocks(source);
    expect(blocks.filter(block => block.kind === 'text')).toHaveLength(2);
    expect(blocks.at(-1)).toMatchObject({ blockId: 'answer', displayMarkdown: '保存的最终回复' });
  });
  it.each(['pending', 'running'] as const)('does not invent a %s answer', status => {
    expect(selectTurnBlocks(turn(status)).some(block => block.blockId === 'text:terminal')).toBe(false);
  });
  it('keeps compaction summaries and blank answers hidden', () => {
    const source = turn();
    source.projection.compaction = {} as NonNullable<TurnRecord['projection']['compaction']>;
    expect(selectTurnBlocks(source).some(block => block.blockId === 'text:terminal')).toBe(false);
    const blank = turn(); blank.projection.content = '  ';
    expect(selectTurnBlocks(blank).some(block => block.blockId === 'text:terminal')).toBe(false);
  });
});
