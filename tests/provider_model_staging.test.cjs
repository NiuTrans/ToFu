const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
function load(file, imports = {}) {
  const mod = {exports: {}};
  new Function('module', 'exports', 'require', ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
  }).outputText)(mod, mod.exports, name => {
    assert.ok(name in imports, 'unexpected dependency ' + name);
    return imports[name];
  });
  return mod.exports;
}
const base = 'frontend/src/features/settings/';
const {addProviderModels} = load(base + 'provider-model-staging.ts', {
  './provider-settings-projection': load(base + 'provider-settings-projection.ts'),
  './provider-connection-staging': load(base + 'provider-connection-staging.ts'),
});

function fixture() {
  const document = {providers: [{provider_id: 'p'}], provider_accesses: [{provider_id: 'p', provider_access_id: 'a'}],
    connections: [{connection_id: 'default', provider_access_id: 'a', base_url: 'https://synthetic.invalid',
      protocol: 'openai', enabled: true, extra_headers: {'x-owner': 'keep'}}],
    credentials: [{credential_id: 'key', provider_access_id: 'a', authorization: {connection_ids: ['default'],
      models: [{creator_id: 'creator', model_id: 'existing', excluded_wire_model_ids: ['denied']}]}}],
    models: ['first', 'second'].map(model_id => ({creator_id: 'creator', model_id, capabilities: ['text'], context_window: 1000})),
    offerings: [], deployments: []};
  let id = 0;
  const events = [];
  const ports = {document: () => document, readCapabilities: async () => ({metadata_inheritance_supported: true}),
    planRequestNames: async () => ({}), planFaces: async () => ({first: {
      base_url: 'https://synthetic.invalid', face: 'required', protocol: 'openai', extra_headers: {'x-selector': 'required'}},
      second: {base_url: 'https://second.synthetic.invalid', protocol: 'openai', extra_headers: {}}}),
    newId: prefix => prefix + '-' + (++id), disabledConnection: row => new Error('disabled:' + row.connection_id),
    selectionChanged: () => new Error('selection changed'), missingConnection: () => events.push('missing'),
    render: () => events.push(['render', document.offerings.length, document.deployments.length]),
    persist: async () => events.push('persist')};
  return {document, ports, events};
}

test('failure while preparing a later deployment leaves all source rows and grants untouched', async () => {
  const {document, ports, events} = fixture();
  const before = structuredClone(document);
  const connection = document.connections[0], headers = connection.extra_headers;
  const authorization = document.credentials[0].authorization;
  const makeId = ports.newId;
  let deployments = 0;
  ports.newId = prefix => {
    if (prefix === 'deployment' && ++deployments === 2) throw new Error('id failure');
    return makeId(prefix);
  };
  await assert.rejects(addProviderModels('p', document.models, [], ports), /id failure/);
  assert.deepEqual(document, before);
  assert.equal(document.connections[0], connection);
  assert.equal(connection.extra_headers, headers);
  assert.equal(document.credentials[0].authorization, authorization);
  assert.deepEqual(events, []);
});

test('failed save retains the complete batch and newer user edits for the existing authority to retry', async () => {
  const {document, ports, events} = fixture();
  const connection = document.connections[0], credential = document.credentials[0];
  const existingGrant = credential.authorization.models[0];
  ports.persist = async () => {
    events.push('persist');
    document.providers[0].name = 'edit during save';
    throw new Error('CAS conflict');
  };
  await assert.rejects(addProviderModels('p', document.models, [], ports), /CAS conflict/);
  assert.deepEqual(events, [['render', 2, 2], 'persist']);
  assert.equal(document.providers[0].name, 'edit during save');
  assert.equal(document.connections[0], connection);
  assert.equal(document.credentials[0], credential);
  assert.equal(credential.authorization.models[0], existingGrant);
  assert.deepEqual(existingGrant.excluded_wire_model_ids, ['denied']);
  assert.equal(document.connections.length, 2);
  assert.equal(connection.extra_headers['x-owner'], 'keep');
  assert.equal(connection.extra_headers['x-selector'], 'required');
  assert.deepEqual(document.offerings.map(row => row.model.model_id), ['first', 'second']);
  assert.deepEqual(document.deployments.map(row => row.wire_model_id), ['first', 'second']);
  assert.ok(document.deployments.every(row => credential.authorization.connection_ids.includes(row.connection_id)));
  assert.ok(document.deployments.every(row => row.probe_status === 'unprobed' && !('enabled' in row)));
});
