/** Page ownership, bounded transport, patch rollback and listener/focus teardown. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'tofu-slide-editor-'));
const bundle = path.join(temporary, 'editor.cjs');
execFileSync(process.execPath, [path.join(root, 'scripts/vite_test_bundle.mjs'), '--bundle', '--format=cjs',
  '--platform=node', '--loader=ts', '--sourcefile=editor-tests.ts', `--outfile=${bundle}`], {
  cwd: root, input: `export { createSlidesEditor } from ${JSON.stringify(path.join(root, 'frontend/src/features/slides/controller.ts'))};`,
  stdio: ['pipe', 'pipe', 'pipe'],
});
const { createSlidesEditor } = require(bundle);
test.after(() => fs.rmSync(temporary, { recursive: true, force: true }));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const settle = () => new Promise(resolve => setImmediate(resolve));
const element = text => ({ elementId: 'title', elementType: 'text', text, style: { fontSize: 32, color: '$primary' } });

function fixture(t, overrides = {}) {
  const dom = new JSDOM('<button id="origin">Open editor</button>', { url: 'http://localhost' });
  global.document = dom.window.document; global.window = dom.window; global.AbortController = dom.window.AbortController;
  const reads = [], writes = [], notices = [], messages = [];
  const api = {
    pageHtmlUrl: (task, page) => `http://localhost/${task}/${page}.html`,
    pageFileUrl: (task, page, rev) => `http://localhost/${task}/${page}.png?v=${rev}`,
    pageElements(task, page) { const request = deferred(); reads.push({ task, page, ...request }); return request.promise; },
    patchPage(task, page, ops) { const request = deferred(); writes.push({ task, page, ops, ...request }); return request.promise; },
    ...overrides,
  };
  const editor = createSlidesEditor({ document: document, getApi: () => api,
    translate: (key, params = {}) => { const messages = { 'sled.noChange': '没有改动', 'sled.failed': '保存失败', 'sled.askAiPrefill': '编辑这个 PPT（任务 {id}）第 {n} 页：' }; return (messages[key] ?? key).replace(/\{(\w+)\}/g, (_, name) => String(params[name])); }, notifyApplied: () => notices.push('applied'), prefill: text => messages.push(text) });
  t.after(() => { editor.close(); dom.window.close(); });
  const click = selector => document.querySelector(selector).click();
  function select() {
    const frame = document.querySelector('iframe');
    // Same-origin fixture document, delivered by the browser iframe in visual tests.
    const content = frame.contentDocument;
    content.write('<html><head></head><body><h1 data-element-id="title">Title</h1></body></html>'); content.close();
    frame.dispatchEvent(new dom.window.Event('load'));
    content.querySelector('h1').click();
  }
  return { dom, editor, reads, writes, notices, messages, click, select };
}

test('X, backdrop, Escape, AI, replacement and pagehide release the active keyboard listener and restore focus', async t => {
  const f = fixture(t, { pageElements: async () => ({ elements: [element('hello')] }) });
  const originalAdd = document.addEventListener.bind(document);
  const listeners = new Set();
  document.addEventListener = (type, listener, options) => {
    if (type === 'keydown') { listeners.add(listener); options.signal.addEventListener('abort', () => listeners.delete(listener)); }
    originalAdd(type, listener, options);
  };
  document.querySelector('#origin').focus();
  for (let n = 0; n < 30; n++) { f.editor.open('deck', 1, 2); assert.equal(listeners.size, 1); f.click('.sled-close'); assert.equal(listeners.size, 0); }
  assert.equal(document.activeElement.id, 'origin');
  f.editor.open('deck', 1, 2); f.editor.open('replacement', 2, 2);
  assert.equal(listeners.size, 1); assert.equal(document.querySelectorAll('.sled-overlay').length, 1);
  f.click('.sled-overlay'); assert.equal(listeners.size, 0);
  f.editor.open('deck', 1, 2); document.dispatchEvent(new f.dom.window.KeyboardEvent('keydown', { key: 'Escape' })); assert.equal(listeners.size, 0);
  f.editor.open('deck', 2, 2); await settle(); f.select(); f.click('[data-sled-ask]');
  assert.deepEqual(f.messages, ['编辑这个 PPT（任务 deck）第 2 页：']); assert.equal(listeners.size, 0);
  f.editor.open('deck', 1, 2); f.dom.window.dispatchEvent(new f.dom.window.Event('pagehide')); assert.equal(listeners.size, 0);
});

test('rapid page changes retain only the current page data and one trailing read', async t => {
  const f = fixture(t); f.editor.open('deck', 1, 8);
  for (let page = 2; page <= 8; page++) f.click(`[data-sled-page="${page}"]`);
  assert.equal(f.reads.length, 1);
  f.reads[0].resolve({ elements: [element('stale page one')] }); await settle();
  assert.equal(f.reads.length, 2); assert.equal(f.reads[1].page, 8);
  f.select(); assert.equal(document.querySelector('#sledText'), null);
  f.reads[1].resolve({ elements: [element('current page eight')] }); await settle();
  assert.equal(document.querySelector('#sledText').value, 'current page eight');
  assert.match(document.querySelector('.sled-open-raw').href, /deck\/8\.png/);
});

test('close/reopen of the same page is a new visit and cannot receive the old read', async t => {
  const f = fixture(t); f.editor.open('deck', 1, 2); f.editor.close(); f.editor.open('deck', 1, 2); f.select();
  f.reads[0].resolve({ elements: [element('old visit')] }); await settle();
  assert.equal(document.querySelector('#sledText'), null); assert.equal(f.reads.length, 2);
  f.reads[1].resolve({ elements: [element('new visit')] }); await settle();
  assert.equal(document.querySelector('#sledText').value, 'new visit');
});

test('closing drops trailing read demand and releases iframe selection handlers', async t => {
  const f = fixture(t); f.editor.open('deck', 1, 2); f.select();
  const frameDocument = document.querySelector('iframe').contentDocument;
  f.click('[data-sled-page="2"]'); f.editor.close();
  f.reads[0].resolve({ elements: [element('late')] }); await settle();
  assert.equal(f.reads.length, 1); assert.equal(document.querySelector('.sled-overlay'), null);
  assert.equal(frameDocument.querySelector('style'), null);
});

test('patch preserves owner/page/target, blocks duplicate writes, and retains rollback errors and form on failure', async t => {
  const f = fixture(t); f.editor.open('owner-deck', 2, 2);
  f.reads[0].resolve({ elements: [element('original')] }); await settle(); f.select();
  document.querySelector('#sledText').value = '<new text>';
  document.querySelector('#sledFontSize').value = '40'; document.querySelector('#sledColor').value = '$secondary';
  f.click('[data-sled-apply]'); f.click('[data-sled-apply]');
  assert.equal(f.writes.length, 1);
  assert.deepEqual({ task: f.writes[0].task, page: f.writes[0].page, ops: f.writes[0].ops }, {
    task: 'owner-deck', page: 2, ops: [{ op: 'set_text', target: 'title', text: '<new text>' },
      { op: 'set_style', target: 'title', props: { fontSize: 40, color: '$secondary' } }],
  });
  f.writes[0].resolve({ ok: false, errors: ['layout rollback', 'invalid palette'] }); await settle();
  assert.equal(document.querySelector('#sledText').value, '<new text>');
  assert.equal(document.querySelector('#sledErrors').textContent, 'layout rollback；invalid palette');
  assert.equal(document.querySelector('[data-sled-apply]').disabled, false); assert.equal(f.reads.length, 1);
  assert.deepEqual(f.notices, []);
});

test('a late patch cannot reload, notify or show errors in a replacement session, and does not strand it busy', async t => {
  for (const result of [{ ok: true }, { ok: false, errors: ['old error'] }]) {
    const f = fixture(t); f.editor.open('old', 1, 2); f.reads[0].resolve({ elements: [element('old')] }); await settle(); f.select();
    document.querySelector('#sledText').value = 'changed'; f.click('[data-sled-apply]');
    f.editor.close(); f.editor.open('new', 2, 2); f.reads[1].resolve({ elements: [element('new')] }); await settle(); f.select();
    const src = document.querySelector('iframe').src;
    f.writes[0].resolve(result); await settle();
    assert.equal(document.querySelector('iframe').src, src); assert.equal(document.querySelector('#sledText').value, 'new');
    assert.equal(document.querySelector('#sledErrors').textContent, ''); assert.deepEqual(f.notices, []);
    assert.equal(document.querySelector('[data-sled-apply]').disabled, false);
    f.editor.close(); f.dom.window.close();
  }
});

test('success refreshes that page, rejection is retryable, and synchronous transport throws release write ownership', async t => {
  const f = fixture(t); f.editor.open('deck', 1, 2); f.reads[0].resolve({ elements: [element('before')] }); await settle(); f.select();
  document.querySelector('#sledText').value = 'after'; f.click('[data-sled-apply]'); f.writes[0].resolve({ ok: true }); await settle();
  assert.deepEqual(f.notices, ['applied']); assert.equal(f.reads.length, 2);
  f.reads[1].resolve({ elements: [element('after')] }); await settle(); f.select();
  document.querySelector('#sledText').value = 'retry'; f.click('[data-sled-apply]'); f.writes[1].reject(new Error('offline')); await settle();
  assert.equal(document.querySelector('#sledErrors').textContent, '保存失败'); assert.equal(document.querySelector('[data-sled-apply]').disabled, false);
  const g = fixture(t, { patchPage: () => { throw new Error('sync transport'); } });
  g.editor.open('sync', 1, 1); g.reads[0].resolve({ elements: [element('before')] }); await settle(); g.select();
  document.querySelector('#sledText').value = 'after'; g.click('[data-sled-apply]'); await settle();
  assert.equal(document.querySelector('#sledErrors').textContent, '保存失败'); assert.equal(document.querySelector('[data-sled-apply]').disabled, false);
});

test('text is projected safely, missing style is editable, and no-op applies do not mutate the deck', async t => {
  const f = fixture(t); f.editor.open('deck', 1, 1);
  f.reads[0].resolve({ elements: [{ ...element('<img src=x onerror=bad()>'), style: undefined }] }); await settle(); f.select();
  assert.equal(document.querySelector('#sledText').value, '<img src=x onerror=bad()>'); assert.equal(document.querySelector('.sled-side img'), null);
  f.click('[data-sled-apply]'); assert.equal(f.writes.length, 0); assert.equal(document.querySelector('#sledErrors').textContent, '没有改动');
});

test('a failed or synchronously throwing element read releases the flight and starts only the latest demand', async t => {
  const f = fixture(t); f.editor.open('deck', 1, 3); f.click('[data-sled-page="2"]'); f.click('[data-sled-page="3"]');
  f.reads[0].reject(new Error('offline')); await settle();
  assert.equal(f.reads.length, 2); assert.equal(f.reads[1].page, 3);
  f.reads[1].resolve({ elements: [element('Recovered')] }); await settle(); f.select();
  assert.equal(document.querySelector('#sledText').value, 'Recovered'); f.editor.close();
  let calls = 0;
  const g = fixture(t, { pageElements: () => { calls++; throw new Error('sync read'); } });
  g.editor.open('deck', 1, 2); g.click('[data-sled-page="2"]'); await settle();
  assert.equal(calls, 2); assert.equal(document.querySelector('#sledText'), null);
});

test('closing removes canvas listeners rather than leaving guarded callbacks on detached documents', async t => {
  const f = fixture(t); f.editor.open('deck', 1, 1); f.reads[0].resolve({ elements: [element('Title')] }); await settle(); f.select();
  const content = document.querySelector('iframe').contentDocument, title = content.querySelector('h1');
  const before = new f.dom.window.MouseEvent('click', { bubbles: true, cancelable: true }); title.dispatchEvent(before);
  assert.equal(before.defaultPrevented, true);
  f.editor.close();
  const after = new f.dom.window.MouseEvent('click', { bubbles: true, cancelable: true }); title.dispatchEvent(after);
  assert.equal(after.defaultPrevented, false); assert.equal(content.querySelector('style'), null);
});
