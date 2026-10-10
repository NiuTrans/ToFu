const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
function load(name) {
  const exports = {};
  const source = fs.readFileSync(`frontend/src/features/conversation-bar/${name}.ts`, 'utf8');
  new Function('exports', 'require', ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
  } }).outputText)(exports, name => load(name.replace('./', '')));
  return exports;
}
const { createTurnNavigation } = load('turn-navigation');
const copy = { turns: 'Turn', previousTurn: 'Previous turn', nextTurn: 'Next turn', jumpToTurn: 'Jump to turn', turnPosition: 'Turn {current} of {total}', olderTurns: 'Older turns', newerTurns: 'Newer turns', writes: 'Writes', critic: 'Critic', noTurns: 'No turns' };

Object.assign(copy, { loadEarlierHistory:'Load history', loadingHistory:'Loading', historyFailed:'Failed' });
function setup(count = 12, ports = {}) {
  const dom = new JSDOM('<div id="turnNav"></div><div id="chat"></div><button id="outside">Outside</button>');
  const doc = dom.window.document;
  // Any idle work is a regression; scheduling is only the retained scroll fallback.
  dom.window.requestAnimationFrame = dom.window.setTimeout = () => { throw Error('idle work'); };
  const calls = [];
  const state = { laneOrder: { main: [] }, turnsById: {} };
  for (let i = 0; i < count; i++) {
    const id = `stable-${i}`; state.laneOrder.main.push(id);
    state.turnsById[id] = { turnId: id, actor: i === 0 ? 'critic' : 'human', projection: { content: i === 0 ? '<img src=x onerror=alert(1)>' : `Preview ${i}` } };
  }
  const host = doc.getElementById('turnNav');
  const nav = createTurnNavigation({ host, translate: (key, params = {}) => Object.entries(params).reduce((s, [k, v]) => s.replace(`{${k}}`, v), copy[key.split('.').pop()]), scrollToTurn: id => calls.push(id), ...ports });
  nav.update('conversation', state);
  return { dom, doc, nav, host, calls, state };
}
test('compact native steps and stable jump IDs, no dot-per-turn DOM', () => {
  const { nav, host, doc, calls } = setup();
  assert.equal(host.querySelectorAll('button').length, 3);
  assert.equal(host.querySelector('.turn-nav-position').textContent, 'Turn 12 / 12');
  // Slot scales with the total's digits (2*2+3), not a fixed one-size blank.
  assert.equal(host.querySelector('.turn-nav-readout').style.width, '7ch');
  host.querySelector('[data-direction=previous]').click();
  assert.deepEqual(calls, ['stable-10']);
  host.querySelector('[data-direction=next]').click();
  assert.equal(calls.at(-1), 'stable-11');
  host.querySelector('.turn-nav-position').click();
  doc.querySelector('[data-turn-id="stable-2"]').click();
  assert.equal(calls.at(-1), 'stable-2');
  assert.equal(host.querySelector('.turn-nav-position').textContent, 'Turn 3 / 12');
  assert.equal(doc.querySelector('.turn-jump-panel'), null);
  nav.destroy();
});
test('over 1000 turns mount at most 50 rows and every oldest turn remains reachable', () => {
  const { nav, host, doc, calls } = setup(1203);
  assert.equal(host.querySelector('.turn-nav-readout').style.width, '11ch');
  host.querySelector('.turn-nav-position').click();
  let pages = 0;
  while (!doc.querySelector('[data-direction=older]').disabled) {
    assert.ok(doc.querySelectorAll('.turn-jump-entry').length <= 50);
    doc.querySelector('[data-direction=older]').click();
    assert.ok(++pages < 30);
  }
  const oldest = doc.querySelector('[data-turn-id="stable-0"]');
  assert.ok(oldest.textContent.includes('<img src=x onerror=alert(1)>'));
  assert.ok(oldest.textContent.includes('Critic'));
  assert.equal(doc.querySelector('.turn-jump-panel img'), null);
  oldest.click(); assert.equal(calls.at(-1), 'stable-0');
  assert.equal(host.querySelectorAll('button').length, 3);
  nav.destroy();
});
test('stream updates preserve active selection, focused step and keyed disclosure row', () => {
  const { nav, host, doc, state } = setup();
  const prev = host.querySelector('[data-direction=previous]');
  prev.click(); prev.focus();
  nav.update('conversation', state);
  assert.equal(doc.activeElement, prev);
  assert.equal(host.querySelector('.turn-nav-readout').textContent, '11 / 12');
  host.querySelector('.turn-nav-position').click();
  const row = doc.querySelector('[data-turn-id="stable-3"]'); row.focus();
  state.turnsById['stable-3'].projection.content = 'Repaint';
  nav.update('conversation', state);
  assert.equal(doc.activeElement, row); assert.ok(row.textContent.includes('Repaint'));
  // Escape is owned by the common disclosure, not a second document handler.
  doc.dispatchEvent(new doc.defaultView.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(doc.querySelector('.turn-jump-panel'), null);
  const oldPosition = host.querySelector('.turn-nav-position');
  nav.destroy(); oldPosition.click(); assert.equal(doc.querySelector('.turn-jump-panel'), null);
});
test('active detection scans rendered nodes once and missing window does not reset selection', () => {
  const { nav, host, doc } = setup(1203);
  const inner = doc.getElementById('chat');
  const container = doc.createElement('div'); container.getBoundingClientRect = () => ({ top: 0, height: 100 });
  for (const [id, top] of [['stable-401', -20], ['stable-402', 20], ['stable-403', 60]]) {
    const node = doc.createElement('div'); node.dataset.turnId = id;
    node.getBoundingClientRect = () => ({ top }); inner.append(node);
  }
  let scans = 0; const query = inner.querySelectorAll.bind(inner);
  inner.querySelectorAll = (...args) => { scans++; return query(...args); };
  nav.updateActive(container, inner);
  assert.equal(scans, 1); assert.equal(host.querySelector('.turn-nav-readout').textContent, '403 / 1203');
  inner.replaceChildren(); nav.updateActive(container, inner);
  assert.equal(host.querySelector('.turn-nav-readout').textContent, '403 / 1203');
  nav.destroy();
});
test('write evidence remains associated with preceding main input', () => {
  const { nav, host, doc, state } = setup(2);
  state.laneOrder.main.splice(1, 0, 'assistant');
  state.turnsById.assistant = { actor: 'assistant', projection: { modifiedFiles: 1, modifiedFileList: ['src/example.ts'] } };
  nav.update('conversation', state); host.querySelector('.turn-nav-position').click();
  const row = doc.querySelector('[data-turn-id="stable-0"]');
  assert.ok(row.title.includes('Writes')); assert.ok(row.title.includes('example.ts'));
  nav.destroy();
});

test('older history remains reachable with no input loaded, and A-B-A rejects stale completion', async () => {
  const pending = [];
  const { nav, host, doc, state } = setup(0, { loadEarlier: () => new Promise((resolve, reject) => pending.push({ resolve, reject })) });
  state.historyByLane = { main: { hasMore: true, nextBeforeOrdinal: 100 } };
  nav.update('a', state);
  assert.equal(host.querySelector('.turn-nav-position').disabled, false);
  host.querySelector('.turn-nav-position').click();
  doc.querySelector('.turn-jump-load').click();
  assert.equal(pending.length, 1);
  doc.querySelector('.turn-jump-load').click();
  assert.equal(pending.length, 1);
  nav.update('b', state); nav.update('a', state);
  host.querySelector('.turn-nav-position').click();
  doc.querySelector('.turn-jump-load').click();
  assert.equal(pending.length, 2);
  pending[0].reject(Error('stale'));
  await Promise.resolve(); await Promise.resolve();
  assert.equal(doc.querySelector('.turn-jump-error').textContent, '');
  assert.equal(doc.querySelector('.turn-jump-load').disabled, true);
  pending[1].reject(Error('current'));
  await Promise.resolve(); await Promise.resolve();
  assert.equal(doc.querySelector('.turn-jump-error').textContent, 'Failed');
  assert.equal(doc.querySelector('.turn-jump-load').disabled, false);
  nav.destroy();
});
