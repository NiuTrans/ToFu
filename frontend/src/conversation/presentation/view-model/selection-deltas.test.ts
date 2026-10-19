/** Differential oracle for the lifecycle selector: canonical deltas must match
 * fresh composition, including coalescing, cross-Turn joins and recovery. */
import { describe, expect, it } from 'vitest';
import { createTurnStore } from '../../domain/turn-store';
import type { TurnRecord } from '../../../api/conversation-sync.generated';
import { createConversationViewModelSelector, selectConversationViewModel } from '../conversation-view-model';
import type { ConversationPresentationState, ConversationTurnViewModel } from './types';

function turn(id: string, ordinal: number, extra: Partial<TurnRecord> = {}): TurnRecord {
  return { turnId: id, presentationId: id, conversationId: 'delta-selection', laneId: 'main',
    ordinal, actor: 'assistant', kind: 'reply', status: 'running', runId: '',
    currentAttemptId: `${id}-attempt`, projectionRevision: 1,
    projection: { content: 'before', segments: [{ type: 'text', blockId: 'answer', text: 'before' }] },
    settlement: {}, createdAt: 1, updatedAt: 1, ...extra };
}

function fixture(turns = [turn('old', 1, { status: 'completed' }), turn('live', 2)]) {
  const store = createTurnStore('delta-selection');
  store.dispatch({ type: 'snapshot', snapshot: { turns,
    attempts: turns.map((item) => ({ attemptId: item.currentAttemptId!, turnId: item.turnId,
      taskId: 'task', lastSeq: 1, createdAt: 1, status: item.status })) } });
  const selector = createConversationViewModelSelector();
  store.subscribe((state, update) => selector.observe(state, update));
  const initial = selector.select(store.getState());
  const assertFresh = (presentation: ConversationPresentationState = {}) => {
    const actual = selector.select(store.getState(), {}, presentation);
    expect(actual).toEqual(selectConversationViewModel(store.getState(), {}, presentation));
    return actual;
  };
  const stream = (id = 'live', projection: TurnRecord['projection'] = {}) => {
    const before = store.getState().turnsById[id]!;
    const seq = Number(store.getState().attemptsById[before.currentAttemptId!]?.lastSeq ?? 0) + 1;
    return store.dispatch({ type: 'event', event: { conversationId: 'delta-selection', turnId: id, attemptId: before.currentAttemptId!,
      seq, projectionRevision: before.projectionRevision + 1, type: 'projection_updated',
      payload: { projection: { ...before.projection, content: `stream ${seq}`,
        segments: [{ type: 'text', blockId: 'answer', text: `stream ${seq}` }], ...projection } } } });
  };
  return { store, selector, initial, assertFresh, stream };
}

function allTurns(lane: { turns: ReadonlyArray<ConversationTurnViewModel> }): ConversationTurnViewModel[] {
  return lane.turns.flatMap((item) => [item, ...item.branches.flatMap(allTurns)]);
}

describe('canonical delta selection', () => {
  it('coalesces changes from multiple Turns while preserving untouched identity and old views', () => {
    const f = fixture([turn('old', 1, { status: 'completed' }), turn('live', 2), turn('other', 3)]);
    for (let i = 0; i < 40; i++) f.stream(i % 2 ? 'live' : 'other');
    const actual = f.assertFresh();
    expect(actual.mainLane.turns[0]).toBe(f.initial.mainLane.turns[0]);
    expect(actual.mainLane.turns[1]).not.toBe(f.initial.mainLane.turns[1]);
    expect(f.initial.mainLane.turns[1].source.projection.content).toBe('before');
    expect(actual.mainLane.turns[1].source.projection.content).toBe('stream 21');
  });

  it('does not enumerate transcript indexes or unchanged lane IDs for a local stream', () => {
    const f = fixture(Array.from({ length: 4096 }, (_, i) => turn(`t${i}`, i,
      { status: i === 4095 ? 'running' : 'completed' })));
    let enumerations = 0;
    const selector = createConversationViewModelSelector();
    selector.select(f.store.getState());
    let observed = f.store.getState();
    f.store.subscribe((state, update) => {
      observed = { ...state,
        turnsById: new Proxy(state.turnsById, {
          ownKeys(target) { enumerations++; return Reflect.ownKeys(target); },
        }),
        attemptsById: new Proxy(state.attemptsById, {
          ownKeys(target) { enumerations++; return Reflect.ownKeys(target); },
        }),
      };
      selector.observe(observed, update);
    });
    f.stream('t4095');
    const actual = selector.select(observed);
    expect(enumerations).toBe(0);
    expect(actual).toEqual(selectConversationViewModel(observed));
  });

  it('updates branch ancestors and reuses unrelated lanes', () => {
    const f = fixture([
      turn('root', 1, { projection: { _branchLanes: [{ laneId: 'branch', title: 'Branch' },
        { laneId: 'sibling', title: 'Sibling' }] } }),
      turn('leaf', 2, { laneId: 'branch', parentTurnId: 'root' }),
      turn('sibling', 3, { laneId: 'sibling', parentTurnId: 'root' }),
    ]);
    f.stream('leaf');
    const next = f.assertFresh();
    expect(next.mainLane.turns[0]).not.toBe(f.initial.mainLane.turns[0]);
    expect(next.mainLane.turns[0].branches[1]).toBe(f.initial.mainLane.turns[0].branches[1]);
    expect(allTurns(next.mainLane).find(t => t.turnId === 'leaf')!.source.projection.content).toBe('stream 2');
  });

  it('refreshes pending actions, transport, mutable presentation maps and run summaries', () => {
    const f = fixture();
    f.store.dispatch({ type: 'command_pending', turnId: 'old', operation: 'delete' });
    expect(f.assertFresh().mainLane.turns[0].commandPending).toBe('delete');
    f.store.dispatch({ type: 'command_failed', turnId: 'old' }); f.assertFresh();
    f.store.dispatch({ type: 'transport', status: 'recovering' }); f.assertFresh();
    const modes = new Map<string, 'original' | 'translated'>();
    const presentation: ConversationPresentationState = { translationModeByTurn: modes,
      autopilotSummaries: { run: { status: 'running' } }, syncHealth: { state: 'healthy' } };
    f.assertFresh(presentation);
    modes.set('live', 'original'); presentation.syncHealth!.state = 'offline';
    f.assertFresh(presentation);
    f.stream('live', { _autopilotRunId: 'run' }); f.assertFresh(presentation);
    /* Run summaries are replaced wholesale by the owner
     * (runtime/sections/ui/streaming_render.js assigns the entry object), so
     * the presentation identity changes — mirror that instead of mutating the
     * record in place. */
    presentation.autopilotSummaries = {
      run: { status: 'concluded', reason: 'budget_exhausted' },
    };
    expect(f.assertFresh(presentation).mainLane.turns[1].blocks.some(b => b.kind === 'autopilot-run-notice')).toBe(true);
  });

  it('recomputes Goal root progress when worker phase dependencies change', () => {
    const f = fixture([turn('root', 1, { runId: 'run' }), turn('live', 2,
      { runId: 'run', projection: { orchestration: { iteration: 1 } } })]);
    f.stream('live', { orchestration: { iteration: 2 } });
    const next = f.assertFresh();
    expect(next.mainLane.turns[0]).not.toBe(f.initial.mainLane.turns[0]);
    const block = next.mainLane.turns[0].blocks.find(b => b.kind === 'live-status');
    expect(block?.kind === 'live-status' && block.value.goalProgress?.iteration).toBe('2');
  });

  it('refreshes earlier background handoffs when completion arrives on another Turn', () => {
    const f = fixture([turn('old', 1, { status: 'completed', projection: { toolRounds: [{
      toolName: 'exec_command', toolExecId: 'exec', results: [{ backgrounded: true, backgroundCommandId: 'cmd' }],
    }] } }), turn('live', 2)]);
    f.stream('live', { backgroundCommand: { commandId: 'cmd', status: 'completed', exitCode: 0, command: 'cmd', resultPreview: 'done', resultTruncated: false } });
    const next = f.assertFresh();
    expect(next.mainLane.turns[0].source.projection.toolRounds?.[0].results?.[0]).toMatchObject({ backgroundCompleted: true });
  });

  it('falls back after omitted transitions, unknown presentation overlays and authority reset', () => {
    const f = fixture();
    f.stream();
    const missed = { ...f.store.getState(), livePhase: { phase: 'tool_exec' } };
    expect(f.selector.select(missed)).toEqual(selectConversationViewModel(missed));
    f.store.dispatch({ type: 'snapshot', snapshot: { authoritativeFull: true, conversationRevision: 2,
      turns: [turn('restored', 4, { status: 'completed' })], attempts: [] } });
    expect(f.assertFresh().mainLane.turns.map(t => t.turnId)).toEqual(['restored']);
    f.selector.dispose(); f.assertFresh();
  });
});
