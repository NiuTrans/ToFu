const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const moduleValue = {exports: {}};
new Function('module', 'exports', 'require', ts.transpileModule(fs.readFileSync(
  'frontend/src/features/settings/provider-request-names.ts', 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
}).outputText)(moduleValue, moduleValue.exports, name => name.endsWith('i18n') ? {t: key => key} : {});
const {suggestedRequestNames, renameRequestName} = moduleValue.exports;

test('exact provider template supplies wire names; another endpoint cannot rename a model', () => {
  const templates = [{base_url: 'https://gateway.example/v1', offering_recipes: [
    {model_id: 'deepseek-v4.1-flash', request_ids: ['deepseek-v4-flash', 'deepseek-flash']},
  ]}];
  assert.deepEqual(suggestedRequestNames('deepseek-v4.1-flash', ['https://gateway.example/v1/'], templates),
    ['deepseek-v4-flash', 'deepseek-flash']);
  assert.deepEqual(suggestedRequestNames('deepseek-v4.1-flash', ['https://another.example/v1'], templates),
    ['deepseek-v4.1-flash']);
});

test('wire edit preserves identity and denied grants, invalidates probe, rejects duplicates atomically', () => {
  const document = {offerings: [{offering_id: 'supply', provider_access_id: 'access', model: {creator_id: 'creator', model_id: 'model'}}],
    deployments: [{deployment_id: 'first', offering_id: 'supply', wire_model_id: 'model', probe_status: 'passed'},
      {deployment_id: 'second', offering_id: 'supply', wire_model_id: 'occupied'}],
    credentials: [{provider_access_id: 'access', authorization: {models: [{creator_id: 'creator', model_id: 'model', excluded_wire_model_ids: ['model']}]}}]};
  renameRequestName(document, 'first', 'wire');
  assert.equal(document.offerings[0].model.model_id, 'model');
  assert.equal(document.deployments[0].probe_status, 'unprobed');
  assert.deepEqual(document.credentials[0].authorization.models[0].excluded_wire_model_ids, ['wire']);
  const before = JSON.stringify(document);
  assert.throws(() => renameRequestName(document, 'first', 'occupied'), /Duplicate/);
  assert.throws(() => renameRequestName(document, 'first', ''), /Invalid/);
  assert.equal(JSON.stringify(document), before);
});
