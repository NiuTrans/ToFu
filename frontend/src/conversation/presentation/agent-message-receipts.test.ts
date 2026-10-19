import { describe, it, expect } from 'vitest';
import { applyMessageReceipt, projectMessageReceipts, projectAgentTimeline } from './agent-message-receipts';
import type { Translator } from '../../i18n';
const receipt = { swarmRunId: 'run', agentId: 'agent', messageId: 'message', state: 'submitted' };
function fixture() {
  const send: any = { toolName: 'send_agent_message', toolArgs: { content: '<img src=x>\n正文' }, toolContent: JSON.stringify({ swarm_run_id: 'run', agent_id: 'agent', message_id: 'message' }) };
  const agent: any = { id: 'agent', status: 'running', messageReceipts: [receipt] };
  return { send, agent, projection: { toolRounds: [send, { _swarmRunId: 'run', _swarmAgents: [agent] }] } };
}
const t = ((key: string) => key) as Translator;
describe('authoritative message receipt presentation', () => {
  it('updates only exact identities and preserves the requested body', () => {
    const { send, agent, projection } = fixture();
    expect(applyMessageReceipt(projection, { ...receipt, swarmRunId: 'other', state: 'input_confirmed' })).toBe(false);
    expect(send._agentMessageReceipt).toBeUndefined();
    applyMessageReceipt(projection, { ...receipt, inputInjectedAt: 10 });
    expect(send._agentMessageReceipt.state).toBe('submitted');
    expect(agent.messageReceipts[0].content).toBe('<img src=x>\n正文');
    expect(projectAgentTimeline(agent, t)[0].preview).toBe('<img src=x>\n正文');
    expect(projectAgentTimeline(agent, t)[0].argsBrief).toBe('tool.agentMessageInputInserted');
  });
  it('does not downgrade confirmed or closed receipts on stale replay', () => {
    const { send, projection } = fixture();
    applyMessageReceipt(projection, { ...receipt, state: 'input_confirmed' });
    applyMessageReceipt(projection, receipt);
    expect(send._agentMessageReceipt.state).toBe('input_confirmed');
  });
  it('projects snapshot receipts and ends unconfirmed input without claiming non-consumption', () => {
    const { send, agent, projection } = fixture();
    agent.status = 'done';
    projectMessageReceipts(projection);
    expect(send._agentMessageReceipt.messageId).toBe('message');
    expect(projectAgentTimeline(agent, t)[0].argsBrief).toBe('tool.agentMessageUnconfirmed');
  });
});

import { swarmPresentationOverlay } from '../application/swarm-presentation-overlay';
it('recovers sender text across Turns without changing durable records', () => {
  const { send, agent } = fixture();
  const turn = (turnId: string, rounds: any[]) => ({ turnId, actor: 'assistant', projection: { toolRounds: rounds } });
  const state: any = { laneOrder: { main: ['launch', 'sender'] }, turnsById: {
    launch: turn('launch', [{ _swarm: true, _swarmRunId: 'run', _swarmAgents: [agent] }]),
    sender: turn('sender', [send]),
  } };
  const selected = swarmPresentationOverlay.selectCandidates('conversation', state, () => null);
  expect((selected.find(t => t.turnId === 'launch')!.projection.toolRounds![0]._swarmAgents as any[])[0].messageReceipts[0].content).toBe('<img src=x>\n正文');
  const composed = swarmPresentationOverlay.compose('conversation', state, state, () => null);
  expect((composed.turnsById.launch!.projection.toolRounds![0]._swarmAgents as any[])[0].messageReceipts[0].content).toBe('<img src=x>\n正文');
  expect(agent.messageReceipts[0]).not.toHaveProperty('content');
  expect(send._agentMessageReceipt).toBeUndefined();
});

it('interleaves receipts by evidence without mutating tool calls or inventing order', () => {
  const calls = [{ callId: 'first', startedAt: 10 }, { callId: 'second', startedAt: 30 }];
  const agent = { _toolCalls: calls, messageReceipts: [
    { ...receipt, submittedAt: 20, content: 'body' },
    { ...receipt, submittedAt: 20, state: 'input_confirmed', content: 'body' },
    { ...receipt, messageId: 'legacy', messageSequence: 2 },
  ] };
  const rows = projectAgentTimeline(agent, t);
  expect(rows.map(r => r.callId)).toEqual(['first', 'message:["run","agent","message"]', 'second', 'message:["run","agent","legacy"]']);
  expect(rows[1]).toMatchObject({ entryKind: 'message_receipt', status: 'done', preview: 'body' });
  expect(calls).toHaveLength(2);
  expect(projectAgentTimeline({ ...agent, messageReceipts: [...agent.messageReceipts].reverse() }, t)).toEqual(rows);
});
it('retains receipt body without sender args and does not replace it with stale sender text', () => {
  const { send, agent, projection } = fixture();
  applyMessageReceipt(projection, { ...receipt, content: 'authoritative' });
  expect(agent.messageReceipts[0].content).toBe('authoritative');
  delete send.toolArgs;
  applyMessageReceipt(projection, { ...receipt, content: 'authoritative', state: 'input_confirmed' });
  expect(send._agentMessageReceipt.content).toBe('authoritative');
});
