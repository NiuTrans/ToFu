import { afterEach, expect, it, vi } from 'vitest';
import { swarmPresentationOverlay } from './swarm-presentation-overlay';
import { applyMessageReceipts, projectMessageReceipts } from '../presentation/agent-message-receipts';

afterEach(() => vi.restoreAllMocks());

function receiptPressureState(count = 32): any {
  const receipts = Array.from({ length: count }, (_, index) => ({
    swarmRunId: 'run', agentId: 'agent', messageId: `message-${index}`,
    state: 'input_confirmed', inputConfirmedAt: index + 1,
  }));
  const turn = (id: string, rounds: any[]) => ({ turnId: id, actor: 'assistant',
    projectionRevision: 1, projection: { toolRounds: rounds }, updatedAt: 1 });
  return { conversationId: 'pressure', laneOrder: { main: ['launch', 'sender'] }, turnsById: {
    launch: turn('launch', [{ toolName: 'spawn_agents', _swarm: true, _swarmRunId: 'run',
      _swarmAgents: [{ id: 'agent', status: 'done', messageReceipts: receipts }] },
    ...Array.from({ length: 100 }, (_, i) => ({ toolName: 'run_command',
      toolContent: `large unrelated output ${i}: ${'x'.repeat(8192)}` }))]),
    sender: turn('sender', receipts.map(receipt => ({ toolName: 'send_agent_message',
      toolArgs: { content: `Body ${receipt.messageId}` }, toolContent: JSON.stringify({
        swarm_run_id: 'run', agent_id: 'agent', message_id: receipt.messageId,
      }) }))),
  } };
}

it.each([1, 32])('bounds transcript parsing/copying independently of %i receipts', count => {
  const state = receiptPressureState(count), before = structuredClone(state);
  const clone = vi.spyOn(globalThis, 'structuredClone');
  const parse = JSON.parse;
  const outputReads = new Map<string, number>();
  vi.spyOn(JSON, 'parse').mockImplementation((value, reviver) => {
    if (String(value).startsWith('large unrelated output')) {
      outputReads.set(value, (outputReads.get(value) || 0) + 1);
    }
    return parse(value, reviver);
  });
  const result = swarmPresentationOverlay.compose('pressure', state, state, () => null);
  expect(clone.mock.calls.length).toBeLessThanOrEqual(4);
  expect(Math.max(...outputReads.values())).toBeLessThanOrEqual(3);
  expect(state).toEqual(before);
  const messages = result.turnsById.launch!.projection.toolRounds![0]._swarmMessageReceipts as any[];
  expect(messages).toHaveLength(count);
  expect(messages.every(message => message.state === 'input_confirmed'
    && message.content === `Body ${message.messageId}` && message.runEnded)).toBe(true);
  expect(result.turnsById.sender!.projection.toolRounds!.every((round: any) =>
    round._agentMessageReceipt.state === 'input_confirmed')).toBe(true);
});

it('applies mirrored receipts once instead of replaying their copies as new work', () => {
  const state = receiptPressureState(32);
  const projection = state.turnsById.sender.projection;
  for (const round of projection.toolRounds) round.toolArgs = JSON.stringify(round.toolArgs);
  const receipts = state.turnsById.launch.projection.toolRounds[0]._swarmAgents[0].messageReceipts;
  const parse = JSON.parse;
  let argumentReads = 0;
  vi.spyOn(JSON, 'parse').mockImplementation((value, reviver) => {
    if (String(value).startsWith('{"content":')) argumentReads++;
    return parse(value, reviver);
  });
  applyMessageReceipts(projection, Array.from({ length: 32 }, () => receipts).flat());
  expect(argumentReads).toBe(32);
  expect(projection.toolRounds.every((round: any) =>
    round._agentMessageReceipt.state === 'input_confirmed')).toBe(true);
});

it('preserves receipt order, exact run identity and monotone facts in a batch', () => {
  const state = receiptPressureState(2);
  const projection = state.turnsById.launch.projection;
  projectMessageReceipts(projection);
  const receipt = { swarmRunId: 'run', agentId: 'agent', messageId: 'message-0' };
  applyMessageReceipts(projection, [
    { ...receipt, state: 'submitted', inputConfirmedAt: 500 },
    { ...receipt, swarmRunId: 'other', state: 'unconfirmed' },
    { ...receipt, messageId: '', state: 'input_confirmed' },
  ]);
  expect(projection.toolRounds[0]._swarmMessageReceipts.map((r: any) => r.messageId))
    .toEqual(['message-0', 'message-1']);
  expect(projection.toolRounds[0]._swarmMessageReceipts[0]).toMatchObject({
    state: 'input_confirmed', inputConfirmedAt: 1, runEnded: true,
  });
});
