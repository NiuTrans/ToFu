const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const moduleValue = {exports: {}};
new Function('module', 'exports', ts.transpileModule(fs.readFileSync(
  'frontend/src/features/settings/provider-settings-projection.ts', 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
}).outputText)(moduleValue, moduleValue.exports);
const {createProviderSettingsProjection, providerSupplyRows, providerWireModelIds,
  providerAddableCatalogModels} = moduleValue.exports;

function routing() {
  return {
    providers: [{provider_id: 'p1', name: 'One'}, {provider_id: 'p2', name: 'Two'}],
    provider_accesses: [{provider_id: 'p2', provider_access_id: 'a2'},
      {provider_id: 'p1', provider_access_id: 'a1'}],
    connections: [{provider_access_id: 'a2', connection_id: 'c2'},
      {provider_access_id: 'a1', connection_id: 'c1'}],
    credentials: [{provider_access_id: 'a2', credential_id: 'k2'},
      {provider_access_id: 'a1', credential_id: 'k1', authorization: {
        connection_ids: ['c1'], models: [{creator_id: 'creator', model_id: 'one',
          excluded_wire_model_ids: ['denied']}],
      }}],
    offerings: [
      {provider_access_id: 'a2', offering_id: 'o2', model: {creator_id: 'creator', model_id: 'two'}, identity_state: 'confirmed', capabilities: ['image']},
      {provider_access_id: 'a1', offering_id: 'o1', model: {creator_id: 'creator', model_id: 'one'}, identity_state: 'confirmed', capabilities: ['text']},
    ],
    deployments: [{offering_id: 'o2', deployment_id: 'd2', wire_model_id: 'two'},
      {offering_id: 'orphan', deployment_id: 'd-orphan', wire_model_id: 'orphan'},
      {offering_id: 'o1', deployment_id: 'd1', wire_model_id: 'one'}],
    models: [{creator_id: 'creator', model_id: 'one', display_name: 'First', release_date: '2026-01-01', lifecycle: 'stable', list_pricing: {input: 1, output: 2}},
      {creator_id: 'creator', model_id: 'two', display_name: 'Second'}],
  };
}

test('missing drafts, providers and accesses produce no supply view', () => {
  for (const document of [null, undefined, {}, {providers: [{provider_id: 'p1'}]},
    {provider_accesses: [{provider_id: 'p1', provider_access_id: 'a1'}]}]) {
    assert.equal(createProviderSettingsProjection(document).context('p1'), null);
  }
  assert.deepEqual(providerSupplyRows(null), []);
});

test('provider joins preserve source indices, live row identity and credential grants', () => {
  const document = routing();
  const before = structuredClone(document);
  const projection = createProviderSettingsProjection(document);
  const one = projection.context('p1'), two = projection.context('p2');
  assert.equal(one.provider, document.providers[0]);
  assert.equal(one.access, document.provider_accesses[1]);
  assert.equal(one.accessIndex, 1);
  for (const [table, indices] of [['connections', [1]], ['credentials', [1]], ['offerings', [1]], ['deployments', [2]]]) {
    assert.deepEqual(one[table].map(item => item.index), indices);
    one[table].forEach((item, index) => assert.equal(item.row, document[table][indices[index]]));
  }
  assert.deepEqual(two.deployments.map(item => item.index), [0]);
  assert.equal(one.credentials[0].row.authorization, document.credentials[1].authorization);
  for (const name of ['modelNames', 'modelReleaseDates', 'modelListPricing', 'modelLifecycles']) {
    assert.equal(one[name], two[name]);
  }
  assert.deepEqual(document, before);
});

test('first provider/access selection and deployment order survive duplicate legacy records', () => {
  const document = routing();
  document.providers.push({provider_id: 'p1', name: 'Later'});
  document.provider_accesses.push({provider_id: 'p1', provider_access_id: 'later'});
  document.offerings.push({...document.offerings[1], provider_access_id: 'a2'});
  document.offerings.push({...document.offerings[1], provider_access_id: 'a1'});
  document.deployments.unshift({offering_id: 'o1', deployment_id: 'first', wire_model_id: 'first'});
  const projection = createProviderSettingsProjection(document);
  const one = projection.context('p1'), two = projection.context('p2');
  assert.equal(one.provider, document.providers[0]);
  assert.equal(one.accessIndex, 1);
  assert.deepEqual(one.deployments.map(item => item.index), [0, 3]);
  assert.deepEqual(two.deployments.map(item => item.index), [0, 1, 3]);
  assert.deepEqual(one.offerings.map(item => item.index), [1, 3]);
});

test('single-provider event reads match the full render without exposing other accesses', () => {
  const document = routing();
  const all = createProviderSettingsProjection(document);
  const scoped = createProviderSettingsProjection(document, 'p1');
  assert.deepEqual(scoped.context('p1'), all.context('p1'));
  assert.equal(scoped.context('p2'), null);
  assert.equal(scoped.context('p1').credentials[0].row, document.credentials[1]);
  assert.equal(createProviderSettingsProjection(document, 'missing').context('missing'), null);
  assert.equal(createProviderSettingsProjection(null, 'p1').context('p1'), null);
});

test('catalog metadata keeps the latest populated fields without erasing earlier evidence', () => {
  const document = routing();
  document.models.push({creator_id: 'creator', model_id: 'one', display_name: 'Renamed', lifecycle: 'retired'});
  const context = createProviderSettingsProjection(document).context('p1');
  assert.equal(context.modelNames['creator::one'], 'Renamed');
  assert.equal(context.modelReleaseDates['creator::one'], '2026-01-01');
  assert.equal(context.modelListPricing['creator::one'], document.models[0].list_pricing);
  assert.equal(context.modelLifecycles['creator::one'], 'retired');
  assert.equal(providerSupplyRows(context)[0].retired, true);
});

test('supply rows retain pricing provenance and separate pending identity from catalog facts', () => {
  const document = routing();
  document.offerings.push({...document.offerings[1], offering_id: 'negotiated', actual_pricing: {input: 0, output: 0}});
  document.offerings.push({...document.offerings[1], offering_id: 'pending', identity_state: 'pending_identity', pending_model_id: ' waiting ', actual_pricing: {input: 4, output: 5}});
  document.offerings.push({provider_access_id: 'a1', offering_id: 'unresolved', capabilities: []});
  document.offerings.push({...document.offerings[1], offering_id: 'empty', model: {creator_id: 'creator', model_id: '   '}});
  const rows = providerSupplyRows(createProviderSettingsProjection(document).context('p1'));
  assert.deepEqual(rows.map(row => row.canonical), ['one', 'one', 'waiting', 'unresolved']);
  assert.deepEqual(rows.map(row => row.offeringIndex), [1, 2, 3, 4]);
  assert.equal(rows[0].offering, document.offerings[1]);
  assert.equal(rows[0].pricing, document.models[0].list_pricing);
  assert.equal(rows[0].catalogPrice, true);
  assert.equal(rows[0].releaseDate, '2026-01-01');
  assert.equal(rows[1].pricing, document.offerings[2].actual_pricing);
  assert.equal(rows[1].catalogPrice, false);
  for (const row of rows.slice(2)) {
    assert.equal(row.pending, true);
    assert.equal(row.retired, false);
    assert.equal(row.releaseDate, '');
    assert.equal(row.pricing, null);
    assert.equal(row.catalogPrice, false);
  }
  assert.deepEqual(rows[0].capabilities, ['text']);
  assert.notEqual(rows[0].capabilities, document.offerings[1].capabilities);
});

test('display aliases deduplicate trimmed names using the first global deployment index', () => {
  const document = routing();
  document.deployments[2].wire_model_id = ' one ';
  document.deployments[2].enabled = false;
  document.deployments.push({offering_id: 'o1', wire_model_id: 'one'},
    {offering_id: 'o1', wire_model_id: ' other '}, {offering_id: 'o1', wire_model_id: ' '});
  const context = createProviderSettingsProjection(document).context('p1');
  const row = providerSupplyRows(context)[0];
  assert.deepEqual(row.wireIds, ['one', 'other']);
  assert.deepEqual(row.deploymentByWire, {one: 2, other: 4});
  assert.deepEqual([...providerWireModelIds(context)], [' one ', 'one', ' other ', ' ']);
});

test('picker excludes only retired catalog rows and exact confirmed offered identities', () => {
  const document = routing();
  document.offerings[1].enabled = false;
  document.models.push({creator_id: 'another', model_id: 'one'},
    {creator_id: 'creator', model_id: 'retired', lifecycle: 'retired'});
  document.offerings.push({provider_access_id: 'a1', offering_id: 'pending', identity_state: 'pending_identity', model: {creator_id: 'creator', model_id: 'two'}});
  const context = createProviderSettingsProjection(document).context('p1');
  const models = providerAddableCatalogModels(document, context);
  assert.deepEqual(models.map(model => [model.creator_id, model.model_id]), [['creator', 'two'], ['another', 'one']]);
  assert.equal(models[0], document.models[1]);
});

test('fresh projections observe structural edits without a cross-render cache', () => {
  const document = routing();
  const first = createProviderSettingsProjection(document).context('p1');
  document.connections.push({provider_access_id: 'a1', connection_id: 'added'});
  document.deployments.splice(0, 1);
  document.offerings[1].enabled = false;
  const second = createProviderSettingsProjection(document).context('p1');
  assert.equal(first.connections.length, 1);
  assert.deepEqual(second.connections.map(item => item.index), [1, 2]);
  assert.deepEqual(second.deployments.map(item => item.index), [1]);
  assert.equal(second.offerings[0].row, first.offerings[0].row);
  assert.equal(second.offerings[0].row.enabled, false);
});
