"""tests/test_frontend_autopilot_vu_start_eager.py — the autopilot VU
placeholder bubble appears the INSTANT autopilot takes over, NOT only once the
first content/phase frame arrives.

WHY (the owner's requirement)
-----------------------------
When autopilot takes over, the user must SEE an "Autopilot · composing…"
placeholder immediately — otherwise they cannot tell whether an automatic
guidance round has started. The VU's first token can be tens of seconds away
(rate-limit / queue waits), so the placeholder must NOT depend on any
content / delta / phase frame: it must ride the ``autopilot_vu_start`` event,
which the backend emits BEFORE the (potentially slow) ``run_virtual_user`` LLM
call.

Two halves of the contract, both load-bearing:
  1. BACKEND source-order guard — in ``maybe_run_autopilot`` the
     ``AUTOPILOT_VU_START`` append MUST precede the ``run_virtual_user(...)``
     call. If it were emitted AFTER, the placeholder would only appear once the
     VU reply is already produced (defeating the purpose during the exact
     rate-limit stall where liveness feedback matters most).
  2. FRONTEND eager-create — ``_handleAutopilotVuEvent`` on ``autopilot_vu_start``
     alone (no inner event, no content) MUST stand up the VU bubble via
     ``_beginVuStreaming``. (The separate ``test_frontend_autopilot_warmup.py``
     locks the LAZY fallback for a dropped-start reconnect; THIS locks the
     PRIMARY eager path.)

NEGATIVE CONTROL (frontend): a shim that ignores ``autopilot_vu_start`` (the
pre-fix "only create on content" behaviour) leaves NO bubble — the assertion
FAILS, proving the eager create is what makes the placeholder appear.

Skips cleanly when node + jsdom aren't installed (frontend half); the backend
source-order guard runs everywhere (pure text inspection).
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# ══════════════════════════════════════════════════════════════════════
#  Backend half — source-order guard (runs everywhere, no node needed).
# ══════════════════════════════════════════════════════════════════════

def test_backend_emits_vu_start_before_running_virtual_user():
    """AUTOPILOT_VU_START must be appended BEFORE the run_virtual_user() call so
    the placeholder appears before any rate-limit first-token stall.

    NEGATIVE CONTROL companion: were the emit moved AFTER run_virtual_user, the
    placeholder would only materialise once the VU reply already exists — this
    ordering assertion (index of the emit < index of the call) would fail.
    """
    src_path = os.path.join(ROOT, 'lib', 'tasks_pkg', 'autopilot.py')
    src = open(src_path, encoding='utf-8').read()

    emit_marker = 'EventType.AUTOPILOT_VU_START'
    call_marker = 'vu_result = run_virtual_user(task, vu_msg_id=vu_msg_id)'
    assert emit_marker in src, 'AUTOPILOT_VU_START emit not found'
    assert call_marker in src, 'run_virtual_user call not found'
    emit_idx = src.index(emit_marker)
    call_idx = src.index(call_marker)
    assert emit_idx < call_idx, (
        'AUTOPILOT_VU_START must be emitted BEFORE run_virtual_user() so the '
        'placeholder appears before the (possibly rate-limited) LLM call')

    # The specific INVOCATION (not the def) must come AFTER the emit within the
    # hook — belt-and-suspenders on the ordering above, keyed on the exact call
    # marker so it isn't tripped by the function definition earlier in the file.
    assert call_marker not in src[:emit_idx], (
        'the run_virtual_user INVOCATION must not precede the VU_START emit')

    # The eager-emit intent is documented near the emit (defends against a
    # future refactor "optimising" the emit to after the call). The rationale
    # block precedes the vu_msg_id mint; scan a generous preamble window.
    between = src[max(0, emit_idx - 1400):emit_idx]
    assert 'EAGERLY' in between or 'eager' in between.lower(), (
        'the eager-emit rationale comment should sit with the VU_START emit')


# ══════════════════════════════════════════════════════════════════════
#  Frontend half — eager create on vu_start alone (jsdom).
# ══════════════════════════════════════════════════════════════════════

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NC = process.argv[3] === 'NC';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

const _I18N = { 'autopilot.warming': 'Autopilot 启动中…' };
win.t = global.t = (k) => (k in _I18N ? _I18N[k] : k);
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.formatClockTime = global.formatClockTime = () => '12:00';
win.raw = global.raw = (s) => ({ _raw: String(s), toString() { return String(s); } });
win.safeHtml = global.safeHtml = function (strings, ...vals) {
  let out = '';
  strings.forEach((s, i) => { out += s; if (i < vals.length) { const v = vals[i]; out += (v && v._raw !== undefined) ? v._raw : win.escapeHtml(v); } });
  return { toString() { return out; } };
};
win.renderMarkdown = global.renderMarkdown = (s) => String(s == null ? '' : s);
win.activeConvId = global.activeConvId = 'C1';
win.conversations = global.conversations = [{ id: 'C1', messages: [] }];

const streamBufs = new Map();
win.streamBufs = global.streamBufs = streamBufs;
win.twStart = global.twStart = (cid) => { streamBufs.set(cid, { content: '', thinking: '', toolRounds: [], phase: null }); };
win.twUpdate = global.twUpdate = () => {};
win.twStop = global.twStop = (cid) => { streamBufs.delete(cid); };

const _noop = () => {}; const _noopStr = () => '';
for (const [name, fn] of [
  ['scrollToBottom', _noop], ['isNearBottom', () => false], ['buildTurnNav', _noop],
  ['_stampFreshness', _noop], ['renderMcpLoginHintHtml', _noopStr],
  ['renderTurnProvenanceHtml', _noopStr], ['renderPreferenceLearnedHtml', _noopStr],
  ['_buildSwarmInboxChipsHTML', _noopStr], ['_fcFingerprint', () => 0],
  ['_renderFileChangesHtml', _noopStr],
  ['_extractFileChangesFromRoundsAsync', () => Promise.resolve([])],
  ['normalizeErrorEnvelope', (x) => x], ['saveConversations', _noop],
  ['Icon', (n) => '<svg data-icon="' + n + '"></svg>'],
]) { if (typeof win[name] === 'undefined') { win[name] = global[name] = fn; } }
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<svg id="critic-avatar"></svg>';
win.ConvCache = global.ConvCache = { put: () => {} };

eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'streaming_render.js'), 'utf8'));
eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'streaming_ui.js'), 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
if (typeof _handleAutopilotVuEvent !== 'function') { console.log('FAIL fn_exposed'); process.exit(0); }
check('fn_exposed', true);

const conv = conversations.find(c => c.id === 'C1');

// NC: shim that IGNORES autopilot_vu_start (pre-fix "only create on content").
let handle = _handleAutopilotVuEvent;
if (NC) {
  handle = function (convId, ev) {
    if (ev.type === 'autopilot_vu_start') return;   // NC: do NOT create eagerly
    return _handleAutopilotVuEvent(convId, ev);
  };
}

// PRECONDITION: no bubble before the start event.
check('precondition_no_bubble', conv.messages.length === 0
  && !document.getElementById('streaming-msg'));

// Deliver ONLY autopilot_vu_start — no inner event, no content, no phase.
handle('C1', { type: 'autopilot_vu_start', vuMsgId: 'vu-eager-1' });

// ASSERT: the placeholder VU bubble exists immediately.
const entry = _findVuMsgById(conv, 'vu-eager-1');
check('bubble_created_on_start', !!entry);
check('bubble_is_streaming_vu', !!entry && entry.msg._isVirtualUser && entry.msg._streamingVu === true);
check('bubble_empty_content', !!entry && (entry.msg.content || '') === '');
check('streaming_msg_in_dom', !!document.getElementById('streaming-msg'));
// And it renders the short warm-up label (composing pulse), not a hang.
const body = document.getElementById('streaming-body');
const html = body ? body.innerHTML : (document.getElementById('chatInner').innerHTML);
check('warmup_label_shown', html.includes('Autopilot 启动中…'));

console.log(out.join('\n'));
"""


def _run(nc: bool) -> str:
    harness = os.path.join(HERE, f'_ap_vu_start_eager_{"nc" if nc else "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        argv = ['node', harness, ROOT]
        if nc:
            argv.append('NC')
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
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
def test_vu_start_alone_creates_placeholder_eagerly():
    output = _run(nc=False)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'eager-vu-start failures:\n' + output
    assert output.count('PASS') >= 7, f'expected >=7 PASS lines:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_ignoring_vu_start_leaves_no_bubble():
    """NC: ignoring autopilot_vu_start (create-only-on-content) means no bubble
    ever appears on start — proving the eager create is load-bearing."""
    output = _run(nc=True)
    lines = output.splitlines()

    def _status(name):
        for ln in lines:
            if ln.endswith(' ' + name) or ln.endswith(name):
                return ln.split(' ', 1)[0]
        return None

    assert _status('bubble_created_on_start') == 'FAIL', \
        'NC should create NO bubble on vu_start:\n' + output
    assert _status('precondition_no_bubble') == 'PASS', \
        'NC harness precondition broke:\n' + output


if __name__ == '__main__':
    test_backend_emits_vu_start_before_running_virtual_user()
    if not _node_deps_available():
        print('SKIP frontend — node + jsdom not available')
    else:
        test_vu_start_alone_creates_placeholder_eagerly()
        test_nc_ignoring_vu_start_leaves_no_bubble()
    print('PASS test_frontend_autopilot_vu_start_eager')
