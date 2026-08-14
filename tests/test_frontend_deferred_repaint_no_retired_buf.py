#!/usr/bin/env python3
"""``showStreamingUIForConv``'s 300ms deferred repaint must not reference the
RETIRED ``streamBufs``/``dBuf`` buffer — it throws a live ReferenceError.

WHY (production incident, 2026-07-25)
-------------------------------------
``ff7176dd`` ("RENDER_CONTRACT Phase 3.5 §7: streamBufs fully RETIRED") deleted
the ``const dBuf = streamBufs.get(...)`` binding that fed the deferred repaint,
but left SEVEN reads of ``dBuf`` inside the ``setTimeout(..., 300)`` body of
``showStreamingUIForConv``. In the browser that is an uncaught

    ReferenceError: dBuf is not defined

fired 300ms after a conversation with a LIVE stream is painted — i.e. every
page refresh / conversation switch onto a generating conversation. It was
observed 10× in ``logs/error.log`` between 13:09 and 17:39 on 2026-07-25 via
the ``[CLIENT-ERROR] [uncaught]`` reporter.

The blast radius is exactly the "deferred repaint" this block exists to
perform: the SSE state that arrived during the connection-setup window is
never painted, so the bubble can sit on stale/"waiting" content until the
NEXT push frame — which for a tool-heavy turn is 20-40s away. It is a
front/back SYNCHRONISATION-TIMELINESS defect, not a cosmetic one.

WHY THE EXISTING SUITE MISSED IT
--------------------------------
Every jsdom harness that loads ``stream_lifecycle.js`` pre-injects a mock
``streamBufs`` global (``win.streamBufs = global.streamBufs = new Map()``) —
16+ files do. Under ``eval`` in that harness ``dBuf`` is still undefined, but
no test ever *drives the 300ms deferred callback*, so the ReferenceError is
never raised. The retirement guards added by ``ff7176dd`` grep for the
``streamBufs`` token; ``dBuf`` is a different token and slipped through.

So this test does two things the others deliberately don't:
  1. It does NOT define ``streamBufs`` — matching the real browser, where the
     symbol no longer exists anywhere in the bundle.
  2. It CAPTURES the 300ms callback and INVOKES it, asserting it neither
     throws nor blanks the bubble.

Tests
  1. ``deferred_repaint_runs`` — the deferred callback executes without a
     ReferenceError. ★ THE FIX (reproduces the production incident).
  2. ``deferred_repaint_paints_message`` — it repaints from the MESSAGE
     document (the §7 single fact-source), preserving checkpointed content
     rather than blanking it to ''. ★ Guards the fix's semantics: a naive
     "delete the block" repair would silently drop the deferred repaint.
  3. ``source_has_no_retired_buf`` — static guard: the retired ``dBuf`` /
     ``streamBufs`` tokens do not reappear in stream_lifecycle.js.
     ★ Byte-revert control.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

// ★ CAPTURE the deferred repaint instead of running it on a real timer, so we
//   can invoke it deterministically and observe a throw.
let deferredFn = null;
global.setTimeout = win.setTimeout = (fn, ms) => { if (ms === 300) deferredFn = fn; return 0; };
global.clearTimeout = win.clearTimeout = () => {};
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;

// ── State ──
const activeStreams = new Map();
let conversations = [];
let activeConvId = 'c1';
win.activeStreams = global.activeStreams = activeStreams;
win.conversations = global.conversations = conversations;
Object.defineProperty(win, 'activeConvId', { get: () => activeConvId, set: v => activeConvId = v });
Object.defineProperty(global, 'activeConvId', { get: () => activeConvId, set: v => activeConvId = v });

// ★★ DELIBERATELY NOT DEFINED: streamBufs / dBuf.
//    ff7176dd retired them; the browser has no such globals. Other harnesses
//    inject a mock streamBufs, which is exactly why they never caught this.

// ── Collaborators ──
const calls = {};
function spy(name) { calls[name] = 0; return (...a) => { calls[name]++; calls[name + '_args'] = a; }; }

// Record every updateStreamingUI payload so we can assert the deferred repaint
// carried the MESSAGE's content rather than a blank.
const painted = [];
win.updateStreamingUI = global.updateStreamingUI = (o) => { painted.push(o); };

for (const n of ['buildTurnNav','updateSendButton','_forceScrollToBottom','scrollToBottom',
                 '_renderStreamingTranslatePreview','_ensureLazyObserver','_destroyLazyObserver',
                 '_captureScrollAnchor','_restoreScrollAnchor']) {
  win[n] = global[n] = spy(n);
}
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
win.renderMessage = global.renderMessage = () => '<div class="message"></div>';
win.formatClockTime = global.formatClockTime = () => '12:00';
win._streamingBubbleHTML = global._streamingBubbleHTML = () => '<div id="streaming-msg"></div>';
win.isNearBottom = global.isNearBottom = () => true;
win._INITIAL_RENDER = global._INITIAL_RENDER = 30;
win._lazyConvId = global._lazyConvId = null;
win._lazyRenderedFrom = global._lazyRenderedFrom = 0;
win._lazyRenderedTo = global._lazyRenderedTo = 0;
win._lazyObserver = global._lazyObserver = { observe: () => {} };
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';

eval(fs.readFileSync(process.argv[3], 'utf8'));  // ui/stream_lifecycle.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof showStreamingUIForConv !== 'function') {
  console.log('FAIL showStreamingUIForConv_exposed'); process.exit(0);
}
check('showStreamingUIForConv_exposed', true);

// ── Paint a conversation whose trailing assistant is STREAMING, with real
//    checkpointed content already on the message (the post-refresh case). ──
const CHECKPOINT = 'the checkpointed answer so far';
const am = { role: 'assistant', content: CHECKPOINT, thinking: 'reasoning so far',
             toolRounds: [{ roundNum: 1 }], _msgId: 'm1', done: false };
conversations.length = 0;
conversations.push({ id: 'c1', title: 'T',
                     messages: [{ role: 'user', content: 'hi' }, am],
                     activeTaskId: 't1' });
activeConvId = 'c1';
activeStreams.set('c1', { controller: {} });

showStreamingUIForConv('c1');

check('deferred_scheduled', typeof deferredFn === 'function');

// ── 1. THE FIX: invoking the deferred repaint must not throw. ──
let threw = null;
try { if (deferredFn) deferredFn(); } catch (e) { threw = e; }
check('deferred_repaint_runs', threw === null);
if (threw) out.push('       threw: ' + (threw && threw.message));
check('deferred_repaint_not_referenceerror',
      !(threw && String(threw.name) === 'ReferenceError'));

// ── 2. Semantics: the deferred repaint must PAINT, carrying the message's
//       checkpointed content (not a blank wipe). ──
const last = painted[painted.length - 1] || null;
check('deferred_repaint_painted', painted.length >= 2);
check('deferred_repaint_keeps_content', !!last && last.content === CHECKPOINT);
check('deferred_repaint_keeps_thinking', !!last && last.thinking === 'reasoning so far');
check('deferred_repaint_keeps_toolrounds',
      !!last && Array.isArray(last.toolRounds) && last.toolRounds.length === 1);

console.log(out.join('\n'));
"""


def _run():
    harness = os.path.join(HERE, '_deferred_repaint_harness.js')
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
    assert not fails, (
        'deferred-repaint failures (a ReferenceError here is the live '
        'production incident):\n' + output)
    assert output.count('PASS') >= 8, f'expected >=8 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_deferred_repaint_no_referenceerror():
    """The 300ms deferred repaint runs without touching the retired buffer."""
    _run()


def test_source_has_no_retired_buf():
    """Static guard: the retired ``dBuf`` / ``streamBufs`` tokens stay gone.

    Byte-revert control — restoring either token in stream_lifecycle.js
    re-arms the exact ReferenceError ``ff7176dd`` shipped.
    """
    with open(os.path.join(JS_DIR, 'ui', 'stream_lifecycle.js'), encoding='utf-8') as f:
        src = f.read()
    # Strip comments so a historical mention in prose stays legal.
    live = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith('*') or stripped.startswith('//') or stripped.startswith('/*'):
            continue
        live.append(line)
    live_src = '\n'.join(live)

    assert 'dBuf' not in live_src, (
        'RETIRED-BUFFER REGRESSION: `dBuf` is referenced in live code in '
        'stream_lifecycle.js, but ff7176dd deleted its binding — this is an '
        'uncaught ReferenceError in the browser 300ms after painting a live '
        'stream. Repaint from the message document instead.')
    assert 'streamBufs' not in live_src, (
        'RETIRED-BUFFER REGRESSION: `streamBufs` was fully retired by ff7176dd '
        '(RENDER_CONTRACT Phase 3.5 §7) — the message document is the single '
        'live fact-source. Re-introducing it restores the second fact-source.')


if __name__ == '__main__':
    test_source_has_no_retired_buf()
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
    else:
        _run()
        print('PASS test_frontend_deferred_repaint_no_retired_buf')
