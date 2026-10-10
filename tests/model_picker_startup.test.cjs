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
