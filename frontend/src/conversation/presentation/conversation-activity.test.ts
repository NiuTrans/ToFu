import { describe, expect, it } from 'vitest';
import type { TurnRecord } from '../../api/conversation-sync.generated';
import { presentConversationActivity } from './conversation-activity';
import { goalRunAffiliations } from './goal-run-affiliation';
import { createTurnState } from '../domain/turn-store';
import { selectAutopilotRunNotices } from './view-model/live-turns';

function turn(id: string, actor: TurnRecord['actor'], status: TurnRecord['status'], runId?: string): TurnRecord {
  return { turnId: id, presentationId: id, conversationId: 'c', runId: runId || '',
    createdAt: 1, updatedAt: 2, laneId: 'main', ordinal: 1, actor, status, kind: 'flow_node', projectionRevision: 1,
    projection: runId ? { _autopilotRunId: runId } : {}, settlement: {} };
}
const conclusions = { run: { status: 'concluded', reason: 'budget_exhausted' } };

describe('shared conversation activity', () => {
  it.each(['assistant', 'planner'] as const)('uses durable %s verdict before legacy finish reason', actor => {
    const completed = turn('a', actor, 'completed');
    Object.assign(completed.projection, { finishReason: 'error' });
    expect(presentConversationActivity({ turns: [completed], streaming: false })).toMatchObject({ errored: false, incomplete: false });
    const failed = { ...completed, status: 'failed' as const };
    expect(presentConversationActivity({ turns: [failed], streaming: false })).toMatchObject({ errored: true });
  });
  it('uses one live phase for normal and Goal; stale human waits do not survive settlement', () => {
    const assistant = turn('a', 'assistant', 'running');
    assistant.projection.toolRounds = [{ toolName: 'ask_human', status: 'awaiting_human' }];
    const common = { streaming: true, livePhase: { phase: 'retrying', detailKey: 'stream.phase.retryRateLimited' }, syncHealth: 'offline' };
    const chat = presentConversationActivity({ ...common, turns: [assistant] });
    const goal = presentConversationActivity({ ...common, turns: [turn('vu', 'virtual_user', 'completed', 'run'), assistant], autopilotSummaries: conclusions });
    expect(goal).toEqual(chat);
    expect(goal).toMatchObject({ awaitingHuman: true, rateLimited: true, unconfirmed: true });
    expect(presentConversationActivity({ ...common, turns: [assistant], streaming: false })).toMatchObject({ awaitingHuman: false, rateLimited: false, unconfirmed: false });
  });
  it('only softens interrupted phases in their own concluded run, never hard failures or human turns', () => {
    const vu = turn('vu', 'virtual_user', 'completed', 'run');
    const assistant = turn('a', 'assistant', 'interrupted');
    const input = { streaming: false, autopilotSummaries: conclusions };
    expect(presentConversationActivity({ ...input, turns: [assistant] }).incomplete).toBe(true);
    expect(presentConversationActivity({ ...input, turns: [vu, assistant] }).incomplete).toBe(false);
    expect(presentConversationActivity({ ...input, turns: [vu, turn('h', 'human', 'completed'), assistant] }).incomplete).toBe(true);
    expect(presentConversationActivity({ ...input, turns: [vu, turn('other', 'virtual_user', 'completed', 'other'), assistant] }).incomplete).toBe(true);
    expect(presentConversationActivity({ ...input, turns: [vu, { ...assistant, status: 'failed' }] }).errored).toBe(true);
  });
  it('joins notices to the same run tail used by sidebar and fences real human input', () => {
    const turns = [turn('vu', 'virtual_user', 'completed', 'run'), turn('a', 'assistant', 'interrupted'), turn('h', 'human', 'completed'), turn('b', 'assistant', 'interrupted')];
    const state = createTurnState('c');
    state.laneOrder.main = turns.map(value => value.turnId);
    state.turnsById = Object.fromEntries(turns.map(value => [value.turnId, value]));
    expect([...goalRunAffiliations(turns)]).toEqual([['vu', 'run'], ['a', 'run']]);
    expect([...selectAutopilotRunNotices(state, { autopilotSummaries: conclusions }).keys()]).toEqual(['a']);
  });
  it('retains metadata fallback and waiting precedence before hydration', () => {
    expect(presentConversationActivity({ turns: [], streaming: false, catalog: { lastMsgRole: 'assistant', lastFinishReason: 'length' } }).incomplete).toBe(true);
    const assistant = turn('a', 'assistant', 'interrupted');
    assistant.projection.waitingOn = { kind: 'dependency' };
    expect(presentConversationActivity({ turns: [assistant], streaming: false })).toMatchObject({ waiting: true, incomplete: false });
  });
});
