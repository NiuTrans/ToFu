"""jsdom regression: a `tool_compacted` for an OLDER message during an active
stream must NOT wipe the whole chat list.

WHY
---
L1 micro-compaction rewrites COLD tool rounds that live in EARLIER assistant
messages (not the in-flight bubble). `_handleToolCompacted`
(static/js/ui/sse_handlers_tool.js) stamps the matching round wherever it lives,
then must repaint that older row so its COMPACTED pill materializes.

The OLD code always did `renderChat(_conv, false)` for that repaint. But during
an active stream, `renderChat()` is intercepted by Guard 1c in chat_render.js →
`showStreamingUIForConv()`, which does `inner.innerHTML = html` — a full
destroy-and-rebuild of the ENTIRE message list (incl. the bottom file-edit
block). A long editing turn fires this handler ~once per compaction, so the user
saw the bottom block flash / re-render several times just to repaint one cold
pill. (The prior scroll-anchor fix only stopped the accompanying jump; the
innerHTML wipe — the flash — still fired.)

THE FIX
-------
When a stream is active, DON'T take the whole-list path (renderChat — since
the Phase 3.5 step-4 SEAM-2 fold, reached via `ConvView.replaceAll`).
Surgically replace ONLY the stamped older message's node
(`document.getElementById('msg-'+idx).outerHTML = renderMessage(msg, idx)`),
leaving the streaming bubble and every other node's DOM identity intact.
With no active stream, keep the whole-list repaint (the engine's per-message
data-mfp diff is already surgical + it runs the grouping/turn-nav passes).

This harness loads the SHIPPED sse_handlers_tool.js under jsdom, seeds a conv
with an older assistant message (whose cold round the event compacts) + a live
streaming bubble, calls `_handleToolCompacted`, and asserts:
  • #chatInner was NOT re-created (its DOM node identity survives — a full wipe
    replaces .innerHTML, which we detect via a sentinel child + a spy on the
    innerHTML setter);
  • the #streaming-msg node object is the SAME instance afterwards;
  • ONLY the stamped older msg node was replaced (its pill now renders);
  • renderChat was NOT called (showStreamingUIForConv never reached).

DOUBLE-NEUTER (both on the SHIPPED file):
  • 'stream_guard'  — force `_streamActive = false` → the handler falls into the
                      renderChat branch even mid-stream → renderChat IS called
                      (the whole-list wipe path), proving the guard is what
                      prevents the wipe.
  • 'surgical'      — no-op the surgical `outerHTML` replace → the older pill is
                      NOT materialized during stream (regression: the fix would
                      silently do nothing), proving the surgical replace is what
                      actually repaints the cold row.
Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
from _runtime_sections import runtime_section_path  # noqa: E402

SSE_HANDLERS_TOOL = runtime_section_path('ui/sse_handlers_tool.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const NEUTER = process.argv[4] || 'none';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = (fn) => 0;

// ── Globals the handler reads ──
const conversations = [];
win.conversations = global.conversations = conversations;
win.activeConvId = global.activeConvId = 'c1';
const activeStreams = new Map();
win.activeStreams = global.activeStreams = activeStreams;
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);

// twUpdate refreshes only the streaming bubble — model it as a no-op spy.
let _twUpdateCalls = 0;
win.twUpdate = global.twUpdate = () => { _twUpdateCalls++; };

// The WHOLE-LIST path we want to AVOID during stream. Post-SEAM-2-fold
// (RENDER_CONTRACT Phase 3.5 step 4), the handler's no-stream branch calls
// window.ConvView.replaceAll — the public full-repaint entry that DELEGATES
// to renderChat. Stub the seam and count both into the same counter: a call
// to EITHER is the whole-list wipe this test guards against.
let _renderChatCalls = 0;
win.renderChat = global.renderChat = () => { _renderChatCalls++; };
win.ConvView = global.ConvView = { replaceAll: () => { _renderChatCalls++; } };

// renderMessage: deterministic marker that reflects the compaction pill so we
// can assert the older row was actually repainted.
win.renderMessage = global.renderMessage = (msg, idx) => {
  const pill = (Array.isArray(msg.toolRounds) && msg.toolRounds.some(r => r.compactionLayer))
    ? '<span class="compacted-pill">COMPACTED</span>' : '';
  return '<div class="message" id="msg-' + idx + '" data-msg-id="' + (msg._msgId || '') + '">'
    + (msg.content || '') + pill + '</div>';
};

// ── Detect a full innerHTML wipe of #chatInner ──
// A real showStreamingUIForConv sets inner.innerHTML = html (full rebuild).
// Spy on the innerHTML SETTER of the inner node so any full-wipe is observable
// independent of jsdom layout.
const inner = document.getElementById('chatInner');
let _innerHtmlSets = 0;
const _innerDesc = Object.getOwnPropertyDescriptor(win.Element.prototype, 'innerHTML');
Object.defineProperty(inner, 'innerHTML', {
  get() { return _innerDesc.get.call(this); },
  set(v) { _innerHtmlSets++; _innerDesc.set.call(this, v); },
  configurable: true,
});

let src = fs.readFileSync(process.argv[2], 'utf8');  // ui/sse_handlers_tool.js

if (NEUTER === 'stream_guard') {
  src = src.replace(
    '        const _streamActive = typeof activeStreams !== \'undefined\'\n          && activeStreams.has(convId)\n          && !!document.getElementById(\'streaming-msg\');',
    '        const _streamActive = false;  // NEUTERED-stream_guard');
  if (src.indexOf('// NEUTERED-stream_guard') < 0) { console.log('FAIL neuter_stream_guard_not_applied'); process.exit(0); }
}
if (NEUTER === 'surgical') {
  src = src.replace(
    '            if (_el) _el.outerHTML = renderMessage(_stampedMsg, _stampedIdx);',
    '            if (_el) void 0;  // NEUTERED-surgical');
  if (src.indexOf('// NEUTERED-surgical') < 0) { console.log('FAIL neuter_surgical_not_applied'); process.exit(0); }
}

eval(src);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Seed a conversation: an OLDER assistant msg (idx 1) whose cold round the
//    event will compact, plus a live in-flight assistant bubble (idx 3). ──
const oldRound = { roundNum: 2, toolCallId: 'tc-COLD', toolName: 'grep_search', status: 'done' };
const conv = {
  id: 'c1', activeTaskId: 't1',
  messages: [
    { role: 'user', content: 'do work', _msgId: 'u0' },
    { role: 'assistant', content: 'older turn', _msgId: 'a1', toolRounds: [oldRound] },
    { role: 'user', content: 'more', _msgId: 'u2' },
    { role: 'assistant', content: 'live', _msgId: 'a3', toolRounds: [] },  // in-flight
  ],
};
conversations.push(conv);
activeStreams.set('c1', { taskId: 't1' });

// Render the settled messages into #chatInner as the app would (surgical target
// nodes), plus the live streaming bubble as #streaming-msg.
inner.innerHTML =
  win.renderMessage(conv.messages[0], 0) +
  win.renderMessage(conv.messages[1], 1) +
  win.renderMessage(conv.messages[2], 2) +
  '<div class="message" id="streaming-msg" data-sentinel="live">streaming…</div>';
// Reset the wipe counter AFTER the initial seed — we only care about wipes
// caused by the handler under test.
_innerHtmlSets = 0;
_renderChatCalls = 0;

const streamingBefore = document.getElementById('streaming-msg');
const olderBefore = document.getElementById('msg-1');
check('setup_streaming_present', !!streamingBefore);
check('setup_older_no_pill', !!olderBefore && olderBefore.innerHTML.indexOf('COMPACTED') < 0);

// ── The in-flight ctx snapshot the dispatcher passes to the handler. The
//    in-flight bubble is a3; the compaction targets the COLD round in a1. ──
const ctx = {
  convId: 'c1', taskId: 't1',
  assistantMsg: conv.messages[3], buf: { toolRounds: [] },
  epCriticPhase: false, epCriticMsg: null, epCriticBuf: null,
};
const ev = { type: 'tool_compacted', toolCallId: 'tc-COLD', compactionLayer: 'L1',
             compactedFromChars: 8000, compactedToChars: 200, toolTokens: 60 };

_handleToolCompacted(ev, ctx);

// ── Assertions ──
// 1. The cold round was stamped in the OLDER message's data.
check('cold_round_stamped', oldRound.compactionLayer === 'L1');

// 2. NO full innerHTML wipe of #chatInner (the flash) fired.
check('no_innerHTML_wipe', _innerHtmlSets === 0);

// 3. renderChat (→ showStreamingUIForConv whole-list rebuild) was NOT called.
check('renderChat_not_called', _renderChatCalls === 0);

// 4. The streaming bubble node object is the SAME instance (untouched).
const streamingAfter = document.getElementById('streaming-msg');
check('streaming_node_identity_survives',
  streamingAfter === streamingBefore && streamingAfter.getAttribute('data-sentinel') === 'live');

// 5. The stamped OLDER row now shows the COMPACTED pill (surgically repainted).
const olderAfter = document.getElementById('msg-1');
check('older_pill_materialized', !!olderAfter && olderAfter.innerHTML.indexOf('COMPACTED') >= 0);

console.log(out.join('\n'));
"""


def _run(neuter: str = 'none'):
    harness = os.path.join(HERE, '_compacted_no_wipe_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             SSE_HANDLERS_TOOL,  # argv[2]
             ROOT,                                                # argv[3]
             neuter,                                              # argv[4]
             ],
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


def _lines(output):
    return {ln[5:]: ln[:4].strip() for ln in output.splitlines() if ln[:4].strip() in ('PASS', 'FAIL')}


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_compacted_older_message_no_wipe_during_stream():
    output = _run('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'tool_compacted no-wipe failures:\n' + output
    assert output.count('PASS') >= 7, f'expected >=7 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_stream_guard_prevents_the_wipe():
    """Neuter the stream guard (→ always false) → the handler takes the
    renderChat branch even mid-stream, so renderChat IS called (the whole-list
    wipe path). Proves the guard is what keeps the flash from firing."""
    lines = _lines(_run('stream_guard'))
    assert lines.get('renderChat_not_called') == 'FAIL', lines
    # The cold round is still stamped regardless.
    assert lines.get('cold_round_stamped') == 'PASS', lines


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_surgical_replace_is_load_bearing():
    """Neuter the surgical outerHTML replace (→ no-op) → the older row's
    COMPACTED pill is NOT materialized during stream (the fix would silently do
    nothing). Proves the surgical replace is what actually repaints the cold
    row without a wipe."""
    lines = _lines(_run('surgical'))
    assert lines.get('older_pill_materialized') == 'FAIL', lines
    # Still no wipe / no renderChat — the guard held; only the repaint is gone.
    assert lines.get('no_innerHTML_wipe') == 'PASS', lines
    assert lines.get('renderChat_not_called') == 'PASS', lines
