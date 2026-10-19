import { describe, it, expect } from 'vitest';
import { swarmMessagesProjection } from './swarm-messages-projection';
import { applyMessageReceipt } from './agent-message-receipts';
const receipt = { swarmRunId: 'run', agentId: 'run', messageId: 'm', senderTaskId: 'child-task', state: 'submitted', content: 'To coordinator' };
const turn = (round: any) => ({ projection: { toolRounds: [round] } });
describe('run communication projection', () => {

  it('retains live child communication when the run snapshot carries only coordinator receipts', () => {
    const child = { ...receipt, agentId: 'child', messageId: 'child-message', content: 'Child message' };
    const result = swarmMessagesProjection([turn({ _swarm: true, _swarmRunId: 'run',
      _swarmSnapshot: { swarm_id: 'run', messageReceipts: [receipt] },
      _swarmAgents: [{ id: 'child', messageReceipts: [child] }] })]);
    expect(result[0].messageReceipts).toEqual([receipt, child]);
  });

  it('recovers complete history from the activity API after push-only messages', () => {
    const live = turn({ _swarm: true, _swarmRunId: 'run' });
    applyMessageReceipt(live.projection, receipt);
    expect(swarmMessagesProjection([live])[0].messageReceiptsAvailable).not.toBe(true);
    const [view] = swarmMessagesProjection([live], [{ swarm_key: 'reusable-key', swarm_id: 'run',
      messageReceipts: [receipt], messageReceiptsAvailable: true }]);
    expect(view.messageReceiptsAvailable).toBe(true);
    expect(view.messageReceipts).toEqual([receipt]);
  });
  it('keeps coordinator receipts without a child recipient row and survives reload', () => {
    const round: any = { _swarm: true, _swarmRunId: 'run', _swarmAgents: [{ id: 'child', status: 'done' }] };
    const live = turn(round);
    expect(applyMessageReceipt(live.projection, receipt)).toBe(true);
    const view = swarmMessagesProjection([live])[0];
    expect(view.messageReceipts).toEqual([receipt]);
    const restored = turn({ _swarmSnapshot: { swarm_id: 'run', agents: [], messageReceipts: [receipt] } });
    expect(swarmMessagesProjection([restored])).toEqual([view]);
  });
  it('deduplicates waves, scopes exact runs and never downgrades confirmation', () => {
    const first = turn({ _swarmSnapshot: { swarm_id: 'run', messageReceipts: [{ ...receipt, state: 'input_confirmed', inputConfirmedAt: 3 }] } });
    const second = turn({ _swarmSnapshot: { swarm_id: 'run', messageReceipts: [receipt] } });
    const other = turn({ _swarmSnapshot: { swarm_id: 'other', messageReceipts: [receipt] } });
    const result = swarmMessagesProjection([first, second, other]);
    expect(result).toHaveLength(2);
    expect(result[0].messageReceipts).toHaveLength(1);
    expect(result[0].messageReceipts?.[0].state).toBe('input_confirmed');
    expect(result[1].messageReceipts).toEqual([]);
  });
  it('keeps incomplete history and authoritative sender mappings', () => {
    const participants = [{ id: 'child', taskId: 'child-task', label: 'Researcher' }];
    const result = swarmMessagesProjection([], [{ swarm_id: 'run', messageReceipts: [receipt], messageReceiptsAvailable: false, messageParticipants: participants }]);
    expect(result[0].messageReceiptsAvailable).toBe(false);
    expect(result[0].messageParticipants).toEqual(participants);
  });
});
