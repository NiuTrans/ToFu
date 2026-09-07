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
const artifactHints = [];
const snapshot = {
  ok: true,
  contract: 'tofu.conversation-sync.snapshot/v1',
  conversationId: conversation.id,
  conversationRevision: 1,
  syncSeq: 1,
  cursor: 'cursor-1',
  serverBootId: 'boot-test',
  heartbeatIntervalMs: 15000,
  hasArtifacts: false,
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
  applySnapshotMetadata(_conversation, value) {
    artifactHints.push(value.hasArtifacts);
  },
});
const store = await runtime.hydrateConversation(conversation);
await new Promise(resolve => setTimeout(resolve, 0));
assert.strictEqual(store.getState().conversationId, conversation.id);
assert.strictEqual(snapshotCalls, 1);
assert.strictEqual(connectingReentries, 1);
assert.deepStrictEqual(artifactHints, [false]);
console.log(JSON.stringify({ snapshotCalls, connectingReentries, artifactHints }));
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
        'artifactHints': [False],
    }


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD),
    reason='node + vite test bundler unavailable',
)
def test_push_invalidations_do_not_reload_a_live_conversation_snapshot(tmp_path):
    """Wake hints leave projection ownership with the ordered SSE cursor."""
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

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.listeners = new Map();
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
  }
  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }
  removeEventListener(name, listener) {
    this.listeners.set(name, (this.listeners.get(name) || [])
      .filter(candidate => candidate !== listener));
  }
  close() { this.closed = true; }
}

async function main() {
  const realDateNow = Date.now;
  let now = 1_000_000;
  Date.now = () => now;
  const conversation = { id: 'invalidation-wake' };
  const conversations = new Map([[conversation.id, conversation]]);
  const sources = [];
  const snapshotCalls = new Map();
  let activeId = conversation.id;
  const snapshotFor = (conversationId) => ({
    ok: true,
    contract: 'tofu.conversation-sync.snapshot/v1',
    conversationId,
    conversationRevision: 1,
    syncSeq: 1,
    cursor: 'cursor-1',
    serverBootId: 'boot-test',
    heartbeatIntervalMs: 15000,
    settings: {}, turns: [], attempts: [], queueItems: [],
  });
  const runtime = createConversationTurnRuntime({
    api: {
      async snapshot(conversationId) {
        snapshotCalls.set(
          conversationId, (snapshotCalls.get(conversationId) || 0) + 1,
        );
        return snapshotFor(conversationId);
      },
      eventsUrl(conversationId) { return `/events/${conversationId}`; },
    },
    findConversation(id) { return conversations.get(id) || null; },
    isActive(candidate) { return candidate.id === activeId; },
    eventSourceFactory(url) {
      const source = new FakeEventSource(url);
      sources.push(source);
      return source;
    },
  });

  const store = await runtime.wakeConversation(conversation);
  assert.strictEqual(sources.length, 1);
  sources[0].onopen();

  // Space the hints across separate turns of the event loop. The hydration
  // singleflight must not be what hides repeated full snapshot requests.
  for (let index = 0; index < 6; index += 1) {
    runtime.invalidateConversation(conversation.id, `cursor-${index + 2}`);
    await new Promise(resolve => setTimeout(resolve, 0));
  }

  assert.strictEqual(snapshotCalls.get(conversation.id), 1);
  assert.strictEqual(sources.length, 1);
  assert.strictEqual(sources[0].closed, false);

  // Changing activeConvId does not emit a TurnStore frame. The explicit shell
  // lifecycle boundary must still pause the now-inactive settled conversation;
  // otherwise every sidebar selection leaks another durable SSE poller.
  activeId = null;
  runtime.reconcileConversationActivity(conversation.id);
  assert.strictEqual(sources[0].closed, true);

  // Waking it again must reopen from cursor-1 instead of downloading the
  // snapshot a second time. Repeated browser/network wake events stay cheap.
  now += 1_000_000;
  activeId = conversation.id;
  for (let index = 0; index < 6; index += 1) {
    await runtime.wakeConversation(conversation);
    runtime.invalidateConversation(conversation.id, `resume-${index}`);
    await new Promise(resolve => setTimeout(resolve, 0));
  }

  assert.strictEqual(snapshotCalls.get(conversation.id), 1);
  assert.strictEqual(sources.length, 2);
  assert.strictEqual(sources[1].closed, false);

  // Resource budget: browsing settled conversations must transfer the one
  // active-stream lease instead of retaining one poller per visited item.
  let previousId = conversation.id;
  for (let index = 0; index < 32; index += 1) {
    const browsed = { id: `browsed-${index}` };
    conversations.set(browsed.id, browsed);
    activeId = browsed.id;
    await runtime.wakeConversation(browsed);
    runtime.reconcileConversationActivity(previousId, browsed.id);
    previousId = browsed.id;
  }
  assert.strictEqual(sources.filter(source => !source.closed).length, 1);
  assert.strictEqual(snapshotCalls.get(conversation.id), 1);

  for (const conversationId of conversations.keys()) {
    runtime.disposeConversation(conversationId);
  }
  Date.now = realDateNow;
  console.log(JSON.stringify({
    originalSnapshotCalls: snapshotCalls.get(conversation.id),
    sources: sources.length,
    liveSources: sources.filter(source => !source.closed).length,
  }));
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
        'originalSnapshotCalls': 1,
        'sources': 34,
        'liveSources': 0,
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
global.humanGuidancePresentation = {
  read() { return null; },
  patch() { return null; },
  decorate(_conversationId, round) { return round; },
  clearConversation() {},
};
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
