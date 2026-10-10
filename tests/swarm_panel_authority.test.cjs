/* Run: node --test tests/swarm_panel_authority.test.cjs */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const root = require('node:path').resolve(__dirname, '..');
function presentationModule() {
  const exports = {};
  const source = fs.readFileSync(`${root}/frontend/src/conversation/presentation/swarm-agent-presentation.ts`, 'utf8');
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText,
    { exports, Date, Map, Set });
  return exports;
}
function failureModule() {
  const exports = {};
  const source = fs.readFileSync(`${root}/frontend/src/conversation/presentation/swarm-failure-presentation.ts`, 'utf8');
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText,
    { exports, Date, Map, Set, require: () => presentationModule() });
  return exports;
}
function runtime() {
  const locale = JSON.parse(fs.readFileSync(`${root}/frontend/src/i18n/locales/en.json`, 'utf8'));
  const context = vm.createContext({ console, Date, Set, Map,
    escapeHtml: String, t: (key, params = {}) => (/^(bgActivity\.summary|swarm\.summary|swarm\.failure|swarm\.phase\.failed)/.test(key)
      ? locale[key] || key : key).replace(/\{(\w+)\}/g, (match, name) => params[name] ?? match), renderMarkdown: String,
    _toolIcon: () => '', _toolDisplayName: String,
    _TOOL_DISPLAY: {},
    _swEnsureTicker: () => {}, _swDemandReconciliation: () => {},
    SWARM_RECONCILIATION_POLICY: { intervalMs: 20000, fastMs: 1000 },

    createSwarmReconciliationScheduler: () => ({}),
  });
  Object.assign(context, presentationModule(), failureModule());
  for (const name of ['streaming_swarm_panel', 'swarm_live_projection']) {
    vm.runInContext(fs.readFileSync(`${root}/frontend/src/runtime/sections/ui/${name}.js`, 'utf8'), context);
  }
  vm.runInContext('_swEnsureTicker = () => {}; _swDemandReconciliation = () => {}; _swScheduleUnconfirmedProbe = () => {};', context);
  return context;
}
const agent = (id, status) => ({ id, role: 'review', status, preview: status === 'completed' ? 'final answer' : '' });
const terminal = () => ({ toolName: 'spawn_agents', _swarm: true, roundNum: 1, toolCallId: 'launch-1', taskId: 'exec-1', _swarmActive: true,
  _swarmAgents: [agent('a', 'pending'), agent('b', 'running')],
  _swarmSnapshot: { settled: true, swarm_id: 'exec-1', agents: [agent('a', 'completed'), agent('b', 'completed')] } });
test('settled roster wins over stale live cards, equally after reload; rendering is read-only', () => {
  const c = runtime(), round = terminal(), before = JSON.stringify(round);
  const html = c._buildSwarmPanelHTML(round, [round]);
  assert.match(html, /2\/2 agents finished/);
  assert.match(html, /> Complete</);
  assert.match(html, /final answer/);
  assert.doesNotMatch(html, /sw-a-running|sw-a-pending|data-sw-start=/);
  assert.equal(JSON.stringify(round), before);
  delete round._swarmAgents;
  assert.equal(c._buildSwarmPanelHTML(round, [round]), html);
});
test('aborted, stalled and partially unresolved rosters never claim Complete', () => {
  const c = runtime();
  for (const status of ['aborted', 'stalled', 'unknown']) {
    const round = terminal(); round._swarmSnapshot.agents[1].status = status;
    const html = c._buildSwarmPanelHTML(round, [round]);
    assert.doesNotMatch(html, /> Complete</);
    assert.match(html, status === 'aborted' ? /2\/2 agents finished/ : /1\/2 agents finished/);
  }
});
test('failed agent card distinguishes no-slot admission from generic errors and escapes expandable details', () => {
  const c = runtime();
  c.escapeHtml = value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const render = (failure, error, modifiedFiles = 0) => {
    const round = { toolName: 'spawn_agents', _swarm: true, _swarmEndTime: Date.now(),
      _swarmSnapshot: { settled: true, agents: [{ id: 'a', role: 'review', status: 'failed',
        failure, error, modifiedFiles, elapsed: 0.8, tokens: 0 }] } };
    return c._buildSwarmPanelHTML(round, [round]);
  };
  const structured = render({ code: 'route_unavailable', message: '<img src=x onerror=alert(1)>' }, 'No route');
  assert.match(structured, /No route slot for this agent/);
  assert.match(structured, /No upstream request was sent/);
  assert.match(structured, /<details class="sw-a-err-details"><summary>Technical details<\/summary>/);
  assert.match(structured, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(structured, /<img src=x|Partial edits may remain|provider outage/);
  const legacy = render(undefined, 'No slot available for model=kimi-k3; no upstream request was sent');
  assert.match(legacy, /No route slot for this agent/);
  const generic = render(undefined, 'Worker failed <script>bad()<\/script>');
  assert.match(generic, /Agent could not finish/);
  assert.match(generic, /Review the details and retry/);
  assert.doesNotMatch(generic, /No upstream request was sent|<script>/);
  const edited = render(undefined, 'Worker failed', 2);
  assert.match(edited, /Partial edits may remain/);
});
function overlayModule() {
  const source = fs.readFileSync(`${root}/frontend/src/conversation/application/swarm-presentation-overlay.ts`, 'utf8');
  const exports = {};
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText,
    { exports, Date, Map, Set, structuredClone, require: name => name.includes('swarm-failure') ? failureModule() : presentationModule() });
  return exports;
}
const turn = rounds => ({ turnId: 't', actor: 'assistant', status: 'completed', projectionRevision: 1, projection: { toolRounds: rounds } });
test('durable terminal round discards stale presentation and provisional launch binds without duplicate', () => {
  const { swarmPresentationOverlay: overlay } = overlayModule();
  const durable = terminal(), previous = { ...durable, _swarmAgents: [agent('a', 'running')], status: 'searching' };
  delete previous._swarmSnapshot;
  const result = overlay.rebase(turn([durable]), turn([previous]));
  assert.equal(result.projection.toolRounds[0].status, durable.status);
  const provisional = { _swarm: true, toolName: 'spawn_agents', roundNum: 99, taskId: 'exec-2' };
  const rounds = [terminal(), { toolName: 'await_agents', roundNum: 2 }, { toolName: 'get_agent_result', roundNum: 3 },
    { toolName: 'spawn_agents', taskId: 'exec-2', roundNum: 4, toolCallId: 'launch-2' }];
  const rebased = overlay.rebase(turn(rounds), turn([provisional]));
  assert.equal(rebased.projection.toolRounds.length, 4);
  assert.equal(rebased.projection.toolRounds.filter(r => r._swarm).length, 2);
});
test('successive execution uses actual launch; late events cannot resurrect terminal roster', () => {
  const c = runtime(), old = terminal();
  const current = { toolName: 'spawn_agents', roundNum: 4, taskId: 'exec-2', toolCallId: 'launch-2' };
  const projection = { _swarmRoundNum: 1, toolRounds: [old, {toolName: 'await_agents'}, {toolName: 'get_agent_result'}, current] };
  const context = { convId: 'conv', taskId: 'exec-2', assistantProjection: projection };
  c._handleSwarmPhase({ type: 'swarm_phase', phase: 'spawning', swarmId: 'exec-2', agents: [{id:'c'}] }, context);
  assert.equal(projection.toolRounds.length, 4);
  assert.equal(current._swarmAgents[0].id, 'c');
  const before = JSON.stringify(old);
  c._handleSwarmAgent({type: 'swarm_agent_phase', swarmId: 'exec-1', agentId: 'a', phase: 'thinking'}, context);
  assert.equal(JSON.stringify(old), before);
  assert.equal(current._swarmAgents.length, 1);
});

function pushHarness(turns) {
  const c = runtime(), overlays = new Map();
  const state = { turnsById: Object.fromEntries(turns.map(t => [t.turnId, t])), laneOrder: { main: turns.map(t => t.turnId) } };
  const controller = overlayModule().createSwarmPushPresentationController({
    findConversation: id => id === 'conv' ? { id } : null, readTurnState: () => state,
    readOverlay: (_conv, id) => overlays.get(id) || null,
    upsertOverlay: (_conv, value) => overlays.set(value.turnId, value),
    removeOverlay: (_conv, id) => overlays.delete(id),
    hydrateConversation: () => null, attachAutoContinue() {},
    reducePhase: (frame, context) => c._handleSwarmPhase(frame, context),
    reduceAgent: (frame, context) => c._handleSwarmAgent(frame, context),
    debug: (_message, error) => { throw error; }, warn() {},
  });
  return { overlays, controller, state, r: c, send: frame => controller.handleFrame({ type: 'swarm_phase', convId: 'conv', ...frame }) };
}

const activeRound = (id = 'live') => ({
  toolName: 'spawn_agents', _swarm: true, taskId: 'exec-1', roundNum: 2, toolCallId: 'launch-live',
  _swarmSnapshot: { swarm_id: 'exec-1', settled: false, version: 2, agents: [{
    id, role: 'coder', model: 'actual-model', status: 'running', startedAt: Date.now() - 60000,
    tools: ['read_files'], toolCalls: [{ callId: 'read-1', toolName: 'read_files', status: 'done', preview: 'verified file contents' }],
  }] },
});

test('phase-only worker start makes the card, counts and phase agree without mutating authority', () => {
  const round = { toolName: 'spawn_agents', _swarm: true, taskId: 'exec-1', roundNum: 1,
    toolCallId: 'launch', status: 'done', toolContent: JSON.stringify({ agents: [{ id: 'rust_port', role: 'coder' }], swarm_run_id: 'exec-1' }) };
  const durable = freezeDeep(turn([round])), h = pushHarness([durable]);
  h.send({ phase: 'spawning', swarmId: 'exec-1', agents: [{ id: 'rust_port', role: 'coder' }] });
  h.send({ type: 'swarm_agent_phase', swarmId: 'exec-1', agentId: 'rust_port', phase: 'running' });
  const projected = h.overlays.get('t').projection.toolRounds[0];
  assert.equal(projected._swarmAgents[0].status, 'running');
  const html = runtime()._buildSwarmPanelHTML(projected, [projected]);
  assert.match(html, /1 running/);
  assert.doesNotMatch(html, /1 queued|sw-a-pending/);
  assert.match(html, /sw-a-running sw-a-open/);
  assert.match(html, />agent-coder-rust_port<\/button>/);
});
test('stale pending cards cannot demote a running durable agent or obscure its tools', () => {
  const round = activeRound('rust_port');
  round._swarmActive = true;
  round._swarmAgents = [{ id: 'rust_port', status: 'pending', phase: 'waiting' }];
  const html = runtime()._buildSwarmPanelHTML(freezeDeep(round), [round]);
  assert.match(html, /1 running/);
  assert.doesNotMatch(html, /1 queued|sw-a-pending/);
  assert.match(html, /verified file contents/);
});
test('tool events reattach to a receipt-only durable round without presentation flags', () => {
  const round = { toolName: 'spawn_agents', taskId: 'exec-1', toolCallId: 'launch', roundNum: 1,
    toolContent: JSON.stringify({ summary: 'launched', items: [{ swarm_run_id: 'exec-1', agents: [{ id: 'rust_port' }] }] }) };
  const h = pushHarness([freezeDeep(turn([round]))]);
  for (const callStatus of ['running', 'done']) h.send({ type: 'swarm_agent_tool_call', swarmId: 'exec-1',
    agentId: 'rust_port', callId: 'child-1', toolName: 'read_files', callStatus,
    preview: callStatus === 'done' ? 'live file result' : '' });
  const projected = h.overlays.get('t').projection.toolRounds[0];
  assert.equal(projected._swarmAgents?.[0]._toolCalls?.[0].preview, 'live file result');
  assert.equal(projected._swarmAgents[0]._toolCalls.length, 1);
  assert.match(runtime()._buildSwarmPanelHTML(projected, [projected]), /live file result/);
});
test('unknown agents are not labeled queued and a fully queued roster is not running', () => {
  const c = runtime();
  for (const status of ['pending', 'unknown']) {
    const round = { toolName: 'spawn_agents', _swarm: true, _swarmActive: true,
      _swarmAgents: [{ id: 'waiting', role: 'coder', status }] };
    const html = c._buildSwarmPanelHTML(round, [round]);
    if (status === 'pending') {
      assert.match(html, /sw-pill-queued/);
      assert.doesNotMatch(html, /sw-pill-running/);
    } else {
      assert.doesNotMatch(html, /1 queued/);
      assert.match(html, /1 unconfirmed/);
    }
  }
});
test('pending capacity and dependency evidence survives snapshot reload without running clocks', () => {
  const c = runtime();
  const round = {toolName:'spawn_agents', _swarmSnapshot:{settled:false, agents:[
    {id:'a', role:'coder', status:'pending', waitReason:'capacity'},
    {id:'b', role:'reviewer', status:'pending', waitReason:'dependencies', dependsOn:['a']},
  ]}};
  const before = JSON.stringify(round);
  const recovered = c._recoverSwarmAgents(round, []);
  assert.equal(recovered[0].waitReason, 'capacity');
  assert.equal(recovered[1].dependsOn[0], 'a');
  const html = c._buildSwarmPanelHTML(round, [round]);
  assert.match(html, /swarm.wait.capacity/);
  assert.match(html, /swarm.wait.dependencies/);
  assert.doesNotMatch(html, /sw-a-running|sw-a-timer/);
  assert.equal(JSON.stringify(round), before);
});
test('panel summaries are localized and omit zero counts; waits never claim queue or running', () => {
  for (const language of ['en', 'zh']) {
    const c = runtime(), locale = JSON.parse(fs.readFileSync(`${root}/frontend/src/i18n/locales/${language}.json`, 'utf8'));
    c.t = (key, params = {}) => (locale[key] || key).replace(/\{(\w+)\}/g, (match, name) => params[name] ?? match);
    for (const status of ['running', 'pending']) {
      const round = { toolName: 'spawn_agents', _swarm: true, _swarmActive: true,
        _swarmAgents: ['a', 'b'].map(id => ({ id, role: 'coder', status, ...(status === 'pending' ? { waitReason: 'capacity' } : {}) })) };
      const html = c._buildSwarmPanelHTML(round, [round]);
      const subtitle = html.match(/<span class="sw-header-subtitle">(.*?)<\/span>/)[1];
      const key = status === 'running' ? 'bgActivity.summaryRunning' : 'bgActivity.summaryWaiting';
      assert.ok(subtitle.includes(c.t(key, { count: 2 })), language);
      assert.doesNotMatch(subtitle, /\b0\b|queued|排队/);
      if (status === 'pending') {
        assert.ok(html.includes(locale['swarm.wait.capacity']));
        assert.doesNotMatch(html, /sw-pill-running|排队|>Queued</);
      }
    }
  }
});
test('retry capacity evidence resets a previous running phase and clock, then admission restarts it', () => {
  const c = runtime(), round = activeRound('rust_port');
  const h = pushHarness([freezeDeep(turn([round]))]);
  h.send({ type: 'swarm_agent_phase', swarmId: 'exec-1', agentId: 'rust_port',
    status: 'pending', phase: 'pending', waitReason: 'capacity' });
  let projected = h.overlays.get('t').projection.toolRounds[0];
  let html = c._buildSwarmPanelHTML(projected, [projected]);
  assert.match(html, /swarm.wait.capacity/);
  assert.doesNotMatch(html, /sw-a-running|sw-a-timer|1 running/);
  assert.match(html, /verified file contents/, 'old tool evidence survives retry admission');
  h.send({ type: 'swarm_agent_phase', swarmId: 'exec-1', agentId: 'rust_port', phase: 'running' });
  projected = h.overlays.get('t').projection.toolRounds[0];
  html = c._buildSwarmPanelHTML(projected, [projected]);
  assert.match(html, /1 running/);
  assert.doesNotMatch(html, /swarm.wait.capacity/);

  const active = activeRound('rust_port');
  assert.ok(projected._swarmAgents[0]._startedAt > round._swarmSnapshot.agents[0].startedAt + 30000,
    'the second attempt cannot inherit the first attempt clock');
  c._applyBackendSwarmAgents(active, [{ id: 'rust_port', status: 'pending', waitReason: 'capacity', startedAt: null }]);
  html = c._buildSwarmPanelHTML(active, [active]);
  assert.match(html, /swarm.wait.capacity/);
  assert.doesNotMatch(html, /sw-a-running|sw-a-timer|1 running/);
});
test('an incomplete live card cannot hide persisted model, clock, or tool results', () => {
  const c = runtime(), round = activeRound();
  round._swarmAgents = [{ id: 'live', role: 'coder', model: '', status: 'running', phase: 'thinking', tools: [], _toolCalls: [] }];
  const before = JSON.stringify(round), html = c._buildSwarmPanelHTML(round, [round]);
  assert.match(html, /actual-model/);
  assert.match(html, /sw-a-timer/);
  assert.match(html, /verified file contents/);
  assert.equal(JSON.stringify(round), before);
});
test('running durable roster renders active immediately without live-only launch flags', () => {
  const c = runtime(), round = activeRound('rust_port');
  delete round._swarm;
  round.status = 'done'; // launching is complete, not the worker
  const html = c._buildSwarmPanelHTML(freezeDeep(round), [round]);
  assert.match(html, /sw-panel sw-active/);
  assert.match(html, /1 running/);
  assert.match(html, /sw-pill-running/);
});
test('recovery restores missed tool results with a live parent or a receipt-only reload', async () => {
  for (const liveFlags of [false, true]) {
    const c = runtime(), round = activeRound('rust_port');
    delete round._swarm;
    round.status = 'done';
    round._swarmSnapshot.agents[0].toolCalls = [];
    if (liveFlags) { round._swarm = true; round._swarmActive = true; }
    const durable = freezeDeep({ ...turn([round]), status: 'running' });
    const projection = structuredClone(durable.projection);
    const requests = [];
    c.conversations = [{ id: 'conv' }];
    c.runtimeScope = { ConversationSwarmPresentation: {
      candidates: () => [{ ...durable, projection }],
      update: (_conv, _id, update) => update(projection),
    } };
    c.Api = { swarm: { status: async id => {
      requests.push(id);
      return { active: true, known: true, agents: [
        { ...activeRound('rust_port')._swarmSnapshot.agents[0], round: 24 },
        activeRound('another-wave')._swarmSnapshot.agents[0],
      ] };
    } } };
    await c._reconcileStuckSwarmPanelsOnce();
    assert.deepEqual(requests, ['conv'], `flags=${liveFlags}`);
    const html = c._buildSwarmPanelHTML(projection.toolRounds[0], projection.toolRounds);
    assert.match(html, /verified file contents/);
    assert.doesNotMatch(html, /another-wave/);
    assert.equal(durable.projection.toolRounds[0]._swarmSnapshot.agents[0].toolCalls.length, 0);
    await c._reconcileStuckSwarmPanelsOnce();
    assert.equal(requests.length, 1, 'backoff avoids hot-loop polling');
  }
});
test('a capacity status response sampled before a newer push cannot regress admission', async () => {
  const c = runtime(), round = activeRound('rust_port');
  round._swarmSnapshot.agents[0] = { id: 'rust_port', status: 'pending', waitReason: 'capacity' };
  const projection = { toolRounds: [round] };
  c.conversations = [{ id: 'conv' }];
  c.runtimeScope = { ConversationSwarmPresentation: {
    candidates: () => [{ ...turn([round]), projection }],
    update: (_conv, _id, update) => update(projection),
  } };
  let resolve;
  c.Api = { swarm: { status: () => new Promise(done => { resolve = done; }) } };
  const pending = c._reconcileStuckSwarmPanelsOnce();
  c._handleSwarmAgent({ type: 'swarm_agent_phase', swarmId: 'exec-1', agentId: 'rust_port', phase: 'running' },
    { convId: 'conv', taskId: 'exec-1', assistantProjection: projection });
  resolve({ active: true, known: true, agents: [{ id: 'rust_port', status: 'pending', waitReason: 'capacity' }] });
  await pending;
  const html = c._buildSwarmPanelHTML(round, [round]);
  assert.match(html, /1 running/);
  assert.doesNotMatch(html, /swarm.wait.capacity/);
});
test('handle-only recovery preserves the model that was actually recorded', () => {
  const c = runtime();
  const round = { toolName: 'spawn_agents', toolContent: JSON.stringify({ summary: 'launched', items: [{
    swarm_run_id: 'exec-1', agents: [{ id: 'live', model: 'recorded-model', role: 'coder' }],
  }] }) };
  assert.match(c._buildSwarmPanelHTML(round, [round]), /recorded-model/);
});
test('status reconciliation enriches both existing and newly discovered cards', () => {
  const c = runtime(), round = { _swarmAgents: [{ id: 'known', status: 'running', tools: [] }] };
  const backend = ['known', 'new'].map(id => ({ ...activeRound(id)._swarmSnapshot.agents[0] }));
  c._applyBackendSwarmAgents(round, backend);
  for (const id of ['known', 'new']) {
    const html = c._buildSwarmPanelHTML({ ...round, _swarmAgents: round._swarmAgents.filter(a => a.id === id) }, []);
    assert.match(html, /actual-model/, id);
    assert.match(html, /verified file contents/, id);
    assert.match(html, /sw-a-timer/, id);
  }
});
test('late status replies cannot demote started or cancelled cards', () => {
  const c = runtime();
  const recovered = { _swarmAgents: [{ id: 'unknown', status: 'unknown', phase: 'unknown' }] };
  c._applyBackendSwarmAgents(recovered, [{ id: 'unknown', status: 'running' }]);
  assert.equal(recovered._swarmAgents[0].status, 'running');
  assert.equal(recovered._swarmAgents[0].phase, 'running');
  for (const status of ['running', 'aborted', 'cancelled']) {
    const round = { _swarmAgents: [{ id: 'own', status }] };
    c._applyBackendSwarmAgents(round, [{ id: 'own', status: 'pending' }]);
    assert.equal(round._swarmAgents[0].status, status);
  }
});
test('a settled earlier wave cannot veto a current agent sharing the parent execution ID', () => {
  const durable = freezeDeep(turn([terminal(), activeRound()])), h = pushHarness([durable]);
  h.send({ type: 'swarm_agent_tool_call', swarmId: 'exec-1', agentId: 'live', callId: 'read-2',
    toolName: 'grep_search', callStatus: 'done', preview: 'new result' });
  const result = h.overlays.get('t');
  assert.ok(result, 'current wave receives its event despite the settled sibling');
  assert.ok(result.projection.toolRounds[1]._swarmAgents[0]._toolCalls.some(c => c.preview === 'new result'));
  assert.deepEqual(result.projection.toolRounds[0], durable.projection.toolRounds[0]);
});
test('first tool event after reload can recover its own launch receipt without a snapshot', () => {
  const round = { toolName: 'spawn_agents', _swarm: true, taskId: 'exec-1', roundNum: 1, toolCallId: 'launch',
    toolContent: JSON.stringify({ summary: 'launched', items: [{ swarm_run_id: 'exec-1', agents: [{ id: 'live', model: 'recorded-model' }] }] }) };
  const h = pushHarness([freezeDeep(turn([round]))]);
  h.send({ type: 'swarm_agent_tool_call', swarmId: 'exec-1', agentId: 'live', callId: 'read-1', toolName: 'read_files', callStatus: 'done' });
  const result = h.overlays.get('t');
  assert.ok(result, 'receipt roster is sufficient ownership evidence');
  assert.equal(result.projection.toolRounds[0]._swarmAgents[0]._toolCalls[0].status, 'done');
});
function freezeDeep(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(freezeDeep);
    Object.freeze(value);
  }
  return value;
}
test('late launch phases for a durably settled run never create an overlay on old or current turns', () => {
  for (const phase of ['planning', 'spawning', 'spawn_more']) {
    for (const newer of [false, true]) {
      const old = turn([terminal()]);
      const turns = freezeDeep(newer ? [old, { ...turn([]), turnId: 'current', status: 'running' }] : [old]);
      const before = JSON.stringify(turns), h = pushHarness(turns);
      h.send({ phase, swarmId: 'exec-1', agents: [{ id: 'late' }] });
      assert.equal(h.overlays.size, 0, `${phase}, newer=${newer}`);
      assert.equal(JSON.stringify(turns), before);
    }
  }
});
test('retained launches work on completed parents; provisional launches require the current live producer', () => {
  for (const phase of ['planning', 'spawning', 'spawn_more']) {
    for (const toolStarted of [false, true]) {
      const rounds = [terminal()];
      if (toolStarted) rounds.push({ toolName: 'spawn_agents', taskId: 'exec-new', toolCallId: 'new-call', roundNum: 2 });
      const durable = freezeDeep({ ...turn(rounds), status: toolStarted ? 'completed' : 'running',
        currentAttemptId: 'current-attempt' });
      const before = JSON.stringify(durable), h = pushHarness([durable]);
      h.state.attemptsById = { 'current-attempt': { attemptId: 'current-attempt', turnId: 't',
        taskId: 'exec-new', status: 'running' } };
      h.send({ phase, conversationId: 'conv', taskId: 'conv', executionTaskId: toolStarted ? undefined : 'exec-new',
        swarmId: 'exec-new', agents: [{ id: 'new-agent' }] });
      const first = h.overlays.get('t'), firstBefore = JSON.stringify(first);
      assert.equal(first.projection.toolRounds.length, 2);
      assert.equal(first.projection.toolRounds[1]._swarmAgents[0].id, 'new-agent');
      h.send({ phase: 'spawn_more', swarmId: 'exec-new', agents: [{ id: 'followup' }] });
      const updated = h.overlays.get('t');
      assert.equal(updated.projection.toolRounds.length, 2);
      assert.equal(updated.projection.toolRounds[1]._swarmAgents.length, 2);
      assert.equal(JSON.stringify(first), firstBefore, 'previous overlay is immutable');
      assert.equal(JSON.stringify(durable), before, 'durable input is immutable');
    }
  }
});
test('session status cannot graft another launch roster into this panel', () => {
  const c = runtime(), round = activeRound('own');
  c._applyBackendSwarmAgents(round, ['own', 'other'].map(id => activeRound(id)._swarmSnapshot.agents[0]));
  assert.deepEqual(Array.from(round._swarmAgents, a => a.id), ['own']);
});
test('receipt forms share ownership and reject foreign envelopes', () => {
  const p = presentationModule(), handle = { swarm_run_id: 'run', agents: [{ id: 'own' }] };
  for (const body of [handle, { summary: 'launched', items: [handle] },
    { contractVersion: 'tofu.tool-result/v2', items: [handle] }]) {
    assert.deepEqual(Array.from(p.swarmRoundAgentIds({ toolContent: JSON.stringify(body) })), ['own']);
  }
  assert.equal(p.swarmRoundAgentIds({ toolContent: JSON.stringify({ contractVersion: 'foreign/v1', items: [handle] }) }).length, 0);
});
test('timeline merging is occurrence-safe, terminal-monotonic, immutable and bounded', () => {
  const p = presentationModule();
  const durable = freezeDeep({ id: 'a', status: 'done', _toolCalls: [{ callId: 'x', toolName: 'read_files', status: 'done', preview: 'real result' }] });
  const stale = freezeDeep({ id: 'a', status: 'running', _toolCalls: [{ callId: 'x', toolName: 'read_files', status: 'running', preview: '' }] });
  const merged = p.mergeSwarmAgent(durable, stale);
  assert.equal(merged.status, 'done');
  assert.equal(merged._toolCalls.length, 1);
  assert.equal(merged._toolCalls[0].status, 'done');
  assert.equal(merged._toolCalls[0].preview, 'real result');
  const long = p.mergeSwarmAgent({}, { _toolCalls: Array.from({ length: 100 }, (_, i) => ({ callId: `${i}`, toolName: 'read_files', preview: '🧪'.repeat(2000) })) });
  assert.ok(long._toolCalls.length <= 30);
  assert.ok(JSON.stringify(long._toolCalls).replace(/[^\x00-\x7f]/g, '\\u0000').length <= 32768);
  assert.equal(long._toolCalls.length + long._toolCallsOmitted, 100);
});
test('repeated older recovery windows preserve latest activity and omission counts', () => {
  const p = presentationModule();
  const call = n => ({ callId: String(n), toolName: 'read_files', status: 'done' });
  const snapshot = { id: 'a', toolCalls: Array.from({ length: 30 }, (_, n) => call(n + 10)), toolCallsOmitted: 10 };
  let live = p.mergeSwarmAgent({}, snapshot);
  live = p.mergeSwarmAgent(live, { _toolCalls: [call(40)] });
  const expected = JSON.stringify(live._toolCalls);
  for (let n = 0; n < 4; n += 1) {
    live = p.mergeSwarmAgent(live, snapshot);
    assert.equal(JSON.stringify(live._toolCalls), expected);
    assert.equal(live._toolCallsOmitted, 11);
    assert.equal(live._toolCalls.at(-1).callId, '40');
  }
  const newer = { toolCalls: Array.from({ length: 30 }, (_, n) => call(n + 25)), toolCallsOmitted: 25 };
  live = p.mergeSwarmAgent(live, newer);
  assert.equal(live._toolCallsOmitted, 25);
  assert.equal(live._toolCalls.at(-1).callId, '54');
  live = p.mergeSwarmAgent(live, { _toolCalls: [call(55)] });
  assert.equal(live._toolCallsOmitted, 26);
  assert.equal(live._toolCalls.at(-1).callId, '55');
  const late = { toolCalls: Array.from({ length: 20 }, (_, n) => call(n)), toolCallsOmitted: 0 };
  live = p.mergeSwarmAgent(live, late);
  assert.equal(live._toolCallsOmitted, 26);
  assert.equal(live._toolCalls.at(-1).callId, '55');
});
test('disjoint recovery windows keep a contiguous recent suffix; legacy polls do not duplicate rows', () => {
  const p = presentationModule();
  const call = n => ({ callId: String(n), toolName: 'read_files', status: 'done' });
  let live = p.mergeSwarmAgent({}, { toolCalls: [0, 1, 2, 3, 4].map(call), toolCallsOmitted: 0 });
  live = p.mergeSwarmAgent(live, { toolCalls: [128, 129].map(call), toolCallsOmitted: 128 });
  assert.deepEqual(Array.from(live._toolCalls, call => call.callId), ['128', '129']);
  assert.equal(live._toolCallsOmitted, 128);
  live = p.mergeSwarmAgent(live, { toolCalls: Array.from({ length: 28 }, (_, n) => call(100 + n)), toolCallsOmitted: 100 });
  assert.deepEqual(Array.from(live._toolCalls, call => call.callId), ['128', '129']);
  assert.equal(live._toolCallsOmitted, 128);
  const legacy = { toolCalls: [{ toolName: 'read_files', status: 'done', preview: 'old checkpoint' }], toolCallsOmitted: 0 };
  let recovered = p.mergeSwarmAgent({}, legacy);
  for (let n = 0; n < 4; n += 1) recovered = p.mergeSwarmAgent(recovered, legacy);
  assert.equal(recovered._toolCalls.length, 1);
  assert.equal(recovered._toolCallsOmitted, 0);
  recovered = p.mergeSwarmAgent(recovered, { toolCalls: [...legacy.toolCalls, { toolName: 'grep_search', status: 'done', preview: 'new checkpoint' }] });
  assert.equal(recovered._toolCalls.length, 2);
  assert.equal(recovered._toolCalls.at(-1).preview, 'new checkpoint');
});
test('normalizing a recovered status card cannot resurrect its stale wire timeline', () => {
  const p = presentationModule();
  const recovered = p.normalizeSwarmAgent({ id: 'live', toolCalls: [
    { callId: 'read-1', toolName: 'read_files', status: 'done', preview: 'old result' },
  ] });
  const enriched = p.mergeSwarmAgent(recovered, { id: 'live', _toolCalls: [
    { callId: 'read-2', toolName: 'grep_search', status: 'done', preview: 'new result' },
  ] });
  const next = p.mergeSwarmAgent(enriched, { id: 'live', model: 'actual-model' });
  assert.deepEqual(Array.from(next._toolCalls, call => call.callId), ['read-1', 'read-2']);
});
test('a delayed live tool start cannot erase a completed occurrence result', () => {
  const c = runtime(), round = activeRound();
  round._swarmAgents = [{ id: 'live', status: 'running', _toolCalls: [] }];
  const context = { convId: 'conv', taskId: 'exec-1', assistantProjection: { toolRounds: [round] } };
  const frame = { type: 'swarm_agent_tool_call', swarmId: 'exec-1', agentId: 'live',
    callId: 'read-2', toolName: 'grep_search' };
  c._handleSwarmAgent({ ...frame, callStatus: 'done', preview: 'verified new result', callElapsed: 2 }, context);
  c._handleSwarmAgent({ ...frame, callStatus: 'running', preview: '' }, context);
  const call = round._swarmAgents[0]._toolCalls.find(call => call.callId === 'read-2');
  assert.equal(call.status, 'done');
  assert.equal(call.preview, 'verified new result');
  assert.equal(call.elapsed, 2);
});
test('launch phases prefer the new tool row and never overwrite parent execution identity', () => {
  const c = runtime(), old = activeRound('old');
  const current = { toolName: 'spawn_agents', taskId: 'followup-task', toolCallId: 'new-call', roundNum: 3, status: 'searching' };
  const projection = { toolRounds: [old, current], _swarmRoundNum: 2 };
  c._handleSwarmPhase({ type: 'swarm_phase', phase: 'spawn_more', swarmId: 'exec-1', agents: [{ id: 'new' }] },
    { convId: 'conv', taskId: 'exec-1', assistantProjection: projection });
  assert.equal(current.taskId, 'followup-task');
  assert.equal(current._swarmAgents?.[0].id, 'new');
  assert.equal(old._swarmAgents, undefined, 'old wave remains untouched');
});
test('rebase binds repeated call IDs by scoped occurrence without mutating either input', () => {
  const overlay = overlayModule().swarmPresentationOverlay;
  const scopes = [{ attemptId: 'old' }, { attemptId: 'new' }, { taskId: 'task' }, {}];
  const rounds = scopes.flatMap((scope, i) => [0, 1].map(n => ({ ...scope, toolCallId: 'reused', toolName: 'inspect_image', toolContent: `${i}:${n}`, llmRound: 0 })));
  // Segment order differs across scopes: global occurrence counters would bind
  // the new attempt to the old one, while scalar maps repeat its first crop.
  const segments = [1, 0, 2, 3].flatMap(i => [0, 1, 2].map(n => ({ ...scopes[i], type: 'tool_use', id: 'reused', llmRound: 0, expected: n < 2 ? `${i}:${n}` : undefined })));
  segments.push({ type: 'tool_use', id: 'missing', llmRound: 0, attemptId: 'new' });
  const durable = freezeDeep({ ...turn(rounds), projection: { toolRounds: rounds, segments } });
  const previous = freezeDeep(turn([{ _swarm: true, toolName: 'spawn_agents', toolCallId: 'launch-overlay' }]));
  const before = JSON.stringify([durable, previous]);
  const result = overlay.rebase(durable, previous);
  for (const segment of result.projection.segments) {
    assert.equal(segment._round?.toolContent, segment.expected);
    if (segment._round) assert.ok(result.projection.toolRounds.includes(segment._round));
  }
  assert.equal(JSON.stringify([durable, previous]), before);
});

test('persisted launch failure is visible, escaped, terminal and never a missing-roster claim', () => {
  const c=runtime();
  c.escapeHtml=value=>String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const error="Swarm tool error: ImportError: cannot import name 'observed_usage_totals' <script>bad()</script>";
  const round={toolName:'spawn_agents',toolCallId:'launch-failed',roundNum:1,status:'done',toolContent:error};
  const before=JSON.stringify(round), html=c._buildSwarmPanelHTML(round,[round]);
  assert.match(html,/swarm.launchFailed/);
  assert.match(html,/observed_usage_totals/);
  assert.match(html,/&lt;script&gt;/);
  assert.doesNotMatch(html,/<script>|swarm.panelEmpty|swarm.phase.unconfirmed|data-sw-start=/);
  assert.equal(JSON.stringify(round),before);
  assert.equal(c.swarmRoundNeedsRecovery(round),false);
  assert.equal(c.swarmRoundIsActive(round,[]),false);
});

test('valid roster evidence outranks launch-looking text in a result preview', () => {
  const p=presentationModule();
  const round={toolName:'spawn_agents',toolContent:'Swarm tool error: quoted in user task',
    _swarmSnapshot:{settled:true,agents:[{id:'real',status:'completed'}]}};
  assert.equal(p.swarmLaunchFailure(round),'');
});


test('restart pause never seals a panel and a resumed child reuses the original launch', () => {
  const c = runtime(), round = activeRound('a');
  const h = pushHarness([freezeDeep(turn([round]))]);
  h.send({type: 'swarm_phase', phase: 'interrupted', swarmId: 'exec-1'});
  let projected = h.overlays.get('t').projection.toolRounds[0];
  assert.equal(projected._swarmAgents[0].status, 'interrupted');
  assert.equal(Boolean(projected._swarmEndTime), false);
  assert.equal(Boolean(projected._swarmSnapshot?.settled), false);
  let html = c._buildSwarmPanelHTML(projected, [projected]);
  assert.match(html, /swarm.phase.interrupted/);
  assert.doesNotMatch(html, /sw-a-timer|sw-pill-complete|sw-a-running/);
  h.send({type: 'swarm_agent_phase', agentId: 'a', phase: 'running', swarmId: 'exec-1'});
  projected = h.overlays.get('t').projection.toolRounds[0];
  assert.equal(projected._swarmAgents[0].status, 'running');
  assert.equal(Boolean(projected._swarmEndTime), false);
  html = c._buildSwarmPanelHTML(projected, [projected]);
  assert.match(html, /swarm.phase.running/);
});

test('status pause is known awaiting Continue, not an inactive terminal roster', async () => {
  const round = {toolName: 'spawn_agents', toolCallId: 'launch', taskId: 'run', roundNum: 1,
    _swarmActive: true, _asyncRunning: true,
    toolContent: JSON.stringify({swarm_run_id: 'run', agents: [{id: 'a', role: 'coder'}]}),
    _swarmAgents: [{id: 'a', role: 'coder', status: 'running'}]};
  const c = runtime(), projection = {toolRounds: [round]};
  c.conversations = [{id: 'conv'}];
  c.runtimeScope = {ConversationSwarmPresentation: {
    candidates: () => [{...turn([round]), projection}],
    update: (_conv, _id, update) => update(projection),
  }};
  c.Api = {swarm: {status: async () => ({active: false, known: true, terminated: false,
    awaitingContinue: true, agents: [{id: 'a', role: 'coder', status: 'interrupted'}]})}};
  await c._reconcileStuckSwarmPanelsOnce();
  const paused = projection.toolRounds[0];
  assert.equal(paused._swarmAgents[0].status, 'interrupted');
  assert.equal(Boolean(paused._swarmEndTime), false);
  assert.equal(Boolean(paused._swarmSnapshot?.settled), false);
  const html = c._buildSwarmPanelHTML(paused, [paused]);
  assert.match(html, /swarm.phase.interrupted/);
  assert.doesNotMatch(html, /sw-pill-complete|sw-a-running/);
});

// Exercise real transport shapes without the legacy convId convenience wrapper.
test('canonical mirrored tool frames route by conversation and preserve execution ownership', () => {
  const shapes = [
    { conversationId: 'conv', taskId: 'exec-1' }, // older post-delivery mirror
    { conversationId: 'conv', taskId: 'conv', executionTaskId: 'exec-1' },
    { convId: 'conv', taskId: 'exec-1' }, // pre-canonical compatibility
    { taskId: 'conv' }, // unstamped conversation mirror
  ];
  for (const identity of shapes) {
    const round = activeRound(); round._swarmSnapshot.agents[0].toolCalls = [];
    const durable = freezeDeep(turn([round])), before = JSON.stringify(durable);
    const h = pushHarness([durable]);
    h.controller.handleFrame({ channel: 'swarm', type: 'swarm_agent_tool_call',
      swarmId: 'exec-1', agentId: 'live', callId: 'wire-call', toolName: 'read_files',
      callStatus: 'done', preview: 'exact wire result', ...identity });
    const updated = h.overlays.get('t');
    assert.ok(updated, JSON.stringify(identity));
    assert.equal(updated.projection.toolRounds[0].taskId, 'exec-1');
    assert.equal(updated.projection.toolRounds[0]._swarmAgents[0]._toolCalls[0].preview, 'exact wire result');
    assert.equal(JSON.stringify(durable), before);
  }
});
test('canonical conversation identity wins; foreign conversations and run rosters are fenced', () => {
  const h = pushHarness([freezeDeep(turn([activeRound()]))]);
  const base = { channel: 'swarm', type: 'swarm_agent_tool_call', agentId: 'live',
    callId: 'wire', toolName: 'read_files', callStatus: 'done', swarmId: 'exec-1' };
  h.controller.handleFrame({ ...base, conversationId: 'foreign', convId: 'conv', taskId: 'conv' });
  h.controller.handleFrame({ ...base, conversationId: 'conv', taskId: 'conv', swarmId: 'foreign-run' });
  assert.equal(h.overlays.size, 0);
  h.controller.handleFrame({ ...base, conversationId: 'conv', convId: 'foreign', taskId: 'exec-1' });
  assert.equal(h.overlays.size, 1);
});
test('canonical auto-continuation notifications use conversation identity, not execution identity', () => {
  const calls = [];
  const controller = overlayModule().createSwarmPushPresentationController({
    findConversation: () => null, readTurnState: () => null, readOverlay: () => null,
    upsertOverlay: () => false, removeOverlay() {}, hydrateConversation: () => null,
    attachAutoContinue: id => calls.push(id), reducePhase() {}, reduceAgent() {},
    debug: (_message, error) => { throw error; }, warn() {},
  });
  controller.handleFrame({ type: 'swarm_autocontinue_started', conversationId: 'conv', taskId: 'exec-1' });
  assert.deepEqual(calls, ['conv']);
});
test('recovery keys distinguish repeated provider ids across execution, attempt and launch roster', () => {
  const c = runtime();
  const base = { ...activeRound(), toolCallId: 'reused', attemptId: 'first' };
  const variants = [base, { ...base, attemptId: 'second' }, { ...base, taskId: 'exec-2' },
    { ...base, roundNum: 3 }, { ...base, _swarmSnapshot: { ...base._swarmSnapshot,
      agents: [{ id: 'other', status: 'running' }] } }];
  const keys = variants.map(round => c._swarmPresentationRoundKey(round, 0));
  assert.equal(new Set(keys).size, variants.length);
  assert.equal(c._swarmPresentationRoundKey(base, 0), c._swarmPresentationRoundKey(base, 12),
    'stable scoped launches survive unrelated insertion');
});
test('delayed recovery never targets replaced, ambiguous or newly settled launch rows', () => {
  const c = runtime(), original = activeRound();
  const key = c._swarmPresentationRoundKey(original, 0);
  const replacement = { ...original, taskId: 'exec-2', attemptId: 'next' };
  assert.equal(c._swFindPresentationRound({ toolRounds: [replacement] }, key, 0), null);
  assert.equal(c._swFindPresentationRound({ toolRounds: [original, structuredClone(original)] }, key), null);
  assert.equal(c._swFindPresentationRound({ toolRounds: [{ ...original,
    _swarmSnapshot: { ...original._swarmSnapshot, settled: true } }] }, key), null);
  assert.equal(c._swFindPresentationRound({ toolRounds: [{ toolName: 'read_files' }, original] }, key), original);
});
test('partial terminal live roster does not suppress recovery for an unresolved durable child', async () => {
  const c = runtime(), round = activeRound('done-agent');
  round._swarmSnapshot.agents[0].status = 'completed';
  round._swarmSnapshot.agents.push({ id: 'running-agent', status: 'running', toolCalls: [] });
  round._swarmAgents = [{ id: 'done-agent', status: 'done' }];
  const projection = { toolRounds: [round] }, requests = [];
  c.conversations = [{ id: 'conv' }];
  c.runtimeScope = { ConversationSwarmPresentation: {
    candidates: () => [{ ...turn([round]), projection }],
    update: (_conv, _id, update) => update(projection),
  } };
  c.Api = { swarm: { status: async id => {
    requests.push(id);
    return { active: true, known: true, agents: [{ id: 'running-agent', status: 'running',
      toolCalls: [{ callId: 'recovered', toolName: 'grep_search', status: 'done', preview: 'missing child recovered' }] }] };
  } } };
  await c._reconcileStuckSwarmPanelsOnce();
  assert.deepEqual(requests, ['conv']);
  assert.match(c._buildSwarmPanelHTML(round, projection.toolRounds), /missing child recovered/);
});
test('a response racing authoritative replacement cannot mutate the new launch at the same index', async () => {
  const c = runtime(), round = activeRound(), projection = { toolRounds: [round] };
  c.conversations = [{ id: 'conv' }];
  c.runtimeScope = { ConversationSwarmPresentation: {
    candidates: () => [{ ...turn([round]), projection }],
    update: (_conv, _id, update) => update(projection),
  } };
  let resolve;
  c.Api = { swarm: { status: () => new Promise(done => { resolve = done; }) } };
  const pending = c._reconcileStuckSwarmPanelsOnce();
  const replacement = { ...activeRound('new-owner'), taskId: 'new-task', attemptId: 'new-attempt' };
  projection.toolRounds = [replacement];
  const before = JSON.stringify(replacement);
  resolve({ active: false, known: true, agents: [{ id: 'live', status: 'completed' }] });
  await pending;
  assert.equal(JSON.stringify(replacement), before);
});


test('repeated spawn ids keep one-to-one launch overlays and reject ambiguous or replaced owners', () => {
  const { swarmPresentationOverlay: overlay } = overlayModule();
  const launch = (num, id) => ({ ...activeRound(id), taskId: 'exec', attemptId: 'attempt',
    roundNum: num, toolCallId: 'repeated', _swarmSnapshot: { swarm_id: 'session', settled: false,
      agents: [{ id, status: 'running' }] } });
  const first = launch(1, 'first'), second = launch(2, 'second');
  const durable = freezeDeep(turn([first, second]));
  const live = freezeDeep(turn([{ ...first, _swarmAgents: [{ id: 'first', status: 'running',
    _toolCalls: [{ callId: 'first-call', toolName: 'read_files', status: 'done', preview: 'first evidence' }] }] },
  { ...second, _swarmEndTime: 1234, _swarmAgents: [{ id: 'second', status: 'done',
    _toolCalls: [{ callId: 'second-call', toolName: 'grep_search', status: 'done', preview: 'second evidence' }] }] }]));
  const before = JSON.stringify([durable, live]);
  const rounds = overlay.rebase(durable, live).projection.toolRounds;
  assert.equal(rounds[0]._swarmAgents[0].id, 'first');
  assert.equal(rounds[0]._swarmEndTime, undefined);
  assert.equal(rounds[1]._swarmAgents[0].id, 'second');
  assert.equal(rounds[1]._swarmEndTime, 1234);
  assert.equal(rounds[0]._swarmAgents[0]._toolCalls[0].preview, 'first evidence');
  assert.equal(rounds[1]._swarmAgents[0]._toolCalls[0].preview, 'second evidence');
  assert.equal(JSON.stringify([durable, live]), before);
  const ambiguous = { ...first }; delete ambiguous.roundNum;
  const sameRoster = { ...first }; delete sameRoster.roundNum;
  const ignored = overlay.rebase(turn([sameRoster, { ...sameRoster }]), turn([{ ...ambiguous,
    _swarmEndTime: 9, _swarmAgents: [{ id: 'first', status: 'done' }] }]));
  assert.equal(ignored.projection.toolRounds.length, 2);
  assert.ok(ignored.projection.toolRounds.every(row => !row._swarmEndTime && !row._swarmAgents));
  const replaced = overlay.rebase(turn([second]), turn([{ ...first, _swarmEndTime: 9,
    _swarmAgents: [{ id: 'first', status: 'done' }] }]));
  assert.equal(replaced.projection.toolRounds.length, 1);
  assert.equal(replaced.projection.toolRounds[0]._swarmAgents, undefined);
});

test('conversation-routed frames without run ids honor explicit producer and attempt and reject ambiguity', () => {
  const older = { ...turn([{ ...activeRound('shared'), taskId: 'old-exec', attemptId: 'old-attempt' }]), turnId: 'old' };
  const current = { ...turn([{ ...activeRound('shared'), taskId: 'new-exec', attemptId: 'new-attempt' }]), turnId: 'new' };
  const h = pushHarness([freezeDeep(older), freezeDeep(current)]);
  const frame = { type: 'swarm_agent_tool_call', conversationId: 'conv', taskId: 'conv',
    executionTaskId: 'old-exec', attemptId: 'old-attempt', agentId: 'shared',
    callId: 'old-call', toolName: 'read_files', callStatus: 'done', preview: 'old producer evidence' };
  h.send(frame);
  assert.ok(h.overlays.has('old'));
  assert.equal(h.overlays.has('new'), false);
  assert.equal(h.overlays.get('old').projection.toolRounds[0]._swarmAgents[0]._toolCalls.at(-1).preview,
    'old producer evidence');
  const before = JSON.stringify([...h.overlays]);
  h.send({ ...frame, attemptId: 'new-attempt', callId: 'contradictory' });
  h.send({ ...frame, executionTaskId: 'missing-exec', callId: 'foreign' });
  h.send({ ...frame, executionTaskId: undefined, attemptId: undefined, callId: 'ambiguous' });
  assert.equal(JSON.stringify([...h.overlays]), before);
  // Old-server mirrors still carry the execution task in taskId.
  h.send({ ...frame, executionTaskId: undefined, taskId: 'old-exec', callId: 'legacy-call' });
  assert.equal(h.overlays.get('old').projection.toolRounds[0]._swarmAgents[0]._toolCalls.at(-1).callId, 'legacy-call');
});

test('delayed status and intervening canonical push stay on the same repeated spawn occurrence through controller', async () => {
  const make = (num, id) => ({ ...activeRound(id), taskId: 'exec', attemptId: 'attempt',
    toolCallId: 'repeated', roundNum: num, _swarmSnapshot: { swarm_id: 'session', settled: false,
      agents: [{ id, status: 'running', toolCalls: [] }] } });
  const durable = freezeDeep(turn([make(1, 'first'), make(2, 'second')]));
  const h = pushHarness([durable]), c = runtime();

  c.conversations = [{ id: 'conv' }];
  c.runtimeScope = { ConversationSwarmPresentation: h.controller.presentation };
  let release; const pending = new Promise(resolve => { release = resolve; }); let calls = 0;
  c.Api = { swarm: { status: async () => ++calls === 1 ? pending : null } };
  const reconciliation = c._reconcileStuckSwarmPanelsOnce();
  h.send({ type: 'swarm_agent_tool_call', conversationId: 'conv', taskId: 'conv', executionTaskId: 'exec',
    attemptId: 'attempt', swarmId: 'session', agentId: 'second', callId: 'push-second',
    toolName: 'grep_search', callStatus: 'done', preview: 'second launch live evidence' });
  h.state.turnsById.t = freezeDeep({ ...durable, projectionRevision: 2,
    projection: { ...durable.projection, content: 'new parent revision' } });
  release({ active: true, known: true, agents: [{ id: 'first', status: 'running', toolCalls: [{
    callId: 'status-first', toolName: 'read_files', status: 'done', preview: 'first launch status evidence' }] },
  { id: 'second', status: 'running', toolCalls: [] }] });
  await reconciliation;
  const state = h.controller.presentation.compose({ id: 'conv' }, h.state, h.state);
  const rounds = state.turnsById.t.projection.toolRounds;
  assert.equal(state.turnsById.t.projection.content, 'new parent revision');
  assert.equal(rounds[0]._swarmAgents[0].id, 'first');
  assert.equal(rounds[0]._swarmAgents[0]._toolCalls.at(-1).preview, 'first launch status evidence');
  assert.equal(rounds[1]._swarmAgents[0].id, 'second');
  assert.equal(rounds[1]._swarmAgents[0]._toolCalls.at(-1).preview, 'second launch live evidence');
  assert.ok(rounds.every(row => !row._swarmEndTime));
  assert.equal(durable.projection.toolRounds[0]._swarmAgents, undefined);
});

test('terminal status recovery replaces stale active phases without losing failure evidence', () => {
  const c = runtime();
  for (const [status, expectedStatus, expectedPhase] of [
    ['failed', 'failed', 'error'], ['completed', 'done', 'done'], ['cancelled', 'cancelled', 'aborted'],
  ]) {
    const round = { _swarm: true, toolName: 'spawn_agents', _swarmActive: true,
      _swarmAgents: [{ id: 'a', status: 'running', phase: 'tool_use' }] };
    c._applyBackendSwarmAgents(round, [{ id: 'a', status, error: status === 'failed' ? 'provider failed' : '' }]);
    assert.equal(round._swarmAgents[0].status, expectedStatus);
    assert.equal(round._swarmAgents[0].phase, expectedPhase);
    if (status === 'failed') assert.equal(round._swarmAgents[0].error, 'provider failed');
  }
});


test('late launch frames cannot reopen an overlay-ended roster before terminal hydration', () => {
  for (const phase of ['planning', 'spawning', 'spawn_more']) {
    const round = { ...activeRound(), attemptId: 'attempt', _swarmActive: true };
    const durable = freezeDeep(turn([round])), before = JSON.stringify(durable), h = pushHarness([durable]);
    const identity = { conversationId: 'conv', taskId: 'conv', executionTaskId: 'exec-1',
      attemptId: 'attempt', swarmId: 'exec-1' };
    h.controller.handleFrame({ ...identity, type: 'swarm_phase', phase: 'complete',
      agents: [{ id: 'live', status: 'completed' }] });
    const ended = h.overlays.get('t'), endedBefore = JSON.stringify(ended);
    assert.ok(ended.projection.toolRounds[0]._swarmEndTime);
    h.controller.handleFrame({ ...identity, type: 'swarm_phase', phase, agents: [{ id: 'live' }] });
    assert.equal(JSON.stringify(h.overlays.get('t')), endedBefore, phase);
    assert.equal(JSON.stringify(durable), before, 'late lifecycle never changes authority');
    // The reducer independently fences this race, even outside the controller.
    const projection = structuredClone(ended.projection), projectionBefore = JSON.stringify(projection);
    h.r._handleSwarmPhase({ ...identity, type: 'swarm_phase', phase, agents: [{ id: 'live' }] },
      { convId: 'conv', taskId: 'exec-1', assistantProjection: projection });
    assert.equal(JSON.stringify(projection), projectionBefore, 'ended is not an absent launch');
  }
});

test('ambiguous launch selection never creates a provisional third panel', () => {
  for (const separateTurns of [false, true]) {
    const launches = [1, 2].map(roundNum => ({ toolName: 'spawn_agents', status: 'searching',
      taskId: 'exec-1', attemptId: 'attempt', toolCallId: 'reused', roundNum }));
    const turns = freezeDeep(separateTurns ? launches.map((launch, index) =>
      ({ ...turn([launch]), turnId: `launch-${index}`, status: 'running' })) : [turn(launches)]);
    const before = JSON.stringify(turns), h = pushHarness(turns);
    const frame = { conversationId: 'conv', taskId: 'conv', executionTaskId: 'exec-1',
      attemptId: 'attempt', swarmId: 'new-run', type: 'swarm_phase', phase: 'spawning',
      agents: [{ id: 'new' }] };
    h.controller.handleFrame(frame);
    assert.equal(h.overlays.size, 0, `multiple candidate launches, separateTurns=${separateTurns}`);
    assert.equal(JSON.stringify(turns), before);
    const projection = { toolRounds: structuredClone(launches) }, projectionBefore = JSON.stringify(projection);
    h.r._handleSwarmPhase(frame, { convId: 'conv', taskId: 'exec-1', assistantProjection: projection });
    assert.equal(JSON.stringify(projection), projectionBefore, 'ambiguous is not an absent launch');
  }
});

test('a distinct current tool launch can follow an overlay-ended wave sharing the session run', () => {
  const ended = { ...activeRound('old'), _swarmActive: false, _swarmEndTime: 2000, attemptId: 'attempt' };
  const current = { toolName: 'spawn_agents', status: 'searching', taskId: 'exec-1',
    attemptId: 'attempt', toolCallId: 'new-launch', roundNum: 2 };
  const durable = freezeDeep(turn([ended, current])), before = JSON.stringify(durable), h = pushHarness([durable]);
  h.controller.handleFrame({ conversationId: 'conv', taskId: 'conv', executionTaskId: 'exec-1',
    attemptId: 'attempt', swarmId: 'exec-1', type: 'swarm_phase', phase: 'spawning', agents: [{ id: 'new' }] });
  const updated = h.overlays.get('t');
  assert.equal(updated.projection.toolRounds.length, 2);
  assert.deepEqual(updated.projection.toolRounds[0], ended);
  assert.equal(updated.projection.toolRounds[1]._swarmAgents[0].id, 'new');
  assert.equal(JSON.stringify(durable), before);
});

test('composed launch keeps distinct run identity before receipt and accepts later child tools', () => {
  for (const withCallId of [true, false]) {
    const launch = { toolName: 'spawn_agents', status: 'searching', taskId: 'exec',
      attemptId: 'attempt', roundNum: 1, ...(withCallId ? { toolCallId: 'launch' } : {}) };
    const durable = freezeDeep({ ...turn([launch]), status: 'running', currentAttemptId: 'attempt' });
    const h = pushHarness([durable]), before = JSON.stringify(durable);
    const scope = { conversationId: 'conv', taskId: 'conv', executionTaskId: 'exec',
      attemptId: 'attempt', swarmId: 'distinct-run', turnId: 't' };
    h.controller.handleFrame({ ...scope, type: 'swarm_phase', phase: 'spawning', agents: [{ id: 'child' }] });
    let composed = h.controller.presentation.compose({ id: 'conv' }, h.state, h.state);
    let rounds = composed.turnsById.t.projection.toolRounds;
    assert.equal(rounds.length, 1, `one launch, call id=${withCallId}`);
    assert.equal(rounds[0]._swarmAgents?.[0].id, 'child', 'compose must retain the unbound launch overlay');
    assert.equal(rounds[0]._swarmRunId, 'distinct-run');
    assert.equal(rounds[0].taskId, 'exec');
    h.controller.handleFrame({ ...scope, type: 'swarm_agent_tool_call', agentId: 'child',
      callId: 'distinct-call', toolName: 'read_files', callStatus: 'done', preview: 'distinct run evidence' });
    composed = h.controller.presentation.compose({ id: 'conv' }, h.state, h.state);
    rounds = composed.turnsById.t.projection.toolRounds;
    assert.equal(rounds.length, 1);
    assert.equal(rounds[0]._swarmAgents[0]._toolCalls.at(-1).preview, 'distinct run evidence');
    assert.equal(JSON.stringify(durable), before);
  }
});

test('absent historical attempts cannot provision active panels on a regenerated Turn', () => {
  const current = { toolName: 'spawn_agents', status: 'searching', taskId: 'current-exec',
    attemptId: 'current-attempt', toolCallId: 'current-launch', roundNum: 1 };
  for (const status of ['completed', 'failed', 'cancelled', 'superseded', 'running']) {
    const durable = freezeDeep({ ...turn([current]), status: 'running', currentAttemptId: 'current-attempt' });
    const h = pushHarness([durable]);
    h.state.attemptsById = {
      'current-attempt': { attemptId: 'current-attempt', turnId: 't', taskId: 'current-exec', status: 'running' },
      'old-attempt': { attemptId: 'old-attempt', turnId: 't', taskId: 'old-exec', status },
    };
    const before = JSON.stringify(h.state);
    h.controller.handleFrame({ conversationId: 'conv', taskId: 'conv', executionTaskId: 'old-exec',
      attemptId: 'old-attempt', turnId: 't', swarmId: 'old-run', type: 'swarm_phase', phase: 'spawning',
      agents: [{ id: 'stale-old-worker' }] });
    assert.equal(h.overlays.size, 0, `absent historical attempt: ${status}`);
    assert.equal(h.controller.presentation.compose({ id: 'conv' }, h.state, h.state).turnsById.t.projection.toolRounds.length, 1);
    assert.equal(JSON.stringify(h.state), before);
    // A retained, unsettled old receipt remains valid ownership evidence for
    // a detached child even after its parent's execution is historical.
    const oldLaunch = { ...current, taskId: 'old-exec', attemptId: 'old-attempt',
      toolCallId: 'old-launch', roundNum: 2, toolContent: JSON.stringify({
        swarm_run_id: 'old-run', agents: [{ id: 'retained-child' }] }) };
    h.state.turnsById.t = freezeDeep({ ...durable, projection: { toolRounds: [current, oldLaunch] } });
    h.controller.handleFrame({ conversationId: 'conv', taskId: 'conv', executionTaskId: 'old-exec',
      attemptId: 'old-attempt', turnId: 't', swarmId: 'old-run', type: 'swarm_agent_tool_call',
      agentId: 'retained-child', callId: 'retained-call', toolName: 'read_files', callStatus: 'done' });
    const composed = h.controller.presentation.compose({ id: 'conv' }, h.state, h.state);
    assert.equal(composed.turnsById.t.projection.toolRounds.length, 2);
    assert.equal(composed.turnsById.t.projection.toolRounds[1]._swarmAgents[0]._toolCalls[0].callId, 'retained-call');
  }
});

test('only the current live producer can provision a launch before durable tool-start delivery', () => {
  const durable = freezeDeep({ ...turn([]), status: 'running', currentAttemptId: 'attempt' });
  const h = pushHarness([durable]);
  h.state.attemptsById = { attempt: { attemptId: 'attempt', turnId: 't', taskId: 'exec', status: 'running' } };
  h.controller.handleFrame({ conversationId: 'conv', taskId: 'conv', executionTaskId: 'exec',
    attemptId: 'attempt', turnId: 't', swarmId: 'distinct-run', type: 'swarm_phase', phase: 'spawning',
    agents: [{ id: 'current-child' }] });
  assert.equal(h.controller.presentation.compose({ id: 'conv' }, h.state, h.state)
    .turnsById.t.projection.toolRounds[0]._swarmAgents[0].id, 'current-child');
});


test('internal silence failure shows an error card live and after reload while preserving partial work', () => {
  const c = runtime();
  const reason = 'Invocation stalled after 900 seconds without progress';

  c.errorEnvelopeMessage = c.swarmFailureReason; // Existing message port; this regression carries a string reason.
  const partial = 'Partial implementation evidence remains available';
  const live = { toolName: 'spawn_agents', _swarm: true, roundNum: 1,
    toolCallId: 'stall-launch', taskId: 'stall-exec', _swarmActive: true,
    _swarmAgents: [{ id: 'silent-child', role: 'coder', status: 'running', preview: partial }] };
  c._handleSwarmAgent({ type: 'swarm_agent_complete', swarmId: 'stall-exec',
    agentId: 'silent-child', status: 'failed', error: reason, summary: partial,
    elapsed: 933.9, tokens: 93184, modifiedFiles: 2 },
    { convId: 'stall-conv', taskId: 'stall-exec', assistantProjection: { toolRounds: [live] } });
  assert.equal(live._swarmAgents[0].status, 'failed');
  assert.equal(live._swarmAgents[0].error, reason);
  const cold = { toolName: 'spawn_agents', _swarm: true, roundNum: 1,
    _swarmSnapshot: { settled: true, agents: [{ id: 'silent-child', role: 'coder',
      status: 'failed', error: reason, preview: partial, elapsed: 933.9,
      tokens: 93184, modifiedFiles: 2 }] } };
  for (const round of [live, cold]) {
    const before = JSON.stringify(round);
    const html = c._buildSwarmPanelHTML(round, [round]);
    assert.match(html, /data-sw-agent-status="failed"/);
    assert.match(html, /sw-a-err-title/);
    assert.match(html, /Invocation stalled after 900 seconds without progress/);
    assert.match(html, /Partial edits may remain/);
    assert.doesNotMatch(html, /swarm\.phase\.aborted|> Complete</);
    assert.equal(JSON.stringify(round), before);
  }
  assert.match(c._buildSwarmPanelHTML(cold, [cold]), /Partial implementation evidence remains available/);
});
