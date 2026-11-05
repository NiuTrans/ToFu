import { describe, it, expect, vi } from 'vitest';
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
    const [view] = swarmMessagesProjection([live], [{ swarm_key: 'reusable-key', swarm_id: 'run',
      messageReceipts: [receipt] }]);
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
  it('keeps authoritative sender mappings', () => {
    const participants = [{ id: 'child', taskId: 'child-task', label: 'Researcher' }];
    const result = swarmMessagesProjection([], [{ swarm_id: 'run', messageReceipts: [receipt], messageParticipants: participants }]);
    expect(result[0].messageReceipts).toEqual([receipt]);
    expect(result[0].messageParticipants).toEqual(participants);
  });

  it('indexes each incoming receipt once across many waves of the same run', () => {
    const size = 128;
    const turns = Array.from({ length: size }, (_, index) => turn({
      _swarmSnapshot: { swarm_id: 'run', messageReceipts: [{ ...receipt, messageId: 'm-' + index }] },
    }));
    const stringify = vi.spyOn(JSON, 'stringify');
    let keyBuilds: number;
    let result: ReturnType<typeof swarmMessagesProjection>;
    try {
      result = swarmMessagesProjection(turns);
      keyBuilds = stringify.mock.calls.filter(([value]) => Array.isArray(value)
        && value.length === 3 && value[0] === 'run').length;
    } finally {
      stringify.mockRestore();
    }
    expect(result![0].messageReceipts?.map(value => value.messageId)).toEqual(
      Array.from({ length: size }, (_, index) => 'm-' + index));
    expect(keyBuilds!).toBeLessThanOrEqual(size);
  });

  it('keeps first-seen order, confirmation and latest participant evidence across all sources', () => {
    const confirmed = { ...receipt, state: 'input_confirmed', inputInjectedAt: 2, inputConfirmedAt: 3 };
    const other = { ...receipt, messageId: 'other', state: 'unconfirmed' };
    const turns = [turn({ _swarmSnapshot: { swarm_id: 'run', messageReceipts: [confirmed],
      messageParticipants: [{ id: 'child', taskId: 'worker', label: 'Before' }] } }),
    turn({ _swarmSnapshot: { swarm_id: 'run', messageReceipts: [other], agents: [{ messageReceipts: [receipt] }] },
      _swarmMessageReceipts: [{ ...receipt, inputInjectedAt: 20, inputConfirmedAt: undefined, content: undefined }] })];
    const swarms = [{ swarm_id: 'run', messageReceipts: [{ ...other, state: 'submitted' },
      { ...receipt, swarmRunId: 'foreign' }, { swarmRunId: 'run', messageId: 'invalid' }],
    messageParticipants: [{ id: 'child', taskId: 'worker', label: 'After' },
      { id: 'child', taskId: 'second-worker', label: 'Distinct worker' }] }];
    const before = JSON.stringify([turns, swarms]);
    const [view] = swarmMessagesProjection(turns, swarms);
    expect(view.messageReceipts).toEqual([confirmed, other]);
    expect(view.messageParticipants).toEqual(swarms[0].messageParticipants);
    expect(JSON.stringify([turns, swarms])).toBe(before);
  });
});
