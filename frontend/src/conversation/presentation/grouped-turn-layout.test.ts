/** Grouped reply zones: one reasoning summary, one tool panel, then prose. */
import { describe, expect, it } from 'vitest';
import { createTurnState } from '../domain/turn-store';
import { selectConversationViewModel } from './conversation-view-model';
import { entryRegions, groupedTurnEntries, groupedTurnLayout, TOOL_PANEL_BLOCK_ID } from './grouped-turn-layout';
import type { ConversationTurnViewModel } from './view-model/types';

const scope = { attemptId: 'attempt-1', taskId: 'task-1' };

function tool(id: string, llmRound: number, status = 'done') {
  return {
    type: 'tool_use', blockId: id, id, name: 'read_files', llmRound, ...scope,
    input: { path: `${id}.ts` }, result: { content: id, status },
    _round: { toolName: 'read_files', toolCallId: id, status, llmRound, ...scope, results: [{ exitCode: 0 }] },
  };
}

function reply(
  status: string, extra: Record<string, unknown> = {}, settlement: Record<string, unknown> = {},
): ConversationTurnViewModel {
  const state = createTurnState('conv');
  state.turnsById.reply = {
    turnId: 'reply', presentationId: 'reply', runId: '', conversationId: 'conv', laneId: 'main', ordinal: 1,
    actor: 'assistant', kind: 'reply', status, currentAttemptId: null,
    projectionRevision: 1, createdAt: 1, updatedAt: 2, settlement,
    projection: {
      content: 'Final answer.',
      segments: [
        { type: 'thinking', blockId: 'think-0', llmRound: 0, ...scope, text: 'Plan the reads.' },
        { type: 'text', blockId: 'narrate-0', llmRound: 0, ...scope, text: 'Reading the engine first.' },
        tool('tool-a', 0),
        { type: 'thinking', blockId: 'think-1', llmRound: 1, ...scope, text: 'Compare both files.' },
        tool('tool-b', 1),
        tool('tool-c', 1, 'error'),
        { type: 'text', blockId: 'answer', llmRound: 2, ...scope, text: 'Final answer.', terminal: true, deliverable: true },
      ],
      activityTimeline: { entries: [{
        id: 'retry-1', spanId: 'retry-1', seq: 1, occurredAt: 1, kind: 'model', status: 'failed',
        severity: 'error', count: 1, summary: 'Model request failed', ...scope, anchorBlockId: 'tool-a',
      }] },
      ...extra,
    },
  } as never;
  state.laneOrder.main = ['reply'];
  return selectConversationViewModel(state).mainLane.turns[0];
}

const ids = (entries: ReadonlyArray<{ blockId: string }>) => entries.map(entry => entry.blockId);

describe('grouped turn layout', () => {
  it('separates reasoning, the tool panel and prose; a closed panel mounts no calls', () => {
    const layout = groupedTurnLayout(reply('completed'))!;
    expect(layout.thinking?.memberBlockIds).toEqual(['think-0', 'think-1']);
    expect(layout.thinking?.markdown).toBe('Plan the reads.\n\nCompare both files.');
    expect(ids(groupedTurnEntries(layout, false)))
      .toEqual(['think-0', TOOL_PANEL_BLOCK_ID, 'narrate-0', 'answer']);
  });

  it('keeps chronology inside the panel so a failure sits below the call it followed', () => {
    const layout = groupedTurnLayout(reply('completed'))!;
    expect(ids(groupedTurnEntries(layout, true))).toEqual([
      'think-0', TOOL_PANEL_BLOCK_ID, 'tool-a', 'activity:retry-1', 'tool-b', 'tool-c', 'narrate-0', 'answer',
    ]);
    expect(layout.panel).toEqual({
      toolCount: 3, roundCount: 2, errorCount: 2, active: false, defaultOpen: false,
    });
  });

  it('keeps the Turn failure after the prose, outside the panel and its error count', () => {
    const failed = reply('failed', {}, { error: { kind: 'provider_error', message: 'Upstream failed' } });
    const layout = groupedTurnLayout(failed)!;
    expect(ids(layout.trail)).toEqual(['activity:terminal-error']);
    expect(layout.panel!.errorCount).toBe(2);
    expect(ids(groupedTurnEntries(layout, false)).slice(-2)).toEqual(['answer', 'activity:terminal-error']);
  });

  it('folds by default only a completed reply with its own answer and nothing pending', () => {
    expect(groupedTurnLayout(reply('running'))!.panel!.defaultOpen).toBe(true);
    expect(groupedTurnLayout(reply('failed'))!.panel!.defaultOpen).toBe(true);
    expect(groupedTurnLayout(reply('interrupted'))!.panel!.defaultOpen).toBe(true);
    expect(groupedTurnLayout(reply('completed'))!.panel!.defaultOpen).toBe(false);
    // Without an explicit answer the calls are the reply's content.
    const toolsOnly = reply('completed', { content: '', segments: [tool('tool-a', 0), tool('tool-b', 1)] });
    expect(groupedTurnLayout(toolsOnly)!.panel!.defaultOpen).toBe(true);
    // A pending proposal renders outside the panel, so it does not hold it open.
    const proposal = groupedTurnLayout(reply('completed', { proposedPlan: {
      blockId: 'proposed-plan', planId: 'plan-1', revision: 1, format: 'markdown', text: '1. Ship it.',
    } }))!;
    expect(proposal.panel!.defaultOpen).toBe(false);
    expect(ids(proposal.content)).toContain('proposed-plan');
  });

  it('folds interim narration by the same settlement, independent of the calls', () => {
    expect(groupedTurnLayout(reply('running'))!.narrationDefaultOpen).toBe(true);
    expect(groupedTurnLayout(reply('failed'))!.narrationDefaultOpen).toBe(true);
    expect(groupedTurnLayout(reply('completed'))!.narrationDefaultOpen).toBe(false);
    // Without an explicit answer the narration is the reply's content.
    const unanswered = reply('completed', { content: '', segments: [
      { type: 'text', blockId: 'narrate-0', llmRound: 0, ...scope, text: 'Reading.' },
      tool('tool-a', 0),
      { type: 'text', blockId: 'narrate-1', llmRound: 1, ...scope, text: 'Still reading.' },
    ] });
    expect(groupedTurnLayout(unanswered)!.narrationDefaultOpen).toBe(true);
    // A call still running keeps the panel open; the settled notes still fold.
    const running = groupedTurnLayout(reply('completed', { segments: [
      { type: 'text', blockId: 'narrate-0', llmRound: 0, ...scope, text: 'Starting the watcher.' },
      tool('tool-a', 0, 'running'),
      { type: 'text', blockId: 'answer', llmRound: 1, ...scope, text: 'Final answer.', terminal: true, deliverable: true },
    ] }))!;
    expect(running.panel!.defaultOpen).toBe(true);
    expect(running.narrationDefaultOpen).toBe(false);
  });

  it('windows calls and prose-with-receipts as separate regions and pins only the head', () => {
    const failed = reply('failed', {}, { error: { kind: 'provider_error', message: 'Upstream failed' } });
    const layout = groupedTurnLayout(failed)!;
    const open = groupedTurnEntries(layout, true);
    // think-0 and the header are pinned; the terminal failure closes the prose.
    expect(entryRegions(open)).toEqual([
      { kind: 'process', start: 2, end: 6 },
      { kind: 'prose', start: 6, end: 9 },
    ]);
    expect(ids(open.slice(2, 6))).toEqual(['tool-a', 'activity:retry-1', 'tool-b', 'tool-c']);
    expect(ids(open.slice(6))).toEqual(['narrate-0', 'answer', 'activity:terminal-error']);
    expect(entryRegions(groupedTurnEntries(layout, false))).toEqual([{ kind: 'prose', start: 2, end: 5 }]);
    expect(entryRegions(failed.blocks)).toEqual([{ kind: 'turn', start: 0, end: failed.blocks.length }]);
  });

  it('memoizes entries per immutable Turn and panel state', () => {
    const turn = reply('completed');
    const layout = groupedTurnLayout(turn)!;
    expect(groupedTurnLayout(turn)).toBe(layout);
    expect(groupedTurnEntries(layout, true)).toBe(groupedTurnEntries(layout, true));
    expect(groupedTurnEntries(layout, false)).not.toBe(groupedTurnEntries(layout, true));
  });

  it('renders process rows inline without a panel when a reply made no calls', () => {
    const state = createTurnState('conv');
    state.turnsById.reply = {
      turnId: 'reply', presentationId: 'reply', runId: '', conversationId: 'conv', laneId: 'main', ordinal: 1,
      actor: 'assistant', kind: 'reply', status: 'completed', currentAttemptId: null,
      projectionRevision: 1, createdAt: 1, updatedAt: 2, settlement: {},
      projection: {
        content: 'Answer.',
        segments: [{ type: 'text', blockId: 'answer', llmRound: 0, ...scope, text: 'Answer.', terminal: true, deliverable: true }],
        activityTimeline: { entries: [{
          id: 'wait', spanId: 'wait', seq: 1, occurredAt: 1, kind: 'status', status: 'waiting',
          severity: 'warning', count: 1, summary: 'Rate limited', ...scope, phase: 'retrying',
        }] },
      },
    } as never;
    state.laneOrder.main = ['reply'];
    const layout = groupedTurnLayout(selectConversationViewModel(state).mainLane.turns[0])!;
    expect(layout.panel).toBeNull();
    expect(ids(groupedTurnEntries(layout, false))).toEqual(['activity:wait', 'answer']);
  });

  it('leaves input Turns in authored order', () => {
    const state = createTurnState('conv');
    state.turnsById.input = {
      turnId: 'input', presentationId: 'input', runId: '', conversationId: 'conv', laneId: 'main', ordinal: 0,
      actor: 'human', kind: 'input', status: 'completed', currentAttemptId: null,
      projectionRevision: 1, createdAt: 1, updatedAt: 1, settlement: {}, projection: { content: 'Hi' },
    } as never;
    state.laneOrder.main = ['input'];
    expect(groupedTurnLayout(selectConversationViewModel(state).mainLane.turns[0])).toBeNull();
  });
});
