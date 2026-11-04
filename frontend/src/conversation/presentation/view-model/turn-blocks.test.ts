/** Saved terminal text survives a checkpoint ending at a tool. */
import { describe, expect, it } from 'vitest';
import type { TurnRecord } from '../../../api/conversation-sync.generated';
import { selectTurnBlocks } from './turn-blocks';
import { visibleActivityEntries } from './activity-blocks';
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

describe('answer presentation during live generation and translation', () => {
  function accumulator(status: TurnRecord['status']): TurnRecord {
    return { ...turn(status), projection: {
      content: 'I will check the file.',
      segments: [{ type: 'text', blockId: 'text:terminal', text: 'I will check the file.',
        terminal: true, deliverable: true }],
    } };
  }

  it.each(['pending', 'running'] as const)(
    'keeps the %s accumulator as narration before, during and after translation', status => {
      const source = accumulator(status), before = JSON.stringify(source);
      const original = selectTurnBlocks(source)[0];
      const preview = selectTurnBlocks(source, {}, 'translated', {
        status: 'pending', partialByRound: { 'text:terminal': '我会检查' },
      })[0];
      const translated = selectTurnBlocks({ ...source, projection: { ...source.projection,
        translatedContent: '我会检查文件。', translation: { status: 'completed', model: 'translator' },
      } })[0];
      for (const block of [original, preview, translated]) {
        expect(block).toMatchObject({ kind: 'text', blockId: 'text:terminal', deliverable: false });
      }
      expect(original).toMatchObject({ displayMarkdown: 'I will check the file.' });
      expect(preview).toMatchObject({ displayMarkdown: '我会检查' });
      expect(translated).toMatchObject({ displayMarkdown: '我会检查文件。' });
      expect(JSON.stringify(source)).toBe(before);
    });

  it.each(['completed', 'interrupted', 'truncated', 'failed'] as const)(
    'promotes only the %s turn tail without needing a text or translation change', status => {
      const source = accumulator('running');
      expect(selectTurnBlocks(source)[0]).toMatchObject({ deliverable: false });
      expect(selectTurnBlocks({ ...source, status })[0]).toMatchObject({
        blockId: 'text:terminal', deliverable: true, displayMarkdown: source.projection.content,
      });
      // Resume immediately removes the final-answer decoration again.
      expect(selectTurnBlocks({ ...source, status: 'running' })[0]).toMatchObject({ deliverable: false });
    });
});


describe('saved execute_tools failure replay', () => {
  it('leaves child failures on their cards, not in duplicate gateway rows', () => {
    const parent = {
      roundNum: 67, toolCallId: 'gateway-call', toolName: 'execute_tools',
      taskId: 'task-a', attemptId: 'attempt-a', status: 'error',
      toolContent: JSON.stringify({ artifactRef: 'tr:batch', items: [{ results: [
        { call_id: 'child-a', name: 'read_doc', status: 'error', resultId: 'receipt-a' },
        { call_id: 'child-b', name: 'read_doc', status: 'error', resultId: 'receipt-b' },
      ] }] }),
    };
    const childRound = (suffix: string, ordinal: number) => ({
      roundNum: ordinal, toolCallId: `child-${suffix}`, toolName: 'read_doc',
      taskId: 'task-a', attemptId: 'attempt-a', status: 'error',
      toolResultEvidence: { resultId: `receipt-${suffix}`, executionStatus: 'error' },
    });
    const fallback = (suffix: string) => ({
      id: `activity:batch:gateway-${suffix}`, spanId: `gateway-tool:${suffix}`,
      seq: 1, occurredAt: 1000, count: 1, kind: 'tool', status: 'failed', severity: 'error',
      toolName: 'read_doc', toolCallId: `child-${suffix}`, anchorToolRoundNum: 67,
      taskId: 'task-a', attemptId: 'attempt-a', summaryKey: 'activity.tool.failed',
      detail: `failure ${suffix}`,
    });
    const source = turn();
    source.projection.toolRounds = [parent, childRound('a', 8700032),
      childRound('b', 8700034)] as never;
    source.projection.segments = [parent, childRound('a', 8700032),
      childRound('b', 8700034)].map(round => ({
      type: 'tool_use', blockId: `tool:${round.toolCallId}`, id: round.toolCallId,
      name: round.toolName, roundNum: round.roundNum, taskId: 'task-a', attemptId: 'attempt-a',
      input: {}, result: {},
    })) as never;
    source.projection.activityTimeline = { blockId: 'activity-timeline', version: 1,
      entries: [fallback('a'), fallback('b')] } as never;
    const blocks = selectTurnBlocks(source);
    const children = blocks.filter(block => block.kind === 'tool' && block.name === 'read_doc');
    expect(children).toHaveLength(2);
    expect(visibleActivityEntries(source.projection.activityTimeline,
      source.projection.toolRounds, blocks)).toEqual([]);
  });
});
describe('retry storms in the projection pipeline', () => {
  const activity = (id: string, changes: Record<string, unknown>) => ({
    id, spanId: id, seq: 1, occurredAt: 1000, count: 1,
    kind: 'model', status: 'failed', severity: 'error',
    model: 'deepseek-v4.1-flash-m17', statusCode: 429, ...changes,
  });
  function storm(): TurnRecord {
    const source = turn('completed');
    source.projection.activityTimeline = {
      blockId: 'activity-timeline', version: 1,
      entries: [
        activity('fail-1', {}),
        activity('wait-1', { kind: 'status', status: 'waiting',
          severity: 'warning', phase: 'retrying', durationMs: 11000 }),
        activity('fail-2', {}),
        activity('wait-2', { kind: 'status', status: 'waiting',
          severity: 'warning', phase: 'retrying', durationMs: 11000 }),
      ],
    } as TurnRecord['projection']['activityTimeline'];
    return source;
  }

  it('collapses a live retry storm into one group and keeps the answer', () => {
    const source = storm();
    const blocks = selectTurnBlocks(source);
    const kinds = blocks.map(block => block.kind);
    expect(kinds.filter(kind => kind === 'activity-event')).toHaveLength(0);
    expect(kinds.filter(kind => kind === 'activity-event-group')).toHaveLength(1);
    const group = blocks.find(block => block.kind === 'activity-event-group');
    if (group?.kind !== 'activity-event-group') throw new Error('expected group');
    expect(group.items.map(item => item.value.id))
      .toEqual(['fail-1', 'wait-1', 'fail-2', 'wait-2']);
    // The final answer and its settlement are untouched by display grouping.
    expect(blocks.some(block => block.kind === 'text' && block.deliverable)).toBe(true);
    expect(source.projection.activityTimeline?.entries).toHaveLength(4);
  });
});
