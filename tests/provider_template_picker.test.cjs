const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');

const vm = require('node:vm');
const loaded = { exports: {} };
new Function('module', 'exports', ts.transpileModule(fs.readFileSync('frontend/src/features/settings/provider-template-picker.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText)(loaded, loaded.exports);
const { createProviderTemplatePicker } = loaded.exports;
const copy = JSON.parse(fs.readFileSync('frontend/src/i18n/locales/en.json'));
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup(t, load, extra = {}) {
  const dom = new JSDOM('<button id="open">Templates</button><button id="outside">Outside</button>');
  const doc = dom.window.document;
  const selected = [];
  const picker = createProviderTemplatePicker({ document: doc, translate: key => copy[key], load, select: key => selected.push(key), ...extra });
  const trigger = doc.getElementById('open');
  t.after(() => { picker.destroy(); dom.window.close(); });
  return { dom, doc, trigger, picker, selected };
}
const row = (key, category, extra = {}) => ({ key, name: key, category, base_url: 'https://api.example.com/v1', offering_recipes: [{ model_id: 'seed-alpha' }], ...extra });

test('discovery never creates empty availability, hides secrets and respects close epochs', async t => {
  let finish; const calls = [];
  const s = setup(t, async () => [row('Discover', 'relay', { offering_recipes: [], catalog_mode: 'discovery' })], {
    discover: (key, secret, current) => { calls.push({ key, secret, current }); return new Promise(resolve => { finish = resolve; }); },
  });
  s.picker.toggle(s.trigger); await tick(); s.doc.querySelector('[data-tpl-key]').click();
  assert.deepEqual(s.selected, []);
  const input = s.doc.querySelector('input[type=password]');
  assert.ok(input); assert.equal(input.autocomplete, 'off'); input.value = 'test-only-key';
  s.doc.querySelector('form').dispatchEvent(new s.dom.window.Event('submit', { bubbles: true, cancelable: true }));
  assert.equal(calls.length, 1); assert.equal(calls[0].secret, 'test-only-key');
  assert.equal(input.value, ''); assert.equal(input.disabled, true);
  assert.equal(s.doc.body.textContent.includes('test-only-key'), false);
  s.picker.destroy(); assert.equal(calls[0].current(), false); finish(); await tick();
  assert.equal(s.doc.querySelector('[role=dialog]'), null);
});
test('dismissing discovery clears a key even before submitting', async t => {
  const s = setup(t, async () => [row('Discover', 'official', { offering_recipes: [], catalog_mode: 'discovery' })]);
  s.picker.toggle(s.trigger); await tick(); s.doc.querySelector('[data-tpl-key]').click();
  const input = s.doc.querySelector('input[type=password]');
  input.value = 'test-only-unsubmitted-key';
  s.picker.destroy();
  assert.equal(input.value, '');
  assert.equal(s.doc.querySelector('[role=dialog]'), null);
});

test('discovery failure is retryable without echoing upstream credential text', async t => {
  const s = setup(t, async () => [row('Discover', 'official', { offering_recipes: [], catalog_mode: 'discovery' })], {
    discover: async () => { throw Error('secret-from-upstream'); },
  });
  s.picker.toggle(s.trigger); await tick(); s.doc.querySelector('[data-tpl-key]').click();
  s.doc.querySelector('input[type=password]').value = 'test-only-key';
  s.doc.querySelector('form').dispatchEvent(new s.dom.window.Event('submit', { bubbles: true, cancelable: true }));
  await tick();
  assert.match(s.doc.querySelector('[role=status]').textContent, /Could not discover/);
  assert.ok(!s.doc.body.textContent.includes('secret-from-upstream'));
  assert.equal(s.doc.querySelector('input[type=password]').disabled, false);
});
test('one malformed template does not break the rest or render unsafe source HTML', async t => {
  const s = setup(t, async () => [row('Bad', 'relay', { offering_recipes: 'not an array' }),
    row('<img src=x onerror=alert(1)>', 'official', { catalog_source: { url: 'javascript:alert(1)' } })]);
  s.picker.toggle(s.trigger); await tick();
  assert.equal(s.doc.querySelectorAll('[data-tpl-key]').length, 1);
  assert.equal(s.doc.querySelector('img,a'), null);
});
test('server categories, compact tiles, search across hidden model ids and discovery eligibility', async t => {
  const s = setup(t, async () => [row('Vendor', 'official', { catalog_source: { url: 'https://docs.example.com', checked_at: '2026-01-02' } }),
    row('Gateway', 'relay', { offering_recipes: ['a', 'b', 'c', 'hidden-match'].map(model_id => ({ model_id })) }),
    row('Unknown', 'invented'), row('Local', 'local', { offering_recipes: [] }),
    row('Discover', 'relay', { offering_recipes: [], catalog_mode: 'discovery' }),
    row('Empty', 'official', { offering_recipes: [] })]);
  s.picker.toggle(s.trigger); await tick();
  assert.equal(s.doc.querySelectorAll('[data-tpl-key]').length, 4);
  assert.match(s.doc.body.textContent, /Third-party providers \(2\)/);
  assert.match(s.doc.body.textContent, /Other providers \(1\)/);
  // Tiles carry brand mark + name only: no source links, seed counts or check dates.
  assert.equal(s.doc.querySelector('.stg-provider-catalog-results a'), null);
  const tile = s.doc.querySelector('[data-tpl-key="Vendor"]');
  assert.equal(tile.className, 'stg-provider-catalog-tile');
  assert.equal(tile.querySelector('.stg-provider-catalog-name').textContent, 'Vendor');
  assert.equal(tile.title, 'Vendor · api.example.com');
  const search = s.doc.querySelector('input');
  search.value = 'hidden-match'; search.dispatchEvent(new s.dom.window.Event('input'));
  assert.equal(s.doc.querySelectorAll('[data-tpl-key]').length, 1);
  s.doc.querySelector('[data-tpl-key]').click();
  assert.deepEqual(s.selected, ['Gateway']);
  assert.equal(s.doc.activeElement, s.trigger);
  assert.equal(s.doc.querySelector('[role=dialog]'), null);
});
test('adding a compiled template preserves unprobed state and gives explicit setup guidance', async () => {
  for (const apiKey of ['', 'test-only-key']) {
    const posted = []; const toasts = [];
    const context = vm.createContext({
      Set, Date, Math,
      _stgModelRouting: { providers: [], connections: [], creators: [], models: [] },
      _stgModelRoutingRevision: 1,
      Api: { modelRouting: { createProvider: async (draft, revision) => {
        posted.push({ draft, revision }); return { provider: draft.provider, revision: 2 };
      } } },
      _loadModelRoutingAuthority: async () => {}, _renderModelRoutingTabs: () => {},
      document: { querySelectorAll: () => [] },
      showAlert: message => assert.fail(message), showToast: message => toasts.push(message),
      t: key => copy[key],
    });
    vm.runInContext(fs.readFileSync('frontend/src/runtime/sections/settings/provider_render.js', 'utf8'), context);
    const bundle = {
      provider: { provider_id: 'new-provider' }, provider_access: { provider_access_id: 'access' },
      connections: [{ connection_id: 'connection', base_url: 'https://provider.example/v1', extra_headers: {} }],
      credentials: [{ credential_id: 'credential', kind: 'api_key', secret_reference: '' }],
      creators: [], models: [], offerings: [],
      deployments: [{ probe_status: 'unprobed' }],
      credential_extra_headers: { 'X-Client-Name': 'Tofu' },
    };
    const before = JSON.stringify(bundle);
    assert.equal(await context._stageModelRoutingProviderBundle(bundle, apiKey), true);
    const { draft, revision } = posted[0];
    assert.equal(revision, 1);
    assert.equal('enabled' in draft.deployments[0], false);
    assert.equal(draft.deployments[0].probe_status, 'unprobed');
    assert.equal(draft.connections[0].extra_headers['X-Client-Name'], 'Tofu');
    assert.equal('credential_extra_headers' in draft, false);
    assert.equal(JSON.stringify(bundle), before);
    if (apiKey) {
      assert.equal(draft.credentials.length, 1);
      assert.equal(JSON.parse(draft.credential_secrets.credential).api_key, apiKey);
    } else {
      assert.equal(draft.credentials.length, 0);
      assert.equal('credential_secrets' in draft, false);
    }
    assert.match(toasts[0], /optional matrix probes diagnose availability/);
    assert.match(toasts[0], /[Rr]eview model access/);
    assert.equal(toasts[0].includes('test-only-key'), false);
  }
});

test('fresh single-flight reads, stale response ignored, Escape and outside cleanup', async t => {
  const resolvers = []; let calls = 0;
  const s = setup(t, () => { calls++; return new Promise(resolve => resolvers.push(resolve)); });
  s.picker.toggle(s.trigger); await tick();
  assert.equal(calls, 1);
  s.picker.toggle(s.trigger); s.picker.toggle(s.trigger);
  s.picker.toggle(s.trigger); s.picker.toggle(s.trigger);
  await tick(); assert.equal(calls, 1);
  resolvers.shift()([row('stale', 'official')]); await tick();
  assert.equal(calls, 2); assert.equal(s.doc.querySelector('[data-tpl-key]'), null);
  resolvers.shift()([row('fresh', 'relay')]); await tick();
  assert.equal(s.doc.querySelector('[data-tpl-key]').dataset.tplKey, 'fresh');
  s.doc.dispatchEvent(new s.dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(s.doc.querySelector('[role=dialog]'), null);
  assert.equal(s.doc.activeElement, s.trigger);
  s.picker.toggle(s.trigger); await tick(); assert.equal(calls, 3);
  s.doc.getElementById('outside').click();
  resolvers.shift()([]); await tick();
  assert.equal(s.doc.querySelector('[role=dialog]'), null);
});
test('failure retry, empty state, focus trap and trigger removal disposal', async t => {
  let calls = 0;
  const s = setup(t, async () => { if (++calls === 1) throw Error('offline'); return []; });
  s.picker.toggle(s.trigger); await tick();
  assert.match(s.doc.querySelector('[role=status]').textContent, /could not be loaded/);
  const retry = [...s.doc.querySelectorAll('button')].find(el => el.textContent === 'Retry');
  retry.focus();
  retry.dispatchEvent(new s.dom.window.KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }));
  assert.equal(s.doc.activeElement.textContent, 'Close');
  retry.click(); await tick();
  assert.match(s.doc.querySelector('[role=status]').textContent, /No selectable templates/);
  s.trigger.remove(); await tick();
  assert.equal(s.doc.querySelector('[role=dialog]'), null);
});
