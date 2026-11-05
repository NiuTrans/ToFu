/** The running reply's live row reports its own projected activity. */
import { describe, expect, it } from 'vitest';
import type { AttemptRecord, TurnRecord } from '../../../api/conversation-sync.generated';
import { createTurnState, type LiveContentGrowth } from '../../domain/turn-store';
import { selectConversationViewModel } from '../conversation-view-model';

const STARTED = 1_793_693_000_000;
const thinking = (id: string, text = 'reasoning') => ({ type: 'thinking', blockId: `thinking:${id}`, text, llmRound: 0 });
const call = (id: string, status: string, toolArgs: Record<string, unknown> = {}, tStart = STARTED, name = 'run_command') => ({
  type: 'tool_use', blockId: `tool:${id}`, id, name, input: toolArgs, llmRound: 0,
  result: { status, content: '' },
  _round: { toolCallId: id, toolName: name, status, tStart, toolArgs },
});
const receipt = { blockId: 'file-changes', count: 1, state: 'applied', files: [{ path: 'lib/slides/recipe.py' }] };

function liveStatus(
  segments: unknown[], livePhase: unknown = null, extra: Record<string, unknown> = {},
  contentGrowth?: LiveContentGrowth,
) {
  const state = createTurnState('conv');
  state.turnsById.reply = {
    turnId: 'reply', conversationId: 'conv', laneId: 'main', ordinal: 1, actor: 'assistant', kind: 'reply',
    runId: '', status: 'running', currentAttemptId: 'attempt', projectionRevision: 2, settlement: {},
    projection: { segments, content: '', ...extra }, createdAt: 1, updatedAt: 2,
  } as unknown as TurnRecord;
  state.attemptsById.attempt = { attemptId: 'attempt', turnId: 'reply', status: 'running', taskId: 'task' } as AttemptRecord;
  state.laneOrder.main = ['reply'];
  state.livePhase = livePhase;
  if (contentGrowth) state.liveContentGrowth = contentGrowth;
  const turn = selectConversationViewModel(state).mainLane.turns[0]!;
  return { kinds: turn.blocks.map(block => block.kind),
    value: turn.blocks.find(block => block.kind === 'live-status')?.value };
}

describe('projected live activity', () => {
  it('names a running call and its intent with the backend start instead of the empty-phase placeholder', () => {
    const { value } = liveStatus([thinking('a'), call('c1', 'searching', {
      command: 'python3 -m pytest -q tests/slides', description: 'Run the slides tests',
    })], null, { fileChanges: receipt });
    expect(value).toMatchObject({
      phase: 'tool_exec', tools: ['run_command'], startedAt: STARTED,
      detailKey: 'stream.phase.runningToolIntent',
      detailArgs: { tool: 'run_command', intent: 'Run the slides tests' },
    });
  });

  it('falls back to the call target, and to the bare tool list without one', () => {
    expect(liveStatus([call('c1', 'running', { path: 'lib/slides/recipe.py' }, STARTED, 'read_files')]).value)
      .toMatchObject({ detailArgs: { tool: 'read_files', intent: 'lib/slides/recipe.py' } });
    expect(liveStatus([call('c1', 'running', { ids: ['agent-1'] }, STARTED, 'poll')]).value)
      .toMatchObject({ detailKey: 'stream.phase.runningTools', detailArgs: { tools: 'poll' } });
  });

  it('decorates a tool_exec frame with the still-active subset and the earliest start, keeping its raw detail', () => {
    const { value } = liveStatus([
      call('c1', 'done', {}, STARTED - 9_000, 'read_files'),
      call('c2', 'running', {}, STARTED + 2_000, 'run_command'),
      call('c3', 'executing', {}, STARTED + 1_000, 'web_search'),
    ], { phase: 'tool_exec', detail: 'Executing 3 tools: Reading files, run_command, Searching the web',
      tools: ['read_files', 'run_command', 'web_search'] });
    expect(value).toMatchObject({
      phase: 'tool_exec', tools: ['run_command', 'web_search'], startedAt: STARTED + 1_000,
      detail: 'Executing 3 tools: Reading files, run_command, Searching the web',
      detailKey: 'stream.phase.runningTools', detailArgs: { tools: 'run_command, web_search' },
    });
  });

  it('reports streaming reasoning when calls or receipts render between it and the row', () => {
    const afterCalls = liveStatus([thinking('a'), call('c1', 'done'), thinking('b')]);
    expect(afterCalls.kinds).toEqual(['thinking', 'tool', 'thinking', 'live-status']);
    expect(afterCalls.value).toMatchObject({ phase: 'thinking_active' });
    expect(afterCalls.value?.detailKey).toBeUndefined();
    const beforeReceipt = liveStatus([thinking('a')], null, { fileChanges: receipt });
    expect(beforeReceipt.kinds).toEqual(['thinking', 'file-changes', 'live-status']);
    expect(beforeReceipt.value).toMatchObject({ phase: 'thinking_active' });
  });

  it('keeps an adjacent streaming block as the only live surface', () => {
    expect(liveStatus([thinking('a')]).kinds).toEqual(['thinking']);
    expect(liveStatus([thinking('a'), { type: 'text', blockId: 'text:b', text: 'partial', llmRound: 1 }]).kinds)
      .toEqual(['thinking', 'text']);
  });

  it('never overrides an explicit phase, a background call, or the honest no-evidence fallback', () => {
    const waiting = { phase: 'waiting_model', detailKey: 'stream.phase.waitingForModel', detailArgs: { model: 'm' }, model: 'm' };
    expect(liveStatus([thinking('a'), call('c1', 'done'), thinking('b')], waiting).value)
      .toMatchObject({ phase: 'waiting_model', detailKey: 'stream.phase.waitingForModel' });
    const detached = call('c1', 'done');
    detached._round = { ...detached._round, results: [{ backgrounded: true }] } as typeof detached._round;
    expect(liveStatus([detached], null, { fileChanges: receipt }).value)
      .toMatchObject({ phase: 'waiting', detailKey: 'stream.phase.waitingWorkerStatus' });
    expect(liveStatus([]).value).toMatchObject({ phase: 'waiting', detailKey: 'stream.phase.waitingWorkerStatus' });
    expect(liveStatus([]).value?.startedAt).toBeUndefined();
  });
});

describe('frame evidence for a cleared phase', () => {
  // Wire shape: the round's live accumulators are thinking:terminal followed
  // by text:terminal, which the producer stamps terminal.
  const narration = { type: 'text', blockId: 'text:terminal', text: 'Using the guide.', terminal: true,
    deliverable: true, llmRound: 0 };
  // The live accumulator renders the projection's `content`.
  const answer = { content: narration.text };

  it('reads reasoning that streams above narration as thinking, not waiting', () => {
    const { kinds, value } = liveStatus([thinking('terminal'), narration], null, answer, 'reasoning');
    expect(kinds).toEqual(['thinking', 'text', 'live-status']);
    expect(value).toMatchObject({ phase: 'thinking_active' });
    expect(value?.detailKey).toBeUndefined();
  });

  it('lets a streaming answer tail stand alone and names it below other blocks', () => {
    expect(liveStatus([thinking('terminal'), narration], null, answer, 'answer').kinds).toEqual(['thinking', 'text']);
    const { kinds, value } = liveStatus([thinking('a'), narration, call('c1', 'done')], null, answer, 'answer');
    expect(kinds).toEqual(['thinking', 'text', 'tool', 'live-status']);
    expect(value).toMatchObject({ phase: 'responding' });
  });

  it('keeps active calls first and never revives settled-looking reasoning after a frame that grew nothing', () => {
    expect(liveStatus([thinking('a'), call('c1', 'running')], null, {}, 'reasoning').value)
      .toMatchObject({ phase: 'tool_exec', tools: ['run_command'] });
    const quiet = liveStatus([thinking('a')], null, {}, 'none');
    expect(quiet.kinds).toEqual(['thinking', 'live-status']);
    expect(quiet.value).toMatchObject({ phase: 'waiting', detailKey: 'stream.phase.waitingWorkerStatus' });
  });

  it('never overrides an explicit phase', () => {
    const waiting = { phase: 'waiting_model', detailKey: 'stream.phase.waitingForModel', detailArgs: { model: 'm' }, model: 'm' };
    expect(liveStatus([thinking('a'), narration], waiting, answer, 'reasoning').value)
      .toMatchObject({ phase: 'waiting_model', detailKey: 'stream.phase.waitingForModel' });
  });
});
