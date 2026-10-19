// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { agentDockProjection } from './agent-sidebar-dock-projection';
const base = () => ({ turns: [] as any[], swarms: [] as any[], recover: (_round: any, _rounds: any[]): any[] => [],
  renderPanel: vi.fn(() => '<div class="sw-agent" data-agent-id="agent-1"><div class="sw-a-body"><details data-call-id="real-call"><summary>run_command</summary><pre>Actual output (truncated)</pre></details></div></div>'),
  renderActivity: (_agent: any) => document.createElement('div'), openConversation: vi.fn(), translate: (_: string, fallback: string, vars?: Record<string, string | number>) => fallback.replace(/\{(\w+)\}/g, (match, key) => String(vars?.[key] ?? match)) });
const turn = (run: string, status = 'running') => ({ turnId: 'turn-' + run, projection: { toolRounds: [{ _swarm: true, _swarmRunId: run, _swarmKey: 'session', attemptId: run,
  _swarmAgents: [{ id: 'agent-1', role: 'tester', status, _toolCalls: [{ id: 'real-call', name: 'run_command' }], conversationId: 'child' }] }] } });
describe('agent dock canonical projection', () => {

  it('updates a unique unsettled launch from activity after its parent has completed', async () => {
    const ports = base(); ports.turns = [{ ...turn('run-one', 'pending'), status: 'completed' }];
    const before = JSON.stringify(ports.turns);
    ports.swarms = [{ swarm_key: 'session', agents: [{ id: 'agent-1', status: 'running', round: 42 }] }];
    const items = agentDockProjection(ports);
    expect(items).toHaveLength(1); expect(items[0].status).toBe('running');
    expect(items[0].statusLabel).toBe('running · round 42');
    expect(await items[0].render()).toContain('real-call');
    expect(JSON.stringify(ports.turns)).toBe(before);
  });
  it('uses explicit run evidence to deduplicate terminal rows without reopening success', () => {
    const ports = base(); ports.turns = [turn('run-one', 'done')];
    ports.swarms = [{ swarm_key: 'session', swarm_id: 'run-one', agents: [{ id: 'agent-1', status: 'running' }] }];
    const items = agentDockProjection(ports);
    expect(items).toHaveLength(1); expect(items[0].status).toBe('done');
  });
  it('does not bind session-only activity across ambiguous reused launch identities', () => {
    const ports = base(); ports.turns = [turn('run-one'), turn('run-two')];
    ports.swarms = [{ swarm_key: 'session', agents: [{ id: 'agent-1', status: 'pending', waitReason: 'capacity' }] }];
    const items = agentDockProjection(ports);
    expect(items).toHaveLength(3);
    expect(items[0].status).toBe('running'); expect(items[1].status).toBe('running');
    expect(items[2].statusLabel).toBe('Waiting for capacity');
  });
  it('empty activity does not imply success or remove unresolved canonical evidence', () => {
    const ports = base(); ports.turns = [turn('run-one')];
    expect(agentDockProjection(ports)[0].status).toBe('running');
  });
  it('delegates actual execution detail to canonical renderer and declares bounded evidence', async () => {
    const ports = base(); ports.turns = [turn('run-one')];
    const [agent] = agentDockProjection(ports); expect(ports.renderPanel).not.toHaveBeenCalled();
    const html = await agent.render(); expect(ports.renderPanel).toHaveBeenCalledTimes(1);
    expect(html).toContain('real-call'); expect(html).toContain('not a full transcript');
    agent.openConversation?.(); expect(ports.openConversation).toHaveBeenCalledWith('child');
  });
  it('does not collapse repeated agent/tool identities across runs and attempts', () => {
    const ports = base(); ports.turns = [turn('run-one'), turn('run-two')];
    const items = agentDockProjection(ports); expect(items).toHaveLength(2); expect(items[0].key).not.toBe(items[1].key);
  });
  it('keeps activity-only evidence separate when no canonical owner matches', async () => {
    const ports = base(); ports.turns = [turn('run-one')];
    ports.swarms = [{ swarm_key: 'different-session', created_at: 1, agents: [{ id: 'agent-1', status: 'running' }] }];
    const items = agentDockProjection(ports); expect(items).toHaveLength(2);
    const html = await items[1].render(); expect(html).toContain('not yet available'); expect(html).not.toContain('real-call');
  });
  it('escapes labels and never mutates canonical source', async () => {
    const ports = base(); ports.turns = [turn('run-one')]; const before = JSON.stringify(ports.turns);
    ports.translate = () => '<script>injection</script>';
    expect(await agentDockProjection(ports)[0].render()).toContain('&lt;script&gt;');
    expect(JSON.stringify(ports.turns)).toBe(before);
  });
});

it('renders exact-run live receipts in the panel body without mutating the launch', async () => {
  const ports = base(); ports.turns = [turn('run-one')];
  const before = JSON.stringify(ports.turns);
  const messageReceipts = [{ swarmRunId: 'run-one', agentId: 'agent-1', messageId: 'm', content: 'New instruction', state: 'submitted' }];
  ports.swarms = [{ swarm_id: 'run-one', agents: [{ id: 'agent-1', status: 'running', messageReceipts }] }];
  await agentDockProjection(ports)[0].render();
  const rendered = (ports.renderPanel.mock.calls as unknown as any[][])[0][0];
  expect(rendered._swarmAgents[0].messageReceipts).toEqual(messageReceipts);
  expect(JSON.stringify(ports.turns)).toBe(before);
});

const launch = (ids: string[], extra: Record<string, any> = {}) => ({
  _swarm: true, toolName: 'spawn_agents', toolCallId: 'launch-' + ids.join('-'),
  taskId: 'task-one', attemptId: 'attempt-one', _swarmRunId: 'run-one', _swarmKey: 'session',
  _swarmAgents: ids.map(id => ({ id, status: 'pending' })), ...extra,
});
const withRounds = (...toolRounds: any[]) => ({ turnId: 'turn-one', projection: { toolRounds } });

describe('assignment-scoped sidebar identity', () => {
  it('unions duplicated [A,B] launches, later [C] and activity into three rows instead of seven', async () => {
    const ports = base();
    const first = launch(['A', 'B'], { toolCallId: undefined });
    const copy = launch(['A', 'B'], { _swarmAgents: [
      { id: 'A', role: 'researcher', status: 'running', objective: 'Full assignment instruction', model: 'model-a',
        conversationId: 'child-a', _toolCalls: [{ callId: 'call-a', toolName: 'read_files', preview: 'saved output' }] },
      { id: 'B', status: 'running' },
    ] });
    ports.turns = [withRounds(first, copy, launch(['C']))];
    ports.swarms = [{ swarm_id: 'run-one', swarm_key: 'session', agents: ['A', 'B', 'C'].map(id =>
      ({ id, status: 'running', round: 42 })) }];
    const before = JSON.stringify([ports.turns, ports.swarms]);
    const items = agentDockProjection(ports);
    expect(items.map(item => item.agentId)).toEqual(['A', 'B', 'C']);
    expect(items.every(item => item.statusLabel === 'running · round 42')).toBe(true);
    await items[0].render();
    const rendered = (ports.renderPanel.mock.calls as unknown as any[][])[0][0];
    expect(rendered._swarmAgents[0]).toMatchObject({ id: 'A', role: 'researcher', model: 'model-a', round: 42,
      objective: 'Full assignment instruction', _toolCalls: [{ callId: 'call-a', preview: 'saved output' }] });
    items[0].openConversation?.(); expect(ports.openConversation).toHaveBeenCalledWith('child-a');
    expect(JSON.stringify([ports.turns, ports.swarms])).toBe(before);
  });

  it('uses one stable key for duplicate canonical assignments regardless of launch position', () => {
    const ports = base(); const first = launch(['A']);
    ports.turns = [withRounds(first, { ...first })];
    const initial = agentDockProjection(ports);
    expect(initial).toHaveLength(1);
    ports.turns = [withRounds({ toolName: 'read_files' }, { ...first })];
    expect(agentDockProjection(ports)[0].key).toBe(initial[0].key);
  });

  it('resolves session activity against unique assignments, not duplicate physical rounds', () => {
    const ports = base(); const first = launch(['A']);
    ports.turns = [withRounds(first, { ...first })];
    ports.swarms = [{ swarm_key: 'session', agents: [{ id: 'A', status: 'running', round: 9 }] }];
    expect(agentDockProjection(ports).map(item => [item.agentId, item.statusLabel])).toEqual([['A', 'running · round 9']]);
  });

  it('keeps reused IDs separate across runs and attempts and binds only explicitly scoped activity', () => {
    const ports = base();
    ports.turns = [withRounds(launch(['A']), launch(['A'], { _swarmRunId: 'run-two' }),
      launch(['A'], { attemptId: 'attempt-two' }))];
    ports.swarms = [{ swarm_id: 'run-one', attemptId: 'attempt-two',
      agents: [{ id: 'A', status: 'running', round: 8 }] }];
    const items = agentDockProjection(ports);
    expect(items).toHaveLength(3); expect(new Set(items.map(item => item.key)).size).toBe(3);
    expect(items.map(item => item.status)).toEqual(['pending', 'pending', 'running']);
    expect(items[2].statusLabel).toBe('running · round 8');
  });

  it('keeps explicit parent execution conflicts separate within the same run and attempt', () => {
    const ports = base();
    ports.turns = [withRounds(
      launch(['A'], { taskId: 'parent-old', _swarmAgents: [{ id: 'A', status: 'done' }] }),
      launch(['A'], { taskId: 'parent-new' }),
    )];
    ports.swarms = [{ swarm_id: 'run-one', executionTaskId: 'parent-new',
      agents: [{ id: 'A', status: 'running', round: 5 }] }];
    const items = agentDockProjection(ports);
    expect(items.map(item => item.status)).toEqual(['done', 'running']);
    expect(new Set(items.map(item => item.key)).size).toBe(2);
  });

  it('does not guess a parent execution for an ambiguous unstamped launch', () => {
    const ports = base();
    ports.turns = [withRounds(launch(['A'], { taskId: undefined }),
      launch(['A'], { taskId: 'parent-old' }), launch(['A'], { taskId: 'parent-new' }))];
    ports.swarms = [{ swarm_id: 'run-one', agents: [{ id: 'A', status: 'running' }] }];
    const items = agentDockProjection(ports);
    expect(items.map(item => item.status)).toEqual(['pending', 'pending', 'pending', 'running']);
    expect(new Set(items.map(item => item.key)).size).toBe(4);
  });

  it('does not guess an attempt when exact-run activity still has multiple assignment owners', () => {
    const ports = base();
    ports.turns = [withRounds(launch(['A']), launch(['A'], { attemptId: 'attempt-two' }))];
    ports.swarms = [{ swarm_id: 'run-one', agents: [{ id: 'A', status: 'running' }] }];
    expect(agentDockProjection(ports).map(item => item.status)).toEqual(['pending', 'pending', 'running']);
  });

  it.each(['interrupted', 'unknown', 'done'].flatMap(status => [false, true].map(settledFirst => ({ status, settledFirst }))))(
    'keeps settled $status authority over duplicate live cards (settled first: $settledFirst)', async ({ status, settledFirst }) => {
    const ports = base();
    const settled = launch(['agent-1'], { _swarmSnapshot: { swarm_id: 'run-one', settled: true,
      agents: [{ id: 'agent-1', status, preview: 'Final bounded evidence' }] } });
    const stale = launch(['agent-1'], { _swarmAgents: [{ id: 'agent-1', status: 'running' }] });
    ports.recover = round => round._swarmSnapshot?.agents || [];
    ports.turns = [withRounds(...(settledFirst ? [settled, stale] : [stale, settled]))];
    ports.swarms = [{ swarm_id: 'run-one', agents: [{ id: 'agent-1', status: 'running', round: 99 }] }];
    const items = agentDockProjection(ports);
    expect(items).toHaveLength(1); expect(items[0].status).toBe(status);
    await items[0].render();
    const rendered = (ports.renderPanel.mock.calls as unknown as any[][])[0][0];
    expect(rendered._swarmSnapshot.settled).toBe(true);
    expect(rendered._swarmSnapshot.agents[0].status).toBe(status);
    expect(rendered._swarmSnapshot.agents[0].phase).not.toBe('running');
  });

  it('does not infer shared ownership from a legacy task/session and reused agent ID', () => {
    const ports = base();
    ports.turns = [withRounds(launch(['A'], { _swarmRunId: undefined, toolCallId: 'first' }),
      launch(['A'], { _swarmRunId: undefined, toolCallId: 'second' }))];
    ports.swarms = [{ swarm_key: 'session', agents: [{ id: 'A', status: 'running' }] }];
    expect(agentDockProjection(ports).map(item => item.status)).toEqual(['pending', 'pending', 'running']);
  });

  it('unions legacy copies only with the same scoped launch occurrence, not a reused tool ID', () => {
    const ports = base();
    const legacy = launch(['A'], { _swarmRunId: undefined, roundNum: 7 });
    ports.turns = [withRounds(legacy, { ...legacy }, { ...legacy, roundNum: 8 })];
    const items = agentDockProjection(ports);
    expect(items).toHaveLength(2);
    ports.turns = [withRounds({ toolName: 'read_files' }, { ...legacy })];
    expect(agentDockProjection(ports)[0].key).toBe(items[0].key);
  });

  it('merges duplicate activity by run identity without losing rich fields or combining other runs', async () => {
    const ports = base();
    const activity = { swarm_id: 'run-one', swarm_key: 'session', created_at: 1,
      agents: [{ id: 'A', status: 'running', objective: 'Full objective', model: 'model-a' }] };
    ports.swarms = [activity, { ...activity, created_at: 2, agents: [{ agentId: 'A', status: 'running', round: 4 }] },
      { ...activity, swarm_id: 'run-two' }];
    const renderActivity = vi.fn((_agent: any) => document.createElement('div'));
    ports.renderActivity = renderActivity;
    const items = agentDockProjection(ports);
    expect(items).toHaveLength(2); expect(items[0].swarmRunId).toBe('run-one');
    expect(items[1].swarmRunId).toBe('run-two'); expect(items[0].statusLabel).toBe('running · round 4');
    await items[0].render();
    expect(renderActivity).toHaveBeenCalledWith(expect.objectContaining({ model: 'model-a', objective: 'Full objective', round: 4 }));
    ports.swarms = [{ ...activity, created_at: 9 }];
    expect(agentDockProjection(ports)[0].key).toBe(items[0].key);
  });
});

describe('worker execution identity within a reused run', () => {

  it.each(['live', 'recovered', 'both'])('partitions conflicting worker records before merging %s evidence', async source => {
    const ports = base();
    const old = { id: 'A', executionTaskId: 'worker-old', status: 'done', objective: 'Old assignment',
      _toolCalls: [{ callId: 'old-tool', toolName: 'read_files' }] };
    const current = { id: 'A', executionTaskId: 'worker-new', status: 'running', objective: 'New assignment' };
    const round = launch(['A'], { _swarmAgents: source === 'live' ? [old, current] : source === 'both' ? [current] : [],
      ...(source !== 'live' ? { _swarmSnapshot: { swarm_id: 'run-one', agents: source === 'both' ? [old] : [old, current] } } : {}) });
    ports.recover = value => value._swarmSnapshot?.agents || [];
    ports.turns = [withRounds(round)];
    const before = JSON.stringify(round);
    const items = agentDockProjection(ports);
    expect(items.map(item => [item.identity?.workerTask, item.status, item.objective])).toEqual([
      ['worker-old', 'done', 'Old assignment'], ['worker-new', 'running', 'New assignment'],
    ]);
    for (const item of items) {
      await item.render();
      const rendered = (ports.renderPanel.mock.calls as unknown as any[][]).at(-1)![0];
      expect(rendered._swarmAgents).toHaveLength(1);
      expect(rendered._swarmSnapshot.agents).toEqual(rendered._swarmAgents);
      expect(rendered._swarmAgents[0].executionTaskId).toBe(item.identity?.workerTask);
      if (item.identity?.workerTask === 'worker-new') expect(rendered._swarmAgents[0]._toolCalls || []).toHaveLength(0);
    }
    expect(JSON.stringify(round)).toBe(before);
  });

  it('keeps distinct explicit worker executions separate while consuming only matching activity', () => {
    const ports = base();
    ports.turns = [withRounds(
      launch(['A'], { _swarmAgents: [{ id: 'A', status: 'done', executionTaskId: 'worker-old' }] }),
      launch(['A'], { _swarmAgents: [{ id: 'A', status: 'pending', executionTaskId: 'worker-new' }] }),
    )];
    ports.swarms = [{ swarm_id: 'run-one', agents: [{ id: 'A', status: 'running', executionTaskId: 'worker-new' }] }];
    const items = agentDockProjection(ports);
    expect(items.map(item => item.status)).toEqual(['done', 'running']);
    expect(new Set(items.map(item => item.key)).size).toBe(2);
  });

  it('exposes compatible hydration identity without reusing an explicit execution key', () => {
    const ports = base();
    ports.turns = [withRounds(launch(['A']))];
    const [initial] = agentDockProjection(ports);
    ports.turns = [withRounds(launch(['A'], { _swarmAgents: [{ id: 'A', status: 'running', executionTaskId: 'worker-one' }] }))];
    const [hydrated] = agentDockProjection(ports);
    expect(hydrated.key).not.toBe(initial.key);
    expect(hydrated.identity).toEqual({ ...initial.identity, workerTask: 'worker-one' });
  });

  it('binds explicit worker activity to its exact owner despite an ambiguous unstamped copy', () => {
    const ports = base();
    ports.turns = [withRounds(launch(['A']),
      launch(['A'], { _swarmAgents: [{ id: 'A', status: 'pending', executionTaskId: 'worker-one' }] }),
      launch(['A'], { _swarmAgents: [{ id: 'A', status: 'pending', executionTaskId: 'worker-two' }] }))];
    ports.swarms = [{ swarm_id: 'run-one', agents: [{ id: 'A', status: 'running', executionTaskId: 'worker-two' }] }];
    expect(agentDockProjection(ports).map(item => item.status)).toEqual(['pending', 'pending', 'running']);
  });

  it('unions an unstamped canonical copy only when one worker execution is known', () => {
    const ports = base();
    const unstamped = launch(['A']);
    const stamped = launch(['A'], { _swarmAgents: [{ id: 'A', status: 'running', executionTaskId: 'worker-one' }] });
    ports.turns = [withRounds(unstamped, stamped)];
    const items = agentDockProjection(ports);
    expect(items).toHaveLength(1);
    ports.turns = [withRounds(stamped)];
    expect(agentDockProjection(ports)[0].key).toBe(items[0].key);
    ports.turns = [withRounds(unstamped, stamped,
      launch(['A'], { _swarmAgents: [{ id: 'A', status: 'running', executionTaskId: 'worker-two' }] }))];
    ports.swarms = [{ swarm_id: 'run-one', agents: [{ id: 'A', status: 'running' }] }];
    expect(agentDockProjection(ports)).toHaveLength(4);
  });

  it('keeps activity-only worker executions distinct despite shared run and session timestamps', () => {
    const ports = base();
    ports.swarms = [{ swarm_id: 'run-one', agents: [
      { id: 'A', status: 'done', executionTaskId: 'worker-old' },
      { id: 'A', status: 'running', executionTaskId: 'worker-new' },
    ] }];
    expect(agentDockProjection(ports).map(item => item.status)).toEqual(['done', 'running']);
  });
});


describe('order-independent execution evidence', () => {


  it.each([false, true])('hydrates a unique explicit-run producer before merging completed activity (reverse=%s)', async reverse => {
    const ports = base();
    const pending = launch(['A'], { taskId: undefined, _swarmAgents: [{ id: 'A', status: 'running', objective: 'Canonical assignment' }] });
    ports.turns = [withRounds(pending)];
    const copies = [
      { swarm_id: 'run-one', attemptId: 'attempt-one', agents: [{ id: 'A', status: 'running' }] },
      { swarm_id: 'run-one', attemptId: 'attempt-one', executionTaskId: 'producer',
        agents: [{ id: 'A', executionTaskId: 'worker', status: 'done', preview: 'Completed output' }] },
    ];
    ports.swarms = reverse ? [...copies].reverse() : copies;
    const before = JSON.stringify([ports.turns, ports.swarms]);
    const items = agentDockProjection(ports);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ status: 'done', objective: 'Canonical assignment',
      identity: { producerTask: 'producer', workerTask: 'worker' } });
    await items[0].render();
    const rendered = (ports.renderPanel.mock.calls as unknown as any[][])[0][0];
    expect(rendered._swarmAgents[0]).toMatchObject({ status: 'done', preview: 'Completed output' });
    expect(JSON.stringify([ports.turns, ports.swarms])).toBe(before);
    ports.turns = [withRounds({ ...pending, taskId: 'producer' })];
    ports.swarms = [copies[1]];
    expect(agentDockProjection(ports)[0].key).toBe(items[0].key);
  });
  it('does not hydrate a legacy launch producer from session identity alone', () => {
    const ports = base();
    ports.turns = [withRounds(launch(['A'], { taskId: undefined, _swarmRunId: undefined }))];
    ports.swarms = [{ swarm_key: 'session', executionTaskId: 'producer',
      agents: [{ id: 'A', executionTaskId: 'worker', status: 'done' }] }];
    const items = agentDockProjection(ports);
    expect(items).toHaveLength(2);
    expect(items[0].identity?.producerTask).toBe('');
    expect(items.map(item => item.status)).toEqual(['pending', 'done']);
  });

  it.each(['worker', 'producer'])('keeps unstamped activity unbound when canonical and live %s stamps conflict', async stamp => {
    const ports = base();
    ports.turns = [withRounds(launch(['A'], stamp === 'producer' ? { taskId: 'producer-one' }
      : { _swarmAgents: [{ id: 'A', executionTaskId: 'worker-one', status: 'pending' }] }))];
    ports.swarms = [
      { swarm_id: 'run-one', agents: [{ id: 'A', status: 'pending', objective: 'Unbound activity',
        _toolCalls: [{ callId: 'unbound-tool', toolName: 'read_files' }] }] },
      { swarm_id: 'run-one', ...(stamp === 'producer' ? { executionTaskId: 'producer-two' } : {}),
        agents: [{ id: 'A', status: 'running', ...(stamp === 'worker' ? { executionTaskId: 'worker-two' } : {}) }] },
    ];
    const renderActivity = vi.fn((_agent: any) => document.createElement('div')); ports.renderActivity = renderActivity;
    const items = agentDockProjection(ports);
    expect(items).toHaveLength(3);
    const field = stamp === 'worker' ? 'workerTask' : 'producerTask';
    expect(items.map(item => item.identity?.[field]).sort()).toEqual(['', stamp + '-one', stamp + '-two']);
    for (const item of items.filter(item => item.identity?.[field])) {
      expect(item.objective).not.toBe('Unbound activity'); await item.render();
    }
    expect((ports.renderPanel.mock.calls as unknown as any[][])[0][0]._swarmAgents[0]._toolCalls || []).toHaveLength(0);
    expect(renderActivity.mock.calls[0][0]._toolCalls || []).toHaveLength(0);
  });

  it.each(['worker', 'producer'].flatMap(stamp => [false, true].map(reverse => ({ stamp, reverse }))))(
    'does not attribute ambiguous $stamp evidence across canonical and activity sources (reverse=$reverse)', async ({ stamp, reverse }) => {
    const ports = base();
    const unknown = launch(['A'], { ...(stamp === 'producer' ? { taskId: undefined } : {}),
      _swarmAgents: [{ id: 'A', status: 'pending', objective: 'Unbound assignment',
        _toolCalls: [{ callId: 'unbound-tool', toolName: 'read_files' }] }] });
    const known = launch(['A'], stamp === 'producer' ? { taskId: 'producer-one' }
      : { _swarmAgents: [{ id: 'A', status: 'pending', executionTaskId: 'worker-one' }] });
    ports.turns = [withRounds(...(reverse ? [known, unknown] : [unknown, known]))];
    ports.swarms = [{ swarm_id: 'run-one', ...(stamp === 'producer' ? { executionTaskId: 'producer-two' } : {}),
      agents: [{ id: 'A', status: 'running', ...(stamp === 'worker' ? { executionTaskId: 'worker-two' } : {}) }] }];
    const renderActivity = vi.fn((_agent: any) => document.createElement('div')); ports.renderActivity = renderActivity;
    const items = agentDockProjection(ports);
    expect(items).toHaveLength(3);
    const field = stamp === 'worker' ? 'workerTask' : 'producerTask';
    expect(items.map(item => item.identity?.[field]).sort()).toEqual(['', stamp + '-one', stamp + '-two']);
    const unbound = items.find(item => !item.identity?.[field])!;
    expect(unbound.objective).toBe('Unbound assignment'); expect(unbound.status).toBe('pending');
    for (const item of items.filter(item => item !== unbound)) {
      expect(item.objective).not.toBe('Unbound assignment'); await item.render();
    }
    expect((ports.renderPanel.mock.calls as unknown as any[][])[0][0]._swarmAgents[0]._toolCalls || []).toHaveLength(0);
    expect(renderActivity.mock.calls[0][0]._toolCalls || []).toHaveLength(0);
  });

  it.each([false, true])('does not assign ambiguous live worker evidence to unstamped launch tools (reverse=%s)', async reverse => {
    const ports = base();
    ports.turns = [withRounds(launch(['A'], { _swarmAgents: [{ id: 'A', status: 'pending', objective: 'Canonical only',
      _toolCalls: [{ callId: 'canonical-tool', name: 'read_files' }] }] }))];
    const workers = [{ id: 'A', status: 'done', executionTaskId: 'worker-old' },
      { id: 'A', status: 'running', executionTaskId: 'worker-new' }];
    ports.swarms = [{ swarm_id: 'run-one', agents: reverse ? [...workers].reverse() : workers }];
    const renderActivity = vi.fn((_agent: any) => document.createElement('div'));
    ports.renderActivity = renderActivity;
    const items = agentDockProjection(ports);
    expect(items).toHaveLength(3);
    expect(items[0].status).toBe('pending');
    expect(items[0].objective).toBe('Canonical only');
    expect(items[0].identity?.workerTask).toBe('');
    for (const item of items.slice(1)) await item.render();
    expect(renderActivity.mock.calls.map(([agent]) => agent.executionTaskId).sort()).toEqual(['worker-new', 'worker-old']);
    for (const [agent] of renderActivity.mock.calls) {
      expect(agent.objective).toBeUndefined(); expect(agent._toolCalls || []).toHaveLength(0);
    }
  });

  it.each([false, true])('unions uniquely hydrated activity-only evidence (reverse=%s)', async reverse => {
    const ports = base();
    const copies = [{ swarm_id: 'run-one', agents: [{ id: 'A', status: 'pending', objective: 'Retained objective' }] },
      { swarm_id: 'run-one', executionTaskId: 'producer', agents: [{ id: 'A', status: 'running', executionTaskId: 'worker' }] }];
    ports.swarms = reverse ? [...copies].reverse() : copies;
    const renderActivity = vi.fn((_agent: any) => document.createElement('div'));
    ports.renderActivity = renderActivity;
    const [item, ...rest] = agentDockProjection(ports);
    expect(rest).toHaveLength(0);
    expect(item.identity).toMatchObject({ producerTask: 'producer', workerTask: 'worker' });
    await item.render();
    expect(renderActivity).toHaveBeenCalledWith(expect.objectContaining({ executionTaskId: 'worker', objective: 'Retained objective' }));
    ports.swarms = [copies[1]];
    expect(agentDockProjection(ports)[0].key).toBe(item.key);
  });

  it('leaves ambiguous activity stamps separate rather than attributing unknown tools', () => {
    const ports = base();
    ports.swarms = [{ swarm_id: 'run-one', agents: [
      { id: 'A', status: 'pending', objective: 'Unknown worker' },
      { id: 'A', status: 'done', executionTaskId: 'worker-one' },
      { id: 'A', status: 'running', executionTaskId: 'worker-two' },
    ] }];
    const items = agentDockProjection(ports);
    expect(items).toHaveLength(3);
    expect(items.map(item => item.identity?.workerTask)).toEqual(['', 'worker-one', 'worker-two']);
    expect(items.map(item => item.objective)).toEqual(['Unknown worker', undefined, undefined]);
  });

  it.each(['producer', 'worker'])('keeps %s keys stable when siblings appear and distinct on replacement', kind => {
    const ports = base();
    const record = (stamp: string) => launch(['A'], kind === 'producer' ? { taskId: stamp }
      : { _swarmAgents: [{ id: 'A', status: 'running', executionTaskId: stamp }] });
    ports.turns = [withRounds(record('one'))];
    const [first] = agentDockProjection(ports);
    ports.turns = [withRounds(record('two'))];
    expect(agentDockProjection(ports)[0].key).not.toBe(first.key);
    ports.turns = [withRounds(record('one'), record('two'))];
    const siblings = agentDockProjection(ports);
    expect(siblings[0].key).toBe(first.key);
    expect(new Set(siblings.map(item => item.key)).size).toBe(2);
  });

  it.each([false, true])('gives durable settled authority precedence over a provisional end marker (reverse=%s)', async reverse => {
    const ports = base();
    const provisional = launch(['A'], { _swarmEndTime: 1, _swarmAgents: [{ id: 'A', status: 'unknown', phase: 'unknown' }] });
    const durable = launch(['A'], { _swarmSnapshot: { swarm_id: 'run-one', settled: true,
      agents: [{ id: 'A', status: 'done', phase: 'done' }] } });
    ports.recover = round => round._swarmSnapshot?.agents || [];
    ports.turns = [withRounds(...(reverse ? [durable, provisional] : [provisional, durable]))];
    ports.swarms = [{ swarm_id: 'run-one', agents: [{ id: 'A', status: 'running', round: 99 }] }];
    const items = agentDockProjection(ports);
    expect(items).toHaveLength(1); expect(items[0].status).toBe('done');
    await items[0].render();
    expect((ports.renderPanel.mock.calls as unknown as any[][])[0][0]._swarmSnapshot.settled).toBe(true);
  });
});
