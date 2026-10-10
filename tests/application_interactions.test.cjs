/** Native application interactions: resource bounds, routing authority and stale-result fencing. */
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const { execFileSync } = require('node:child_process'), { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..'), temp = fs.mkdtempSync(path.join(os.tmpdir(), 'tofu-interactions-'));
const bundle = path.join(temp, 'owners.cjs');
execFileSync(process.execPath, [path.join(root, 'scripts/vite_test_bundle.mjs'), '--bundle', '--format=cjs', '--platform=node',
  '--loader=ts', '--sourcefile=owners.ts', `--outfile=${bundle}`], { cwd: root, input: [
    'frontend/src/application-shell/interactions/index.ts', 'frontend/src/lifecycle.ts',
  ].map(p => `export * from ${JSON.stringify(path.join(root, p))};`).join('\n'), stdio: ['pipe', 'pipe', 'pipe'] });
const { createLifecycleScope, installApplicationInteractions, installViewportHeightGuard } = require(bundle);
test.after(() => fs.rmSync(temp, { recursive: true, force: true }));
const drain = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

function fixture() {
  const dom = new JSDOM('<body><div id="convList"></div><textarea id="userInput"></textarea><div id="dropOverlay">'
    + '<span class="drop-text"></span><span class="drop-hint"></span></div><div id="settingsModal"></div>'
    + '<div id="sidebarSearchWrapper" style="display:none"></div><div id="applyModal"></div><div id="previewModal"></div>'
    + '<div class="chat-wrapper"><div id="chatContainer"></div><div class="input-area"></div></div></body>');
  const w = dom.window, d = w.document, frames = new Map(), timers = new Map(), intervals = new Map();
  let next = 1, clock = 0;
  w.setTimeout = (fn, ms) => { const id = next++; timers.set(id, { fn, at: clock + ms }); return id; };
  w.clearTimeout = id => timers.delete(id);
  w.setInterval = fn => { const id = next++; intervals.set(id, fn); return id; };
  w.clearInterval = id => intervals.delete(id);
  w.requestAnimationFrame = fn => { const id = next++; frames.set(id, fn); return id; };
  w.cancelAnimationFrame = id => frames.delete(id);
  const observers = [];
  w.ResizeObserver = class { constructor(fn) { this.fn = fn; this.active = false; observers.push(this); }
    observe() { this.active = true; } disconnect() { this.active = false; } };
  const calls = [], errors = []; let context = 'conv-a', modal = false, paper = false;
  const ports = { window: w, document: d, initializeFolders() {},
    sidebar: { activate: id => calls.push(['activate', id]), copy: async id => calls.push(['copy', id]),
      duplicate: id => calls.push(['duplicate', id]), delete: id => calls.push(['delete', id]),
      rename: id => calls.push(['rename', id]), reference: (id, title) => calls.push(['reference', id, title]),
      assignFolder: id => calls.push(['folder', id]), reportFailure: e => errors.push(e) },
    composer: { contextIdentity: () => context, pendingCleanOriginalText: () => null,
      handleImage: async f => calls.push(['image', f.name]), detectLogNoise: async text => ({ text }),
      showLogClean: r => calls.push(['clean', r.text]), hideLogClean: () => calls.push(['hide-clean']), reportFailure: e => errors.push(e) },
    files: { enabled: true, modalOwnsFiles: () => modal, paperMode: () => paper,
      isArchive: f => f.name.endsWith('.zip'), isDocument: f => f.name.endsWith('.docx'),
      handlePaper: async f => calls.push(['paper', f.name]), handlePdf: async f => calls.push(['pdf', f.name]),
      handleImage: async f => calls.push(['image', f.name]), handleArchive: async f => calls.push(['archive', f.name]),
      handleDocument: async f => calls.push(['document', f.name]), labels: p => ({ title: p ? 'PDF' : 'Files', hint: 'hint' }),
      reportFailure: e => errors.push(e) },
    geometry: { updateActiveTurn: () => calls.push(['turn']), updateScrollButton: () => calls.push(['scroll-button']), reportFailure: e => errors.push(e) },
    shortcuts: Object.fromEntries(['toggleSearch', 'closeSearch', 'closeApply', 'closePreview', 'closeSettings', 'closeProject',
      'closeLocalControl', 'closeDailyReport'].map(name => [name, () => calls.push([name])])) };
  function fire(node, type, properties = {}) {
    const event = new w.Event(type, { bubbles: true, cancelable: true });
    for (const [key, value] of Object.entries(properties)) Object.defineProperty(event, key, { value });
    node.dispatchEvent(event); return event;
  }
  function tick(ms = 0) { clock += ms; for (const [id, job] of [...timers]) if (job.at <= clock) { timers.delete(id); job.fn(); } }
  function frame() { const jobs = [...frames]; frames.clear(); for (const [, fn] of jobs) fn(clock); }
  return { dom, w, d, ports, calls, errors, frames, timers, intervals, observers, fire, tick, frame,
    setContext: v => { context = v; }, setModal: v => { modal = v; }, setPaper: v => { paper = v; } };
}

test('completed/cancelled tasks release ownership; teardown prevents new work and preserves cleanup fan-out', () => {
  const f = fixture(), scope = createLifecycleScope(f.w); let delivered = 0;
  for (let i = 0; i < 10000; i++) { scope.timeout(() => delivered++, 1); scope.frame(() => delivered++); f.tick(1); f.frame(); }
  assert.equal(delivered, 20000); assert.deepEqual(scope.pendingTasks, { timeouts: 0, intervals: 0, frames: 0 });
  const timer = scope.timeout(() => delivered++, 1), frame = scope.frame(() => delivered++);
  scope.cancelTimeout(timer); scope.cancelFrame(frame); f.tick(1); f.frame(); assert.equal(delivered, 20000);
  const interval = scope.interval(() => delivered++, 1); scope.cancelInterval(interval);
  let cleaned = 0; scope.add(() => cleaned++); scope.add(() => { throw new Error('cleanup fault'); });
  scope.destroy(); scope.destroy(); scope.timeout(() => delivered++, 1); scope.frame(() => delivered++);
  scope.listen(f.d, 'click', () => delivered++); f.fire(f.d, 'click');
  assert.equal(cleaned, 1); assert.equal(delivered, 20000); assert.equal(f.timers.size + f.frames.size + f.intervals.size, 0);
  f.dom.window.close();
});

test('mouse press activates through a row rebuild; click is deduplicated and touch/keyboard still activate', () => {
  const f = fixture(), dispose = installApplicationInteractions(f.ports), list = f.d.getElementById('convList');
  list.innerHTML = '<div class="conv-item" data-conv-id="a"><span>Title</span></div>';
  f.fire(list.firstChild.firstChild, 'pointerdown', { pointerType: 'mouse', button: 0 });
  list.innerHTML = '<div class="conv-item" data-conv-id="a"><span>New title</span></div>';
  f.fire(list.firstChild.firstChild, 'click');
  f.fire(list.firstChild.firstChild, 'pointerdown', { pointerType: 'touch', button: 0 });
  f.fire(list.firstChild.firstChild, 'click'); f.fire(list.firstChild.firstChild, 'click');
  assert.deepEqual(f.calls.filter(c => c[0] === 'activate'), [['activate', 'a'], ['activate', 'a'], ['activate', 'a']]);
  f.fire(list.firstChild.firstChild, 'pointerdown', { pointerType: 'mouse', button: 0 });
  f.fire(list.firstChild.firstChild, 'pointerdown', { pointerType: 'touch', button: 0 });
  f.fire(list.firstChild.firstChild, 'click');
  assert.equal(f.calls.filter(c => c[0] === 'activate').length, 5);
  dispose(); f.fire(list.firstChild, 'click'); assert.equal(f.calls.filter(c => c[0] === 'activate').length, 5);
  f.dom.window.close();
});

test('row actions never navigate; clipboard remains in the gesture and only one feedback timer is retained', async () => {
  const f = fixture(), list = f.d.getElementById('convList'), copied = [];
  let gesture = false; f.ports.sidebar.copy = async id => { assert.ok(gesture); copied.push(id); };
  list.innerHTML = '<div class="conv-item" data-conv-id="a">' + ['dup', 'delete', 'rename', 'ref', 'folder-assign', 'copy-id']
    .map(name => `<button class="conv-${name}" data-conv-id="a" data-conv-title="Title">${name}</button>`).join('') + '</div>';
  const dispose = installApplicationInteractions(f.ports);
  for (const button of list.querySelectorAll('button')) { gesture = true; f.fire(button, 'click'); gesture = false; }
  await drain(); assert.deepEqual(copied, ['a']); assert.equal(f.calls.filter(c => c[0] === 'activate').length, 0);
  const copy = list.querySelector('.conv-copy-id'); assert.equal(copy.textContent, '✓');
  gesture = true; f.fire(copy, 'click'); gesture = false; await drain(); assert.equal(f.timers.size, 1);
  f.tick(1200); assert.equal(copy.textContent, 'copy-id'); dispose(); assert.equal(f.timers.size, 0); f.dom.window.close();
});

test('typing/scroll bursts get one frame each; unchanged height notifications do no repaint and dispose restores geometry', () => {
  const f = fixture(), input = f.d.getElementById('userInput'), chat = f.d.getElementById('chatContainer');
  let reads = 0; Object.defineProperty(input, 'scrollHeight', { get: () => { reads++; return 250; } });
  Object.defineProperty(input, 'clientHeight', { value: 100 });
  const dispose = installApplicationInteractions(f.ports);
  for (let i = 0; i < 100; i++) { f.fire(input, 'input'); f.fire(chat, 'scroll'); }
  assert.equal(f.frames.size, 2); assert.equal(reads, 0); f.frame(); assert.equal(reads, 2); assert.equal(input.style.height, '200px');
  assert.equal(f.calls.filter(c => c[0] === 'turn').length, 1);
  const initialPaints = f.calls.filter(c => c[0] === 'scroll-button').length;
  for (let i = 0; i < 100; i++) f.observers[0].fn();
  assert.equal(f.calls.filter(c => c[0] === 'scroll-button').length, initialPaints);
  f.fire(input, 'input'); f.fire(chat, 'scroll'); dispose(); f.frame(); f.observers[0].fn();
  assert.equal(f.observers.filter(o => o.active).length, 0); assert.equal(f.frames.size, 0);
  assert.equal(f.calls.filter(c => c[0] === 'turn').length, 1); f.dom.window.close();
});

test('log inspection is bounded and never paints over edited text, switched conversations or disposal', async () => {
  const f = fixture(), input = f.d.getElementById('userInput'), replies = []; let active = 0, maxActive = 0;
  f.ports.composer.detectLogNoise = text => new Promise(resolve => { active++; maxActive = Math.max(maxActive, active); replies.push(v => { active--; resolve(v ?? { text }); }); });
  const dispose = installApplicationInteractions(f.ports);
  const paste = text => { f.fire(input, 'paste', { clipboardData: { items: [], getData: () => text } }); input.value = text; f.fire(input, 'input'); };
  paste('a'.repeat(250)); f.tick(50); await drain(); assert.equal(replies.length, 1);
  for (let i = 0; i < 100; i++) paste('b'.repeat(250)); assert.equal(f.timers.size, 1);
  f.tick(50); await drain(); assert.equal(replies.length, 1); replies.shift()(); await drain();
  assert.equal(f.calls.filter(c => c[0] === 'clean').length, 0); f.tick(); await drain(); assert.equal(replies.length, 1);
  replies.shift()(); await drain(); assert.deepEqual(f.calls.filter(c => c[0] === 'clean'), [['clean', 'b'.repeat(250)]]);
  paste('c'.repeat(250)); f.tick(50); await drain(); f.setContext('conv-b'); replies.shift()(); await drain();
  assert.equal(f.calls.filter(c => c[0] === 'clean').length, 1);
  paste('d'.repeat(250)); f.tick(50); await drain(); dispose(); replies.shift()(); await drain();
  assert.equal(f.calls.filter(c => c[0] === 'clean').length, 1); assert.equal(maxActive, 1); assert.equal(f.timers.size + f.frames.size, 0);
  f.dom.window.close();
});

test('settling an inspection preserves one newer paste debounce instead of duplicating it', async () => {
  const f = fixture(), input = f.d.getElementById('userInput'), requests = [];
  f.ports.composer.detectLogNoise = text => new Promise(resolve => requests.push({ text, resolve }));
  const dispose = installApplicationInteractions(f.ports);
  const paste = text => { f.fire(input, 'paste', { clipboardData: { items: [], getData: () => text } }); input.value = text; f.fire(input, 'input'); };
  paste('a'.repeat(250)); f.tick(50); await drain();
  paste('b'.repeat(250)); f.tick(50); await drain();
  paste('c'.repeat(250)); requests[0].resolve({ text: requests[0].text }); await drain();
  assert.equal(f.timers.size, 1); assert.equal(requests.length, 1);
  f.tick(50); await drain(); assert.equal(requests.length, 2); assert.equal(requests[1].text, 'c'.repeat(250));
  requests[1].resolve({ text: requests[1].text }); await drain();
  assert.deepEqual(f.calls.filter(c => c[0] === 'clean'), [['clean', 'c'.repeat(250)]]);
  dispose(); f.dom.window.close();
});

test('file ownership prevents navigation; paper chooses one PDF and chat preserves PDF/image/archive/document order', async () => {
  const f = fixture(), dispose = installApplicationInteractions(f.ports);
  const files = [new f.w.File([''], 'a.png', { type: 'image/png' }), new f.w.File([''], 'b.pdf'),
    new f.w.File([''], 'c.zip'), new f.w.File([''], 'd.docx'), new f.w.File([''], 'e.pdf')];
  const transfer = { types: ['Files'], files };
  f.fire(f.d.body, 'dragenter', { dataTransfer: transfer });
  assert.ok(f.d.getElementById('dropOverlay').classList.contains('visible'));
  f.fire(f.d.body, 'dragleave', { dataTransfer: { types: [], files: [] } });
  assert.ok(!f.d.getElementById('dropOverlay').classList.contains('visible'));
  f.setModal(true); const outside = f.fire(f.d.body, 'drop', { dataTransfer: transfer }); await drain();
  assert.equal(outside.defaultPrevented, true); assert.equal(f.calls.filter(c => c[0] === 'image').length, 0);
  f.setModal(false); f.setPaper(true); f.fire(f.d.body, 'drop', { dataTransfer: transfer }); await drain();
  assert.deepEqual(f.calls.filter(c => c[0] === 'paper'), [['paper', 'b.pdf']]);
  f.setPaper(false); f.fire(f.d.body, 'drop', { dataTransfer: transfer }); await drain();
  assert.deepEqual(f.calls.filter(c => ['pdf', 'image', 'archive', 'document'].includes(c[0])),
    [['image', 'a.png'], ['pdf', 'b.pdf'], ['archive', 'c.zip'], ['document', 'd.docx'], ['pdf', 'e.pdf']]);
  dispose(); f.dom.window.close();
});

test('Escape uses one winning shell target; settings backdrop ignores inner controls', () => {
  const f = fixture(), dispose = installApplicationInteractions(f.ports);
  for (const id of ['applyModal', 'previewModal', 'settingsModal']) f.d.getElementById(id).classList.add('open');
  f.d.getElementById('sidebarSearchWrapper').style.display = '';
  f.fire(f.d, 'keydown', { key: 'Escape' }); assert.equal(f.calls.at(-1)[0], 'closeSearch');
  f.d.getElementById('sidebarSearchWrapper').style.display = 'none';
  f.fire(f.d, 'keydown', { key: 'Escape' }); assert.equal(f.calls.at(-1)[0], 'closeApply');
  f.fire(f.d, 'keydown', { key: 'k', ctrlKey: true }); assert.equal(f.calls.at(-1)[0], 'toggleSearch');
  const settings = f.d.getElementById('settingsModal'); settings.innerHTML = '<button>inside</button>';
  const before = f.calls.length; f.fire(settings.firstChild, 'click'); assert.equal(f.calls.length, before);
  f.fire(settings, 'click'); assert.equal(f.calls.at(-1)[0], 'closeSettings'); dispose(); f.dom.window.close();
});

test('viewport resize bursts and orientation retries are bounded; invalid metrics cannot collapse the page', () => {
  const f = fixture(); f.d.documentElement.style.setProperty('height', '90vh', 'important');
  const dispose = installViewportHeightGuard(f.w, f.d);
  for (let i = 0; i < 100; i++) { f.fire(f.w, 'resize'); f.fire(f.w, 'orientationchange'); }
  assert.equal(f.frames.size, 1); assert.equal(f.timers.size, 2);
  Object.defineProperty(f.w, 'innerHeight', { value: 0, configurable: true }); f.frame(); f.tick(300);
  assert.notEqual(f.d.documentElement.style.height, '0px'); dispose();
  assert.equal(f.d.documentElement.style.height, '90vh'); assert.equal(f.timers.size + f.frames.size, 0); f.dom.window.close();
});

test('partial installation rolls back listeners/observers; late upload failure is visible only while attached', async () => {
  const f = fixture(); f.ports.initializeFolders = () => { throw new Error('folder install fault'); };
  assert.throws(() => installApplicationInteractions(f.ports), /folder install fault/);
  f.d.getElementById('convList').innerHTML = '<div class="conv-item" data-conv-id="a">A</div>';
  f.fire(f.d.getElementById('convList').firstChild, 'click'); assert.equal(f.calls.length, 0);
  f.ports.initializeFolders = () => {};
  f.w.ResizeObserver = class { constructor(fn) { this.active = true; f.observers.push(this); } observe() { throw new Error('observe fault'); } disconnect() { this.active = false; } };
  const dispose = installApplicationInteractions(f.ports); assert.equal(f.observers.filter(o => o.active).length, 0);
  let reject; f.ports.files.handleImage = () => new Promise((_, r) => { reject = r; });
  const image = new f.w.File([''], 'x.png', { type: 'image/png' });
  f.fire(f.d.body, 'drop', { dataTransfer: { types: ['Files'], files: [image] } });
  reject(new Error('upload fault')); await drain(); assert.ok(f.errors.some(e => e.message === 'upload fault'));
  f.fire(f.d.body, 'drop', { dataTransfer: { types: ['Files'], files: [image] } }); dispose();
  reject(new Error('late fault')); await drain(); assert.ok(!f.errors.some(e => e.message === 'late fault')); f.dom.window.close();
});

test('cancellation and teardown fence already queued callbacks even when a host cancellation throws', () => {
  const f = fixture(), scope = createLifecycleScope(f.w); let callbacks = 0;
  const timeout = scope.timeout(() => callbacks++, 1), frame = scope.frame(() => callbacks++);
  const queuedTimeout = f.timers.get(timeout).fn, queuedFrame = f.frames.get(frame);
  scope.cancelTimeout(timeout); scope.cancelFrame(frame); queuedTimeout(); queuedFrame(0); assert.equal(callbacks, 0);
  scope.timeout(() => callbacks++, 1); scope.frame(() => callbacks++);
  const interval = scope.interval(() => callbacks++, 1), queuedInterval = f.intervals.get(interval);
  f.w.clearTimeout = () => { throw new Error('timer release fault'); };
  let cleaned = false; scope.add(() => { cleaned = true; }); scope.destroy();
  queuedInterval(); f.tick(10); f.frame(); assert.equal(callbacks, 0); assert.ok(cleaned);
  assert.equal(f.frames.size + f.intervals.size, 0); assert.deepEqual(scope.pendingTasks, { timeouts: 0, intervals: 0, frames: 0 });
  f.dom.window.close();
});
