const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const {JSDOM} = require('jsdom');
function owner(name) {
  const exports = {};
  new Function('exports', 'require', ts.transpileModule(fs.readFileSync(`frontend/src/features/settings/${name}.ts`, 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}
  }).outputText)(exports, path => path.endsWith('i18n') ? {t: key => key} : path === '../../ui' ? {
    createButton: options => { const b = global.document.createElement('button'); b.addEventListener('click', options.onClick); return b; }
  } : path.startsWith('./') ? owner(path.slice(2)) : {});
  return exports;
}
for (const reason of ['required selector', 'disabled connection', 'shared connection']) test(`refused ${reason} edit leaves the entire draft unchanged`, async () => {
  const dom = new JSDOM('<div data-request-deployment="d"></div>'); global.document = dom.window.document;
  const draft = {models: [], offerings: [{offering_id:'o',provider_access_id:'a'}], credentials: [],
    deployments: [{deployment_id:'d',offering_id:'o',connection_id:'c',wire_model_id:'old',probe_status:'passed'}],
    connections: [{connection_id:'c',extra_headers:{'x-selector':'required'}}]};
  if (reason === 'shared connection') draft.deployments.push({deployment_id:'other',connection_id:'c'});
  const before = JSON.stringify(draft); const alerts = []; let renders = 0;
  owner('provider-request-names').mountRequestNameEditors(dom.window.document.body, {
    document: () => draft, edit: async () => ({wireModelId:'new',headers:[]}),
    route: async () => { throw Error(reason); }, alert: m => alerts.push(m), render: () => renders++ });
  dom.window.document.querySelector('button').click(); await new Promise(r => setImmediate(r));
  assert.equal(alerts.length, 1); assert.equal(renders, 0); assert.equal(JSON.stringify(draft), before);
  dom.window.close(); delete global.document;
});

test('disabled template face is refused before staging headers or authorization', () => {
  const vm = require('node:vm');
  const source = fs.readFileSync('frontend/src/runtime/sections/settings/provider_render.js','utf8');
  const connection = {connection_id:'disabled',provider_access_id:'a',base_url:'https://fixture.example/v1',enabled:false};
  const draft = {connections:[connection]}; const context = {access:{provider_access_id:'a'}, credentials:[{row:{authorization:{connection_ids:[],models:[]}}}]};
  const scope = {_stgModelRouting:draft,t:key=>key}; vm.createContext(scope); vm.runInContext(source,scope);
  const before = JSON.stringify({draft,context});
  assert.throws(() => scope._ensureV2FaceConnection(context,{base_url:connection.base_url,face:'required',extra_headers:{'x-selector':'required'}}), /requestNameConnectionDisabled/);
  assert.equal(JSON.stringify({draft,context}),before);
});
