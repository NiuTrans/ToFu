"""Perception evidence must cross actual DOM commit, not render scheduling."""
from pathlib import Path
import shutil
import subprocess

import pytest
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which('node') is None, reason='node required')
def test_runtime_commit_mount_revision_and_snapshot_receipts():
    bundle = native_module_path(
        'runtime-perception-commit.js', ROOT / 'frontend/src/core/turn-runtime.ts')
    tracker = native_module_path(
        'runtime-mount-tracker.js',
        ROOT / 'frontend/src/conversation/application/turn-mount-tracker.ts')
    harness = r"""
const assert = require('node:assert/strict');
global.window = globalThis;
global.document = { visibilityState: 'visible' };
let now = 1000, frameId = 0;
Date.now = () => now;
const frames = new Map();
global.requestAnimationFrame = cb => { frames.set(++frameId, cb); return frameId; };
global.cancelAnimationFrame = id => frames.delete(id);
require(process.argv[1]);
require(process.argv[2]);
const tick = () => new Promise(r => setTimeout(r, 0));
function frame() {
  const work = [...frames.values()]; frames.clear();
  for (const cb of work) cb();
}
async function paint() { frame(); frame(); await tick(); }
class Source {
  addEventListener() {} removeEventListener() {} close() {}
}
const A = { id: 'A' }, B = { id: 'B' };
let active = 'A', mounted = new Set(), pending = new Map();

let activeConvId = 'A';
const nodes = new Map(), removals = [], cleanups = [];
let createdObservers = 0, disconnectedObservers = 0;
const retainedCompositionLifecycle = { add(cleanup) { cleanups.push(cleanup); } };
global.CSS = { escape: value => value };
global.MutationObserver = class {
  constructor(callback) { this.callback = callback; createdObservers++; }
  observe() {}
  disconnect() { disconnectedObservers++; }
  takeRecords() { return removals.splice(0); }
};
document.getElementById = () => ({ querySelector(selector) {
  const id = selector.match(/"(.*)"/)[1];
  if (!mounted.has(id)) return null;
  if (!nodes.has(id)) nodes.set(id, { nodeType: 1, querySelectorAll: () => [] });
  return nodes.get(id);
} });
const createTokenBridge = () => MOUNT_TOKEN_BRIDGE;
let getTurnMountToken = createTokenBridge();
const sent = [], sources = new Map(), errors = [];
const turn = (id, status = 'running') => ({
  turnId: 't-' + id, presentationId: 't-' + id, conversationId: id,
  laneId: 'main', ordinal: 1, actor: 'assistant', kind: 'assistant', runId: '',
  status, currentAttemptId: 'a-' + id, projection: { content: '' },
  projectionRevision: 1, settlement: {}, createdAt: 1, updatedAt: 1,
});
const attempt = (id) => ({ attemptId: 'a-' + id, conversationId: id,
  turnId: 't-' + id, commandId: 'c-' + id, taskId: 'task-' + id,
  operation: 'submit', status: 'running', baseProjectionRevision: 0,
  resumeAnchor: {}, createdAt: 1, startedAt: 1 });
const snapshot = id => ({
  ok: true, contract: 'tofu.conversation-sync.snapshot/v1',
  scope: { kind: 'conversation', id }, conversationId: id,
  conversationRevision: 1, syncSeq: 0, cursor: 'cursor-0', serverBootId: 'boot',
  heartbeatIntervalMs: 15000, settings: {}, turns: [turn(id)],
  attempts: [attempt(id)], queueItems: [],
  activeAttemptPhase: { turnId: 't-' + id, attemptId: 'a-' + id,
    projectionRevision: 1, phase: { phase: 'waiting_model', emittedAt: 900 } },
});
const appliedStates = [];
const runtime = globalThis.createConversationTurnRuntime({
  api: { snapshot: async id => snapshot(id), eventsUrl: id => id,
    recordPerception: async (c, t, body) => sent.push({ c, t, ...body }) },
  findConversation: id => id === 'A' ? A : B,
  isActive: conv => conv.id === active,
  onStateApplied: (conv, state, update) => appliedStates.push({id:conv.id,
    revision:state.conversationRevision, previous:update?.previousState,
    action:update?.action.type}),
  isTurnMounted: (c, t) => active === c && mounted.has(t),

  getTurnMountToken: (c, t) => { activeConvId = active; return getTurnMountToken(c, t); },
  renderState: (conv, state) => { pending.set(conv.id, state); return true; },
  eventSourceFactory: id => { const s = new Source(); sources.set(id, s); return s; },
  onProtocolError: e => errors.push(String(e)),
});
function commit(id, ids = ['t-' + id]) {
  const state = pending.get(id) || runtime.readRuntimeState(id);
  mounted = new Set(ids);
  runtime.notifyRenderedState(id, state, ids);
}
function event(id, seq, revision, type = 'projection_updated') {
  const payload = type === 'terminal_settlement'
    ? { status: 'completed', settlement: {}, phase: null }
    : { phase: { phase: 'stream_stalled', emittedAt: now - 100 } };

  payload.projection = { content: type === 'terminal_settlement' ? 'done' : '' };
  sources.get(id).onmessage({ lastEventId: 'cursor-' + seq, data: JSON.stringify({
    contract: 'tofu.conversation-sync.event/v1', conversationId: id,
    type: 'attempt.event', syncSeq: seq, occurredAt: now - 50,
    payload: { event: { conversationId: id, turnId: 't-' + id,
      attemptId: 'a-' + id, seq, projectionRevision: revision, type, payload } },
  }) });
}
(async () => {
  await runtime.hydrateConversation(A);
  assert.ok(appliedStates.some(row=>row.id==='A'), 'canonical observers run before any DOM commit');
  await paint(); assert.equal(sent.length, 0, 'scheduled render is not commit');
  commit('A', []); await paint(); assert.equal(sent.length, 0, 'windowed turn not painted');
  commit('A'); await paint(); assert.equal(sent.length, 1);
  assert.equal(sent[0].receivedAt, undefined, 'restore must not invent network time');
  commit('A'); await paint(); assert.equal(sent.length, 1, 'same-node commit dedup');
  for (const replace of [false, true]) {
    // Start a fresh mount, run only frame one, then switch DOM lifetime.
    removals.push({ removedNodes: [nodes.get('t-A')] });
    commit('A'); frame();
    const stale = [...frames.values()];
    const oldNode = nodes.get('t-A');
    mounted.clear();
    removals.push({ removedNodes: [oldNode] });
    if (replace) nodes.set('t-A', { nodeType: 1, querySelectorAll: () => [] });
    const before = sent.length;
    commit('A');
    assert.equal(frames.size, 1, 'old second frame cancelled, fresh first frame scheduled');
    for (const cb of stale) cb();
    assert.equal(sent.length, before, 'retired callback cannot send');
    frame(); await tick(); assert.equal(sent.length, before, 'fresh mount needs two frames');
    frame(); await tick(); assert.equal(sent.length, before + 1);
    assert.equal(sent.at(-1).clientDroppedBefore, replace ? 2 : 1,
      'each interrupted opportunity is retired exactly once');
    assert.equal(sent.at(-1).receivedAt, undefined);
    commit('A'); await paint(); assert.equal(sent.length, before + 1);
  }
  sent.splice(1);
  now = 1100; event('A', 1, 2); await paint();
  assert.ok(appliedStates.some(row=>row.action==='event'
    && row.previous.turnsById['t-A'].projectionRevision===1),
    'canonical observers receive the previous revision before the new DOM commit');
  assert.equal(sent.length, 1, 'applied event still awaits DOM commit');
  now = 1140; commit('A'); await paint();
  assert.equal(sent.length, 2); assert.equal(sent[1].receivedAt, 1100);
  assert.equal(sent[1].paintedAt, 1140);
  now = 1200; event('A', 2, 3); commit('A');
  mounted.clear(); await paint(); assert.equal(sent.length, 2, 'unmounted before paint');
  commit('A'); active = 'B'; runtime.reconcileConversationActivity('A');
  await paint(); assert.equal(sent.length, 2, 'switch cancels pending paint');
  await runtime.hydrateConversation(B); commit('B'); await paint();
  const beforeBackground = sent.length;
  now = 1300; event('A', 3, 4, 'terminal_settlement'); await paint();
  assert.equal(sent.length, beforeBackground, 'background settlement is not painted');
  active = 'A'; runtime.reconcileConversationActivity('B', 'A');
  commit('A'); await paint();
  assert.equal(sent.at(-1).kind, 'terminal_painted');
  const afterTerminal = sent.length;
  commit('A'); await paint(); assert.equal(sent.length, afterTerminal, 'duplicate terminal');
  const beforeHealth = sent.length;
  sources.get('B').onerror();
  await paint(); assert.equal(sent.length, beforeHealth, 'background health not painted');
  active = 'B'; runtime.reconcileConversationActivity('A', 'B');
  commit('B'); await paint();
  assert.equal(sent.at(-1).kind, 'transport_degraded');
  sources.get('B').onopen(); await paint();
  const beforeHealthCommit = sent.length;
  commit('B'); await paint();
  assert.equal(sent.length, beforeHealthCommit + 1);
  assert.equal(sent.at(-1).kind, 'transport_recovered');
  assert.deepEqual(errors, []);
  // RAF alone is not DOM-lifetime evidence on a minimal host.
  delete global.MutationObserver;
  getTurnMountToken = createTokenBridge();
  assert.equal(getTurnMountToken('B', 't-B'), null);
  const beforeUnavailable = sent.length;
  now = 1400; event('B', 1, 2); commit('B'); await paint();
  assert.equal(sent.length, beforeUnavailable, 'missing mount observer fails closed');
  runtime.disposeConversation('A'); runtime.disposeConversation('B');
  assert.equal(createdObservers, 1, 'one lazy observer regardless of turns/commits');
  for (const cleanup of cleanups) { cleanup(); cleanup(); }
  assert.equal(disconnectedObservers, 1, 'composition cleanup is idempotent');
  assert.equal(frames.size, 0); console.log('ok');
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
    adapter = (ROOT / 'frontend/src/runtime/sections/main/conversation_turn_store.js').read_text()
    bridge = adapter.split('getTurnMountToken: ', 1)[1].split('\n    isTurnMounted', 1)[0]
    harness = harness.replace('MOUNT_TOKEN_BRIDGE', bridge.rstrip().removesuffix(','))
    proc = subprocess.run(['node', '-e', harness, str(bundle), str(tracker)], cwd=ROOT,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.strip() == 'ok'


@pytest.mark.skipif(shutil.which('node') is None, reason='node required')
def test_mount_tracker_real_dom_lifetimes_and_cleanup():
    tracker = native_module_path(
        'mount-tracker-real-dom.js',
        ROOT / 'frontend/src/conversation/application/turn-mount-tracker.ts')
    harness = r"""
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
require(process.argv[1]);
const dom = new JSDOM('<main><div data-turn-id="t"><span>old</span></div></main>');
const document = dom.window.document;
const main = document.querySelector('main');
const node = main.firstElementChild;
let observers = 0, disconnects = 0;
class Observer extends dom.window.MutationObserver {
  constructor(callback) { super(callback); observers++; }
  disconnect() { disconnects++; super.disconnect(); }
}
const tracker = createTurnMountTracker({ root: document, observer: Observer,
  findTurn: () => document.querySelector('[data-turn-id="t"]') });
(async () => {
  assert.equal(observers, 0, 'no observer before evidence is queried');
  const first = tracker.getToken('c', 't');
  assert.equal(observers, 1);
  node.innerHTML = '<b>new</b>';
  assert.equal(tracker.getToken('c', 't'), first, 'in-place content commit is same mount');
  node.remove(); main.append(node);
  const reinserted = tracker.getToken('c', 't');
  assert.notEqual(reinserted, first, 'takeRecords fences same-task remove/reinsert');
  main.remove(); document.body.append(main);
  await Promise.resolve();
  const ancestorRemount = tracker.getToken('c', 't');
  assert.notEqual(ancestorRemount, reinserted, 'callback retires descendants of removed roots');
  node.replaceWith(node.cloneNode(true));
  const replacement = tracker.getToken('c', 't');
  assert.notEqual(replacement, ancestorRemount);
  assert.equal(observers, 1, 'all mount lifetimes share one observer');
  tracker.destroy(); tracker.destroy();
  assert.equal(disconnects, 1);
  assert.equal(tracker.getToken('c', 't'), null, 'destroyed tracker cannot mint evidence');
  assert.equal(observers, 1);
  const unsupported = createTurnMountTracker({ root: document,
    findTurn: () => { throw new Error('must not inspect DOM without observation capability'); } });
  assert.equal(unsupported.getToken('c', 't'), null);
  unsupported.destroy(); dom.window.close(); console.log('ok');
})().catch(error => { dom.window.close(); console.error(error); process.exitCode = 1; });
"""
    proc = subprocess.run(['node', '-e', harness, str(tracker)], cwd=ROOT,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.strip() == 'ok'
