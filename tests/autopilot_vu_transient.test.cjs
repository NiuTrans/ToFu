/* Run: node --test tests/autopilot_vu_transient.test.cjs */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const root = require('node:path').resolve(__dirname, '..');

function transientModule() {
  const source = fs.readFileSync(
    `${root}/frontend/src/conversation/application/autopilot-vu-transient.ts`,
    'utf8',
  );
  const exports = {};
  vm.runInNewContext(ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, { exports, Date, Object, Number, Array });
  return exports;
}

function frame(type, inner) {
  return { type: 'autopilot_vu_event', vuMsgId: 'vu-1', inner: { type, ...inner } };
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

test('live tool frames preserve canonical timing and round identities', () => {
  const { createAutopilotVuTransientTurn, reduceAutopilotVuTransientTurn } = transientModule();
  let turn = createAutopilotVuTransientTurn({
    conversationId: 'conv-1', vuMsgId: 'vu-1', timestamp: 100,
  });
  turn = reduceAutopilotVuTransientTurn(turn, frame('tool_start', {
    roundNum: 4, llmRound: 2, agentId: 'agent-1', toolCallId: 'call-1',
    toolName: 'grep_search', query: 'needle', tStart: 1000,
  }), 110);
  turn = reduceAutopilotVuTransientTurn(turn, frame('tool_result', {
    roundNum: 4, llmRound: 3, agentId: 'agent-1', taskId: 'task-1',
    attemptId: 'attempt-1', toolCallId: 'call-1', toolName: 'grep_search',
    query: 'needle', results: [{ path: 'file.ts' }], status: 'done',
    tStart: 1000, tEnd: 1250,
  }), 120);
  assert.deepEqual(plain(turn.projection.toolRounds[0]), {
    roundNum: 4, query: 'needle', results: [{ path: 'file.ts' }], status: 'done',
    toolName: 'grep_search', toolCallId: 'call-1', toolArgs: null,
    attentionKind: '', parentToolCallId: '', llmRound: 3, agentId: 'agent-1',
    tStart: 1000, taskId: 'task-1', attemptId: 'attempt-1', tEnd: 1250,
  });
  assert.equal(turn.projection.segments[0]._round.tEnd, 1250);
});

test('tool_complete patches timing and identity without erasing terminal verdict', () => {
  const { createAutopilotVuTransientTurn, reduceAutopilotVuTransientTurn } = transientModule();
  let turn = createAutopilotVuTransientTurn({ conversationId: 'conv-1', vuMsgId: 'vu-1' });
  turn = reduceAutopilotVuTransientTurn(turn, frame('tool_start', {
    roundNum: 1, toolCallId: 'call-1', toolName: 'run_command', query: 'cmd', tStart: 10,
  }));
  turn = reduceAutopilotVuTransientTurn(turn, frame('tool_complete', {
    roundNum: 1, llmRound: 7, agentId: 'agent-2', taskId: 'task-2',
    attemptId: 'attempt-2', toolCallId: 'call-1', toolName: 'run_command',
    toolContent: 'failed', status: 'error', tStart: 10, tEnd: 30,
  }));
  const round = turn.projection.toolRounds[0];
  assert.equal(round.status, 'error');
  assert.equal(round.tStart, 10);
  assert.equal(round.tEnd, 30);
  assert.equal(round.taskId, 'task-2');
  assert.equal(round.attemptId, 'attempt-2');
  assert.equal(round.agentId, 'agent-2');
  assert.equal(round.llmRound, 7);
});

test('role completion settles only unfinished live rounds and retains them when final row omits rounds', () => {
  const { createAutopilotVuTransientTurn, settleAutopilotVuTransientTurn } = transientModule();
  const current = createAutopilotVuTransientTurn({
    conversationId: 'conv-1', vuMsgId: 'vu-1', timestamp: 100,
    replaySnapshot: { toolRounds: [
      { roundNum: 1, toolCallId: 'pending', status: 'searching', tStart: 1000 },
      { roundNum: 2, toolCallId: 'failed', status: 'error', tStart: 1100, tEnd: 1150 },
    ] },
  });
  const settled = settleAutopilotVuTransientTurn(
    current, 'conv-1', 'vu-1', { content: 'done' }, 1200,
  );
  assert.equal(settled.projection.toolRounds.length, 2);
  assert.equal(settled.projection.toolRounds[0].status, 'done');
  assert.equal(settled.projection.toolRounds[0].tEnd, 1200);
  assert.equal(settled.projection.toolRounds[1].status, 'error');
  assert.equal(settled.projection.toolRounds[1].tEnd, 1150);
  assert.equal(settled.projection.segments.filter(segment => segment.type === 'tool_use').length, 2);
});

test('role completion preserves authoritative terminal segments while backfilling rich rounds', () => {
  const { createAutopilotVuTransientTurn, settleAutopilotVuTransientTurn } = transientModule();
  const current = createAutopilotVuTransientTurn({
    conversationId: 'conv-1', vuMsgId: 'vu-1', timestamp: 100,
    replaySnapshot: {
      content: 'live draft',
      thinking: 'live reasoning',
      toolRounds: [{
        roundNum: 4, llmRound: 3, toolCallId: 'call-1',
        toolName: 'run_command', toolArgs: { command: 'pytest -q' },
        toolContent: '2 passed', results: [{ exitCode: 0 }],
        taskId: 'task-1', attemptId: 'attempt-1', status: 'searching',
        tStart: 1000,
      }],
    },
  });
  const terminalSegments = [{
    type: 'text', blockId: 'text:terminal', text: 'verified answer',
    deliverable: true, terminal: true,
  }];
  const settled = settleAutopilotVuTransientTurn(
    current, 'conv-1', 'vu-1', {
      content: 'verified answer', timestamp: 1500, segments: terminalSegments,
    }, 1600,
  );
  assert.deepEqual(plain(settled.projection.segments), terminalSegments);
  assert.deepEqual(plain(settled.projection.toolRounds[0]), {
    roundNum: 4, llmRound: 3, toolCallId: 'call-1',
    toolName: 'run_command', toolArgs: { command: 'pytest -q' },
    toolContent: '2 passed', results: [{ exitCode: 0 }],
    taskId: 'task-1', attemptId: 'attempt-1', status: 'done',
    tStart: 1000, tEnd: 1500,
  });
});
