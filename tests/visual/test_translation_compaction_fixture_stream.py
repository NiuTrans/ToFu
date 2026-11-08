"""Native-independent specification of the visual fixture's SSE boundary.

Exercise the real generated URL builder and page-wide multiplexer, including a
nonzero subscription index. This is not a substitute for the real App's
snapshot/event reducer, chronology, disclosure or reload assertions.
"""
from __future__ import annotations

import copy
import json
import subprocess

import pytest

from tests._jsdom import require_node_modules
from tests.visual.test_visual_translation_compaction_attribution import (
    CONVERSATION, NOW, ROOT, _install_fixture,
)

pytestmark = pytest.mark.unit


class _FixtureScriptCapture:
    """Capture the existing fixture script without a server or browser."""

    def route(self, pattern, handler):
        self.route_registration = (pattern, handler)

    def add_init_script(self, script):
        self.script = script


_BODY = r"""
const assert = require('node:assert/strict');
const {JSDOM} = require(process.cwd() + '/node_modules/jsdom');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
  const dom = new JSDOM('<!doctype html><body></body>', {
    url: 'http://fixture.invalid/', runScripts: 'outside-only',
  });
  const w = dom.window;
  const fallbackUrls = [];
  // Capture unrelated native transports; never connect them to a server.
  w.EventSource = class CapturedNativeSource extends w.EventTarget {
    constructor(url) { super(); this.url = String(url); this.readyState = 0; fallbackUrls.push(this.url); }
    close() { this.readyState = 2; }
  };
  w.eval(input.fixture);
  w.eval(input.bundle);
  const protocolErrors = [];
  const multiplexer = w.StreamOwner.createConversationEventMultiplexer({
    api: w.StreamOwner.conversationSyncApi, streamClientId: 'visual-fixture-spec',
    onProtocolError: error => protocolErrors.push(String(error)),
  });
  const settle = () => new Promise(resolve => w.queueMicrotask(() => w.queueMicrotask(resolve)));
  const missingSubscription = /Real app did not subscribe to fixture conversation/;
  assert.equal(w.__visualConversationSources.length, 0);
  assert.throws(() => w.__emitVisualConversationEvent(input.event), missingSubscription);
  const sibling = multiplexer.open({conversationId: 'visual-other-fixture', after: 'other-cursor'});
  const target = multiplexer.open({conversationId: input.id, after: 'visual-cursor-1', cursorSource: 'snapshot'});
  const received = [];
  const siblingReceived = [];
  target.addEventListener('turn.upsert', message => received.push({
    event: JSON.parse(message.data), cursor: message.lastEventId,
  }));
  sibling.addEventListener('turn.upsert', message => siblingReceived.push(message.data));
  try {
    await settle();
    assert.equal(fallbackUrls.length, 0);
    assert.equal(w.__visualConversationSources.length, 1);
    assert.equal(target.readyState, 1);
    const native = w.__visualConversationSources[0];
    const requested = new URL(native.url, w.location.href);
    assert.equal(requested.pathname, '/api/v4/conversation-events');
    const subscriptions = JSON.parse(requested.searchParams.get('streams'));
    assert.equal(subscriptions[1].conversationId, input.id);
    assert.equal(subscriptions[1].after, 'visual-cursor-1');
    assert.equal(subscriptions[1].cursorSource, 'snapshot');
    w.__emitVisualConversationEvent(input.event);
    assert.deepEqual(received, [{event: input.event, cursor: 'visual-cursor-2'}]);
    assert.deepEqual(siblingReceived, []);
    assert.deepEqual(protocolErrors, []);
    assert.throws(() => w.__emitVisualConversationEvent({
      ...input.event, conversationId: 'visual-other-fixture',
    }), /Visual fixture event conversation mismatch/);
    target.close();
    await settle();
    assert.equal(native.readyState, 2);
    assert.equal(w.__visualConversationSources.length, 1);
    assert.equal(fallbackUrls.length, 1);
    assert.throws(() => w.__emitVisualConversationEvent(input.event), missingSubscription);
    console.log('PASS actual multiplexed fixture delivery, identity and subscription fencing');
  } finally { target.close(); sibling.close(); dom.window.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def test_translation_compaction_fixture_uses_actual_multiplexed_stream(tmp_path):
    require_node_modules('vite', 'jsdom')
    captured = _FixtureScriptCapture()
    snapshot, _ = _install_fixture(captured, 'http://fixture.invalid', 'en', 'light')
    updated = copy.deepcopy(snapshot['turns'][-1])
    updated['projectionRevision'] = 2
    event = {
        'contract': 'tofu.conversation-sync.event/v1', 'type': 'turn.upsert',
        'conversationId': CONVERSATION, 'syncSeq': 2, 'occurredAt': NOW + 10,
        'payload': {'conversationRevision': 2, 'turns': [updated], 'attempts': []},
    }
    entry = (
        f"export {{createConversationEventMultiplexer}} from '{ROOT}/frontend/src/core/conversation-event-multiplexer.ts';\n"
        f"export {{conversationSyncApi}} from '{ROOT}/frontend/src/api/conversation-sync.generated.ts';\n"
    )
    bundle = tmp_path / 'multiplexed-fixture-owner.js'
    compiled = subprocess.run([
        'node', str(ROOT / 'scripts/vite_test_bundle.mjs'), '--bundle',
        '--format=iife', '--platform=browser', '--global-name=StreamOwner',
        '--loader=ts', '--sourcefile=multiplexed-fixture-owner.ts', f'--outfile={bundle}',
    ], input=entry, cwd=ROOT, capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = subprocess.run(['node', '-e', _BODY], input=json.dumps({
        'fixture': captured.script, 'bundle': bundle.read_text(),
        'id': CONVERSATION, 'event': event,
    }), cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
