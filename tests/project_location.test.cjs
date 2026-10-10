const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const { JSDOM } = require('jsdom');

function load(file) {
  const result = { exports: {} };
  const source = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  new Function('require', 'module', 'exports', source)((name) => (
    name.startsWith('.') ? load(path.resolve(path.dirname(file), name + '.ts')) : require(name)
  ), result, result.exports);
  return result.exports;
}

const { createProjectContext, fitProjectLocationNames, PROJECT_LOCATION_SEP } =
  load(path.resolve('frontend/src/features/conversation-bar/project-context.ts'));
const en = JSON.parse(fs.readFileSync('frontend/src/i18n/locales/en.json', 'utf8'));
const zh = JSON.parse(fs.readFileSync('frontend/src/i18n/locales/zh.json', 'utf8'));

function setup(t) {
  const html = `<div class="project-bar" id="projectBar">
    <button type="button" class="project-location" id="projectBarFolders">
      <svg></svg>
      <span class="project-location-name">Attach project</span>
      <span class="project-location-count" hidden></span>
      <svg class="project-location-chevron"></svg>
    </button>
    <button type="button" id="autoApplyToggle" hidden>Auto</button>
  </div>`;
  const dom = new JSDOM(html);
  const bar = dom.window.document.getElementById('projectBar');
  let copy = en;
  const actions = [];
  const view = createProjectContext({
    bar,
    t: (key, vars = {}) => (copy[key] || key).replace(/\{(\w+)\}/g, (_, name) => vars[name] ?? `{${name}}`),
    manage: () => actions.push('manage'),
    clear: () => actions.push('clear'),
    toggleReadOnly: index => actions.push(`readonly:${index}`),
  });
  t.after(() => { view.destroy(); dom.window.close(); });
  return {
    view, bar, actions, document: dom.window.document,
    name: () => bar.querySelector('.project-location-name').textContent,
    count: () => bar.querySelector('.project-location-count'),
    language(next) {
      copy = next;
      dom.window.dispatchEvent(new dom.window.Event('tofu:language-change'));
    },
  };
}

test('fits every name when leftover covers the joined label', () => {
  const measure = text => text.length;
  assert.deepEqual(
    fitProjectLocationNames(['tofu', 'xuecheng-mcp'], 400, measure),
    { label: `tofu${PROJECT_LOCATION_SEP}xuecheng-mcp`, overflow: 0 },
  );
  assert.deepEqual(
    fitProjectLocationNames(['tofu'], 4, measure),
    { label: 'tofu', overflow: 0 },
  );
  assert.deepEqual(fitProjectLocationNames([], 100, measure), { label: '', overflow: 0 });
});

test('keeps a greedy prefix and only then emits +N', () => {
  const measure = text => text.length;
  const reserve = extra => String(extra).length + 1;
  assert.deepEqual(
    fitProjectLocationNames(['tofu', 'xuecheng-mcp'], 10, measure, reserve),
    { label: 'tofu', overflow: 1 },
  );
  assert.deepEqual(
    fitProjectLocationNames(['alpha', 'bravo', 'charlie'], 16, measure, reserve),
    { label: `alpha${PROJECT_LOCATION_SEP}bravo`, overflow: 1 },
  );
  assert.deepEqual(
    fitProjectLocationNames(['alpha', 'bravo', 'charlie'], 6, measure, reserve),
    { label: 'alpha', overflow: 2 },
  );
});

test('unmeasured layout still paints every short name instead of a premature +N', t => {
  const s = setup(t);
  s.view.update({
    roots: [
      { path: '/workspace/tofu', readOnly: false },
      { path: '/workspace/xuecheng-mcp', readOnly: false },
    ],
  });
  assert.equal(s.name(), 'tofu · xuecheng-mcp');
  assert.equal(s.count().hidden, true);
  assert.match(
    s.bar.querySelector('#projectBarFolders').getAttribute('aria-label'),
    /tofu · xuecheng-mcp/,
  );
});

test('empty location restores attach copy after language change', t => {
  const s = setup(t);
  s.view.update({ roots: [] });
  assert.equal(s.name(), en['conversationBar.attachProject']);
  s.language(zh);
  assert.equal(s.name(), zh['conversationBar.attachProject']);
});
