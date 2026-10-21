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

test('transcript scroll keeps an open directory; only document scroll dismisses it', () => {
  const { nav, host, doc } = setup();
  host.querySelector('.turn-nav-position').click();
  const panel = doc.querySelector('.turn-jump-panel');
  assert.ok(panel);
  // Streaming commits a tool row and the Surface keeps the transcript pinned
  // to the latest turn: an element scroll cannot move the fixed composer
  // anchor, so it must not dismiss the panel.
  doc.getElementById('chat').dispatchEvent(new doc.defaultView.Event('scroll', { bubbles: false }));
  assert.ok(doc.querySelector('.turn-jump-panel'));
  doc.dispatchEvent(new doc.defaultView.Event('scroll'));
  assert.equal(doc.querySelector('.turn-jump-panel'), null);
  nav.destroy();
});

test('a streaming head advance keeps the open directory page instead of blanking it', async () => {
  const { nav, host, doc } = setup(2);
  let head = 'one';
  nav.destroy();
  const exact = createTurnNavigation({ host, translate: key => key, scrollToTurn: () => {}, directoryKey: () => head,
    directory: async (id, offset) => ({ conversationId: id, laneId: 'main', syncSeq: 1, stale: false,
      offset, totalRounds: 2, activeRound: 2, entries: [
        { turnId: 'a', ordinal: 0, actor: 'human', roundNumber: 1 },
        { turnId: 'b', ordinal: 1, actor: 'human', roundNumber: 2 }] }) });
  const state = { laneOrder: { main: ['a', 'b'] }, turnsById: {
    a: { actor: 'human', projection: { content: 'first' } },
    b: { actor: 'human', projection: { content: 'second' } } } };
  exact.update('conv', state);
  await new Promise(resolve => setImmediate(resolve));
  exact.update('conv', state);
  host.querySelector('.turn-nav-position').click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(doc.querySelectorAll('.turn-jump-entry').length, 2);
  head = 'two';
  exact.update('conv', state);
  assert.equal(host.querySelector('.turn-nav-readout').textContent, '2 / 2');
  assert.equal(doc.querySelectorAll('.turn-jump-entry').length, 2);
  exact.destroy();
});

test('a read fenced by a head that advanced underneath it is retried, not reported', async () => {
  const { dom, host, nav } = setup(); nav.destroy();
  const { createTurnNavigation } = load('turn-navigation');
  let head = 'h1'; let reads = 0;
  const exact = createTurnNavigation({ host, translate: key => key, scrollToTurn: () => {},
    directoryKey: () => head,
    directory: async (id, offset) => {
      reads++;
      // The producer of a streamed turn commits a new head while this read is
      // in flight, which is exactly the routine race reported as a failure.
      if (reads === 1) { head = 'h2'; throw new Error('Round directory is stale or mismatched'); }
      return { conversationId: id, laneId: 'main', syncSeq: 2, stale: false, offset,
        totalRounds: 4, activeRound: 4, entries: [
          { turnId: 'a', ordinal: 0, actor: 'human', roundNumber: 1 }] };
    } });
  const state = { laneOrder: { main: ['a'] },
    turnsById: { a: { actor: 'human', projection: { content: 'first' } } } };
  exact.update('conv', state);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(reads, 2);
  assert.equal(host.querySelector('.turn-nav-readout').textContent, '4 / 4');
  assert.equal(dom.window.document.querySelector('.turn-jump-error'), null);
  exact.destroy(); dom.window.close();
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


test('exact directory total, numeric/first/last jump hydrate only targets and keep compact DOM', async () => {
  const {dom, host, calls, nav} = setup(); nav.destroy();
  const {createTurnNavigation} = load('turn-navigation');
  const requests = []; const hydrates = []; const scrolls = [];
  const total = 501;
  const exact = createTurnNavigation({host,
    translate: (key, params) => params ? `${params.current} / ${params.total}` : key,
    scrollToTurn: id => scrolls.push(id), directoryKey: () => 'head',
    directory: async (id, offset, limit, anchor) => {
      requests.push({id, offset, limit, anchor});
      return {conversationId: id, laneId: 'main', syncSeq: 9, stale: false, offset,
        totalRounds: total, activeRound: total,
        entries: Array.from({length: Math.min(limit, total - offset)}, (_, i) => ({
          turnId: `rank-${offset + i + 1}`, ordinal: (offset + i) * 3,
          actor: 'human', roundNumber: offset + i + 1,
        }))};
    }, hydrateRound: async (id, entry) => hydrates.push(entry),
  });
  const partial = ({laneOrder: {main: ['loaded']}, turnsById: {loaded: {actor: 'human', projection: {content: 'loaded'}}}}); partial.historyByLane = {main: {hasMore: true, nextBeforeOrdinal: 1500, totalTurns: 1503}};
  exact.update('a', partial); await new Promise(resolve => setImmediate(resolve));
  assert.equal(host.querySelector('.turn-nav-readout').textContent, '501 / 501');
  assert.equal(host.querySelectorAll('button').length, 3);
  host.querySelector('.turn-nav-position').click(); await new Promise(resolve => setImmediate(resolve));
  const panel = dom.window.document.querySelector('.turn-jump-panel');
  assert.ok(panel.querySelectorAll('.turn-jump-entry').length <= 50);
  const input = panel.querySelector('input'); input.value = '270';
  panel.querySelector('form').dispatchEvent(new dom.window.Event('submit', {bubbles: true, cancelable: true}));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.at(-1).offset, 269); assert.equal(requests.at(-1).limit, 1);
  assert.deepEqual(scrolls, ['rank-270']); assert.deepEqual(hydrates.map(e => e.turnId), ['rank-270']);
  host.querySelector('.turn-nav-position').click(); await new Promise(resolve => setImmediate(resolve));
  panel.querySelector('.turn-jump-first').click(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(scrolls.at(-1), 'rank-1');
  host.querySelector('.turn-nav-position').click(); await new Promise(resolve => setImmediate(resolve));
  panel.querySelector('.turn-jump-last').click(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(scrolls.at(-1), 'rank-501');
  assert.equal(calls.length, 0); exact.destroy(); dom.window.close();
});

test('directory responses and hydrate completions cannot publish after A-B-A or topology invalidation', async () => {
  const {dom, host, nav} = setup(); nav.destroy(); const {createTurnNavigation} = load('turn-navigation');
  const pending = []; const scrolls = []; let version = 0;
  const exact = createTurnNavigation({host, translate: key => key, scrollToTurn: id => scrolls.push(id),
    directoryKey: () => String(version), directory: (id, offset) => new Promise(resolve => pending.push({id, offset, resolve})),
    hydrateRound: async () => {},
  });
  const state = {laneOrder: {main: ['loaded']}, turnsById: {loaded: {actor: 'human', projection: {content: 'loaded'}}}};
  const settle = () => new Promise(resolve => setImmediate(resolve));
  exact.update('a', state); await Promise.resolve();
  exact.update('b', state); await Promise.resolve();
  exact.update('a', state); await Promise.resolve();
  const result = (request, total) => ({conversationId: request.id, laneId: 'main', syncSeq: 1,
    stale: false, totalRounds: total, offset: request.offset, entries: [], activeRound: total});
  // A-B-A: only one read is in flight, and its completion belongs to a head
  // the view has already left, so it never publishes.
  assert.equal(pending.length, 1);
  pending[0].resolve(result(pending[0], 900)); await settle();
  assert.equal(host.querySelector('.turn-nav-readout').textContent, '— / —');
  assert.equal(pending.length, 2);
  pending[1].resolve(result(pending[1], 12)); await settle();
  assert.equal(host.querySelector('.turn-nav-readout').textContent, '12 / 12');
  // A head advance refreshes the shown directory in place: the last good page
  // stays readable while a streamed turn is rendering, and the refresh result
  // replaces it once it lands.
  version++;
  exact.update('a', state); await settle();
  assert.equal(host.querySelector('.turn-nav-readout').textContent, '12 / 12');
  assert.equal(pending.length, 3);
  pending[2].resolve(result(pending[2], 11)); await settle();
  assert.equal(host.querySelector('.turn-nav-readout').textContent, '11 / 11');
  assert.deepEqual(scrolls, []); exact.destroy(); dom.window.close();
});
