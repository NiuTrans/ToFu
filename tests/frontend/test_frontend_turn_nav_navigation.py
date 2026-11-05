"""Specify the retained TurnStore/Surface bridge, not the typed navigation UI.

The compact UI itself is covered by tests/turn_navigation.test.cjs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const calls = Object.fromEntries([
  'scroll', 'reveal', 'render', 'create', 'update', 'active', 'destroy',
  'state', 'key', 'directory', 'hydrate', 'earlier', 'warn', 'translate',
].map(name => [name, []]));
const frames = [];
function node(id) {
  return {
    id, dataset: { turnId: id },
    scrollIntoView: (...args) => calls.scroll.push([id, 'scrollIntoView', ...args]),
    scrollTo: (...args) => calls.scroll.push([id, 'scrollTo', ...args]),
    get scrollTop() { return 0; },
    set scrollTop(value) { calls.scroll.push([id, 'scrollTop', value]); },
    getBoundingClientRect: () => ({ top: 0, height: 200 }),
    querySelector: () => target,
    querySelectorAll: () => [target],
  };
}
const host = node('turnNav');
const container = node('chatContainer');
const inner = node('chatInner');
const target = node('turn-durable-257');
const nodes = new Map([host, container, inner, target].map(node => [node.id, node]));
const convA = { id: 'conv-a', ownerId: 7 };
const convB = { id: 'conv-b', ownerId: 9 };
for (const conv of [convA, convB]) Object.defineProperty(conv, 'messages', {
  get() { assert.fail('TurnStore, not positional messages, owns navigation state'); },
});
const states = new Map([
  [convA.id, { conversationRevision: 1, laneOrder: { main: [target.id] }, turnsById: {} }],
  [convB.id, { conversationRevision: 2, laneOrder: { main: [] }, turnsById: {} }],
]);
let activeConv = convA;
let revealResult = true;
const directoryResult = { conversationId: convA.id, entries: [] };
let hydrationResult = Promise.resolve();
// runtimeScope ports belong on window; bare retained owners belong in the VM.
const scope = {
  ConversationSurfacePresentation: {
    revealTurn: (...args) => { calls.reveal.push(args); return revealResult; },
  },
  requestAuthoritativeConversationRender(id, options) {
    assert.equal(options.forceScroll, false);
    calls.render.push(id);
    return true;
  },
  ConversationTurnStore: {
    ensureRuntimeStore(id) {
      calls.state.push(id);
      return { getState: () => states.get(id) };
    },
    roundDirectoryKey(id) { calls.key.push(id); return `head:${id}`; },
    loadConversationRoundDirectory(...args) {
      calls.directory.push(args); return directoryResult;
    },
    hydrateConversationRound(...args) {
      calls.hydrate.push(args); return hydrationResult;
    },
    loadConversationTurnPage(...args) { calls.earlier.push(args); },
  },
  scrollTo: (...args) => calls.scroll.push(['window', 'scrollTo', ...args]),
};
const bridge = vm.createContext({
  window: scope,
  document: {
    getElementById: id => nodes.get(id) ?? null,
    querySelector: () => target,
    querySelectorAll: () => [target],
  },
  getActiveConv: () => activeConv,
  _getChatContainer: () => container,
  requestAnimationFrame: callback => { frames.push(callback); return frames.length; },
  console: { warn: (...args) => calls.warn.push(args) },
  t: (key, params) => { calls.translate.push([key, params]); return `translated:${key}`; },
  createTurnNavigation(ports) {
    calls.create.push(ports);
    return {
      update: (...args) => calls.update.push(args),
      updateActive: (...args) => calls.active.push(args),
      destroy: () => calls.destroy.push(ports.host),
    };
  },
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), bridge, {
  filename: process.argv[1],
});
function navigation() {
  bridge.buildTurnNav(activeConv);
  assert.equal(calls.create.length, 1);
  return calls.create[0];
}
function advanceFrame() {
  assert.equal(frames.length, 1, 'one convergence frame must be pending');
  frames.shift()();
}
"""


def _run(body: str, *args: str) -> None:
    script = _HARNESS + "\nrequire('node:test')('retained turn navigation bridge', async () => {\n" + body + r"""
  assert.deepEqual(calls.scroll, [], 'only Surface may scroll the transcript');
});
"""
    proc = subprocess.run(
        ['node', '-e', script, runtime_section_path('ui/turn_nav.js'), *args],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f'{proc.stderr}\n{proc.stdout}'


def test_stable_turn_id_delegates_to_surface_without_native_scrolling():
    _run(r"""
const ports = navigation();
ports.scrollToTurn(target.id);
assert.deepEqual(calls.reveal, [[convA.id, target.id]]);
assert.deepEqual(calls.render, []);
assert.deepEqual(frames, []);
""")


@pytest.mark.parametrize('eventual_reveal', ['true', 'false'], ids=['mounted', 'unavailable'])
def test_false_reveal_requests_convergence_then_retries_same_identity(eventual_reveal):
    _run(r"""
const ports = navigation();
revealResult = false;
ports.scrollToTurn(target.id);
assert.deepEqual(calls.reveal, [[convA.id, target.id]]);
assert.deepEqual(calls.render, [convA.id]);
advanceFrame();
assert.deepEqual(calls.reveal, [[convA.id, target.id]], 'first RAF is not a retry');
revealResult = process.argv[2] === 'true';
advanceFrame();
assert.deepEqual(calls.reveal, [[convA.id, target.id], [convA.id, target.id]]);
assert.deepEqual(calls.render, [convA.id]);
assert.deepEqual(frames, [], 'convergence is bounded, not polling');
assert.equal(calls.warn.length, revealResult ? 0 : 1);
if (!revealResult) assert.equal(calls.warn[0].at(-1), target.id);
""", eventual_reveal)


@pytest.mark.parametrize('completed_frames', [0, 1], ids=['before-layout', 'between-layouts'])
@pytest.mark.parametrize('next_conv', ['conv-b', 'none'], ids=['switched', 'closed'])
def test_inactive_conversation_blocks_delayed_reveal(completed_frames, next_conv):
    _run(r"""
const ports = navigation();
revealResult = false;
ports.scrollToTurn(target.id);
for (let i = 0; i < Number(process.argv[2]); i++) advanceFrame();
activeConv = process.argv[3] === 'none' ? null : convB;
bridge.buildTurnNav(activeConv);
for (let i = 0; i < 2 && frames.length; i++) advanceFrame();
assert.deepEqual(calls.reveal, [[convA.id, target.id]]);
assert.deepEqual(calls.render, [convA.id]);
assert.deepEqual(calls.warn, []);
assert.deepEqual(frames, []);
""", str(completed_frames), next_conv)


def test_invalid_ids_and_missing_conversation_do_not_navigate():
    _run(r"""
const ports = navigation();
for (const id of ['', undefined, null, 0, 257, {}, []]) ports.scrollToTurn(id);
activeConv = null;
ports.scrollToTurn(target.id);
delete bridge.getActiveConv;
ports.scrollToTurn(target.id);
assert.deepEqual(calls.reveal, []);
assert.deepEqual(calls.render, []);
assert.deepEqual(frames, []);
""")


def test_bridge_forwards_live_turn_state_and_active_geometry_to_typed_navigation():
    _run(r"""
const ports = navigation();
assert.equal(ports.host, host);
assert.deepEqual(calls.update, [[convA.id, states.get(convA.id)]]);
const latest = { conversationRevision: 3, laneOrder: { main: [] }, turnsById: {} };
states.set(convA.id, latest);
bridge.buildTurnNav(convA);
assert.equal(calls.update.at(-1)[1], latest);
activeConv = convB;
bridge.buildTurnNav(activeConv);
assert.deepEqual(calls.update.at(-1), [convB.id, states.get(convB.id)]);
assert.equal(calls.create.length, 1, 'same host reuses the typed navigation owner');
bridge.updateActiveTurn();
assert.deepEqual(calls.active, [[container, inner]]);
const stateReads = calls.state.length;
for (const conv of [null, {}, { id: convB.id, _localOnly: true }]) bridge.buildTurnNav(conv);
assert.deepEqual(calls.update.slice(-3), [['', null], ['', null], ['', null]]);
assert.equal(calls.state.length, stateReads, 'missing/local-only conversations read no store');
const params = { current: 3, total: 42 };
assert.equal(ports.translate('conversationBar.turnPosition', params), 'translated:conversationBar.turnPosition');
assert.deepEqual(calls.translate, [['conversationBar.turnPosition', params]]);
""")


def test_directory_and_hydration_ports_keep_live_conversation_ownership():
    _run(r"""
const ports = navigation();
const entry = { turnId: target.id, actor: 'human', ordinal: 97, roundNumber: 5 };
assert.equal(ports.directoryKey(convA.id), `head:${convA.id}`);
assert.deepEqual(calls.key, [convA.id]);
assert.equal(await ports.directory(convA.id, 4, 1, 'sync-head'), directoryResult);
assert.deepEqual(calls.directory, [[convA, 'main', 4, 1, 'sync-head']]);
let release;
hydrationResult = new Promise(resolve => { release = resolve; });
let hydrated = false;
const pending = ports.hydrateRound(convA.id, entry).then(() => { hydrated = true; });
await Promise.resolve();
assert.equal(hydrated, false, 'hydration must await the authoritative owner');
release();
await pending;
assert.equal(hydrated, true);
assert.deepEqual(calls.hydrate, [[convA, 'main', entry]]);
await ports.loadEarlier(convA.id, 96);
assert.deepEqual(calls.earlier, [[convA, 'main', 96, 64]]);
activeConv = convB;
await assert.rejects(ports.directory(convA.id, 4, 1, null), /Conversation changed/);
await assert.rejects(ports.hydrateRound(convA.id, entry), /Conversation changed/);
await ports.loadEarlier(convA.id, 96);
assert.equal(calls.directory.length, 1);
assert.equal(calls.hydrate.length, 1);
assert.equal(calls.earlier.length, 1);
await ports.directory(convB.id, 0, 50, null);
await ports.hydrateRound(convB.id, entry);
assert.deepEqual(calls.directory.at(-1), [convB, 'main', 0, 50, null]);
assert.deepEqual(calls.hydrate.at(-1), [convB, 'main', entry]);
activeConv = null;
await assert.rejects(ports.directory(convB.id, 0, 50, null), /Conversation changed/);
await assert.rejects(ports.hydrateRound(convB.id, entry), /Conversation changed/);
await ports.loadEarlier(convB.id, 96);
assert.equal(calls.directory.length, 2);
assert.equal(calls.hydrate.length, 2);
assert.equal(calls.earlier.length, 1);
""")


@pytest.mark.parametrize('completed_frames', [0, 1], ids=['before-layout', 'between-layouts'])
def test_newer_successful_jump_supersedes_delayed_reveal(completed_frames):
    _run(r"""
const ports = navigation();
revealResult = false;
ports.scrollToTurn('older-turn');
for (let i = 0; i < Number(process.argv[2]); i++) advanceFrame();
revealResult = true;
ports.scrollToTurn(target.id);
for (let i = 0; i < 2 && frames.length; i++) advanceFrame();
assert.deepEqual(calls.reveal, [[convA.id, 'older-turn'], [convA.id, target.id]]);
assert.deepEqual(calls.render, [convA.id]);
assert.deepEqual(calls.warn, []);
""", str(completed_frames))


def test_malformed_conversation_cannot_submit_navigation():
    _run(r"""
const ports = navigation();
for (const conv of [{}, { id: '' }, { id: null }, { id: 257 }]) {
  activeConv = conv;
  ports.scrollToTurn(target.id);
}
assert.deepEqual(calls.reveal, []);
assert.deepEqual(calls.render, []);
assert.deepEqual(frames, []);
""")


@pytest.mark.parametrize('completed_frames', [0, 1], ids=['before-layout', 'between-layouts'])
def test_leave_and_return_to_conversation_retires_delayed_reveal(completed_frames):
    _run(r"""
const ports = navigation();
revealResult = false;
ports.scrollToTurn(target.id);
for (let i = 0; i < Number(process.argv[2]); i++) advanceFrame();
activeConv = convB;
bridge.buildTurnNav(activeConv);
activeConv = convA;
bridge.buildTurnNav(activeConv);
revealResult = true;
for (let i = 0; i < 2 && frames.length; i++) advanceFrame();
assert.deepEqual(calls.reveal, [[convA.id, target.id]]);
assert.deepEqual(calls.render, [convA.id]);
assert.deepEqual(calls.warn, []);
assert.deepEqual(frames, []);
""", str(completed_frames))
