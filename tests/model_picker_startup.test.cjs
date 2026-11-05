const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const moduleUnderTest = { exports: {} };
new Function('module', 'exports', ts.transpileModule(
  fs.readFileSync('frontend/src/features/model-catalog/model-picker-startup.ts', 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
).outputText)(moduleUnderTest, moduleUnderTest.exports);
const { createModelPickerStartup } = moduleUnderTest.exports;

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function setup() {
  const config = [], routing = [], applied = [], failures = [];
  let onBootReady = null, onActivity = null, disposed = 0, probe = async () => false;
  let active = true;
  const signals = [];
  const timers = new Map(), delays = [];
  let nextTimer = 0;
  const picker = createModelPickerStartup({
    loadConfig: signal => { signals.push(signal); const next = deferred(); config.push(next); return next.promise; },
    loadRouting: () => { const next = deferred(); routing.push(next); return next.promise; },
    applyConfig: value => applied.push(['config', value]),
    applyRouting: value => applied.push(['routing', value]),
    routingRevision: value => value?.revision,
    showFailure: error => failures.push(error),
    onBootReady: callback => { onBootReady = callback; return () => { disposed++; }; },
    isActive: () => active,
    onActivityChange: callback => { onActivity = callback; return () => { disposed++; }; },
    scheduleRetry: (callback, delay) => { delays.push(delay); timers.set(++nextTimer, callback); return nextTimer; },
    cancelRetry: handle => timers.delete(handle),
    probeBootReady: () => probe(),
  });
  return { picker, config, routing, applied, failures,
    ready: () => onBootReady(), setProbe: next => { probe = next; },
    restored: () => picker.recover(), timers, delays, signals,
    activity: value => { active = value; onActivity(); },
    retry: () => { const [id, callback] = timers.entries().next().value; timers.delete(id); callback(); },
    isDisposed: () => disposed === 2 };
}
const bootError = { status: 503, code: 'server_booting' };

test('settled config coordination releases the response payload while the picker remains alive', () => {
  const compiled = ts.transpileModule(
    fs.readFileSync('frontend/src/features/model-catalog/model-picker-startup.ts', 'utf8'),
    {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}},
  ).outputText;
  const script = `
    const moduleUnderTest = {exports: {}};
    new Function('module', 'exports', ${JSON.stringify(compiled)})(moduleUnderTest, moduleUnderTest.exports);
    (async () => {
      let payload = {bytes: new Uint8Array(1024 * 1024)};
      const reference = new WeakRef(payload), calls = [0, 0, 0, 0];
      const owner = moduleUnderTest.exports.createModelPickerStartup({
        loadConfig: async () => {calls[0]++; return payload;},
        loadRouting: async () => {calls[1]++; return {};},
        applyConfig: value => {if (value.bytes.length === 1024 * 1024) calls[2]++;},
        applyRouting: () => {calls[3]++;},
        showFailure: error => {throw error;}, onBootReady: () => () => {},
      });
      await owner.load();
      payload = null;
      let older = {bytes: new Uint8Array(1024 * 1024)};
      let queued = {bytes: new Uint8Array(1024 * 1024)};
      const olderRef = new WeakRef(older), queuedRef = new WeakRef(queued);
      const waitingOwner = moduleUnderTest.exports.createModelPickerStartup({
        loadConfig: () => {throw Error('unsolicited config read');},
        loadRouting: () => {throw Error('unsolicited catalog read');},
        applyConfig: () => {}, applyRouting: () => {throw Error('policy is not ready');},
        showFailure: error => {throw error;}, onBootReady: () => () => {},
      });
      waitingOwner.adoptRouting(older);
      waitingOwner.adoptRouting(queued);
      older = queued = null;
      for (let turn = 0; turn < 4; turn++) {
        await new Promise(resolve => setImmediate(resolve));
        global.gc();
      }
      const olderQueuedRetained = olderRef.deref() !== undefined;
      const latestQueuedRetained = queuedRef.deref() !== undefined;
      waitingOwner.destroy();
      for (let turn = 0; turn < 4; turn++) {
        await new Promise(resolve => setImmediate(resolve));
        global.gc();
      }
      console.log(JSON.stringify({calls, retained: reference.deref() !== undefined,
        olderQueuedRetained, latestQueuedRetained,
        disposedQueuedRetained: queuedRef.deref() !== undefined,
        postDisposeAccepted: waitingOwner.adoptRouting({})}));
      owner.destroy();
    })().catch(error => {console.error(error); process.exitCode = 1;});
  `;
  const result = require('node:child_process').execFileSync(process.execPath,
    ['--expose-gc', '-e', script], {encoding: 'utf8', timeout: 20_000});
  assert.deepEqual(JSON.parse(result.trim()), {calls: [1, 1, 1, 1], retained: false,
    olderQueuedRetained: false, latestQueuedRetained: true,
    disposedQueuedRetained: false, postDisposeAccepted: false});
});

test('committed routing uses existing config without reads and cannot regress its revision', async () => {
  const s = setup();
  const configured = s.picker.refreshConfig();
  s.config[0].resolve('initial policy');
  await configured;
  s.applied.length = 0;
  const committed = { revision: 4, controls: ['low', 'ultra'] };
  assert.equal(s.picker.adoptRouting(committed), true);
  assert.equal(s.picker.adoptRouting({ revision: 3 }), false);
  assert.deepEqual(s.applied, [['routing', committed]]);
  const refreshed = { revision: 4, availability: 'no_route' };
  assert.equal(s.picker.adoptRouting(refreshed), true);
  assert.equal(s.applied.at(-1)[1], refreshed);
  assert.equal(s.config.length, 1);
  assert.equal(s.routing.length, 0);
  s.picker.destroy();
  s.picker.destroy();
  assert.equal(s.isDisposed(), true);
  assert.equal(s.picker.adoptRouting({ revision: 5 }), false);
  assert.equal(s.applied.length, 2);
});

for (const outcome of ['success', 'failure']) {
  test(`committed routing supersedes a pending catalog ${outcome} but keeps config policy`, async () => {
    const s = setup();
    const loading = s.picker.load();
    const committed = { revision: 4 };
    s.picker.adoptRouting(committed);
    assert.deepEqual(s.applied, [], 'initial config policy must precede committed presentation');
    s.config[0].resolve('policy');
    if (outcome === 'success') s.routing[0].resolve({ revision: 1 });
    else s.routing[0].reject({ status: 503 });
    await loading;
    assert.deepEqual(s.applied, [['config', 'policy'], ['routing', committed]]);
    assert.deepEqual(s.failures, []);
    assert.equal(s.timers.size, 0);
    assert.equal(s.routing.length, 1);
    s.picker.destroy();
  });
}

test('adopting committed routing cannot hide a current config authorization failure', async () => {
  const s = setup();
  const loading = s.picker.load();
  s.picker.adoptRouting({ revision: 4 });
  const denied = { status: 403 };
  s.config[0].reject(denied);
  s.routing[0].resolve({ revision: 1 });
  await loading;
  assert.deepEqual(s.failures, [denied]);
  assert.deepEqual(s.applied, [], 'an unauthorized initial policy cannot expose queued catalog state');
  assert.equal(s.timers.size, 0);
  s.picker.destroy();
});

test('pre-startup publications wait for policy without reading and disposal drops them', async () => {
  const s = setup();
  s.picker.adoptRouting({ revision: 1 });
  s.picker.adoptRouting({ revision: 2 });
  assert.deepEqual(s.applied, []);
  assert.equal(s.config.length + s.routing.length, 0);
  s.picker.destroy();
  await s.picker.refreshConfig();
  assert.deepEqual(s.applied, []);
  assert.equal(s.config.length + s.routing.length, 0);
});

for (const outcome of ['success', 'failure']) {
  test(`new config refresh supersedes older startup config ${outcome}`, async () => {
    const s = setup();
    const loading = s.picker.load();
    const refreshing = s.picker.refreshConfig();
    await Promise.resolve();
    s.config[1].resolve('new policy');
    await refreshing;
    if (outcome === 'success') s.config[0].resolve('old policy');
    else s.config[0].reject({ status: 503 });
    s.routing[0].resolve({ revision: 1 });
    await loading;
    assert.deepEqual(s.applied, [['config', 'new policy'], ['routing', { revision: 1 }]]);
    assert.deepEqual(s.failures, []);
    assert.equal(s.timers.size, 0);
    s.picker.destroy();
  });
}

test('superseded config-refresh failure cannot start another full catalog read', async () => {
  const s = setup();
  const first = s.picker.refreshConfig();
  await Promise.resolve();
  const second = s.picker.refreshConfig();
  await Promise.resolve();
  s.config[1].resolve('current');
  await second;
  const aborted = { code: 'aborted' };
  s.config[0].reject(aborted);
  await assert.rejects(first, error => error === aborted);
  assert.equal(s.routing.length, 0);
  assert.equal(s.config.length, 2);
  assert.deepEqual(s.applied, [['config', 'current']]);
  s.picker.destroy();
});

test('startup joins the latest pending config before applying its catalog', async () => {
  const s = setup();
  const loading = s.picker.load();
  const refreshing = s.picker.refreshConfig();
  s.config[0].resolve('old policy');
  s.routing[0].resolve({ revision: 2 });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(s.applied, []);
  s.config[1].resolve('current policy');
  await Promise.all([loading, refreshing]);
  assert.deepEqual(s.applied, [['config', 'current policy'], ['routing', { revision: 2 }]]);
  s.picker.destroy();
});

test('a pre-commit read may still carry a provably newer server revision', async () => {
  const s = setup();
  const loading = s.picker.load();
  s.picker.adoptRouting({ revision: 4 });
  s.config[0].resolve('policy');
  s.routing[0].resolve({ revision: 5 });
  await loading;
  assert.deepEqual(s.applied.at(-1), ['routing', { revision: 5 }]);
  s.picker.destroy();
});

test('parallel reads, one in-flight pair, and config policy applied before routing', async () => {
  const s = setup();
  const first = s.picker.load();
  assert.equal(s.picker.load(), first);
  assert.equal(s.config.length, 1);
  assert.equal(s.routing.length, 1);
  s.routing[0].resolve('catalog');
  await Promise.resolve();
  assert.deepEqual(s.applied, []);
  s.config[0].resolve('hidden-policy');
  await first;
  assert.deepEqual(s.applied, [['config', 'hidden-policy'], ['routing', 'catalog']]);
  s.picker.destroy();
  assert.equal(s.isDisposed(), true);
});

test('config policy paints as soon as its response arrives; routing remains pending', async () => {
  const s = setup();
  const first = s.picker.load();
  s.config[0].resolve('policy');
  await Promise.resolve();
  assert.deepEqual(s.applied, [['config', 'policy']]);
  s.routing[0].resolve('catalog');
  await first;
  assert.deepEqual(s.applied, [['config', 'policy'], ['routing', 'catalog']]);
  s.picker.destroy();
});

test('boot-gate 503 waits for readiness and retries both requests once', async () => {
  const s = setup();
  const first = s.picker.load();
  s.config[0].reject(bootError);
  s.routing[0].reject(bootError);
  await first;
  assert.equal(s.config.length, 1);
  assert.deepEqual(s.failures, []);
  s.ready();
  assert.equal(s.config.length, 2);
  assert.equal(s.routing.length, 2);
  s.config[1].resolve('policy');
  s.routing[1].resolve('models-and-reasoning');
  await s.picker.load();
  assert.deepEqual(s.applied, [['config', 'policy'], ['routing', 'models-and-reasoning']]);
  s.picker.destroy();
});

test('missed ready signal is recovered by a single readiness probe; repeated 503 never spins', async () => {
  const s = setup();
  s.setProbe(async () => true);
  const first = s.picker.load();
  s.config[0].reject(bootError);
  s.routing[0].reject(bootError);
  await first;
  await Promise.resolve();
  assert.equal(s.config.length, 2);
  s.config[1].reject(bootError);
  s.routing[1].reject(bootError);
  await s.picker.load();
  assert.equal(s.config.length, 2);
  assert.deepEqual(s.failures, []);
  assert.equal(s.timers.size, 1);
  assert.deepEqual(s.delays, [1000]);
  s.picker.destroy();
});

test('routing failure keeps applied server config; non-boot failure is visible and manually retryable', async () => {
  const s = setup();
  const first = s.picker.load();
  const networkError = { status: 503, code: 'backend_unavailable' };
  s.config[0].resolve('policy');
  s.routing[0].reject(networkError);
  await first;
  assert.deepEqual(s.applied, [['config', 'policy']]);
  assert.deepEqual(s.failures, [networkError]);
  const retry = s.picker.load();
  assert.equal(s.config.length, 2);
  s.config[1].resolve('updated-policy');
  s.routing[1].resolve({ model_routing: [], reasoning_controls: [] });
  await retry;
  assert.equal(s.applied[2][0], 'routing');
  s.picker.destroy();
});

test('destroy removes readiness listener and late responses cannot repaint', async () => {
  const s = setup();
  const first = s.picker.load();
  s.picker.destroy();
  s.config[0].resolve('policy');
  s.routing[0].resolve('catalog');
  await first;
  s.ready();
  assert.deepEqual(s.applied, []);
  assert.equal(s.config.length, 1);
  assert.equal(s.isDisposed(), true);
});

for (const error of [new TypeError('Failed to fetch'), { status: 0 },
  { status: 502 }, { status: 503, code: 'database_unavailable' }, { status: 504 },
  { status: 200, code: 'network' }, { code: 'timeout' }]) {
  test(`transient ${error.status ?? 'fetch'} failure retries without a boot event or reload`, async () => {
    const s = setup();
    const first = s.picker.load();
    s.config[0].reject(error);
    s.routing[0].reject(error);
    await first;
    assert.equal(s.timers.size, 1);
    assert.deepEqual(s.delays, [1000]);
    s.retry();
    const retry = s.picker.load();
    assert.equal(s.config.length, 2);
    s.config[1].resolve('policy');
    s.routing[1].resolve('catalog');
    await retry;
    assert.equal(s.timers.size, 0);
    assert.equal(s.applied.at(-1)[1], 'catalog');
    s.picker.destroy();
  });
}

test('readiness arriving before an in-flight failure is not lost', async () => {
  const s = setup();
  const first = s.picker.load();
  s.ready();
  s.config[0].reject(bootError);
  s.routing[0].reject(bootError);
  await first;
  assert.equal(s.config.length, 2);
  assert.equal(s.timers.size, 0);
  s.config[1].resolve('policy');
  s.routing[1].resolve('catalog');
  await s.picker.load();
  s.picker.destroy();
});

test('backend-restored joins recovery and cancels the pending timer', async () => {
  const s = setup();
  const first = s.picker.load();
  s.config[0].reject({ status: 503 });
  s.routing[0].reject({ status: 503 });
  await first;
  s.restored();
  s.ready();
  assert.equal(s.config.length, 2);
  assert.equal(s.timers.size, 0);
  s.config[1].resolve('policy');
  s.routing[1].resolve('catalog');
  await s.picker.load();
  s.picker.destroy();
});

test('persistent outage exhausts six capped-backoff retries and can resume on real recovery', async () => {
  const s = setup();
  for (let i = 0; i < 7; i++) {
    const pending = s.picker.load(); // joins the retry already in flight
    s.config[i].reject({ status: 503 });
    s.routing[i].reject({ status: 503 });
    await pending;
    assert.equal(s.timers.size, i < 6 ? 1 : 0);
    if (i < 6) s.retry();
  }
  assert.deepEqual(s.delays, [1000, 2000, 4000, 8000, 16000, 30000]);
  s.restored();
  assert.equal(s.config.length, 8);
  s.config[7].reject({ status: 503 });
  s.routing[7].reject({ status: 503 });
  await s.picker.load();
  assert.equal(s.timers.size, 1);
  s.picker.destroy();
  assert.equal(s.timers.size, 0);
  s.ready();
  s.restored();
  assert.equal(s.config.length, 8);
});

test('authorization failure is not retried even if the other request lost transport', async () => {
  const s = setup();
  const first = s.picker.load();
  s.config[0].reject({ status: 503 });
  s.routing[0].reject({ status: 403 });
  await first;
  s.ready();
  s.restored();
  assert.equal(s.config.length, 1);
  assert.equal(s.timers.size, 0);
  assert.deepEqual(s.failures, [{ status: 403 }]);
  s.picker.destroy();
});


for (const error of [{ status: 0, code: 'aborted' }, { status: 401 }, { status: 403 }]) {
  test(`permanent ${error.code || error.status} failure never polls`, async () => {
    const s = setup();
    const pending = s.picker.load();
    s.config[0].reject(error);
    s.routing[0].reject({ code: 'network' });
    await pending;
    s.ready();
    s.restored();
    assert.equal(s.config.length, 1);
    assert.equal(s.timers.size, 0);
    s.picker.destroy();
  });
}

test('hidden page cancels retry; becoming active recovers automatically', async () => {
  const s = setup();
  const pending = s.picker.load();
  s.config[0].reject({ code: 'network' });
  s.routing[0].reject({ code: 'network' });
  await pending;
  s.activity(false);
  assert.equal(s.timers.size, 0);
  s.ready();
  assert.equal(s.config.length, 1);
  s.activity(true);
  assert.equal(s.config.length, 2);
  s.config[1].resolve('policy');
  s.routing[1].resolve('catalog');
  await s.picker.load();
  s.picker.destroy();
});

test('destroy aborts hanging reads without waiting for a response', () => {
  const s = setup();
  void s.picker.load();
  assert.equal(s.signals[0].aborted, false);
  s.picker.destroy();
  assert.equal(s.signals[0].aborted, true);
});

test('stale false readiness probe cannot erase a true ready event', async () => {
  const s = setup();
  const probe = deferred();
  s.setProbe(() => probe.promise);
  const pending = s.picker.load();
  s.config[0].reject(bootError);
  s.routing[0].reject(bootError);
  await pending;
  s.ready();
  probe.resolve(false);
  s.config[1].resolve('policy');
  s.routing[1].resolve('catalog');
  await s.picker.load();
  assert.equal(s.applied.at(-1)[1], 'catalog');
  assert.equal(s.timers.size, 0);
  s.picker.destroy();
});

for (const readyBeforeFailure of [false, true]) {
  test(`failed boot waits without polling and recovers on successful retry (early=${readyBeforeFailure})`, async () => {
    const s = setup();
    const pending = s.picker.load();
    if (readyBeforeFailure) s.ready();
    s.config[0].reject({ status: 503, code: 'server_boot_failed' });
    s.routing[0].reject({ status: 503, code: 'server_boot_failed' });
    await pending;
    if (!readyBeforeFailure) {
      s.restored();
      s.activity(false);
      s.activity(true);
      assert.equal(s.config.length, 1);
      assert.equal(s.timers.size, 0);
      // Hidden readiness must survive until the visible wake.
      s.activity(false);
      s.ready();
      assert.equal(s.config.length, 1);
      s.activity(true);
    }
    assert.equal(s.config.length, 2);
    s.config[1].resolve('policy');
    s.routing[1].resolve('catalog');
    await s.picker.load();
    assert.equal(s.applied.at(-1)[1], 'catalog');
    assert.equal(s.timers.size, 0);
    s.picker.destroy();
  });
}

test('an ordinary restore arriving before failed boot does not bypass the boot latch', async () => {
  const s = setup();
  const pending = s.picker.load();
  s.restored();
  s.config[0].reject({ status: 503, code: 'server_boot_failed' });
  s.routing[0].reject({ status: 503, code: 'server_boot_failed' });
  await pending;
  assert.equal(s.config.length, 1);
  assert.equal(s.timers.size, 0);
  s.picker.destroy();
});

for (const status of [401, 403]) {
  for (const denialInConfig of [false, true]) {
    test(`authorization ${status} wins over other permanent failures (config=${denialInConfig})`, async () => {
      const s = setup();
      const first = s.picker.load();
      s.config[0].resolve('policy');
      s.routing[0].resolve('catalog');
      await first;
      const refresh = s.picker.load();
      const denied = { status };
      s.config[1].reject(denialInConfig ? denied : { status: 500 });
      s.routing[1].reject(denialInConfig ? { status: 500 } : denied);
      await refresh;
      assert.deepEqual(s.failures, [denied]);
      s.ready();
      s.restored();
      assert.equal(s.config.length, 2);
      assert.equal(s.timers.size, 0);
      s.picker.destroy();
    });
  }
}
