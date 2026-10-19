import { describe, expect, it } from 'vitest';
import type { TurnActivityEntry, TurnActivityTimeline } from '../../api/conversation-sync.generated';
import type { ConversationBlockViewModel } from './conversation-view-model';
import {
  activityAnchorIndex, activityAnchorWindows, activityEntryAttemptId,
} from './activity-anchor';

function entry(extra: Partial<TurnActivityEntry> = {}): TurnActivityEntry {
  return { id: 'diagnostic', spanId: 'span', count: 1, seq: 9, occurredAt: 10, kind: 'tool',
    status: 'failed', severity: 'error', summary: 'failed', ...extra };
}
function tool(roundNum: number, taskId = 'task-a', attemptId = 'attempt-a'): ConversationBlockViewModel {
  return { kind: 'tool', blockId: `card:${taskId}:${roundNum}`, identitySource: 'contract',
    name: 'read', input: {}, result: {}, toolCallId: 'reused',
    source: { type: 'tool_use', blockId: `tool:${taskId}:${roundNum}`, id: 'reused',
      roundNum, taskId, attemptId, name: 'read', result: '', llmRound: 0 },
    round: { toolName: 'read', roundNum, toolCallId: 'reused', taskId, attemptId, llmRound: 0 },
  };
}
function prose(blockId: string, taskId = 'task-a', attemptId = 'attempt-a'): ConversationBlockViewModel {
  return { kind: 'text', blockId, identitySource: 'contract', markdown: 'response',
    displayMarkdown: 'response', displayMode: 'original',
    deliverable: false, terminal: false, resumable: false,
    source: { type: 'text', blockId, text: 'response', taskId, attemptId, llmRound: 0 },
  };
}
function timeline(entries: TurnActivityEntry[]): TurnActivityTimeline {
  return { blockId: 'activity-timeline', version: 1, entries };
}

describe('producer occurrence anchors', () => {
  it('uses the exact occurrence despite recycled provider IDs and a stale VM pairing', () => {
    const first = tool(1);
    const second = tool(2);
    if (first.kind === 'tool' && second.kind === 'tool') second.round = first.round;
    const blocks = [first, second, prose('answer')];
    expect(activityAnchorIndex(blocks, entry({ taskId: 'task-a', attemptId: 'attempt-a',
      anchorToolRoundNum: 2, toolCallId: 'reused' }))).toBe(2);
  });

  it('scopes repeated round ordinals by producer task/attempt, not display attempt', () => {
    const blocks = [tool(1), tool(1, 'task-child', 'attempt-child'), prose('answer')];
    expect(activityAnchorIndex(blocks, entry({ id: 'activity:display:9', taskId: 'task-child',
      attemptId: 'attempt-child', anchorToolRoundNum: 1 }))).toBe(2);
  });

  it('does not first-match an ambiguous or cross-task legacy call ID', () => {
    expect(activityAnchorIndex([tool(1), tool(2), prose('answer')], entry({ toolCallId: 'reused' }))).toBe(0);
    expect(activityAnchorIndex([tool(1), prose('answer')], entry({
      taskId: 'missing-child', toolCallId: 'reused', llmRound: 0 }))).toBe(2);
    expect(activityAnchorIndex([tool(1), prose('answer')], entry({ toolCallId: 'reused' }))).toBe(1);
  });

  it('uses an explicit content block and never aliases missing exact anchors to a call ID', () => {
    const blocks = [prose('think'), tool(1), prose('answer')];
    expect(activityAnchorIndex(blocks, entry({ anchorBlockId: 'answer', toolCallId: 'reused' }))).toBe(3);
    expect(activityAnchorIndex(blocks, entry({ anchorBlockId: 'trimmed', toolCallId: 'reused' }))).toBe(0);
    expect(activityAnchorIndex(blocks, entry({ anchorToolRoundNum: 999, toolCallId: 'reused' }))).toBe(0);
  });

  it('builds chronological windows from explicit occurrences, not duplicate call-ID first matches', () => {
    const blocks = [tool(1), tool(2), prose('answer')];
    const error = entry({ id: 'between' });
    const windows = activityAnchorWindows(timeline([
      entry({ id: 'first', anchorToolRoundNum: 1, toolCallId: 'reused', severity: 'info' }),
      error,
      entry({ id: 'second', anchorToolRoundNum: 2, toolCallId: 'reused', severity: 'info' }),
    ]), blocks);
    expect(activityAnchorIndex(blocks, error, windows.get(error.id))).toBe(1);
    const ambiguous = activityAnchorWindows(timeline([
      entry({ id: 'old', toolCallId: 'reused', severity: 'info' }), error,
    ]), blocks);
    expect(ambiguous.get(error.id)).toEqual({});
  });

  it('keeps stable order for multiple diagnostics at one explicit occurrence', () => {
    const original = entry({ anchorToolRoundNum: 1 });
    const blocks = [tool(1), { kind: 'activity-event', blockId: 'diagnostic', identitySource: 'contract',
      value: original, source: original } as ConversationBlockViewModel, prose('answer')];
    expect(activityAnchorIndex(blocks, entry({ id: 'next', anchorToolRoundNum: 1 }))).toBe(2);
  });

  it('handles replay window trimming without deriving occurrence from retained index', () => {
    expect(activityAnchorIndex([tool(917), prose('answer')], entry({ anchorToolRoundNum: 917 }))).toBe(1);
    expect(activityAnchorIndex([tool(917), prose('answer')], entry({
      anchorToolRoundNum: 916, toolCallId: 'reused' }))).toBe(0);
  });

  it('rejects conflicting scopes, duplicated explicit keys and malformed occurrence numbers', () => {
    const blocks = [tool(1), tool(1), prose('answer')];
    expect(activityAnchorIndex(blocks, entry({ anchorToolRoundNum: 1 }))).toBe(0);
    expect(activityAnchorIndex(blocks, entry({ anchorToolRoundNum: 0.5, toolCallId: 'reused' }))).toBe(0);
    expect(activityAnchorIndex(blocks, entry({ anchorBlockId: 'tool:task-a:1', taskId: 'other' }))).toBe(3);
  });

  it('does not merge adjacent unscoped resumes when model counters are equal', () => {
    const first = tool(1, '', '');
    const resumed = tool(1, '', '');
    const blocks = [first, resumed, prose('answer', '', '')];
    const diagnostic = entry({ llmRound: 0 });
    expect(activityAnchorIndex(blocks, diagnostic)).toBe(0);
    const continuous = [first, tool(2, '', ''), prose('answer', '', '')];
    expect(activityAnchorIndex(continuous, diagnostic)).toBe(3);
    // A proven fence narrows history to a single run and restores fallback.
    first.source = { ...first.source, blockId: 'old' };
    expect(activityAnchorIndex(blocks, diagnostic, {
      before: entry({ anchorBlockId: 'answer' }),
      after: entry({ anchorBlockId: 'old' }),
    })).toBe(2);
  });

  it('prefers explicit producer metadata and only decodes legacy activity IDs when absent', () => {
    expect(activityEntryAttemptId(entry({ id: 'activity:display:42:gateway-0', attemptId: 'child' }))).toBe('child');
    expect(activityEntryAttemptId(entry({ id: 'activity:display:42', taskId: 'child' }))).toBe('');
    expect(activityEntryAttemptId(entry({ id: 'activity:attempt:with:colons:42:gateway-0' }))).toBe('attempt:with:colons');
  });
});


describe('compaction causal boundaries', () => {
  const compaction = (extra: Partial<TurnActivityEntry> = {}) => entry({
    kind: 'status', reasonCode: 'context_compaction', phase: 'compacting',
    taskId: 'task-a', attemptId: 'attempt-a', llmRound: 0, ...extra,
  });

  it('keeps preflight before the first token for live, replay and snapshots', () => {
    const activity = compaction({ anchorPosition: 'producer-start', trigger: 'pre_turn' });
    for (const blocks of [[], [prose('first')], [prose('first'), prose('later')]]) {
      expect(activityAnchorIndex(blocks, activity)).toBe(0);
      expect(activityAnchorIndex(blocks, JSON.parse(JSON.stringify(activity)))).toBe(0);
    }
  });

  it('retains exact midloop boundary despite future output in the same round', () => {
    const activity = compaction({ anchorBlockId: 'before-summary', anchorPosition: 'after' });
    expect(activityAnchorIndex([prose('before-summary')], activity)).toBe(1);
    expect(activityAnchorIndex([prose('before-summary'), prose('future')], activity)).toBe(1);
    expect(activityAnchorIndex([prose('before-summary'), prose('future'), prose('retry')], activity)).toBe(1);
  });

  it('honors explicit before boundaries and preserves repeated diagnostics order', () => {
    const activity = compaction({ anchorBlockId: 'next-output', anchorPosition: 'before' });
    const blocks = [prose('prior'), prose('next-output')];
    expect(activityAnchorIndex(blocks, activity)).toBe(1);
    blocks.splice(1, 0, {kind: 'activity-event', blockId: activity.id,
      identitySource: 'contract', value: activity, source: activity} as ConversationBlockViewModel);
    expect(activityAnchorIndex(blocks, {...activity, id: 'retry-summary'})).toBe(2);
  });

  it('scopes reset round0 starts to the resumed producer', () => {
    const blocks = [prose('old'), prose('resumed', 'task-a', 'attempt-b'),
      prose('future', 'task-a', 'attempt-b')];
    expect(activityAnchorIndex(blocks, compaction({attemptId: 'attempt-b',
      anchorPosition: 'producer-start'}))).toBe(1);
    expect(activityAnchorIndex(blocks, compaction({attemptId: 'attempt-b',
      anchorPosition: 'after', anchorBlockId: 'resumed'}))).toBe(2);
  });

  it('leaves failed/sparse legacy compaction before compatible output without archive fetch', () => {
    const blocks = [prose('answer'), prose('later')];
    for (const extra of [{trigger: 'pre_turn'}, {status: 'failed' as const},
      {anchorBlockId: 'trimmed', anchorPosition: 'after' as const}]) {
      expect(activityAnchorIndex(blocks, compaction(extra))).toBe(0);
    }
  });
});


it('prefers witnessed resumed producer content to unknown legacy block stamps', () => {
  const blocks = [prose('legacy', '', ''), prose('resumed', 'task-a', 'attempt-b')];
  expect(activityAnchorIndex(blocks, entry({reasonCode: 'context_compaction',
    taskId: 'task-a', attemptId: 'attempt-b', anchorPosition: 'producer-start'}))).toBe(1);
});
