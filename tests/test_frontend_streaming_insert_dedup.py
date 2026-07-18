"""tests/test_frontend_streaming_insert_dedup.py — guard for Fix ① of the
empty-"Agent" air-bubble root fix: STRUCTURAL DEDUP AT THE INSERT BOUNDARY.

WHY
---
Seven live-path call sites used to insert the streaming bubble with a RAW
``inner.insertAdjacentHTML('beforeend', _streamingBubbleHTML(...))`` that
BYPASSED ``ConvView.startStreaming``'s ``_evictByMsgId`` dedup
(main_send_pipeline:57, main_translating_bubble:58,
sse_pipeline:872/1289/1367/1507/1560). Under reconnect/replan churn a residual
``#streaming-msg`` or a drifted static twin for the SAME ``_msgId`` could
coexist with the fresh insert → MULTIPLE empty "Agent" bubbles for one logical
turn. Fix ① routes every live insert through ``ConvView.startStreaming``, which
calls ``_evictByMsgId(inner, msgId, null)`` BEFORE inserting and also drops any
leftover ``#streaming-msg`` singleton — so the invariant
"one ``_msgId`` ⇒ at most one DOM node in #chatInner" holds at the INSERT
boundary, not merely at render-time cleanup.

This test drives the REAL ``static/js/conv_view.js`` (which defines
``ConvView.startStreaming`` + ``_evictByMsgId``) plus the real
``_streamingBubbleHTML`` and asserts the invariant under two churn scenarios.

CHECKS (GREEN with the dedup in place)
  A. Two consecutive startStreaming for the SAME _msgId → exactly ONE node
     carrying that data-msg-id, and exactly ONE #streaming-msg.
  B. A stranded static twin (a `msg-3` node ALSO carrying the _msgId, as a
     drifted index would leave) + then startStreaming → the twin is evicted,
     exactly ONE node with that _msgId remains.
  C. A leftover #streaming-msg with a DIFFERENT _msgId + startStreaming for a
     new _msgId → the old streaming singleton is gone, one node per id.

NEUTER CONTROL
  • nc_evict_off: neuter ``_evictByMsgId`` to a no-op → A and B FAIL (twins
    survive), proving the eviction is load-bearing.
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
ESCAPE_HTML = os.path.join(JS_DIR, 'core', 'escape_html.js')
SAFE_HTML = os.path.join(JS_DIR, 'core', 'safe_html.js')
STREAMING_RENDER = os.path.join(JS_DIR, 'ui', 'streaming_render.js')
CONV_VIEW = os.path.join(JS_DIR, 'conv_view.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[6];
const NC = process.argv[7] || '';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Minimal globals the two real modules read ──
const _conv = { id: 'c1', messages: [], activeTaskId: null };
win.conversations = global.conversations = [_conv];
win.activeConvId = global.activeConvId = 'c1';
win.formatClockTime = global.formatClockTime = () => '00:00';
win.t = global.t = (k) => k;
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<img data-avatar="worker">';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<img data-avatar="planner">';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<img data-avatar="critic">';
win._ensureMsgId = global._ensureMsgId = (m) => m;
win.renderMessage = global.renderMessage = (m) => '<div class="message">rm</div>';
win._convRenderFingerprint = global._convRenderFingerprint = () => 'fp';
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';

// ── Load real deps: escape_html, safe_html, streaming_render (_streamingBubbleHTML) ──
(0, eval)(fs.readFileSync(process.argv[2], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // safe_html.js
// streaming_render.js references many helpers at CALL time only; only
// _streamingBubbleHTML is exercised here, so a bare eval of the file (defining
// all its fns) is enough. Guard against a missing dep throwing at load.
try { (0, eval)(fs.readFileSync(process.argv[4], 'utf8')); }
catch (e) { console.log('FAIL streaming_render_load ' + e.message); process.exit(0); }
if (typeof _streamingBubbleHTML !== 'function') {
  console.log('FAIL streamingBubbleHTML_missing'); process.exit(0);
}
win._streamingBubbleHTML = global._streamingBubbleHTML = _streamingBubbleHTML;

// ── Load conv_view.js (defines window.ConvView, _evictByMsgId), optionally neutered ──
let cvSrc = fs.readFileSync(process.argv[5], 'utf8');
const CV = cvSrc;
if (NC === 'nc_evict_off') {
  // Neuter _evictByMsgId to a no-op so the dedup can't fire.
  cvSrc = CV.replace('function _evictByMsgId(inner, msgId, exceptEl) {',
                     'function _evictByMsgId(inner, msgId, exceptEl) {\n    return 0; // NC nc_evict_off');
}
const _applied = (NC === '') || (cvSrc !== CV);
check('nc_pattern_applied', _applied);
(0, eval)(cvSrc);
if (typeof win.ConvView === 'undefined' || typeof win.ConvView.startStreaming !== 'function') {
  console.log('FAIL convview_missing'); process.exit(0);
}
const inner = document.getElementById('chatInner');
function countByMsgId(id) {
  return inner.querySelectorAll('[data-msg-id="' + id + '"]').length;
}
function countStreaming() {
  return inner.querySelectorAll('#streaming-msg').length;
}

// ══ A. two consecutive startStreaming, SAME _msgId ══
{
  inner.innerHTML = '';
  win.ConvView.startStreaming('c1', { role: 'worker', msgId: 'M-A' });
  win.ConvView.startStreaming('c1', { role: 'worker', msgId: 'M-A' });
  check('A_single_node_same_msgid', countByMsgId('M-A') === 1);
  check('A_single_streaming_singleton', countStreaming() === 1);
}

// ══ B. stranded static twin at a drifted index also carrying the _msgId ══
{
  inner.innerHTML = '';
  // A finalized static bubble left at msg-3 that ALSO carries M-B (the drift
  // scenario the eviction targets).
  inner.insertAdjacentHTML('beforeend',
    '<div class="message" id="msg-3" data-msg-id="M-B">stale twin</div>');
  win.ConvView.startStreaming('c1', { role: 'worker', msgId: 'M-B' });
  check('B_twin_evicted', countByMsgId('M-B') === 1);
}

// ══ C. leftover #streaming-msg with a DIFFERENT _msgId ══
{
  inner.innerHTML = '';
  inner.insertAdjacentHTML('beforeend',
    '<div class="message" id="streaming-msg" data-msg-id="M-OLD">old streaming</div>');
  win.ConvView.startStreaming('c1', { role: 'worker', msgId: 'M-NEW' });
  check('C_old_streaming_gone', countByMsgId('M-OLD') === 0);
  check('C_new_present', countByMsgId('M-NEW') === 1);
  check('C_single_streaming_singleton', countStreaming() === 1);
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_stream_dedup_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ESCAPE_HTML, SAFE_HTML, STREAMING_RENDER, CONV_VIEW, ROOT, nc],
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
def test_streaming_insert_dedups_by_msgid():
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'streaming-insert dedup failures:\n' + output
    for want in ('PASS A_single_node_same_msgid', 'PASS A_single_streaming_singleton',
                 'PASS B_twin_evicted', 'PASS C_old_streaming_gone',
                 'PASS C_new_present', 'PASS C_single_streaming_singleton'):
        assert want in output, 'missing expected pass:\n' + output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_evict_off_regression_is_caught():
    """Neutering _evictByMsgId must let twins survive (A + B fail)."""
    output = _run('nc_evict_off')
    assert 'PASS nc_pattern_applied' in output, f'NC did not apply:\n{output}'
    assert 'FAIL A_single_node_same_msgid' in output or 'FAIL B_twin_evicted' in output, (
        'Neutering _evictByMsgId did NOT surface a twin — eviction is not '
        'load-bearing:\n' + output)


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
    else:
        test_streaming_insert_dedups_by_msgid()
        test_nc_evict_off_regression_is_caught()
        print('PASS test_frontend_streaming_insert_dedup')
