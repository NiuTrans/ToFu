/** Model-picker contracts: canonical projection, linear search, fresh preference and disposed/stale action fencing. */
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const { execFileSync } = require('node:child_process'), { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..'), temp = fs.mkdtempSync(path.join(os.tmpdir(), 'tofu-model-picker-'));
const built = path.join(temp, 'owners.cjs');
execFileSync(process.execPath, [path.join(root, 'scripts/vite_test_bundle.mjs'), '--bundle', '--format=cjs', '--platform=node',
  '--loader=ts', '--sourcefile=owners.ts', `--outfile=${built}`], { cwd: root, input: [
  'frontend/src/application-shell/model-picker/index.ts', 'frontend/src/application-shell/model-picker/filter.ts',
  'frontend/src/core/model-display-fold.ts',
].map(p => `export * from ${JSON.stringify(path.join(root, p))};`).join('\n'), stdio: ['pipe', 'pipe', 'pipe'] });
const { createModelPicker, projectModelPickerCatalog, createModelPickerFilter, modelDisplayUnits } = require(built);
test.after(() => fs.rmSync(temp, { recursive: true, force: true }));
function fixture() {
  const dom = new JSDOM('<body><div id="presetWrapper"><div id="presetDropdown"><div id="presetDropdownSearch">'
    + '<input id="presetDropdownSearchInput"></div><div id="presetDropdownList"></div><footer id="thinkingDepthSection">Depth</footer></div></div></body>');
  const w = dom.window, d = w.document, picks = []; let selection = { modelId: 'a', preferredProviderId: '', modelRef: null };
  const ports = { window: w, document: d, list: d.getElementById('presetDropdownList'), selection: () => selection, hiddenModels: () => new Set(), isChatModel: () => true,
    groupKey: model => model.creator_id || 'group', groupLabel: (key, model) => model.creator_name || key,
    compare: (a, b) => String(a.model_id || a).localeCompare(String(b.model_id || b)), fold: modelDisplayUnits,
    recentIds: () => [], shortName: id => id, brand: () => 'generic', icon: () => '', text: key => key,
    select: model => { picks.push(model); selection = { modelId: model.model_id, preferredProviderId: model.provider_id || '',
      modelRef: model.pending_identity ? { provider_id: model.provider_id, offering_id: model.offering_id }
        : model.routing_v2 ? { creator_id: model.creator_id, model_id: model.model_id } : null }; }, openSettings() {} };
  return { dom, w, d, picks, ports, setSelection: value => { selection = value; } };
}
const v2 = creator => ({ model_id: 'a', creator_id: creator, creator_name: creator, routing_v2: true, provider_options: [
  { provider_id: 'p1', provider_name: 'Provider One', offering_id: 'o1' },
  { provider_id: 'p2', provider_name: 'Provider Two', offering_id: 'o2' },
] });

test('canonical projection unions capability metadata once and preserves distinct creator/model identities and pending offerings', () => {
  const value = { contract_version: 'tofu.model-routing/v2', creators: [{ creator_id: 'c1', name: 'Creator' }],
    models: [{ creator_id: 'c1', model_id: 'a' }, { creator_id: 'c2', model_id: 'a' }],
    providers: [{ provider_id: 'p', name: 'Provider' }], provider_accesses: [{ provider_access_id: 'access', provider_id: 'p', enabled: true }],
    offerings: [
      { offering_id: 'one', provider_access_id: 'access', enabled: true, identity_state: 'confirmed', model: { creator_id: 'c1', model_id: 'a' }, capabilities: ['thinking'] },
      { offering_id: 'two', provider_access_id: 'access', enabled: true, identity_state: 'confirmed', model: { creator_id: 'c1', model_id: 'a' }, capabilities: ['chat', 'thinking'] },
      { offering_id: 'three', provider_access_id: 'access', enabled: true, identity_state: 'confirmed', model: { creator_id: 'c2', model_id: 'a' }, capabilities: ['chat'] },
      { offering_id: 'pending', provider_access_id: 'access', enabled: true, identity_state: 'pending_identity', pending_model_id: 'draft', capabilities: [] },
      { offering_id: 'stale', provider_access_id: 'access', enabled: true, stale: true, identity_state: 'pending_identity', pending_model_id: 'old', capabilities: [] },
      { offering_id: 'unsupplied', provider_access_id: 'access', enabled: true, identity_state: 'pending_identity', pending_model_id: 'missing', capabilities: [] },
    ], deployments: ['one', 'two', 'three', 'pending', 'stale'].map(offering_id => ({ offering_id })) };
  const before = JSON.stringify(value), rows = projectModelPickerCatalog(value);
  assert.equal(JSON.stringify(value), before); assert.equal(rows.length, 3);
  assert.deepEqual(rows[0].capabilities, ['chat', 'thinking']); assert.equal(rows[0].provider_options.length, 2);
  assert.equal(rows[1].creator_id, 'c2'); assert.equal(rows[2].pending_identity, true); assert.equal(rows[2].offering_id, 'pending');
  assert.deepEqual(projectModelPickerCatalog(null), []); assert.deepEqual(projectModelPickerCatalog({ contract_version: 'other' }), []);
});

test('provider reopening reads the latest preference; keyboard dismissal returns focus; same-name creators stay independent', () => {
  const f = fixture(), picker = createModelPicker(f.ports); picker.populate([v2('c1'), v2('c2')]);
  const rows = f.d.querySelectorAll('.preset-dropdown-item'), chip = rows[0].querySelector('.ps-dd-provider-chip');
  chip.click(); f.d.querySelectorAll('.ps-dd-provider-menu-item')[2].click(); assert.equal(f.picks[0].provider_id, 'p2');
  assert.equal(rows[0].querySelector('.ps-dd-provider-chip-label').textContent, 'Provider Two');
  assert.equal(rows[1].querySelector('.ps-dd-provider-chip-label').textContent, '自动服务商');
  assert.ok(rows[0].classList.contains('active')); assert.ok(!rows[1].classList.contains('active'));
  chip.click(); const selected = f.d.querySelector('.ps-dd-provider-menu-item.active');
  assert.equal(selected.textContent, 'Provider Two'); assert.ok(selected.querySelector('.ps-dd-provider-check svg')); assert.equal(f.d.activeElement, selected);
  selected.dispatchEvent(new f.w.KeyboardEvent('keydown', { key: 'Home', bubbles: true, cancelable: true }));
  assert.equal(f.d.activeElement, f.d.querySelector('.ps-dd-provider-menu-item'));
  f.d.activeElement.dispatchEvent(new f.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
  assert.equal(f.d.getElementById('psProviderMenu'), null); assert.equal(f.d.activeElement, chip);
  picker.destroy(); f.dom.window.close();
});

test('search remains linear with arbitrary section IDs; repeated queries visit no nodes and never interpolate selectors', () => {
  const f = fixture(), index = createModelPickerFilter(); const size = 1000, elements = [];
  for (let i = 0; i < size; i++) {
    const element = f.d.createElement('div'); elements.push(element); index.addRow({ element, section: `group"${i}`, haystack: `model ${i}` });
    index.addLabel(`group"${i}`, f.d.createElement('div'));
  }
  assert.equal(index.apply('999'), size); assert.equal(index.firstVisible(), elements[999]); assert.equal(elements[0].style.display, 'none'); assert.equal(index.apply('999'), 0);
  assert.equal(index.apply(''), size); f.dom.window.close();
});

test('render indices preserve folds, recents-independent selection, footer and prototype-like IDs; stale rows cannot select', () => {
  const f = fixture(); f.setSelection({ modelId: 'mirror', preferredProviderId: '', modelRef: null });
  f.ports.groupKey = () => '__proto__';
  const picker = createModelPicker(f.ports); picker.populate([
    { model_id: 'a', fold_group: 'alias', fold_canonical: 'a' }, { model_id: 'mirror', fold_group: 'alias', fold_canonical: 'a' },
  ]);
  const old = f.d.querySelector('.ps-dd-model-choice');
  assert.equal(f.d.getElementById('thinkingDepthSection').textContent, 'Depth');
  const sub = f.d.querySelector('.ps-dd-sub'), badge = f.d.querySelector('.ps-dd-fold-badge');
  assert.ok(sub.classList.contains('open')); assert.equal(badge.tagName, 'BUTTON'); assert.equal(badge.getAttribute('aria-expanded'), 'true');
  badge.click(); assert.ok(!sub.classList.contains('open'));
  picker.filter('mirror'); assert.ok(sub.classList.contains('search-open')); assert.equal(badge.getAttribute('aria-expanded'), 'true');
  picker.filter(''); assert.ok(!sub.classList.contains('search-open')); assert.ok(!sub.classList.contains('open'));
  picker.populate([v2('current')]); old.click(); assert.equal(f.picks.length, 0);
  const current = f.d.querySelector('.ps-dd-model-choice'); picker.destroy(); current.click(); assert.equal(f.picks.length, 0);
  f.dom.window.close();
});

test('rerendered search uses its current index, handles Enter/Escape, and repeated toggles add no delayed close work', () => {
  const f = fixture(), picker = createModelPicker(f.ports); picker.populate([{ model_id: 'old' }]);
  picker.populate([{ model_id: 'new' }]); const search = f.d.getElementById('presetDropdownSearchInput');
  search.value = 'new'; search.dispatchEvent(new f.w.Event('input', { bubbles: true }));
  search.dispatchEvent(new f.w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })); assert.equal(f.picks[0].model_id, 'new');
  const event = { stopPropagation() {} }; for (let i = 0; i < 100; i++) picker.toggle(event);
  picker.toggle(event); search.dispatchEvent(new f.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
  assert.ok(f.d.getElementById('presetWrapper').classList.contains('open')); assert.equal(search.value, '');
  search.dispatchEvent(new f.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
  assert.ok(!f.d.getElementById('presetWrapper').classList.contains('open')); picker.destroy(); f.dom.window.close();
});

test('unchanged selection skips repaint and an unavailable preference keeps intent while giving the popup keyboard focus', () => {
  const f = fixture(), picker = createModelPicker(f.ports);
  f.setSelection({ modelId: 'a', preferredProviderId: 'removed-provider', modelRef: { creator_id: 'c', model_id: 'a' } });
  picker.populate([v2('c')]);
  let writes = 0; const item = f.d.querySelector('.preset-dropdown-item'), toggle = item.classList.toggle.bind(item.classList);
  item.classList.toggle = (...args) => { writes++; return toggle(...args); };
  for (let i = 0; i < 100; i++) picker.refreshSelection(); assert.equal(writes, 0);
  item.querySelector('.ps-dd-provider-chip').click();
  assert.equal(f.d.activeElement, f.d.querySelector('.ps-dd-provider-menu-item'));
  assert.equal(f.ports.selection().preferredProviderId, 'removed-provider');
  assert.equal(f.d.querySelectorAll('.ps-dd-provider-menu-item[aria-selected="true"]').length, 0);
  picker.destroy(); f.dom.window.close();
});

test('multiple offerings from one provider produce one actionable preference and one selected option', () => {
  const f = fixture(), model = v2('c'); model.provider_options.push({ provider_id: 'p1', provider_name: 'Another access', offering_id: 'other' });
  f.setSelection({ modelId: 'a', preferredProviderId: 'p1', modelRef: { creator_id: 'c', model_id: 'a' } });
  const picker = createModelPicker(f.ports); picker.populate([model]); f.d.querySelector('.ps-dd-provider-chip').click();
  assert.equal(f.d.querySelectorAll('.ps-dd-provider-menu-item').length, 3);
  assert.equal(f.d.querySelectorAll('.ps-dd-provider-menu-item[aria-selected="true"]').length, 1);
  assert.equal(f.d.querySelector('.ps-dd-provider-menu-item.active').textContent, 'Provider One');
  picker.destroy(); f.dom.window.close();
});

test('creator IDs cannot collide with pending or recent display sections', () => {
  const f = fixture(); f.ports.groupKey = model => model.pending_identity ? '__pending_identity__' : model.creator_id;
  const picker = createModelPicker(f.ports);
  picker.populate([v2('__pending_identity__'), { model_id: 'quarantined-only', provider_id: 'p1', offering_id: 'draft', routing_v2: true, pending_identity: true }]);
  assert.equal(f.d.querySelectorAll('.ps-dd-section-label').length, 2);
  const sections = [...f.d.querySelectorAll('.preset-dropdown-item')].map(row => row.dataset.section);
  assert.notEqual(sections[0], sections[1]); picker.filter('quarantined-only');
  assert.equal(f.d.querySelectorAll('.ps-dd-section-label[style="display: none;"]').length, 1);
  f.ports.recentIds = () => ['favorite'];
  picker.populate([{ ...v2('__recent'), model_id: 'favorite' }, ...Array.from({ length: 8 }, (_, i) => ({ ...v2('other'), model_id: `other-${i}` }))]);
  const favorites = [...f.d.querySelectorAll('[data-value="favorite"]')];
  assert.equal(favorites.length, 2); assert.notEqual(favorites[0].dataset.section, favorites[1].dataset.section);
  picker.filter('favorite');
  assert.equal([...f.d.querySelectorAll('.ps-dd-section-label')].filter(label => label.style.display !== 'none').length, 2);
  picker.destroy(); f.dom.window.close();
});

test('failed provider installation removes popup and earlier listeners before returning the original failure', () => {
  const f = fixture(), picker = createModelPicker(f.ports); picker.populate([v2('c')]);
  const add = f.d.addEventListener.bind(f.d); f.d.addEventListener = (type, ...args) => { if (type === 'scroll') throw new Error('installation fault'); return add(type, ...args); };
  const errors = []; f.w.addEventListener('error', event => { errors.push(event.error); event.preventDefault(); });
  const chip = f.d.querySelector('.ps-dd-provider-chip'); chip.click();
  assert.equal(errors[0].message, 'installation fault'); assert.equal(chip.getAttribute('aria-expanded'), 'false'); assert.equal(f.d.getElementById('psProviderMenu'), null);
  f.d.addEventListener = add; chip.click(); assert.ok(f.d.getElementById('psProviderMenu')); picker.destroy();
  assert.equal(f.d.getElementById('psProviderMenu'), null); f.dom.window.close();
});

test('reopening rereads shared recents, keeps the current model and skips unchanged DOM rebuilds', () => {
  const f = fixture(); let recent = ['old', 'a'];
  f.ports.recentIds = () => recent;
  const picker = createModelPicker(f.ports);
  picker.populate([{ model_id: 'a' }, { model_id: 'old' }, { model_id: 'new' },
    ...Array.from({ length: 7 }, (_, i) => ({ model_id: `other-${i}` }))]);
  const recentRows = () => [...f.d.querySelectorAll('.preset-dropdown-item')]
    .filter(row => row.dataset.section === JSON.stringify(['recent']));
  assert.deepEqual(recentRows().map(row => row.dataset.value), ['old', 'a']);
  const event = { stopPropagation() {} }, oldNode = recentRows()[0];
  picker.toggle(event); picker.toggle(event);
  assert.equal(recentRows()[0], oldNode);
  // Another conversation or tab changes the shared persistence while closed.
  recent = ['new', 'a', 'unavailable'];
  picker.toggle(event);
  assert.deepEqual(recentRows().map(row => row.dataset.value), ['new', 'a']);
  assert.ok(recentRows()[1].classList.contains('active'));
  oldNode.click(); assert.equal(f.picks.length, 0);
  const newNode = recentRows()[0]; picker.toggle(event); picker.toggle(event);
  assert.equal(recentRows()[0], newNode);
  f.setSelection({ modelId: 'new', preferredProviderId: '', modelRef: null });
  picker.toggle(event); picker.toggle(event);
  assert.ok(newNode.classList.contains('active'));
  picker.destroy(); f.dom.window.close();
});

test('opening before a catalog arrives preserves the existing startup failure surface', () => {
  const f = fixture(); f.ports.list.textContent = 'retry loading';
  const picker = createModelPicker(f.ports);
  picker.toggle({ stopPropagation() {} });
  assert.equal(f.ports.list.textContent, 'retry loading');
  picker.destroy(); f.dom.window.close();
});
