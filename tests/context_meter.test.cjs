const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
function load(file) {
  const result = { exports: {} };
  const source = ts.transpileModule(fs.readFileSync(file, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  new Function('require', 'module', 'exports', source)((name) => name.startsWith('.') ? load(path.resolve(path.dirname(file), name + '.ts')) : require(name), result, result.exports);
  return result.exports;
}
const { createContextMeter } = load(path.resolve('frontend/src/features/conversation-bar/context-meter.ts'));
function setup(overrides = {}) {
  const dom = new JSDOM('<div id="convStatusStrip"><button id="projectBar">Project</button><div id="bgActivity"></div><button id="turnNav">Turns</button></div><button id="outside">Outside</button>');
  const doc = dom.window.document;
  let snapshot = { used: 12500, limit: 100000, known: true, exact: false, pctRaw: .125, pct: 13, zone: 'ok', compactions: 3, convId: 'c1', busy: false, hasHistory: true, ...overrides };
  const commands = [];
  const meter = createContextMeter({ host: doc.querySelector('#convStatusStrip'), translate: (_, fallback) => fallback, read: () => snapshot, onCompact: id => commands.push(['compact', id]), onHistory: id => commands.push(['history', id]) });
  meter.el.getClientRects = () => [{ width: 100 }];
  return { dom, doc, meter, commands, update: values => { snapshot = { ...snapshot, ...values }; meter.update(snapshot); } };
}
test('native linear control follows project, keeps stable nodes and normalized fill', () => {
  const { meter, doc, update } = setup();
  assert.equal(meter.el.tagName, 'BUTTON');
  assert.deepEqual([...doc.querySelector('#convStatusStrip').children].map(el => el.id), ['projectBar', 'contextHealthBar', 'bgActivity', 'turnNav']);
  assert.equal(meter.el.querySelector('svg'), null);
  assert.equal(meter.el.querySelector('.ctx-meter-label').textContent, 'Context');
  assert.equal(meter.el.style.getPropertyValue('--ctx-fill'), '12.50%');
  const fill = meter.el.querySelector('.ctx-meter-fill');
  update({ pctRaw: 2, pct: 100, zone: 'crit' });
  assert.equal(meter.el.style.getPropertyValue('--ctx-fill'), '100.00%');
  assert.equal(meter.el.dataset.zone, 'crit');
  assert.equal(meter.el.querySelector('.ctx-meter-fill'), fill);
  update({ compacting: true, compactionPending: true });
  assert.equal(meter.el.dataset.compacting, '1');
  assert.match(meter.el.getAttribute('aria-label'), /Compacting/);
  update({ compacting: false, compactionPending: false });
  assert.equal(meter.el.dataset.compacting, undefined);
  meter.destroy();
});
test('deliberate details show full counts, estimates and actions; Escape and Close restore focus', () => {
  const { meter, doc, dom, commands } = setup();
  meter.el.click();
  const panel = doc.querySelector('#ctxBarPopover');
  assert.equal(panel.getAttribute('role'), 'dialog');
  assert.deepEqual([...panel.querySelectorAll('dd')].map(el => el.textContent), ['12500', '100000 · Estimated', '3']);
  assert.equal(doc.activeElement, panel.querySelector('.ctx-pop-compact'));
  doc.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(doc.activeElement, meter.el);
  assert.equal(doc.querySelector('#ctxBarPopover'), null);
  meter.el.click();
  doc.querySelector('.ctx-pop-compact').click();
  assert.deepEqual(commands, [['compact', 'c1']]);
  meter.el.click();
  doc.querySelector('.ctx-pop-history').click();
  assert.deepEqual(commands[1], ['history', 'c1']);
  meter.el.click();
  doc.querySelector('.ctx-pop-close').click();
  assert.equal(doc.activeElement, meter.el);
  meter.destroy();
});
test('unknown and busy details stay reachable; navigation, outside focus and destroy close disclosure', () => {
  const { meter, doc, update } = setup({ used: 0, limit: null, known: false, busy: true, hasHistory: false, zone: 'unknown' });
  meter.el.click();
  assert.deepEqual([...doc.querySelectorAll('dd')].map(el => el.textContent), ['Unknown', 'Unknown', '3']);
  assert.equal(doc.activeElement, doc.querySelector('.ctx-pop-close'));
  assert.equal(doc.querySelector('.ctx-pop-compact').disabled, true);
  update({ convId: 'c2' });
  assert.equal(doc.querySelector('#ctxBarPopover'), null);
  meter.el.click();
  doc.querySelector('#outside').focus();
  assert.equal(meter.el.getAttribute('aria-expanded'), 'false');
  meter.openLiveSummary('c2');

  update({ convId: 'c3', compacting: false, compactionPending: true });
  assert.equal(doc.querySelector('.ctx-live-summary'), null);
  meter.openLiveSummary('c2');
  assert.equal(doc.querySelector('.ctx-live-summary'), null);
  meter.el.click();
  assert.equal(doc.querySelector('.ctx-pop-compact').disabled, true);
  update({ convId: 'c2', compactionPending: false });
  meter.openLiveSummary('c2');
  meter.appendLiveSummary('other', 'ignored');
  meter.appendLiveSummary('c2', '<b>plain</b>');
  assert.equal(doc.querySelector('.ctx-live-summary-body').textContent, '<b>plain</b>');
  assert.equal(doc.querySelector('.ctx-live-summary-body b'), null);
  meter.el.click();
  meter.destroy();
  assert.equal(doc.querySelector('#ctxBarPopover, #ctxLiveSummary, #contextHealthBar'), null);
});

test('manual command deduplicates POSTs and offscreen summary events cannot reopen another conversation', async () => {
  const vm = require('node:vm');
  const { dom, doc } = setup();
  doc.querySelector('#contextHealthBar').remove();
  const requests = [], toasts = [], subscriptions = new Map();
  const conversations = [{ id:'a' }, { id:'b' }];
  const runtimeScope = {
    ConversationTurnRead:{ state:() => ({}), ordered:() => [] },
    requestAuthoritativeConversationRender() {},
    getCompactionHistory:() => [],
  };
  const sandbox = vm.createContext({
    runtimeScope, window:dom.window, document:doc, createContextMeter,
    getConvById:id => conversations.find(c => c.id === id), activeConvId:'a',
    config:{ model:'test' }, convIsBusy:() => false,
    requestAnimationFrame:callback => callback(), console,
    Api:{compactions:{compactNow:id => new Promise(resolve => requests.push({id,resolve}))}},
    pushSubscribe:(_channel,id,handler) => subscriptions.set(id,handler),
    pushUnsubscribe:(_channel,id) => subscriptions.delete(id),
    showToast:(...args) => toasts.push(args),
  });
  vm.runInContext(fs.readFileSync('frontend/src/runtime/sections/context-bar.js','utf8'),sandbox);
  runtimeScope.updateContextBar();
  const first = runtimeScope.runManualCompaction('a');
  await runtimeScope.runManualCompaction('a');
  assert.equal(requests.length,1);
  const oldEvents = subscriptions.get('a');
  oldEvents({type:'summary_start'});
  assert.ok(doc.querySelector('.ctx-live-summary'));
  sandbox.activeConvId = 'b'; runtimeScope.updateContextBar();
  assert.equal(doc.querySelector('.ctx-live-summary'),null);
  oldEvents({type:'summary_start'});
  assert.equal(doc.querySelector('.ctx-live-summary'),null);
  assert.equal(doc.querySelector('#contextHealthBar').dataset.compacting,undefined);
  doc.querySelector('#contextHealthBar').click();
  assert.equal(doc.querySelector('.ctx-pop-compact').disabled,true);
  await runtimeScope.runManualCompaction('b');
  assert.equal(requests.length,1);
  requests[0].resolve({ok:false,code:'nothing_to_compact'}); await first;
  assert.equal(subscriptions.size,0);
  assert.equal(toasts.length,1);
  const second = runtimeScope.runManualCompaction('b');
  subscriptions.get('b')({type:'summary_start'});
  oldEvents({type:'summary_start'});
  assert.ok(doc.querySelector('.ctx-live-summary'));
  assert.equal(doc.querySelector('#contextHealthBar').dataset.compacting,'1');
  requests[1].resolve({ok:false,code:'nothing_to_compact'}); await second;
  assert.equal(doc.querySelector('.ctx-live-summary'),null);
  dom.window.close();
});
