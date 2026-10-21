/** Model-picker label contract: the registered Model name beats the pricing-era
 * short name; ids with no registered name still fall back to it. Two Models can
 * share one pricing name (DeepSeek V4 Flash vs V4.1 Flash), so a label taken
 * from the pricing cache alone is ambiguous. */
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const { execFileSync } = require('node:child_process'), { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..'), temp = fs.mkdtempSync(path.join(os.tmpdir(), 'tofu-model-picker-label-'));
const built = path.join(temp, 'owners.cjs');
execFileSync(process.execPath, [path.join(root, 'scripts/vite_test_bundle.mjs'), '--bundle', '--format=cjs', '--platform=node',
  '--loader=ts', '--sourcefile=owners.ts', `--outfile=${built}`], { cwd: root, input: [
  'frontend/src/application-shell/model-picker/index.ts', 'frontend/src/application-shell/model-picker/filter.ts',
  'frontend/src/core/model-display-fold.ts',
].map(p => `export * from ${JSON.stringify(path.join(root, p))};`).join('\n'), stdio: ['pipe', 'pipe', 'pipe'] });
const { createModelPicker, projectModelPickerCatalog } = require(built);
test.after(() => fs.rmSync(temp, { recursive: true, force: true }));

const PRICING_NAMES = { 'deepseek-v4-flash': 'DeepSeek V4 Flash', 'deepseek-v4.1-flash': 'DeepSeek V4.1 Flash' };

function fixture() {
  const dom = new JSDOM('<body><div id="presetWrapper"><div id="presetDropdown"><div id="presetDropdownSearch">'
    + '<input id="presetDropdownSearchInput"></div><div id="presetDropdownList"></div><footer id="thinkingDepthSection">Depth</footer></div></div></body>');
  const w = dom.window, d = w.document;
  const ports = { window: w, document: d, list: d.getElementById('presetDropdownList'),
    selection: () => ({ modelId: 'deepseek-v4.1-flash', preferredProviderId: '', modelRef: null }),
    hiddenModels: () => new Set(), isChatModel: () => true,
    groupKey: model => model.creator_id || 'group', groupLabel: (key, model) => model.creator_name || key,
    compare: (a, b) => String(a.model_id || a).localeCompare(String(b.model_id || b)),
    recentIds: () => [], shortName: id => PRICING_NAMES[id] || id,
    brand: () => 'generic', icon: () => '', text: key => key, select() {}, openSettings() {} };
  return { dom, d, ports };
}

function catalog(displayNames) {
  const ids = Object.keys(PRICING_NAMES);
  return {
    contract_version: 'tofu.model-routing/v2',
    creators: [{ creator_id: 'deepseek', name: 'DeepSeek' }],
    providers: [{ provider_id: 'sankuai', name: 'Meituan' }],
    provider_accesses: [{ provider_access_id: 'access', provider_id: 'sankuai', display_name: 'Meituan', enabled: true }],
    models: ids.map(model_id => ({ creator_id: 'deepseek', model_id, display_name: displayNames[model_id] ?? model_id })),
    offerings: ids.map((model_id, index) => ({ offering_id: `o${index}`, provider_access_id: 'access', enabled: true,
      identity_state: 'confirmed', model: { creator_id: 'deepseek', model_id }, capabilities: ['text'] })),
    deployments: ids.map((_id, index) => ({ offering_id: `o${index}`, wire_model_id: `wire-${index}` })),
  };
}

test('projection carries the registered name and rows render it instead of the pricing label', () => {
  const rows = projectModelPickerCatalog(catalog({ 'deepseek-v4-flash': 'DeepSeek V4 Flash', 'deepseek-v4.1-flash': 'DeepSeek V4.1 Flash' }));
  assert.deepEqual(rows.map(row => row.display_name).sort(), ['DeepSeek V4 Flash', 'DeepSeek V4.1 Flash']);
  const f = fixture(), picker = createModelPicker(f.ports);
  picker.populate(rows);
  const labels = [...f.d.querySelectorAll('.ps-dd-label')].map(node => node.textContent);
  assert.ok(labels.includes('DeepSeek V4 Flash'), labels.join('|'));
  assert.ok(labels.includes('DeepSeek V4.1 Flash'), labels.join('|'));
  picker.destroy(); f.dom.window.close();
});

test('a row with no registered name keeps the pricing fallback and never renders the raw id', () => {
  const value = catalog({ 'deepseek-v4-flash': 'deepseek-v4-flash' });
  const rows = projectModelPickerCatalog(value);
  const f = fixture(), picker = createModelPicker(f.ports);
  picker.populate(rows);
  const labels = [...f.d.querySelectorAll('.ps-dd-label')].map(node => node.textContent);
  assert.ok(labels.includes('DeepSeek V4 Flash'), labels.join('|'));
  assert.ok(!labels.includes('deepseek-v4-flash'), labels.join('|'));
  picker.destroy(); f.dom.window.close();
});
