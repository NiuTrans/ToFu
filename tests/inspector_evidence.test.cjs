const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const Module = require('node:module');
const path = require('node:path');
const filename = path.resolve(__dirname, '../frontend/src/features/inspector-evidence.ts');
const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
const mod = new Module(filename, module);
mod._compile(compiled, filename);
const { inspectorReference, resolveInspectorView } = mod.exports;

test('explicit root/child R1 addresses override every legacy guess', () => {
  const ref = { taskId: 'child-executor', roundNum: 1, turn: 'worker:invocation' };
  assert.deepEqual(inspectorReference({ inspectorRef: ref, agentId: 'wrong', llmRound: 90 }, 'wrong-parent'), ref);
  assert.equal(inspectorReference({ inspectorRef: {}, llmRound: 0 }, 'parent'), null);
  assert.deepEqual(inspectorReference({ llmRound: 0 }, 'parent'), { taskId: 'parent', roundNum: 1, turn: '' });
});
test('exact turn survives state/request fallback; no owner widening', async () => {
  const calls = [];
  const view = await resolveInspectorView(async (...args) => {
    calls.push(args);
    return args[3] === 'request' ? { messages: [{ content: 'request only' }] } : null;
  }, 'child', 1, 'worker:original');
  assert.equal(view.kind, 'request');
  assert.deepEqual(calls, [['child', 1, 'worker:original', 'state'], ['child', 1, 'worker:original', 'request']]);
});

test('state evidence wins without requesting or widening to request history', async () => {
  const calls = [];
  const payload = { messages: [{ role: 'tool', content: 'full result' }] };
  const view = await resolveInspectorView(async (...args) => { calls.push(args); return payload; }, 'root', 1, 'root:original');
  assert.equal(view.kind, 'state');
  assert.equal(view.payload, payload);
  assert.deepEqual(calls, [['root', 1, 'root:original', 'state']]);
});
test('invalid explicit addresses never fall back to plausible legacy owners', () => {
  for (const inspectorRef of [null, {}, { taskId: 'child', roundNum: 0, turn: 'original' },
    { taskId: 'child', roundNum: 1, turn: '' }, { taskId: 'child', roundNum: 1.5, turn: 'original' },
    { taskId: 'x'.repeat(257), roundNum: 1, turn: 'original' }]) {
    assert.equal(inspectorReference({ inspectorRef, llmRound: 1, agentId: 'plausible' }, 'parent'), null);
  }
});
test('empty exact evidence stays unavailable and fetch errors propagate', async () => {
  const calls = [];
  assert.equal(await resolveInspectorView(async (...args) => { calls.push(args); return { messages: [] }; }, 'child', 1, 'original'), null);
  assert.deepEqual(calls, [['child', 1, 'original', 'state'], ['child', 1, 'original', 'request']]);
  const failure = new Error('evidence unavailable');
  await assert.rejects(resolveInspectorView(async () => { throw failure; }, 'child', 1, 'original'), error => error === failure);
});
