/** Exact content identity, environment changes, bounded reuse and lazy code ownership. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');
const { JSDOM } = require('jsdom');
const marked = require('marked');
const root = path.resolve(__dirname, '..');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'tofu-markdown-tests-'));
const bundle = path.join(directory, 'markdown.cjs');
test.after(() => fs.rmSync(directory, { recursive: true, force: true }));
execFileSync(process.execPath, [path.join(root, 'scripts/vite_test_bundle.mjs'), '--bundle',
  '--format=cjs', '--platform=node', '--loader=ts', '--sourcefile=markdown-tests.ts', `--outfile=${bundle}`],
  { cwd: root, input: `export { createMarkdownRenderer } from ${JSON.stringify(path.join(root, 'frontend/src/markdown/index.ts'))};\n`
    + `export { createMarkdownRenderCache } from ${JSON.stringify(path.join(root, 'frontend/src/markdown/render-cache.ts'))};`, stdio: ['pipe', 'pipe', 'pipe'] });
const { createMarkdownRenderer, createMarkdownRenderCache } = require(bundle);

function fixture(t, extra = {}) {
  const dom = new JSDOM('<main id="output"></main>');
  let parses = 0;
  const environment = { document: dom.window.document, parser: { parse: source => { parses++; return marked.parse(source); } },
    sanitizer: null, highlighter: null, mathRenderer: null, basePath: '', projectActive: false };
  const renderer = createMarkdownRenderer({ getEnvironment: () => ({ ...environment }), maximumCacheEntries: 3,
    ensureMath: () => {}, ...extra });
  t.after(() => { renderer.dispose(); dom.window.close(); });
  return { dom, environment, renderer, parses: () => parses };
}

test('same length and identical edges never reuse a different middle, or a short hash collision', t => {
  const f = fixture(t);
  const prefix = 'a'.repeat(80), suffix = 'z'.repeat(80);
  const a = prefix + 'FIRST' + suffix, b = prefix + 'OTHER' + suffix;
  assert.match(f.renderer.render(a), /FIRST/);
  assert.match(f.renderer.render(b), /OTHER/);
  assert.equal(f.renderer.render(a), f.renderer.render(a));
  assert.match(f.renderer.render('Aa'), /Aa/);
  assert.match(f.renderer.render('BB'), /BB/);
});

test('raw and escaped variants share one LRU ceiling, replacement and clear release references', () => {
  const cache = createMarkdownRenderCache(2);
  cache.set('same', false, '<b>raw</b>'); cache.set('same', true, 'escaped');
  assert.equal(cache.get('same', false), '<b>raw</b>');
  cache.set('new', false, 'new');
  assert.equal(cache.get('same', true), undefined);
  assert.equal(cache.get('same', false), '<b>raw</b>');
  cache.set('same', false, 'replacement');
  assert.equal(cache.stats().entries, 2);
  cache.clear(); assert.deepEqual(cache.stats(), { entries: 0, retainedCharacters: 0 });
  const disabled = createMarkdownRenderCache(0); disabled.set('x', false, 'x');
  assert.equal(disabled.get('x', false), undefined);
});

test('project mode, base path and optional-library identity invalidate cached HTML', t => {
  const f = fixture(t);
  const source = '```js\none\ntwo\nthree\nfour\n```\n\n[asset](/api/v4/asset)';
  assert.doesNotMatch(f.renderer.render(source), /apply-btn/);
  const before = f.parses(); f.renderer.render(source); assert.equal(f.parses(), before);
  f.environment.projectActive = true;
  assert.match(f.renderer.render(source), /apply-btn/);
  f.environment.basePath = '/proxy';
  assert.match(f.renderer.render(source), /href="\/proxy\/api\/v4\/asset"/);
  f.environment.highlighter = { getLanguage: () => true, highlight: () => ({ value: '<span>new grammar</span>' }), highlightAuto: () => ({ value: '' }) };
  assert.match(f.renderer.render(source), /new grammar/);
  f.environment.sanitizer = { sanitize: () => '<p>revised sanitizer</p>' };
  assert.equal(f.renderer.render(source), '<p>revised sanitizer</p>');
});

test('streaming bypasses a terminal cache hit and never records degraded math or highlighting', t => {
  let highlighting = 0, typesetting = 0;
  const f = fixture(t);
  f.environment.highlighter = { getLanguage: () => true, highlight: text => { highlighting++; return { value: text }; }, highlightAuto: () => ({ value: '' }) };
  f.environment.mathRenderer = { renderToString: tex => { typesetting++; return `<span class="katex">${tex}</span>`; } };
  const source = '```js\nlet x = 1;\n```\n\n$E=mc^2$';
  const complete = f.renderer.render(source);
  assert.equal(highlighting, 1); assert.equal(typesetting, 1);
  const streaming = f.renderer.render(source, { streaming: true });
  assert.match(streaming, /math-pending/);
  assert.equal(highlighting, 1); assert.equal(typesetting, 1);
  assert.equal(f.renderer.render(source), complete);
  assert.equal(highlighting, 1); assert.equal(typesetting, 1);
});

test('temporary code DOM is reused but empty after success and highlighting failure', t => {
  const f = fixture(t); const scratch = [];
  const create = f.environment.document.createElement.bind(f.environment.document);
  f.environment.document.createElement = name => { const node = create(name); if (name === 'div') scratch.push(node); return node; };
  f.environment.highlighter = { getLanguage: () => true, highlight: () => { throw Error('injected grammar failure'); }, highlightAuto: () => ({ value: '' }) };
  assert.match(f.renderer.render('```js\nfirst\n```'), /first/);
  assert.match(f.renderer.render('```js\nsecond\n```'), /second/);
  assert.equal(scratch.length, 1);
  assert.equal(scratch[0].childNodes.length, 0);
  assert.equal(f.renderer.cacheStats().entries, 0);
});

test('lazy grammar upgrades mounted terminal code, preserves current text and ignores unrelated nodes', async t => {
  let ready = false, release, loads = 0;
  const pending = new Promise(resolve => { release = resolve; });
  const f = fixture(t, { ensureHighlightLanguage: () => { loads++; return pending; } });
  f.environment.highlighter = { getLanguage: () => ready, highlight: text => ({ value: `<span class="fresh">${text}</span>` }), highlightAuto: text => ({ value: text }) };
  const source = '```python-repl\noriginal\n```';
  const root = f.environment.document.querySelector('main');
  root.innerHTML = f.renderer.render(source) + '<pre><code class="language-python-repl">unrelated</code></pre>';
  f.renderer.render(source);
  await Promise.resolve(); assert.equal(loads, 1);
  root.querySelector('code').textContent = 'current';
  ready = true; release(true); await new Promise(resolve => setImmediate(resolve));
  assert.equal(root.querySelector('.fresh').textContent, 'current');
  assert.equal(root.querySelectorAll('code')[1].innerHTML, 'unrelated');
  assert.equal(f.renderer.cacheStats().entries, 0);
});

test('late grammar completion after disposal never touches mounted code', async t => {
  let ready = false, release;
  let f, reads = 0;
  f = fixture(t, { ensureHighlightLanguage: () => new Promise(resolve => { release = resolve; }),
    getEnvironment: () => { reads++; return f.environment; } });
  f.environment.highlighter = { getLanguage: () => ready, highlight: () => ({ value: 'late replacement' }), highlightAuto: text => ({ value: text }) };
  f.environment.document.querySelector('main').innerHTML = f.renderer.render('```python\noriginal\n```');
  const original = f.environment.document.querySelector('code').textContent;
  await Promise.resolve(); f.renderer.dispose(); const readsAtDisposal = reads;
  ready = true; release(true);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.environment.document.querySelector('code').textContent, original);
  assert.deepEqual(f.renderer.cacheStats(), { entries: 0, retainedCharacters: 0 });
  assert.equal(reads, readsAtDisposal);
});

test('disposal before optional work starts never loads a library', async t => {
  let requests = 0;
  const f = fixture(t, { ensureHighlightLanguage: async () => { requests++; return true; },
    ensureMath: async () => { requests++; } });
  f.environment.highlighter = { getLanguage: () => false, highlight: () => ({ value: '' }), highlightAuto: text => ({ value: text }) };
  f.renderer.render('```rust\ncode\n```\n\n$x$'); f.renderer.dispose();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests, 0);
});

test('parser failure shows escaped source and never admits it as a successful render', t => {
  const errors = []; const f = fixture(t, { reportFailure: error => errors.push(error) });
  f.environment.parser = { parse: () => { throw Error('injected parser failure'); } };
  assert.equal(f.renderer.render('<b>source</b>'), '<pre>&lt;b&gt;source&lt;/b&gt;</pre>');
  assert.equal(errors.length, 1); assert.equal(f.renderer.cacheStats().entries, 0);
});

test('one pending math load serves streaming frames and completes only mounted terminal math', async t => {
  let release, loads = 0;
  const pending = new Promise(resolve => { release = resolve; });
  const f = fixture(t, { ensureMath: () => { loads++; return pending; } });
  const root = f.environment.document.querySelector('main');
  root.innerHTML = f.renderer.render('$first$') + f.renderer.render('$live$', { streaming: true });
  for (let i = 0; i < 50; i++) f.renderer.render('$live$', { streaming: true });
  await Promise.resolve(); assert.equal(loads, 1);
  root.querySelector('.math-pending').textContent = 'current';
  f.environment.mathRenderer = { renderToString: tex => `<span class="katex">${tex}</span>` };
  release(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(root.querySelector('.katex').textContent, 'current');
  assert.equal(root.querySelector('.math-pending').textContent, 'live');
});

test('unknown grammar outside the admitted catalog remains cacheable without a lazy request', t => {
  let loads = 0;
  const f = fixture(t, { ensureHighlightLanguage: async () => { loads++; return false; }, canEnsureHighlightLanguage: () => false });
  f.environment.highlighter = { getLanguage: () => false, highlight: () => ({ value: '' }), highlightAuto: text => ({ value: text }) };
  const source = '```unknown-grammar\nplain evidence\n```';
  f.renderer.render(source); const parses = f.parses(); f.renderer.render(source);
  assert.equal(f.parses(), parses); assert.equal(loads, 0);
});
