#!/usr/bin/env python3
"""Phase-2 (completion-workflow consolidation): ``finishStream`` must be
RENDER-ONLY at turn-end — it must NOT issue a full-conversation PUT to the
server, and it must NOT blank an offline/partial bubble.

WHY
---
The backend's ``_sync_result_to_conversation`` is the SOLE authoritative writer
of the settled turn into ``conversations.messages`` (it commits BEFORE the
terminal ``done`` event and ships the committed dict as ``committedMessage``).
The old ``finishStream`` ALSO issued ``syncConversationToServer(conv)`` — a
full-conv PUT that merely re-uploaded what the backend already wrote AND raced
it. Three skip-guards (queue-race / autopilot-inbound / server_offline) existed
ONLY to suppress that PUT in the windows where it would clobber a backend
write. Removing the PUT makes all three moot.

The one thing that MUST survive removal: the OFFLINE FALLBACK. When the server
dies mid-stream there is no committed dict; the trailing assistant holds only
the streamed partial (stamped ``finishReason='server_offline'`` by
``_forceFinishDeadStream``). ``finishStream`` must render that partial, never
blank it.

Tests (drive the REAL shipped ``finishStream`` under jsdom, collaborators
stubbed as spies):
  1. ``normal finish`` — NO ``syncConversationToServer`` call; the settled
     bubble is finalized via ``ConvView.finalizeStreaming`` on the trailing
     assistant. ★ THE FIX (no PUT).
  2. ``offline partial`` — a trailing assistant with streamed partial content +
     ``finishReason='server_offline'`` is NOT blanked; content is preserved and
     finalized. ★ OFFLINE-FALLBACK INVARIANT.
  3. Source guards — the 3 skip-guard log strings are GONE and
     ``syncConversationToServer(conv)`` no longer appears in stream_lifecycle.js.
     ★ Byte-revert control: re-adding the PUT (a raw grep hit) fails the guard.
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
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = (fn) => 0;   // neuter the trailing _checkForQueuedTask timer
global.clearTimeout = win.clearTimeout = () => {};
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;

// ── State the function reads ──
const activeStreams = new Map();
const streamBufs = new Map();
let conversations = [];
let activeConvId = 'c1';
win.activeStreams = global.activeStreams = activeStreams;
win.streamBufs = global.streamBufs = streamBufs;
win.conversations = global.conversations = conversations;
Object.defineProperty(win, 'activeConvId', { get: () => activeConvId, set: v => activeConvId = v });
Object.defineProperty(global, 'activeConvId', { get: () => activeConvId, set: v => activeConvId = v });

// ── Spy factory ──
const calls = {};
function spy(name) { calls[name] = 0; return (...a) => { calls[name]++; calls[name + '_args'] = a; }; }

// finalizeStreaming records WHICH message it finalized (offline-fallback proof).
win.ConvView = global.ConvView = {
  finalizeStreaming: (convId, msg) => { calls.finalizeStreaming = (calls.finalizeStreaming || 0) + 1; calls.finalizeStreaming_msg = msg; },
};
// The server PUT — MUST stay at 0.
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
// finishStream now also consults the EFFECTIVE resolver (unification fix —
// live global toggle wins when ON). Both OFF here so no translation fires.
win.convAutoTranslateEffective = global.convAutoTranslateEffective = () => false;
win._startAutoTranslateForMsg = global._startAutoTranslateForMsg = spy('_startAutoTranslateForMsg');
win.autoTranslate = global.autoTranslate = false;
win.formatClockTime = global.formatClockTime = () => '';
win._streamingBubbleHTML = global._streamingBubbleHTML = () => '';
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';

// stream_lifecycle.js also defines showStreamingUIForConv + HG helpers that
// reference more globals at PARSE time? No — all refs are inside function
// bodies (runtime). Provide the few the finishStream path touches; the rest
// stay undefined and are never called on this path.
eval(fs.readFileSync(process.argv[3], 'utf8'));  // ui/stream_lifecycle.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof finishStream !== 'function') { console.log('FAIL finishStream_exposed'); process.exit(0); }
check('finishStream_exposed', true);

function makeStreamingDom() {
  document.getElementById('chatInner').innerHTML =
    '<div class="message" id="streaming-msg" data-msg-id="m1">' +
    '<div class="message-body" id="streaming-body"></div></div>';
}

// ── 1. Normal finish: NO server PUT; settled bubble finalized. ──
{
  calls.syncConversationToServer = 0; calls.finalizeStreaming = 0;
  conversations.length = 0;
  const am = { role: 'assistant', content: 'the complete answer', thinking: '', toolRounds: [], _msgId: 'm1' };
  conversations.push({ id: 'c1', title: 'T', messages: [{ role: 'user', content: 'hi' }, am], activeTaskId: 't1' });
  activeConvId = 'c1';
  activeStreams.set('c1', { controller: {} });
  makeStreamingDom();
  finishStream('c1');
  check('normal_no_server_put', calls.syncConversationToServer === 0);
  check('normal_finalizes_bubble', calls.finalizeStreaming === 1 &&
    calls.finalizeStreaming_msg === am);
  check('normal_content_preserved', am.content === 'the complete answer');
  check('normal_clears_activeTaskId', conversations[0].activeTaskId === null);
}

// ── 2. OFFLINE FALLBACK: server died; trailing assistant holds only the
//        streamed partial + finishReason=server_offline. finishStream must NOT
//        blank it, must NOT PUT, and must finalize the partial bubble. ──
{
  calls.syncConversationToServer = 0; calls.finalizeStreaming = 0;
  conversations.length = 0;
  const partial = 'streamed partial before the server went offline';
  const am = { role: 'assistant', content: partial, thinking: 'partial reasoning',
               toolRounds: [], finishReason: 'server_offline', _msgId: 'm1' };
  conversations.push({ id: 'c1', title: 'T', messages: [{ role: 'user', content: 'hi' }, am], activeTaskId: 't1' });
  activeConvId = 'c1';
  activeStreams.set('c1', { controller: {} });
  makeStreamingDom();
  finishStream('c1');
  check('offline_no_server_put', calls.syncConversationToServer === 0);
  check('offline_content_not_blanked', am.content === partial &&
    am.thinking === 'partial reasoning');
  check('offline_finalizes_partial', calls.finalizeStreaming === 1 &&
    calls.finalizeStreaming_msg === am);
  // The local IDB cache write still happens (it's not the server PUT).
  check('offline_local_cache_written', calls.ConvCache_put >= 1);
}

console.log(out.join('\n'));
"""


def _run():
    harness = os.path.join(HERE, '_finishstream_no_put_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT, os.path.join(JS_DIR, 'ui', 'stream_lifecycle.js')],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'finishStream-no-put failures:\n' + output
    assert output.count('PASS') >= 8, f'expected >=8 PASS lines, got:\n{output}'

    # ── Source guards: the full-conv PUT + its 3 skip-guards are GONE. ──
    with open(os.path.join(JS_DIR, 'ui', 'stream_lifecycle.js'), encoding='utf-8') as f:
        sl_src = f.read()
    # Isolate the finishStream function body (up to the next top-level function).
    fs_start = sl_src.find('function finishStream(')
    assert fs_start >= 0, 'finishStream not found'
    fs_end = sl_src.find('\nfunction _retriggerHgTranslations', fs_start)
    assert fs_end > fs_start, 'could not bound finishStream body'
    fs_body = sl_src[fs_start:fs_end]

    assert 'syncConversationToServer(conv)' not in fs_body, (
        'Phase-2 regression: the full-conv PUT was reintroduced into finishStream '
        '— it races the backend authoritative writer (the whole reason the 3 '
        'skip-guards existed).')
    for guard in ('Skipping syncConversationToServer',
                  '_fsHasQueued', '_fsAutopilotInbound', '_fsIsServerOffline'):
        assert guard not in fs_body, (
            f'Phase-2 regression: skip-guard {guard!r} still present in finishStream '
            '— if the PUT is gone the guard is dead code; if the guard is back the '
            'PUT probably is too.')
    # The local cache write MUST remain (it is NOT the server PUT).
    assert 'ConvCache.put(conv)' in fs_body, (
        'regression: the local IndexedDB cache write was removed with the PUT — '
        'it must stay for instant reload.')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_finishstream_no_put():
    _run()


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
    else:
        _run()
        print('PASS test_finishstream_no_put')
