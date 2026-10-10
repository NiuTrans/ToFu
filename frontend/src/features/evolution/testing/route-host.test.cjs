/** Route loading and teardown contracts; actual host with controlled import flights. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const source = readFileSync(resolve(__dirname, '../route-host.ts'), 'utf8');
const api = {};
new Function('exports', ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
} }).outputText)(api);
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup(t, overrides = {}, path = 'evolution') {
  const dom = new JSDOM('<main></main>'); t.after(() => dom.window.close());
  const root = dom.window.document.querySelector('main');
  const flights = [], mounts = [], disposals = [], updates = [], failures = [], reports = [];
  const mount = kind => (root, path) => {
    mounts.push([kind, path]); root.textContent = kind;
    return { updateRoute: path => updates.push([kind, path]), dispose: () => disposals.push(kind) };
  };
  const host = api.createEvolutionRouteHost({ root,
    loadPage: kind => new Promise((resolve, reject) => flights.push({ kind, resolve, reject })),
    showLoading: () => { root.textContent = 'Loading'; },
    showFailure: error => { failures.push(error); root.textContent = 'Failed'; },
    reportFailure: error => reports.push(error), ...overrides,
  }, path);
  t.after(() => host.dispose());
  return { root, host, flights, mount, mounts, disposals, updates, failures, reports };
}
test('route taxonomy preserves notebook, studio, research and evidence URLs', () => {
  for (const [path, kind] of [['evolution','programs'],['evolution/programs/a','programs'],
    ['evolution/runs/r','studio'],['evolution/start','studio'],['evolution/research/id','research'],
    ['evolution/goals/a','detail'],['evolution/floor','detail']]) assert.equal(api.evolutionPageKind(path), kind);
});
test('only selected page loads; pending same-kind routes mount the latest path once', async t => {
  const e = setup(t); assert.equal(e.flights.length, 0); assert.equal(e.root.getAttribute('aria-busy'), 'true');
  e.host.updateRoute('evolution/programs/latest'); await tick();
  assert.deepEqual(e.flights.map(f => f.kind), ['programs']);
  e.flights[0].resolve(e.mount('programs')); await tick();
  assert.deepEqual(e.mounts, [['programs','evolution/programs/latest']]);
  e.host.updateRoute('evolution/programs/next'); assert.deepEqual(e.updates, [['programs','evolution/programs/next']]);
  assert.equal(e.root.hasAttribute('aria-busy'), false); assert.equal(e.flights.length, 1);
});
test('reversed import completion never mounts or recovers a stale route', async t => {
  let recovered = 0; const e = setup(t, { recoverFailure: () => { recovered++; return false; } }); await tick();
  e.host.updateRoute('evolution/runs/r'); await tick();
  e.flights[1].resolve(e.mount('studio')); await tick(); e.flights[0].reject(Error('Old import failed')); await tick();
  assert.deepEqual(e.mounts, [['studio','evolution/runs/r']]); assert.equal(e.failures.length, 0); assert.equal(recovered, 0);
});
test('stale successful imports cannot replace another mounted page', async t => {
  const e = setup(t); await tick(); e.host.updateRoute('evolution/research'); await tick();
  e.flights[1].resolve(e.mount('research')); await tick(); e.flights[0].resolve(e.mount('programs')); await tick();
  assert.deepEqual(e.mounts, [['research','evolution/research']]); assert.equal(e.root.textContent, 'research');
});
test('dispose before import admission starts no graph; later routes are inert', async t => {
  const e = setup(t); e.host.dispose(); e.host.updateRoute('evolution/runs'); await tick();
  assert.equal(e.flights.length, 0); assert.equal(e.root.textContent, ''); assert.equal(e.root.hasAttribute('aria-busy'), false);
});
test('late completion after disposal cannot mount or show an error', async t => {
  const e = setup(t); await tick(); e.host.dispose(); e.flights[0].resolve(e.mount('programs')); await tick();
  assert.equal(e.mounts.length, 0); assert.equal(e.failures.length, 0); assert.equal(e.root.textContent, '');
});
test('route switch releases mounted ownership before admitting the next graph', async t => {
  const e = setup(t); await tick(); e.flights[0].resolve(e.mount('programs')); await tick();
  e.host.updateRoute('evolution/goals'); assert.deepEqual(e.disposals, ['programs']); await tick();
  e.flights[1].resolve(e.mount('detail')); await tick(); e.host.dispose(); e.host.dispose();
  assert.deepEqual(e.disposals, ['programs','detail']);
});
test('failed graph is visible once and same-kind navigation cannot bypass it', async t => {
  let recovered = 0; const e = setup(t, { recoverFailure: () => { recovered++; return false; } }); await tick();
  e.flights[0].reject(Error('CSS unavailable')); await tick(); e.host.updateRoute('evolution/programs/a'); await tick();
  assert.equal(e.failures.length, 1); assert.equal(recovered, 1); assert.equal(e.flights.length, 1);
  assert.equal(e.root.textContent, 'Failed'); assert.equal(e.root.hasAttribute('aria-busy'), false);
});
test('shared recovery owns its reload without competing failure UI', async t => {
  const e = setup(t, { recoverFailure: () => true }); await tick(); e.flights[0].reject(Error('Chunk unavailable')); await tick();
  assert.equal(e.failures.length, 0); assert.equal(e.reports.length, 1);
});
test('recovery and diagnostic faults cannot reject the completion callback', async t => {
  const e = setup(t, { recoverFailure: () => { throw Error('Storage denied'); },
    reportFailure: () => { throw Error('Diagnostic fault'); } }); await tick();
  e.flights[0].reject(Error('Load failure')); await tick(); assert.equal(e.failures.length, 1);
});
test('mount failure stays visible and releases busy state', async t => {
  const e = setup(t); await tick(); e.flights[0].resolve(() => { throw Error('Mount failed'); }); await tick();
  assert.equal(e.failures.length, 1); assert.equal(e.root.hasAttribute('aria-busy'), false);
});
test('disposer failure does not strand the next page or repeat cleanup', async t => {
  let disposed = 0; const e = setup(t); await tick(); e.flights[0].resolve(() => ({ dispose() { disposed++; throw Error('Cleanup failed'); } })); await tick();
  e.host.updateRoute('evolution/runs'); await tick(); e.flights[1].resolve(e.mount('studio')); await tick();
  assert.equal(disposed, 1); assert.equal(e.reports.length, 1); assert.equal(e.root.textContent, 'studio');
});
test('reentrant disposal during mount also releases the returned page', async t => {
  let released = 0; const e = setup(t); await tick(); e.flights[0].resolve(() => {
    e.host.dispose(); return { dispose: () => { released++; } };
  }); await tick(); assert.equal(released, 1); assert.equal(e.root.hasAttribute('aria-busy'), false);
});
