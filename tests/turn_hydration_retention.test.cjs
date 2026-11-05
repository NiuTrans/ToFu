const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const { test } = require('node:test');

test('pending hydration retains its latest shell and releases superseded and settled payloads', () => {
  // A fresh process makes GC independent of the test runner. The synthetic
  // host deliberately retains no view; this measures the hydration owner.
  const script = String.raw`
    const path = require('node:path');
    const code = require('esbuild').buildSync({
      entryPoints: [path.join(process.argv[1], 'frontend/src/core/turn-hydration.ts')],
      bundle: true, platform: 'node', format: 'cjs', write: false, logLevel: 'silent',
    }).outputFiles[0].text;
    const loaded = {exports: {}};
    new Function('module', 'exports', 'require', code)(loaded, loaded.exports, require);
    (async () => {
      const calls = [0, 0, 0, 0];
      let release, announce;
      const response = new Promise(resolve => {release = resolve;});
      const started = new Promise(resolve => {announce = resolve;});
      const store = {getState: () => ({conversationId: 'conv', turnsById: {}})};
      const coordinator = {hydrate: () => {calls[0]++; announce(); return response;}};
      const owner = loaded.exports.createTurnHydration({
        applySettings: () => {calls[1]++;}, applySnapshotMetadata: () => {calls[2]++;},
      }, {
        ensureRuntimeStore: () => store, readRuntimeStore: () => store,
        coordinatorFor: () => coordinator, isDisposing: () => false,
        boundConversation: () => undefined, bindConversation: () => {calls[3]++;},
        connectAttempt: () => {throw Error('no live attempt exists');},
      });
      let old = {id: 'conv', bytes: new Uint8Array(1024 * 1024)};
      const oldRef = new WeakRef(old);
      const loading = owner.hydrateConversation(old);
      old = null;
      await started;
      let current = {id: 'conv', bytes: new Uint8Array(1024 * 1024)};
      const currentRef = new WeakRef(current);
      const shared = owner.hydrateConversation(current) === loading;
      current = null;
      for (let i = 0; i < 4; i++) {
        await new Promise(resolve => setImmediate(resolve)); global.gc();
      }
      const supersededRetained = oldRef.deref() !== undefined;
      const pendingRetained = currentRef.deref() !== undefined;
      release({settings: {}, turns: []});
      await loading;
      for (let i = 0; i < 4; i++) {
        await new Promise(resolve => setImmediate(resolve)); global.gc();
      }
      console.log(JSON.stringify({calls, shared, supersededRetained, pendingRetained,
        settledRetained: currentRef.deref() !== undefined,
        ownerAlive: owner.isCurrentConversation({id: 'conv'})}));
    })().catch(error => {console.error(error); process.exitCode = 1;});
  `;
  const result = execFileSync(process.execPath,
    ['--expose-gc', '-e', script, path.resolve(__dirname, '..')],
    {encoding: 'utf8', timeout: 20_000});
  assert.deepEqual(JSON.parse(result.trim()), {
    calls: [1, 1, 1, 1], shared: true, supersededRetained: false,
    pendingRetained: true, settledRetained: false, ownerAlive: true,
  });
});
