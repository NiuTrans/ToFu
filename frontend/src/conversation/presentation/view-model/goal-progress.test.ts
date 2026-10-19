/** Goal context joins current durable run/execution evidence, never lifecycle authority. */
import { describe, expect, it } from 'vitest';
import type { TurnRecord, AttemptRecord } from '../../../api/conversation-sync.generated';
import { createTurnState } from '../../domain/turn-store';
import { selectConversationViewModel } from '../conversation-view-model';

function fixture() {
  const state = createTurnState('goal');
  for (const [id, ordinal, status, iteration] of [
    ['root', 150, 'running', 1], ['vu', 155, 'completed', 4], ['worker', 156, 'running', 4],
  ] as const) {
    state.turnsById[id] = { turnId: id, laneId: 'main', ordinal, status,
      actor: id === 'vu' ? 'virtual_user' : 'assistant', kind: 'flow_node', runId: 'run',
      currentAttemptId: id, projectionRevision: 1,
      projection: { orchestration: { iteration } }, settlement: {},
    } as TurnRecord;
    state.attemptsById[id] = { attemptId: id, turnId: id, taskId: 'physical', status } as AttemptRecord;
  }
  state.laneOrder.main = ['root', 'vu', 'worker'];
  state.livePhase = { phase: 'waiting_model', detailKey: 'stream.phase.waitingModel' };
  return state;
}
function statuses(state: ReturnType<typeof fixture>) {
  return selectConversationViewModel(state).mainLane.turns.map(turn =>
    turn.blocks.find(block => block.kind === 'live-status'));
}

describe('Goal progress context', () => {
  it('keeps phase on latest worker and provides uncached older-root context', () => {
    const state = fixture();
    const [root, vu, worker] = statuses(state);
    expect(root?.value).toMatchObject({ phase: 'waiting', goalProgress: { iteration: '4', role: 'worker', rootSummary: true } });
    expect(vu).toBeUndefined();

    expect(root?.value.goalProgress?.currentPhase).toMatchObject({ phase: 'waiting_model' });
    expect(root?.value.goalProgress?.workers).toEqual([]);
    expect(worker?.value).toMatchObject({ phase: 'waiting_model', goalProgress: { rootSummary: false } });
    state.turnsById.worker = { ...state.turnsById.worker!, projection: { orchestration: { iteration: 5 } } };
    expect(statuses(state)[0]?.value.goalProgress?.iteration).toBe('5');
  });
  it('rejects foreign runs and physical executions; normal single-turn waiting remains honest', () => {
    const state = fixture();
    state.turnsById.root = { ...state.turnsById.root!, runId: 'foreign' };
    expect(statuses(state)[0]).toBeUndefined();
    state.turnsById.root = { ...state.turnsById.root!, runId: 'run' };
    state.attemptsById.root = { ...state.attemptsById.root!, taskId: 'different' };
    expect(statuses(state)[0]).toBeUndefined();
    state.laneOrder.main = ['root'];
    state.turnsById.root = { ...state.turnsById.root!, projection: {} };
    state.livePhase = null;
    expect(statuses(state)[0]?.value).toMatchObject({ phase: 'waiting', detailKey: 'stream.phase.waitingWorkerStatus' });
    expect(statuses(state)[0]?.value.goalProgress).toBeUndefined();
  });
  it('reads snapshot telemetry with nested producer identity and never revives settled agents', () => {
    const state = fixture();
    const round = { toolName: 'spawn_agents', taskId: 'nested-producer', _swarmSnapshot: {
      agents: [{ id: 'audit', name: '<Audit>', status: 'running', toolCalls: [
        { toolName: 'read_file', argsBrief: 'actual.py', error: 'permission denied' },
      ] }, { id: 'old', status: 'completed' }],
    } };
    state.turnsById.worker = { ...state.turnsById.worker!, projection: {
      orchestration: { iteration: 4 }, toolRounds: [round],
    } } as TurnRecord;
    expect(statuses(state)[0]?.value.goalProgress?.workers).toEqual([
      { text: '<Audit>', phase: 'running', waitReason: '', latestTool: 'read_file · actual.py', toolError: 'permission denied' },
    ]);
    round._swarmSnapshot = { ...round._swarmSnapshot, settled: true } as typeof round._swarmSnapshot;
    expect(statuses(state)[0]?.value.goalProgress?.workers).toEqual([]);
  });
  it('preserves thinking and capacity evidence without treating an old tool as current execution', () => {
    const state = fixture();
    state.turnsById.worker = { ...state.turnsById.worker!, projection: {
      orchestration: { iteration: 4 }, toolRounds: [{ toolName: 'spawn_agents', _swarmAgents: [
        { id: 'thinking', status: 'running', phase: 'thinking' },
        { id: 'queued', status: 'pending', waitReason: 'capacity', toolCalls: [{ toolName: 'old_read' }] },
      ] }],
    } } as TurnRecord;
    expect(statuses(state)[0]?.value.goalProgress?.workers).toEqual([
      { text: 'thinking', phase: 'thinking', waitReason: '' },
      { text: 'queued', phase: 'pending', waitReason: 'capacity', latestTool: 'old_read' },
    ]);
  });
  it('gives storage warnings priority and removes all live context on settlement', () => {
    const state = fixture();
    state.pushWithheld = true;
    expect(statuses(state)[0]?.value.phase).toBe('storage_wedged');
    expect(statuses(state)[0]?.value.goalProgress).toBeUndefined();
    state.pushWithheld = false;
    for (const id of ['root', 'worker']) state.turnsById[id] = { ...state.turnsById[id]!, status: 'completed' };
    expect(statuses(state).every(status => !status)).toBe(true);
  });
});
