const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const en = require('../frontend/src/i18n/locales/en.json');
function loadTs(path, imports = {}) {
  const mod = {exports: {}};
  new Function('module', 'exports', 'require', ts.transpileModule(fs.readFileSync(path, 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
  }).outputText)(mod, mod.exports, name => {
    if (!(name in imports)) throw Error('Unexpected import: ' + name);
    return imports[name];
  });
  return mod.exports;
}
const owner = loadTs('frontend/src/features/settings/manual-matrix-probe.ts', {
  '../../api/errors': loadTs('frontend/src/api/errors.ts'), '../../i18n': {t: key => en[key]},
});
const indexed = row => ({row});
function fixture() {
  const saved = {
    provider_accesses: [{provider_access_id: 'access', provider_id: 'provider'}],
    offerings: [{offering_id: 'off', provider_access_id: 'access', enabled: false}],
    connections: [{connection_id: 'conn', provider_access_id: 'access', enabled: false, base_url: 'https://not-uploaded.example'}],
  };
  const context = {provider: {provider_id: 'provider'}, access: saved.provider_accesses[0],
    offerings: saved.offerings.map(indexed), connections: saved.connections.map(indexed),
    deployments: [indexed({offering_id: 'off', connection_id: 'conn', wire_model_id: 'renamed', enabled: false}),
      indexed({offering_id: 'off', connection_id: 'conn', wire_model_id: 'new-wire'})],
    credentials: [indexed({credential_id: 'z', enabled: false, secret: 'never-upload', authorization: {models: []}}),
      indexed({credential_id: 'a'})],
  };
  return {context, savedJson: JSON.stringify(saved)};
}

test('manual command covers disabled and unsaved wires without changing grants or sending draft authority', () => {
  const {context, savedJson} = fixture();
  const before = JSON.stringify(context);
  const command = owner.buildManualMatrixProbeCommand(context, savedJson, {
    attempts: 2, force: true, only: {key_idxs: [1], model_ids: ['new-wire'], secret: 'ignored'},
  });
  assert.deepEqual(command, {attempts: 2, force: false, credential_ids: ['z', 'a'],
    targets: [{offering_id: 'off', connection_id: 'conn', wire_model_id: 'renamed'},
      {offering_id: 'off', connection_id: 'conn', wire_model_id: 'new-wire'}],
    only: {key_idxs: [1], model_ids: ['new-wire']}});
  assert.equal(JSON.stringify(context), before);
  assert.equal(owner.buildManualMatrixProbeCommand(context, savedJson, {force: true}).force, true);
});

test('new offering/connection references and empty or oversized grids fail closed', () => {
  const {context, savedJson} = fixture();
  for (const field of ['offering_id', 'connection_id']) {
    const draft = structuredClone(context);
    draft.deployments[0].row[field] = 'not-saved';
    assert.throws(() => owner.buildManualMatrixProbeCommand(draft, savedJson), /provider_probe_invalid_target/);
  }
  assert.throws(() => owner.buildManualMatrixProbeCommand(context, ''), /provider_probe_stale_selection/);
  assert.throws(() => owner.buildManualMatrixProbeCommand({...context, deployments: []}, savedJson), /provider_probe_empty_scope/);
  assert.throws(() => owner.buildManualMatrixProbeCommand({...context, credentials: Array(129).fill(context.credentials[0])}, savedJson), /provider_probe_invalid_target/);
  assert.throws(() => owner.buildManualMatrixProbeCommand({...context, deployments: Array(1025).fill(context.deployments[0])}, savedJson), /provider_probe_invalid_target/);
});

test('start errors localize known backend kinds and preserve only public detail for other failures', () => {
  for (const [kind, key] of [['stale_selection', 'StaleSelection'], ['invalid_target', 'InvalidTarget'], ['empty_scope', 'EmptyScope']]) {
    const error = Object.assign(new Error('HTTP 400'), {envelope: {kind: 'provider_probe_' + kind, message: 'raw', detail: 'internal'}});
    assert.equal(owner.manualMatrixProbeStartError(error), 'Probe not started: ' + en['settings.matrixProbe' + key]);
  }
  const error = Object.assign(new Error('HTTP 503'), {envelope: {kind: 'provider_unavailable',
    message: 'Gateway unavailable', detail: 'Try again in 30 seconds', raw: 'SECRET'}});
  assert.equal(owner.manualMatrixProbeStartError(error), 'Probe not started: Gateway unavailable — Try again in 30 seconds');
  assert.equal(owner.manualMatrixProbeStartError(new Error('add failed')), 'Probe not started: add failed');
});

function runtime() {
  const {context, savedJson} = fixture();
  const starts = [], toasts = [];
  let reloads = 0;
  const sandbox = {runtimeScope: {}, getManualMatrixProbe: () => owner.manualMatrixProbe, console,
    _stgModelRoutingSavedJson: savedJson, _stgRoutingPersistChain: null,
    _modelRoutingProviderContext: () => context,
    showToast: text => toasts.push(text), t: key => en[key],
    _loadModelRoutingAuthority: () => { reloads++; return Promise.resolve(true); },
    Api: {modelRouting: {probeCellsStart: async (id, body) => {
      starts.push({id, body}); return {status: 'done', cells: {}, total: 0};
    }}},
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync('frontend/src/runtime/sections/settings/access_matrix.js', 'utf8'), sandbox);
  // Rendering is exercised by the real browser test, not simulated here.
  sandbox._rerenderMatrix = () => {};
  sandbox._matrixModelRows = () => [{wireIds: ['renamed', 'new-wire'], capabilities: ['text']}];
  return {sandbox, starts, toasts, reloads: () => reloads};
}

test('detect awaits existing add-save then starts without saving or reloading authority at completion', async () => {
  const {sandbox, starts, reloads} = runtime();
  let finish;
  sandbox._stgRoutingPersistChain = new Promise(resolve => { finish = resolve; });
  const pending = sandbox._runMatrixProbe('provider', true, {model_ids: ['renamed']});
  assert.equal(starts.length, 0);
  finish();
  await pending;
  assert.equal(starts.length, 1);
  assert.equal(reloads(), 0);
  assert.equal(sandbox._stgMatrixProbe.provider.status, 'done');
});

test('rejected new-model save aborts probe and exposes Probe not started instead of silently continuing', async () => {
  const {sandbox, starts, toasts} = runtime();
  sandbox._stgRoutingPersistChain = Promise.reject(new Error('model add rejected'));
  await sandbox._runMatrixProbe('provider', false);
  assert.equal(starts.length, 0);
  assert.equal(sandbox._stgMatrixProbe.provider.status, 'error');

  assert.equal(sandbox._stgMatrixProbe.provider.start_error, true);
  assert.equal(toasts.at(-1), 'Probe not started: model add rejected');
});
