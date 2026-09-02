"""Conversation read-model lookups must not recursively create runtimes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path


pytestmark = pytest.mark.unit
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')
ENTRY = os.path.join(ROOT, 'frontend/src/core/turn-runtime.ts')
RUNTIME_BRIDGE = runtime_section_path('main/conversation_turn_store.js')


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD),
    reason='node + vite test bundler unavailable',
)
def test_five_hundred_catalog_reads_create_no_store_or_health_reentry(tmp_path):
    built = tmp_path / 'turn-runtime.js'
    compiled = subprocess.run(
        [ESBUILD, ENTRY, '--bundle', '--format=cjs', '--platform=node',
         f'--outfile={built}'],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr
    harness = r"""
const assert = require('assert');
global.window = globalThis;
const { createConversationTurnRuntime } = require(BUILT);
let healthPublishes = 0;
const runtime = createConversationTurnRuntime({
  api: {},
  onHealth() { healthPublishes += 1; },
});
for (let index = 0; index < 500; index += 1) {
  assert.strictEqual(runtime.readRuntimeState(`catalog-${index}`), null);
}
assert.strictEqual(healthPublishes, 0);
const store = runtime.ensureRuntimeStore('hydrated');
assert.strictEqual(healthPublishes, 1);
assert.strictEqual(runtime.readRuntimeState('hydrated'), store.getState());
console.log(JSON.stringify({ healthPublishes }));
""".replace('BUILT', json.dumps(str(built)))
    result = subprocess.run(
        ['node', '-e', harness], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        'healthPublishes': 1,
    }


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD),
    reason='node + vite test bundler unavailable',
)
def test_hydration_claims_lane_before_connecting_health_reentry(tmp_path):
    """A connecting repaint must share, never recursively relaunch, snapshot."""
    built = tmp_path / 'turn-runtime.js'
    compiled = subprocess.run(
        [ESBUILD, ENTRY, '--bundle', '--format=cjs', '--platform=node',
         f'--outfile={built}'],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr
    harness = r"""
const assert = require('assert');
global.window = globalThis;
const { createConversationTurnRuntime } = require(BUILT);
async function main() {
const conversation = { id: 'hydrate-reentry' };
let runtime;
let snapshotCalls = 0;
let connectingReentries = 0;
const snapshot = {
  ok: true,
  contract: 'tofu.conversation-sync.snapshot/v1',
  conversationId: conversation.id,
  conversationRevision: 1,
  syncSeq: 1,
  cursor: 'cursor-1',
  serverBootId: 'boot-test',
  heartbeatIntervalMs: 15000,
  settings: {}, turns: [], attempts: [], queueItems: [],
};
runtime = createConversationTurnRuntime({
  api: {
    async snapshot() {
      snapshotCalls += 1;
      return snapshot;
    },
    eventsUrl() { return '/events'; },
  },
  onHealth(_conversationId, health) {
    if (health.state !== 'connecting' || connectingReentries >= 50) return;
    connectingReentries += 1;
    void runtime.hydrateConversation(conversation);
  },
});
const store = await runtime.hydrateConversation(conversation);
await new Promise(resolve => setTimeout(resolve, 0));
assert.strictEqual(store.getState().conversationId, conversation.id);
assert.strictEqual(snapshotCalls, 1);
assert.strictEqual(connectingReentries, 1);
console.log(JSON.stringify({ snapshotCalls, connectingReentries }));
}
main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
""".replace('BUILT', json.dumps(str(built)))
    result = subprocess.run(
        ['node', '-e', harness], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        'snapshotCalls': 1,
        'connectingReentries': 1,
    }


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_retained_read_bridge_uses_the_pure_state_port():
    harness = r"""
const fs = require('fs');
global.window = globalThis;
global.addEventListener = () => {};
global.requiredApiTransport = { pageRequestId() { return 'page-test'; } };
global.conversationSyncApi = {};
global.conversations = [];
global.createBranchComposerSession = () => ({
  current() { return null; },
  close() { return null; },
});
global.createHumanGuidancePresentationStore = () => ({
  read() { return null; },
  patch() { return null; },
  decorate(_conversationId, round) { return round; },
  clearConversation() {},
});
global.createClassicConversationRenderers = () => ({});
global.createPlanDecisionBar = () => ({ render() {} });
global.createConversationSurfaceController = () => ({});
global.createTransientTurnOverlay = () => ({});
global.activeConversationAttemptIds = () => [];
global.activeMainConversationAttemptId = () => null;
global.orderedConversationTurns = () => [];
global.latestConversationTurn = () => null;
global.conversationHasActor = () => false;

const reads = [];
global.createConversationTurnRuntime = () => ({
  emptyState() { return {}; },
  reducer(state) { return state; },
  readRuntimeState(conversationId) {
    reads.push(conversationId);
    return conversationId === 'hydrated' ? { conversationId } : null;
  },
  ensureRuntimeStore() {
    throw new Error('read bridge must not create a runtime store');
  },
});

eval(fs.readFileSync(process.argv[1], 'utf8'));
const hydrated = global.ConversationTurnRead.state({ id: 'hydrated' });
const missing = global.ConversationTurnRead.state('missing');
const absent = global.ConversationTurnRead.state(null);
if (hydrated?.conversationId !== 'hydrated') throw new Error('hydrated state lost');
if (missing !== null || absent !== null) throw new Error('missing state must be null');
if (JSON.stringify(reads) !== JSON.stringify(['hydrated', 'missing'])) {
  throw new Error(`unexpected read calls: ${JSON.stringify(reads)}`);
}
console.log(JSON.stringify({ reads }));
"""
    result = subprocess.run(
        ['node', '-e', harness, RUNTIME_BRIDGE], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        'reads': ['hydrated', 'missing'],
    }
