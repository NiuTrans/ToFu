#!/usr/bin/env python3
"""Empty-bubble root fix ③: ``finishStream`` IN-SESSION ghost-tail self-heal.

WHY
---
When a turn settles with an empty trailing assistant that the ``done`` handler
did NOT splice (e.g. a swallowed done → poll fallback → finishStream with no
`done` event to act on, or any path that leaves a bare husk at the tail),
``finishStream`` must apply the SAME verdict the backend GET/startup reconcile
applies (``lib.conversations.reconcile.classify_ghost_tail``, ported to JS as
``_classifyGhostTailJS``):

  * bare empty husk (no content/thinking/finishReason/usage/error/real-round)
    → DELETE (splice it out so the in-memory list matches the backend-cleaned
    DB — otherwise it renders as a blank "Agent" bubble until the next reload);
  * thinking-only husk → INTERRUPT (stamp finishReason='interrupted', keep the
    reasoning);
  * settled / content-bearing tail → KEEP untouched.

This drives the REAL shipped ``finishStream`` under jsdom, providing the real
``_classifyGhostTailJS`` + ``_hasRealToolRound`` (copied verbatim from
chat_render.js — their byte-equivalence to the backend is separately pinned by
tests/test_frontend_ghost_tail_js_backend_equivalence.py).

Scenarios:
  1. bare empty tail → spliced out.
  2. thinking-only tail → kept + finishReason='interrupted'.
  3. content tail → untouched (no self-heal).
NEUTER:
  • nc_selfheal_off: force the ghost-tail verdict branch to skip → scenario 1's
    splice FAILS, proving the self-heal is load-bearing.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NC = process.argv[4] || '';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = (fn) => 0;
global.clearTimeout = win.clearTimeout = () => {};
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;

const activeStreams = new Map();
const streamBufs = new Map();
let conversations = [];
let activeConvId = 'c1';
win.activeStreams = global.activeStreams = activeStreams;
win.streamBufs = global.streamBufs = streamBufs;
win.conversations = global.conversations = conversations;
Object.defineProperty(win, 'activeConvId', { get: () => activeConvId, set: v => activeConvId = v });
Object.defineProperty(global, 'activeConvId', { get: () => activeConvId, set: v => activeConvId = v });

const calls = {};
function spy(name) { calls[name] = 0; return (...a) => { calls[name]++; calls[name + '_args'] = a; }; }
// removeMessage records the id/idx it was asked to drop (DOM eviction no-op).
win.ConvView = global.ConvView = {
  finalizeStreaming: (convId, msg) => { calls.finalizeStreaming = (calls.finalizeStreaming || 0) + 1; calls.finalizeStreaming_msg = msg; },
  removeMessage: (convId, id) => { calls.removeMessage = (calls.removeMessage || 0) + 1; calls.removeMessage_id = id; },
};
win.syncConversationToServer = global.syncConversationToServer = spy('syncConversationToServer');
for (const n of ['saveConversations','renderConversationList','updateSendButton','buildTurnNav',
  'scrollToBottom','renderChat','_checkForQueuedTask','_attachAutopilotFollowup',
  '_maybeAutoGenerateTitle','_armAutoTranslateWatchdog','twStop']) {
  win[n] = global[n] = spy(n);
}
win.ConvCache = global.ConvCache = { put: spy('ConvCache_put') };
win.isNearBottom = global.isNearBottom = () => false;
win._convRenderFingerprint = global._convRenderFingerprint = () => 'fp';
win._findAutopilotPendingCarrier = global._findAutopilotPendingCarrier = () => null;
win._dispatchableQueueCount = global._dispatchableQueueCount = () => 0;
win.convAutoTranslate = global.convAutoTranslate = () => false;
win.convAutoTranslateEffective = global.convAutoTranslateEffective = () => false;
win._startAutoTranslateForMsg = global._startAutoTranslateForMsg = spy('_startAutoTranslateForMsg');
win.autoTranslate = global.autoTranslate = false;
win.formatClockTime = global.formatClockTime = () => '';
win._streamingBubbleHTML = global._streamingBubbleHTML = () => '';
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';

// ── The ghost-tail predicates finishStream consults (verbatim from
//    chat_render.js — loaded BEFORE stream_lifecycle.js in the real bundle).
//    Equivalence to reconcile.py is pinned by a separate test. ──
win._hasRealToolRound = global._hasRealToolRound = function(msg) {
  const rounds = msg && msg.toolRounds;
  if (!Array.isArray(rounds)) return false;
  for (const r of rounds) {
    if (!r || typeof r !== 'object') continue;
    if (r.status === 'done' || r.toolContent) return true;
    if (Array.isArray(r.results) && r.results.length) return true;
  }
  return false;
};
win._classifyGhostTailJS = global._classifyGhostTailJS = function(msg) {
  if (!msg || msg.role !== 'assistant') return null;
  if ((msg.content && String(msg.content).trim())
      || msg.finishReason || msg.usage || msg.error) return null;
  if (win._hasRealToolRound(msg)) return null;
  if (NC === 'nc_selfheal_off') return null;   // NEUTER: never heal
  return (msg.thinking && String(msg.thinking).trim()) ? 'interrupt' : 'delete';
};
// _streamBoundToMsg: no live stream is bound after finishStream's
// activeStreams.delete, so this returns false (matches production).
win._streamBoundToMsg = global._streamBoundToMsg = function(msg) {
  if (!msg || !activeStreams.size) return false;
  for (const s of activeStreams.values()) {
    if (s && s.assistantMsg === msg) return true;
    if (s && s.taskId && msg._taskId && s.taskId === msg._taskId) return true;
  }
  return false;
};

eval(fs.readFileSync(process.argv[3], 'utf8'));  // ui/stream_lifecycle.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
if (typeof finishStream !== 'function') { console.log('FAIL finishStream_exposed'); process.exit(0); }

function makeStreamingDom() {
  document.getElementById('chatInner').innerHTML =
    '<div class="message" id="streaming-msg" data-msg-id="m1">' +
    '<div class="message-body" id="streaming-body"></div></div>';
}

// ── 1. bare empty tail → spliced out. ──
{
  calls.removeMessage = 0;
  conversations.length = 0;
  const am = { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'm1' };
  conversations.push({ id: 'c1', title: 'T', messages: [{ role: 'user', content: 'hi' }, am], activeTaskId: 't1' });
  activeConvId = 'c1';
  activeStreams.set('c1', { controller: {} });   // deleted at top of finishStream
  makeStreamingDom();
  const _before = conversations[0].messages.length;
  finishStream('c1');
  check('bare_empty_spliced', conversations[0].messages.indexOf(am) === -1 &&
    conversations[0].messages.length === _before - 1);
  check('bare_empty_removeMessage_called', calls.removeMessage >= 1);
}

// ── 2. thinking-only tail → kept + finishReason=interrupted. ──
{
  conversations.length = 0;
  const am = { role: 'assistant', content: '', thinking: 'recovered reasoning', toolRounds: [], _msgId: 'm1' };
  conversations.push({ id: 'c1', title: 'T', messages: [{ role: 'user', content: 'hi' }, am], activeTaskId: 't1' });
  activeConvId = 'c1';
  activeStreams.set('c1', { controller: {} });
  makeStreamingDom();
  const _before = conversations[0].messages.length;
  finishStream('c1');
  check('thinking_only_kept', conversations[0].messages.indexOf(am) >= 0 &&
    conversations[0].messages.length === _before);
  check('thinking_only_stamped_interrupted', am.finishReason === 'interrupted');
}

// ── 3. content tail → untouched (no self-heal, real reply). ──
{
  conversations.length = 0;
  const am = { role: 'assistant', content: 'the real answer', thinking: '', toolRounds: [], finishReason: 'stop', _msgId: 'm1' };
  conversations.push({ id: 'c1', title: 'T', messages: [{ role: 'user', content: 'hi' }, am], activeTaskId: 't1' });
  activeConvId = 'c1';
  activeStreams.set('c1', { controller: {} });
  makeStreamingDom();
  const _before = conversations[0].messages.length;
  finishStream('c1');
  check('content_tail_kept', conversations[0].messages.indexOf(am) >= 0 &&
    conversations[0].messages.length === _before &&
    am.content === 'the real answer' && am.finishReason === 'stop');
}

console.log(out.join('\n'));
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_finishstream_selfheal_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT, os.path.join(JS_DIR, 'ui', 'stream_lifecycle.js'), nc],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_finishstream_ghost_tail_selfheal():
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'finishStream ghost-tail self-heal failures:\n' + output
    for want in ('PASS bare_empty_spliced', 'PASS bare_empty_removeMessage_called',
                 'PASS thinking_only_kept', 'PASS thinking_only_stamped_interrupted',
                 'PASS content_tail_kept'):
        assert want in output, 'missing expected pass:\n' + output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_selfheal_off_regression_is_caught():
    """Neutering the ghost-tail verdict (always null) must leave the bare empty
    tail in place → scenario 1's splice FAILS."""
    output = _run('nc_selfheal_off')
    assert 'FAIL bare_empty_spliced' in output, (
        'Neutering the ghost-tail self-heal did NOT leave the empty tail — the '
        'self-heal branch is not load-bearing:\n' + output)


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
    else:
        test_finishstream_ghost_tail_selfheal()
        test_nc_selfheal_off_regression_is_caught()
        print('PASS test_frontend_finishstream_ghost_tail_selfheal')
