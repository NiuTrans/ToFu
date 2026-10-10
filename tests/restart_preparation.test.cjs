/** Restart observation cannot outlive its attempt or overlap slow reads. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const ownerPath = path.resolve(__dirname, '../frontend/src/features/settings/restart-preparation.ts');
const compiled = ts.transpileModule(fs.readFileSync(ownerPath, 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
});
const owner = new Module(ownerPath, module);
owner.filename = ownerPath;
owner.paths = Module._nodeModulePaths(path.dirname(ownerPath));
owner._compile(compiled.outputText, ownerPath);
const { watchRestartPreparation } = owner.exports;
const settle = () => new Promise(resolve => setImmediate(resolve));

async function withTimers(run) {
  const originalSet = global.setTimeout;
  const originalClear = global.clearTimeout;
  const timers = new Map();
  let next = 0;
  global.setTimeout = (callback, delay) => {
    assert.equal(delay, 2000);
    const id = ++next;
    timers.set(id, callback);
    return id;
  };
  global.clearTimeout = id => timers.delete(id);
  const tick = () => {
    assert.equal(timers.size, 1);
    const [id, callback] = timers.entries().next().value;
    timers.delete(id);
    callback();
  };
  try { await run(timers, tick); }
  finally { global.setTimeout = originalSet; global.clearTimeout = originalClear; }
}

const response = (phase, updatedAt = 10) => ({ record: { preparation: { phase, updatedAt, elapsedSeconds: 20 } } });

test('stopping an attempt ignores its slow response and leaves no timer', async () => {
  await withTimers(async timers => {
    let finish;
    const snapshots = [];
    const stop = watchRestartPreparation(() => new Promise(resolve => { finish = resolve; }), value => snapshots.push(value));
    stop();
    finish(response('compiling'));
    await settle();
    assert.deepEqual(snapshots, []);
    assert.equal(timers.size, 0);
  });
});

test('slow observations never overlap, and a lost observation retries', async () => {
  await withTimers(async (timers, tick) => {
    let finish;
    let fail;
    let reads = 0;
    const snapshots = [];
    const stop = watchRestartPreparation(() => {
      reads++;
      return new Promise((resolve, reject) => { finish = resolve; fail = reject; });
    }, value => snapshots.push(value));
    assert.equal(reads, 1);
    assert.equal(timers.size, 0);
    fail(new Error('temporary disconnection'));
    await settle();
    tick();
    assert.equal(reads, 2);
    assert.equal(timers.size, 0);
    finish(response('validating'));
    await settle();
    assert.equal(snapshots[0].phase, 'validating');
    stop();
    assert.equal(timers.size, 0);
  });
});

test('unknown and older phases cannot replace the last confirmed phase', async () => {
  await withTimers(async (_timers, tick) => {
    const values = [response('compiling', 10), response('invented', 11), response('checking', 9), response('frontend', 12)];
    const snapshots = [];
    const stop = watchRestartPreparation(async () => values.shift(), value => snapshots.push(value));
    await settle();
    for (let i = 0; i < 3; i++) { tick(); await settle(); }
    assert.deepEqual(snapshots.map(value => value.phase), ['compiling', 'frontend']);
    stop();
  });
});
