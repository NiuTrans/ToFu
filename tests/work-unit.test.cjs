/** WorkUnit user journeys and mounted lifecycle boundaries. No network, provider or application store. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'tofu-work-unit-'));
const bundle = path.join(temporary, 'work.cjs');
execFileSync(process.execPath, [path.join(root, 'scripts/vite_test_bundle.mjs'), '--bundle', '--format=cjs', '--platform=node',
  '--loader=ts', '--sourcefile=work-tests.ts', `--outfile=${bundle}`], {
  cwd: root, input: `export { createWorkUnitTracker } from ${JSON.stringify(path.join(root, 'frontend/src/conversation/ui/work-unit/tracker.ts'))};
export { renderMotionCard } from ${JSON.stringify(path.join(root, 'frontend/src/conversation/ui/motion-card/index.ts'))};
export { prefillWorkUnitComposer } from ${JSON.stringify(path.join(root, 'frontend/src/conversation/ui/work-unit/composer.ts'))};
export { historicalTurnPlan } from ${JSON.stringify(path.join(root, 'frontend/src/conversation/ui/history-process.ts'))};
export { blockRoundIdentity } from ${JSON.stringify(path.join(root, 'frontend/src/conversation/presentation/round-outline.ts'))};
export { toolRoundAttention } from ${JSON.stringify(path.join(root, 'frontend/src/conversation/presentation/tool-execution-groups.ts'))};`,
  stdio: ['pipe', 'pipe', 'pipe'],
});
const { createWorkUnitTracker, renderMotionCard, prefillWorkUnitComposer, toolRoundAttention, historicalTurnPlan, blockRoundIdentity } = require(bundle);
test.after(() => fs.rmSync(temporary, { recursive: true, force: true }));
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const text = (_, fallback) => fallback;
function unit(over = {}) { return { kind: 'slides-deck', title: 'A useful deliverable', status: 'running', stages: [{ name: 'author', state: 'active' }],
  pages: { total: 3, rendered: [1, 2], rev: { 1: 1, 2: 1 }, href: '/api/v4/slides/slides_one/pages/{n}.png' },
  links: { events: '/api/v4/tasks/slides_one/events', abort: '/api/v4/tasks/slides_one/abort' }, artifacts: [], ...over }; }
function shell(id = 'slides_one', kind = 'slides-deck') {
  return renderMotionCard({ toolName: 'produce_slides', roundNum: 1,
    toolContent: JSON.stringify({ ok: true, topic: 'A useful deliverable', task_id: 'payload-is-not-authority' }),
    results: [{ workUnitRef: { taskId: id, kind } }] }, '', '', { text });
}
function harness(t, initial = unit(), overrides = {}) {
  const dom = new JSDOM('<!doctype html><body><textarea id="userInput"></textarea>' + shell('slides_one', initial.kind) + '</body>',
    { url: 'http://tofu.test/', pretendToBeVisual: true });
  const doc = dom.window.document, timers = new Map(); let now = 0, sequence = 0, owner = 7, current = initial;
  dom.window.setTimeout = (fn, ms) => { const id = ++sequence; timers.set(id, { fn, at: now + ms }); return id; };
  dom.window.clearTimeout = id => timers.delete(id);
  dom.window.HTMLMediaElement.prototype.load = function () {};
  dom.window.HTMLMediaElement.prototype.pause = function () {};
  dom.window.HTMLMediaElement.prototype.play = function () { return Promise.resolve(); };
  const calls = { snapshots: [], responses: [], aborts: [], scenes: [], regenerations: [], slides: [], prefills: [], notifications: [] };
  const ports = { document: doc, text, ownerId: () => owner,
    getSnapshot: async id => { calls.snapshots.push(id); return { workUnit: structuredClone(current) }; },
    humanResponse: async (...args) => { calls.responses.push(args); return { ok: true }; },
    abort: async id => { calls.aborts.push(id); return { status: 'aborting' }; },
    scenes: async id => { calls.scenes.push(id); return { scenes: [{ scene_id: 'scene-001', has_video: true, content_duration_s: 0 }] }; },
    regenerate: async (...args) => { calls.regenerations.push(args); return { task_id: 'revision_one' }; },
    resolveUrl: value => value, sceneFileUrl: (id, scene) => `/api/v4/motion/videos/${id}/scenes/${scene}/file`,
    openSlides: (...args) => calls.slides.push(args), prefill: message => { calls.prefills.push(message); prefillWorkUnitComposer(doc, message, () => {}); },
    notify: (...args) => calls.notifications.push(args), ...overrides };
  const tracker = createWorkUnitTracker(ports);
  t.after(() => { tracker.dispose(); dom.window.close(); });
  async function tick(ms = 50) {
    const until = now + ms; let turns = 0;
    await flush();
    while (true) {
      const next = [...timers].filter(([, item]) => item.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      assert.ok(++turns < 500, 'presentation must not schedule an unbounded loop');
      const [id, item] = next; timers.delete(id); now = item.at; item.fn(); await flush();
    }
    now = until; await flush();
  }
  const click = label => { const button = [...doc.querySelectorAll('button')].find(e => !e.hidden && e.textContent === label); assert.ok(button, label); button.click(); };
  tracker.demand();
  return { dom, doc, tracker, ports, calls, tick, click, timers, set: next => { current = next; }, owner: next => { owner = next; } };
}

test('accepted work is visible outside disclosures and cannot inherit routine folding', t => {
  const h = harness(t);
  const card = h.doc.querySelector('[data-wu-task]');
  assert.equal(card.tagName, 'ARTICLE'); assert.equal(card.closest('details'), null);
  assert.equal(card.dataset.wuTask, 'slides_one');
  assert.equal(card.querySelector('.ptool-work-receipt').open, false);
  assert.equal(toolRoundAttention({ toolName: 'produce_slides', status: 'done', attentionKind: 'routine', results: [{ workUnitRef: { taskId: 'one' } }] }), 'important');
  const onlyPayload = renderMotionCard({ toolName: 'produce_slides', toolContent: '{"ok":true,"task_id":"unaccepted"}' }, '', '', { text });
  assert.doesNotMatch(onlyPayload, /data-wu-task/);
});

test('page previews honor server revisions, open the exact page and retain focus across a changed neighboring preview', async t => {
  const h = harness(t); await h.tick();
  const first = h.doc.querySelector('[data-wu-page="1"]'), second = h.doc.querySelector('[data-wu-page="2"]');
  assert.equal(h.doc.querySelectorAll('img').length, 2); assert.ok(h.doc.querySelector('.ptool-wu-thumb-pending'));
  second.focus(); second.click(); assert.deepEqual(h.calls.slides, [['slides_one', 2, 3]]);
  h.set(unit({ pages: { total: 3, rendered: [1, 2], rev: { 1: 2, 2: 1 }, href: '/api/v4/slides/slides_one/pages/{n}.png' } }));
  await h.tick(2000);
  assert.strictEqual(h.doc.querySelector('[data-wu-page="1"]'), first);
  assert.strictEqual(h.doc.activeElement, second);
  assert.match(first.querySelector('img').src, /1\.png\?v=2$/);
  assert.equal(h.doc.querySelectorAll('.ptool-wu-download').length, 0);
});

test('a pending human gate is visible and its draft, cursor and focus survive authoritative progress updates', async t => {
  const gate = { guidanceId: 'review-one', title: 'Plan', pages: [{ page: 1, key_message: 'Keep the user’s main idea' }] };
  const h = harness(t, unit({ pendingGate: gate })); await h.tick();
  assert.equal(h.doc.querySelector('[data-wu-status]').textContent, 'Needs your input');
  const input = h.doc.querySelector('.ptool-wu-gate textarea');
  assert.equal(input.closest('details'), null); input.value = 'Please keep this draft'; input.focus(); input.setSelectionRange(3, 8);
  h.set(unit({ pendingGate: { ...gate, title: 'Updated plan' }, stages: [{ name: 'outline_gate', state: 'active' }] }));
  await h.tick(2000);
  assert.strictEqual(h.doc.querySelector('.ptool-wu-gate textarea'), input); assert.equal(input.value, 'Please keep this draft');
  assert.strictEqual(h.doc.activeElement, input); assert.equal(input.selectionStart, 3);
  h.click('Generate as planned'); await flush();
  assert.deepEqual(h.calls.responses, [['review-one', 'Please keep this draft']]);
  assert.equal(h.doc.querySelector('[data-wu-status]').textContent, 'Needs your input');
  assert.equal(input.disabled, true); // Accepted answer is not task completion.
  h.set(unit({ pendingGate: undefined })); await h.tick(5000);
  assert.equal(h.doc.querySelector('.ptool-wu-gate').hidden, true);
});

test('review errors keep the input and expose retry; duplicate cards cannot submit the same gate twice', async t => {
  const wait = deferred(), gate = { guidanceId: 'gate', pages: [] };
  let responses = 0;
  const h = harness(t, unit({ pendingGate: gate }), { humanResponse: () => { responses++; return wait.promise; } });
  h.doc.body.insertAdjacentHTML('beforeend', shell()); await h.tick();
  const inputs = h.doc.querySelectorAll('.ptool-wu-gate textarea'); inputs[0].value = 'My revision';
  h.click('Submit changes'); h.click('Submit changes');
  assert.equal(responses, 1); assert.ok([...h.doc.querySelectorAll('.ptool-wu-gate button')].every(e => e.disabled));
  wait.reject(new Error('offline')); await flush();
  assert.equal(inputs[0].value, 'My revision'); assert.equal(inputs[0].disabled, false);
  assert.match(h.doc.querySelector('.ptool-wu-gate .ptool-wu-action-message').textContent, /not confirmed/);
});

test('failed work keeps partial evidence without pending promises or download; degraded quality remains explicit', async t => {
  const h = harness(t, unit({ status: 'failed', failure: { message: 'Export failed', hint: 'Correct the source image.' }, artifacts: [{ label: 'pptx', href: '/api/v4/slides/one/file' }] }));
  await h.tick(); assert.equal(h.doc.querySelectorAll('img').length, 2);
  assert.equal(h.doc.querySelectorAll('.ptool-wu-thumb-pending,.ptool-wu-download').length, 0);
  assert.match(h.doc.querySelector('.ptool-wu-outcome').textContent, /Correct the source/);
  assert.equal(h.tracker.snapshot().polling, false);
  const other = harness(t, unit({ status: 'degraded', quality: { reason: 'Replacement images used' }, artifacts: [{ label: 'pptx', href: '/api/v4/slides/one/file' }] }));
  await other.tick(); assert.match(other.doc.querySelector('.ptool-wu-outcome').textContent, /Replacement/);
  assert.ok(other.doc.querySelector('.ptool-wu-download'));
});

test('mounted duplicate cards share one read, reattached terminal cards hydrate, and removal releases observer and timers', async t => {
  const h = harness(t, unit({ status: 'done' }));
  h.doc.body.insertAdjacentHTML('beforeend', shell()); await h.tick();
  assert.equal(h.calls.snapshots.length, 1); assert.equal(h.tracker.snapshot().views, 2);
  await h.tick(10000); assert.equal(h.calls.snapshots.length, 1);
  h.doc.body.insertAdjacentHTML('beforeend', shell()); await h.tick();
  assert.equal(h.calls.snapshots.length, 1);
  assert.equal(h.doc.querySelectorAll('[data-wu-status][data-state="done"]').length, 3);
  h.doc.querySelectorAll('.ptool-work-card').forEach(e => e.remove()); await h.tick();
  assert.deepEqual(h.tracker.snapshot(), { tasks: 0, views: 0, requests: 0, polling: false, observing: false });
  assert.equal(h.timers.size, 0);
  h.doc.body.insertAdjacentHTML('beforeend', shell()); h.tracker.demand(); await h.tick();
  assert.equal(h.calls.snapshots.length, 2); assert.equal(h.doc.querySelector('[data-wu-status]').textContent, 'Finished');
});

test('late reads cannot repaint after owner change or detached cards; hidden pages stop polling', async t => {
  const read = deferred();
  const h = harness(t, unit(), { getSnapshot: () => read.promise }); await h.tick();
  h.owner(8); read.resolve({ workUnit: unit({ status: 'done', title: 'Old owner result' }) }); await flush();
  assert.doesNotMatch(h.doc.body.textContent, /Old owner result/);
  const fresh = harness(t); await fresh.tick();
  Object.defineProperty(fresh.doc, 'hidden', { configurable: true, value: true });
  fresh.doc.dispatchEvent(new fresh.dom.window.Event('visibilitychange')); await fresh.tick(10000);
  assert.equal(fresh.calls.snapshots.length, 1); assert.equal(fresh.tracker.snapshot().polling, false);
  Object.defineProperty(fresh.doc, 'hidden', { configurable: true, value: false });
  fresh.doc.dispatchEvent(new fresh.dom.window.Event('visibilitychange')); await flush();
  assert.equal(fresh.calls.snapshots.length, 2);
});

test('a failed refresh retains authoritative status and explicit connection feedback', async t => {
  const h = harness(t); await h.tick();
  h.ports.getSnapshot = async () => null;
  await h.tick(2000);
  assert.equal(h.doc.querySelector('[data-wu-status]').textContent, 'In progress');
  assert.equal(h.doc.querySelector('.ptool-wu-connection').hidden, false);
  h.ports.getSnapshot = async () => ({ workUnit: unit({ status: 'done' }) });
  h.click('Retry'); await flush();
  assert.equal(h.doc.querySelector('[data-wu-status]').textContent, 'Finished');
  assert.equal(h.doc.querySelector('.ptool-wu-connection').hidden, true);
});

test('stopping uses the existing task endpoint and waits for the server terminal receipt', async t => {
  const h = harness(t); await h.tick(); h.click('Stop production'); await flush();
  assert.deepEqual(h.calls.aborts, ['slides_one']);
  assert.equal(h.doc.querySelector('[data-wu-status]').textContent, 'In progress');
  assert.ok([...h.doc.querySelectorAll('button')].some(e => e.textContent === 'Stopping…' && e.disabled));
  h.set(unit({ status: 'aborted' })); await h.tick(5000);
  assert.equal(h.doc.querySelector('[data-wu-status]').textContent, 'Stopped');
  assert.equal(h.tracker.snapshot().polling, false);
});

test('video and scenes load only on intent; an async terminal scene receipt still hydrates and revisions get a visible task', async t => {
  const scenes = deferred(); let sceneCalls = 0;
  const video = unit({ kind: 'motion-video', status: 'done', pages: undefined, artifacts: [{ label: 'mp4', href: '/api/v4/motion/videos/one/file' }] });
  const h = harness(t, video, { scenes: () => { sceneCalls++; return scenes.promise; } }); await h.tick();
  assert.equal(h.doc.querySelector('video').hasAttribute('src'), false); assert.equal(sceneCalls, 0);
  h.click('Play video'); assert.match(h.doc.querySelector('video').src, /motion\/videos\/one\/file$/);
  const player = h.doc.querySelector('video');
  const disclosure = h.doc.querySelector('.ptool-wu-scenes'); disclosure.open = true; disclosure.dispatchEvent(new h.dom.window.Event('toggle'));
  assert.equal(sceneCalls, 1); scenes.resolve({ scenes: [{ scene_id: 'scene-001', has_video: true, content_duration_s: 0 }] }); await flush();
  assert.strictEqual(h.doc.querySelector('video'), player); assert.match(h.doc.querySelector('.ptool-wu-scene').textContent, /0s/);
  h.click('Edit'); assert.match(h.calls.prefills[0], /scene-001/);
  h.click('Re-render'); await flush(); await h.tick();
  assert.deepEqual(h.calls.regenerations, [['slides_one', 'scene-001']]);
  assert.ok([...h.doc.querySelectorAll('.ptool-work-card')].some(e => e.dataset.wuTask === 'revision_one'));
});

test('revision controls preserve the human composer draft and never automatically send', async t => {
  const h = harness(t, unit({ status: 'done' })); await h.tick();
  const composer = h.doc.getElementById('userInput'); composer.value = 'Keep my existing request.';
  let inputs = 0; composer.addEventListener('input', () => inputs++);
  h.click('Request changes');
  assert.match(composer.value, /^Keep my existing request\.\n\n/); assert.match(composer.value, /slides_one/);
  assert.strictEqual(h.doc.activeElement, composer); assert.equal(inputs, 1);
});

test('untrusted titles and resource paths remain text; another guidance identity gets a fresh form', async t => {
  const h = harness(t, unit({ title: '<img src=x onerror=bad()>', pendingGate: { guidanceId: 'one', pages: [{ page: 1, key_message: '<script>bad()</script>' }] },
    artifacts: [{ label: 'pptx', href: 'javascript:bad()' }], pages: { total: 1, rendered: [1], href: 'https://outside.test/{n}', rev: {} } }));
  await h.tick(); assert.equal(h.doc.querySelectorAll('script,img').length, 0);
  assert.match(h.doc.querySelector('[data-wu-title]').textContent, /<img/);
  const old = h.doc.querySelector('.ptool-wu-gate textarea'); old.value = 'Previous answer';
  h.set(unit({ pendingGate: { guidanceId: 'two', pages: [] } })); await h.tick(2000);
  assert.equal(h.doc.querySelector('.ptool-wu-gate textarea').value, '');
});


test('a completed chat turn keeps accepted work outside Process and round disclosures', () => {
  const work = { blockId: 'work', kind: 'tool', source: { llmRound: 0 }, round: { status: 'done', llmRound: 0, results: [{ workUnitRef: { taskId: 'deck', kind: 'slides-deck' } }] } };
  const ordinary = { blockId: 'ordinary', kind: 'tool', source: { llmRound: 0 }, round: { status: 'done', llmRound: 0, results: [{ task_id: 'not-authority' }] } };
  const final = { blockId: 'final', kind: 'text', terminal: true, markdown: 'The task has started.', source: { llmRound: 1 } };
  const plan = historicalTurnPlan({ actor: 'assistant', role: 'assistant', status: 'completed', metadata: {}, blocks: [ordinary, work, final] });
  assert.deepEqual(plan.outside.map(block => block.blockId), ['final', 'work']);
  assert.deepEqual(plan.process.map(block => block.blockId), ['ordinary']);
  assert.equal(blockRoundIdentity(work), null);
  assert.notEqual(blockRoundIdentity(ordinary), null);
});

test('cards mounted before identity is available hydrate on owner demand', async t => {
  const h = harness(t); h.owner(null); await h.tick(); assert.equal(h.calls.snapshots.length, 0);
  assert.equal(h.tracker.snapshot().observing, false);
  h.owner(7); h.tracker.demand(); await h.tick();
  assert.equal(h.calls.snapshots.length, 1); assert.equal(h.tracker.snapshot().tasks, 1);
});


test('approval without feedback sends the shared protocol reply, regardless of UI language', async t => {
  const h = harness(t, unit({pendingGate:{guidanceId:'plan',pages:[]}})); await h.tick();
  h.click('Generate as planned'); await flush();
  assert.deepEqual(h.calls.responses, [['plan', 'continue']]);
});

test('ordinary transcript streaming does not schedule card rescans or reads', async t => {
  const h = harness(t, unit({status:'done'})); await h.tick(); await h.tick();
  const text = h.doc.createElement('p'); h.doc.body.append(text); await flush();
  for (let n=0; n<50; n++) text.append(h.doc.createTextNode('token '));
  await flush(); assert.equal(h.timers.size, 0); assert.equal(h.calls.snapshots.length, 1);
});


test('scene preview preserves the API registry forwarded path without adding its prefix twice', async t => {
  const h = harness(t, unit({kind:'motion-video',status:'done',pages:undefined,artifacts:[{label:'mp4',href:'/api/v4/motion/videos/slides_one/file'}]}), {
    resolveUrl: path => '/forwarded' + path,
    sceneFileUrl: () => '/forwarded/api/v4/motion/videos/slides_one/scenes/scene-001/file'
  });
  await h.tick(); const scenes=h.doc.querySelector('.ptool-wu-scenes');scenes.open=true;scenes.dispatchEvent(new h.dom.window.Event('toggle'));await flush();
  h.click('Preview');
  assert.equal(h.doc.querySelector('video').getAttribute('src'), '/forwarded/api/v4/motion/videos/slides_one/scenes/scene-001/file');
});
