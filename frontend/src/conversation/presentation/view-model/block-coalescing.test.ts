/** A provider retry storm renders as ONE collapsed disclosure, not N rows. */
import { describe, expect, it } from 'vitest';
import type { TurnActivityEntry } from '../../../api/conversation-sync.generated';
import { coalesceAdjacentRetryActivityBlocks } from './block-coalescing';
import type { ConversationBlockViewModel } from './types';

function activity(
  id: string,
  changes: Partial<TurnActivityEntry> = {},
): ConversationBlockViewModel {
  const entry: TurnActivityEntry = {
    id, spanId: id, seq: 1, occurredAt: 1000,
    kind: 'model', status: 'failed', severity: 'error',
    model: 'deepseek-v4.1-flash-m17', count: 1,
    statusCode: 429, durationMs: 556, ...changes,
  };
  return {
    blockId: `activity:${id}`, kind: 'activity-event', identitySource: 'contract',
    source: entry, value: entry,
  } as ConversationBlockViewModel;
}

function kinds(blocks: ReadonlyArray<ConversationBlockViewModel>) {
  return blocks.map(block => block.kind);
}

describe('retry/failure activity coalescing', () => {
  it('merges an adjacent storm into one group and keeps member rows', () => {
    const blocks = [
      activity('a'),
      activity('b', { kind: 'status', status: 'waiting', severity: 'warning', phase: 'retrying' }),
      activity('c'),
    ];
    coalesceAdjacentRetryActivityBlocks(blocks);
    expect(kinds(blocks)).toEqual(['activity-event-group']);
    const group = blocks[0];
    if (group.kind !== 'activity-event-group') throw new Error('expected group');
    // The first row keeps the DOM identity so a live storm paints in place.
    expect(group.blockId).toBe('activity:a');
    expect(group.items.map(item => item.value.id)).toEqual(['a', 'b', 'c']);
    expect(group.items.every(item => item.blockId.startsWith('activity:'))).toBe(true);
  });

  it('leaves a lone failure as a plain row', () => {
    const blocks = [activity('a')];
    coalesceAdjacentRetryActivityBlocks(blocks);
    expect(kinds(blocks)).toEqual(['activity-event']);
  });

  it('does not swallow the terminal error receipt', () => {
    const terminal = activity('terminal', {
      kind: 'error', summary: 'Turn failed', statusCode: 500,
    }) as Extract<ConversationBlockViewModel, { kind: 'activity-event' }>;
    terminal.terminalError = { kind: 'rate_limit' } as never;
    const blocks: ConversationBlockViewModel[] = [activity('a'), activity('b'), terminal];
    coalesceAdjacentRetryActivityBlocks(blocks);
    expect(kinds(blocks)).toEqual(['activity-event-group', 'activity-event']);
    expect(blocks[1].blockId).toBe('activity:terminal');
  });

  it('keeps durable boundaries and non-failures out of a run', () => {
    const compaction = activity('compact', {
      kind: 'status', status: 'succeeded', severity: 'info',
      reasonCode: 'context_compaction', statusCode: undefined,
    });
    const succeeded = activity('ok', { status: 'succeeded', severity: 'info', statusCode: undefined });
    const blocks = [
      activity('a'), activity('b'), compaction, activity('c'), activity('d'),
      succeeded, activity('e'), activity('f'),
    ];
    coalesceAdjacentRetryActivityBlocks(blocks);
    expect(kinds(blocks)).toEqual([
      'activity-event-group', 'activity-event', 'activity-event-group',
      'activity-event', 'activity-event-group',
    ]);
    expect(blocks[1]).toBe(compaction);
    expect(blocks[3]).toBe(succeeded);
  });

  it('is idempotent and keeps a single-row run out of a group', () => {
    const blocks = [activity('a'), activity('b')];
    coalesceAdjacentRetryActivityBlocks(blocks);
    const once = [...blocks];
    coalesceAdjacentRetryActivityBlocks(blocks);
    expect(blocks).toEqual(once);
  });
});
