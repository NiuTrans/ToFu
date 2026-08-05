"""jsdom regression: `showStreamingUIForConv` must render a SETTLED trailing
assistant turn STATICALLY even while a stream is registered for a NEW task —
the bubble-owner decision is bound to the live stream entry's identity, never
to "any trailing assistant without an explicit `.done`".

WHY (the mse9r2ir7ql0v4 incident, 2026-08-05)
---------------------------------------------
The old `_lastIsStreamingBubble` heuristic was positional + absence-based:
`(_last.role === "assistant" && !_last.done)`. NO persisted message ever
carries `.done` (it is a client-only streaming marker — the served payloads
for mse9r2ir7ql0v4 all show `done: null`). So any transient that left the
local list at [older…, settled-assistant] while a stream was registered hid
the ENTIRE settled tail behind the streaming bubble (`slice(0,-1)` drops it):
with [m0, m1] the screen showed exactly "first user turn glued to the
streaming bubble" — the reported corruption, invisible to every data-layer
guard because it is a RENDER-layer verdict. RENDER_CONTRACT: placement
decisions use server-assigned stable identity, never transient client state.

THE FIX (static/js/ui/stream_lifecycle.js): the bubble owner is the message
the live stream entry accumulates into — object identity with the bound
`assistantMsg`, or the `_taskId` stamped at bind time. Endpoint planner /
critic, swarm auto-continue and the autopilot VU tail keep explicit flags.

HARNESS — real shipped UI render core under jsdom, a settled trailing
assistant (finishReason='stop', _taskId=tOLD, NO .done — the exact served
shape), a stream entry for a DIFFERENT task tNEW bound to a placeholder that
is NOT the tail:
  • main: msg-0 AND msg-1 render statically alongside #streaming-msg.
  • a legit in-flight tail (the stream entry's own placeholder AT the tail)
    is still sliced off the static list and replaced by the bubble.
DOUBLE-NEUTER (mutated copy; shipped file byte-identical):
  • restore the positional arm `(_last.role === "assistant" && !_last.done)`
    → the settled tail is hidden again (msg-1 gone).
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')

sys.path.insert(0, HERE)
from _jsdom import frontend_module_guard  # noqa: E402

frontend_module_guard(need_jsdom=True)

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NEUTER = process.argv[3] || 'none';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setInterval = win.setInterval = () => 0;
global.setTimeout = win.setTimeout = (fn) => 0;
global.requestAnimationFrame = win.requestAnimationFrame = () => 0;
global.navigator = win.navigator;
global.location = win.location;
global.CSS = win.CSS || { escape: (s) => String(s) };
global.AbortSignal = { timeout: () => undefined };
global.IntersectionObserver = win.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} };
global.sessionStorage = win.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.localStorage = win.localStorage = global.sessionStorage;
global.fetch = async () => { throw new Error('no-fetch'); };

const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
global.escapeHtml = esc;
global.renderMarkdown = (s) => '<p>' + esc(s) + '</p>';
global.t = (k, o) => (o && o.n != null ? k + ':' + o.n : k);
global.Icon = () => '';
global.IconDot = () => '';
global.apiUrl = (p) => p;
global.debugLog = () => {};
global._TOOL_DISPLAY = {};
global.formatClockTime = () => '12:00';
global.getSelection = win.getSelection = () => ({ isCollapsed: true, rangeCount: 0 });
global.showToast = () => {};
global._reportClientError = () => {};
global.stripNoTranslateTags = (s) => String(s == null ? '' : s);
global.formatCny = (n) => '¥' + Number(n || 0).toFixed(2);
global.calcCostCny = () => 0;
global._prefetchConvCosts = async () => false;
global._prefetchConvFileChanges = async () => false;
global.buildTurnNav = () => {};
global._forceScrollToBottom = () => {};
global.scrollToBottom = () => {};
global.isNearBottom = () => true;
global._captureScrollAnchor = () => null;
global._restoreScrollAnchor = () => {};
global._withInstantScroll = (ct, fn) => fn && fn();
global.updateSendButton = () => {};
global.twUpdate = () => {};
global.twStop = () => {};
global.updateStreamingUI = () => {};
global._renderStreamingTranslatePreview = () => {};
global.streamSessions = new Map();
global.saveConversations = () => {};
global.ConvCache = { put: () => {}, get: async () => null, remove: () => {} };
global._restoreConvToolState = () => {};
global._resumePendingTranslations = () => {};
global._runTerminalContinuation = () => {};
global.renderConversationList = () => {};
global._bgRefreshChat = () => {};
global._maybeAutoGenerateTitle = () => {};
global._checkForQueuedTask = () => {};
global._dispatchableQueueCount = () => 0;
global._refreshServerQueue = () => {};
global._reconnectServerTaskIfIdle = () => false;
global._removeStreamingVuBubbleIfTail = () => {};
global._findAutopilotPendingCarrier = () => null;
global._attachAutopilotFollowup = () => {};
global._classifyGhostTailJS = () => null;
global._streamBoundToMsg = () => false;
global._INITIAL_RENDER = 20;
global._lazyObserver = null;
global._lazyConvId = null;
global._lazyRenderedFrom = Infinity;
global._lazyRenderedTo = Infinity;
global._loadingOlder = false;
global._loadingNewer = false;
global._lastRenderedFingerprint = '';
global._openScrollConvId = null;
global._explicitBottomLatch = null;
global.config = {};
global.serverModel = 'kimi-k3';
global._editingMsgIdx = null;

const M0 = { role: 'user', content: 'first question', _msgId: 'm0', timestamp: 1000 };
const M1 = { role: 'assistant', content: 'settled answer', thinking: 'reasoning',
             _msgId: 'm1', finishReason: 'stop', _taskId: 'tOLD-2927c101' };
const conv = { id: 'c1', title: 'c1', messages: [M0, M1], _serverMsgCount: 2,
               createdAt: 1000, updatedAt: 2000, activeTaskId: 'tNEW-cc170530' };
global.conversations = [conv];
global.getActiveConv = () => conversations.find(c => c.id === global.activeConvId) || null;
global.getConvById = (id) => conversations.find(c => c.id === id) || null;

/* The stream entry for the NEW task: bound to a placeholder that is NOT in
 * the array (fresh attach re-target by identity). */
const PH = { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'phNEW' };
global.activeStreams = new Map([['c1', {
  taskId: 'tNEW-cc170530', assistantMsg: PH,
  controller: { signal: { aborted: false } },
}]]);
global.activeConvId = 'c1';

const indirectEval = eval;
for (const f of process.argv.slice(4)) {
  let src = fs.readFileSync(f, 'utf8');
  if (NEUTER === 'positional' && f.endsWith('stream_lifecycle.js')) {
    const needle = "      (!!_liveEntry && (\n        _last === _liveEntry.assistantMsg ||\n        (_last.role === \"assistant\" && !!_last._taskId\n          && _last._taskId === _liveEntry.taskId)\n      )) ||";
    if (src.indexOf(needle) < 0) { console.log('FAIL neuter_target_drifted'); process.exit(0); }
    src = src.replace(needle,
      "      (_last.role === \"assistant\" && !_last.done) ||  // NEUTERED-positional");
    fs.writeFileSync(f + '.neutered', src);
    src = fs.readFileSync(f + '.neutered', 'utf8');
  }
  indirectEval(src);
}

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof showStreamingUIForConv !== 'function') {
  console.log('FAIL fn_exposed showStreamingUIForConv missing'); process.exit(0);
}
check('fn_exposed', true);

/* Pretend the live bubble exists (connectToTask would have inserted it). */
document.getElementById('chatInner').innerHTML =
  '<div class="message" id="streaming-msg" data-msg-id="phNEW"></div>';
showStreamingUIForConv('c1');

const msg0 = document.getElementById('msg-0');
const msg1 = document.getElementById('msg-1');
const sm = document.getElementById('streaming-msg');
check('settled_tail_user_rendered', !!msg0);
check('settled_tail_assistant_rendered_STATICALLY', !!msg1);
check('streaming_bubble_present', !!sm);

/* Legit in-flight tail: the stream entry's placeholder AT the tail is still
 * sliced off the static list (no duplicate static + bubble). */
conv.messages.push(PH);
document.getElementById('chatInner').innerHTML =
  '<div class="message" id="streaming-msg" data-msg-id="phNEW"></div>';
showStreamingUIForConv('c1');
const msg2ph = document.getElementById('msg-2');
check('inflight_tail_not_duplicated_statically', !msg2ph);
check('inflight_bubble_still_present', !!document.getElementById('streaming-msg'));

console.log(out.join('\n'));
console.log('__JSDOM_RESULT__ ' + JSON.stringify({
  pass: out.filter(l => l.startsWith('PASS')).length,
  fail: out.filter(l => l.startsWith('FAIL')).length,
}));
"""

_SOURCES = [
    'static/js/core/safe_html.js',
    'static/js/core/chatinner_dom.js',
    'static/js/core/conv_reducers.js',
    'static/js/core/conv_verify_visibility.js',
    'static/js/core/conv_state_reducer.js',
    'static/js/core.js',
    'static/js/core/conv_persist_helpers.js',
    'static/js/ui/tool_rounds.js',
    'static/js/ui/tool_rounds_rich.js',
    'static/js/ui/finish_info.js',
    'static/js/core/translation_model.js',
    'static/js/ui/streaming_render.js',
    'static/js/ui/chat_render.js',
    'static/js/ui/stream_lifecycle.js',
    'static/js/conv_view.js',
]


def _run(neuter='none'):
    import subprocess
    harness = os.path.join(HERE, '_settled_tail_visible_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT, neuter, *SOURCES_ABS],
            capture_output=True, text=True, timeout=90,
            env={**os.environ, 'JSDOM_HARNESS': os.path.join(HERE, '_jsdom_harness.js')},
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
        neu = os.path.join(JS_DIR, 'ui', 'stream_lifecycle.js.neutered')
        if os.path.exists(neu):
            os.remove(neu)
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


SOURCES_ABS = [os.path.join(ROOT, s) for s in _SOURCES]


def test_streaming_view_renders_settled_tail_statically():
    output = _run('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'settled-tail visibility failures:\n' + output
    for want in ('PASS fn_exposed',
                 'PASS settled_tail_user_rendered',
                 'PASS settled_tail_assistant_rendered_STATICALLY',
                 'PASS streaming_bubble_present',
                 'PASS inflight_tail_not_duplicated_statically',
                 'PASS inflight_bubble_still_present'):
        assert want in output, output


def test_NC_positional_heuristic_hides_the_tail(tmp_path):
    """NEUTER: restore the old positional arm on a mutated copy → the settled
    trailing assistant is hidden again (msg-1 missing). Shipped file untouched."""
    output = _run('positional')
    assert 'FAIL settled_tail_assistant_rendered_STATICALLY' in output, (
        'NEUTER did not bite: settled tail stayed visible with the positional arm restored.\n' + output)
    src = open(os.path.join(JS_DIR, 'ui', 'stream_lifecycle.js'), encoding='utf-8').read()
    assert 'NEUTERED-positional' not in src, 'harness mutated the shipped stream_lifecycle.js'
