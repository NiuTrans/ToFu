"""tests/test_frontend_dup_msgid_reconcile.py — duplicate-_msgId tolerance.

Measured data (conv ms8bx7089s3268): idx1 (fr=aborted) and idx2 (fr=stop)
share ONE _msgId (tmp_196fedef). The id-keyed seams all collapsed on the
first match:
  • `_reconcileFindEl` (querySelector) returned the FIRST node for BOTH
    entries → the second turn never rendered its own bubble;
  • `assertChatInnerOrder` mapped id→last index → a FAITHFUL dup render
    violated the assertion (RENDER ORDER VIOLATION beacon, 22:22:22);
  • `_msgElIndex` (action buttons) resolved the SECOND bubble's buttons
    to the FIRST turn.

The fix keys every seam by OCCURRENCE: the k-th array entry with a dup id
maps to the k-th DOM node / k-th array index. Pinned here against the
REAL shipped JS (chat_render.js reconcile + chatinner_dom.js assertion)
under jsdom — skips cleanly when node+jsdom are absent.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_frontend_dup_msgid_reconcile.py -v
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
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { if (typeof fn === 'function') fn(); return 0; };
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };

const out = [];
function check(name, cond, extra) { out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? (' ' + extra) : '')); }

win.activeStreams = global.activeStreams = new Map();
win.activeConvId = global.activeConvId = 'c1';
win.t = global.t = (k) => k;
win._fmtAbsoluteDateTime = global._fmtAbsoluteDateTime = () => '';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
win.renderToolRoundsHTML = global.renderToolRoundsHTML = () => '';
win.renderSegmentTimelineHTML = global.renderSegmentTimelineHTML = () => '';
const _noop = () => '';
for (const name of [
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar',
  'renderErrorEnvelope','renderBranchZone','renderTurnCtxNote',
  'renderPreferenceLearnedHtml','renderFinishInfo','_buildSwarmInboxChipsHTML',
  '_injectAnchoredBranches','_stampFreshness','buildTurnNav','calcCostCny',
  '_forceScrollToBottom','scrollToBottom','isNearBottom','showStreamingUIForConv',
  '_ensureLazyObserver','_destroyLazyObserver','_captureScrollAnchor','_restoreScrollAnchor',
]) { if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; } }
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<img>';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<img>';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<img>';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<img>';
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;
win._prefetchConvCosts = global._prefetchConvCosts = () => ({ then: () => {} });
win._prefetchConvFileChanges = global._prefetchConvFileChanges = () => ({ then: () => {} });
win._editingMsgIdx = global._editingMsgIdx = null;
win._activeBranch = global._activeBranch = null;
win._lazyRenderedFrom = global._lazyRenderedFrom = 0;
win._lazyRenderedTo = global._lazyRenderedTo = Infinity;
win._lazyConvId = global._lazyConvId = null;
win._openScrollConvId = global._openScrollConvId = null;
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';
win._convRenderFingerprint = global._convRenderFingerprint =
  (c) => 'fp:' + (c ? c.messages.length : 0) + ':' + Math.random();

// chatinner_dom.js is a leaf (document/console only) — real file.
eval(fs.readFileSync(path.join(ROOT, 'static/js/core/chatinner_dom.js'), 'utf8'));
// chat_render.js must be concatenated with the file that `let`-declares
// _explicitBottomLatch (ui/streaming_render.js) — same trick as the
// id_keyed_reconcile harness.
eval(fs.readFileSync(path.join(ROOT, 'static/js/core/escape_html.js'), 'utf8'));
eval(fs.readFileSync(path.join(ROOT, 'static/js/core/safe_html.js'), 'utf8'));
eval(fs.readFileSync(path.join(ROOT, 'static/js/core/translation_model.js'), 'utf8'));
eval(fs.readFileSync(path.join(ROOT, 'static/js/ui/translation_indicator.js'), 'utf8'));
eval(fs.readFileSync(path.join(ROOT, 'static/js/ui/streaming_render.js'), 'utf8') + '\n;\n' +
     fs.readFileSync(path.join(ROOT, 'static/js/ui/chat_render.js'), 'utf8') + `
;
globalThis.__W = {
  set from(v){ _lazyRenderedFrom = v; },
  set to(v){ _lazyRenderedTo = v; },
  set convId(v){ _lazyConvId = v; },
};
`);
__W.from = 0; __W.to = Infinity; __W.convId = null;

if (typeof renderChat !== 'function') { console.log('FAIL fn_exposed renderChat missing'); process.exit(0); }

function mkMsg(id, role, text) {
  return { role: role || 'assistant', _msgId: id, content: text || ('body ' + id) };
}
function domNodes() {
  const inner = win.document.getElementById('chatInner');
  return Array.from(inner.querySelectorAll('[id^="msg-"]'));
}
function idOf(el) { return el.getAttribute('data-msg-id'); }

// ★ THE incident shape: two array entries sharing ONE _msgId.
const conv = { id: 'c1', messages: [
  mkMsg('u1', 'user', 'first question'),
  mkMsg('dup', 'assistant', 'ABORTED attempt body'),
  mkMsg('dup', 'assistant', 'FINAL answer body'),
  mkMsg('u2', 'user', 'second question'),
  mkMsg('a2', 'assistant', 'second answer'),
] };
conv.messages[1].finishReason = 'aborted';
conv.messages[2].finishReason = 'stop';
win.conversations = global.conversations = [conv];
win.getActiveConv = global.getActiveConv = () => conv;

// ── 1) Full render: BOTH dup entries render their own bubble, in order. ──
renderChat(conv, true);
let nodes = domNodes();
check('full_five_nodes', nodes.length === 5, 'got=' + nodes.length);
check('full_dup_two_nodes', nodes.filter(el => idOf(el) === 'dup').length === 2,
  'dup nodes=' + nodes.filter(el => idOf(el) === 'dup').length);
check('full_order', nodes.map(idOf).join(',') === 'u1,dup,dup,u2,a2',
  'got=' + nodes.map(idOf).join(','));
// Each dup bubble's body belongs to its own entry (no content collapse).
const dupEls = nodes.filter(el => idOf(el) === 'dup');
check('dup_first_body_aborted', dupEls[0] && dupEls[0].textContent.indexOf('ABORTED attempt body') !== -1);
check('dup_second_body_final', dupEls[1] && dupEls[1].textContent.indexOf('FINAL answer body') !== -1);

// ── 2) The order assertion on a FAITHFUL dup render must PASS (the beacon
//       fired on exactly this shape before the occurrence-aware pos map). ──
const inner = win.document.getElementById('chatInner');
win.resetChatInnerOrderForTests();
check('faithful_dup_render_no_violation', win.assertChatInnerOrder(inner, conv, 'test') === true);

// ── 3) Surgical re-render after a mid insert: dup nodes keep identity. ──
conv.messages.splice(1, 0, mkMsg('u1b', 'user', 'follow-up'));
renderChat(conv, false);
nodes = domNodes();
check('surgical_six_nodes', nodes.length === 6, 'got=' + nodes.length);
check('surgical_order', nodes.map(idOf).join(',') === 'u1,u1b,dup,dup,u2,a2',
  'got=' + nodes.map(idOf).join(','));
const dupEls2 = nodes.filter(el => idOf(el) === 'dup');
check('surgical_dup_first_body', dupEls2[0] && dupEls2[0].textContent.indexOf('ABORTED attempt body') !== -1);
check('surgical_dup_second_body', dupEls2[1] && dupEls2[1].textContent.indexOf('FINAL answer body') !== -1);
win.resetChatInnerOrderForTests();
check('surgical_no_violation', win.assertChatInnerOrder(inner, conv, 'test') === true);

// ── 4) _msgElIndex: the SECOND dup bubble's buttons act on the SECOND turn. ──
const idxFirst = win._msgElIndex(dupEls2[0]);
const idxSecond = win._msgElIndex(dupEls2[1]);
check('action_index_first_dup', idxFirst === 2, 'got=' + idxFirst);
check('action_index_second_dup', idxSecond === 3, 'got=' + idxSecond);

// ── 5) GENUINE disorder still trips the assertion: move the second dup node
//       AFTER the last message. ──
inner.insertBefore(dupEls2[1], null);
win.resetChatInnerOrderForTests();
check('real_disorder_still_detected', win.assertChatInnerOrder(inner, conv, 'test') === false);

console.log(out.join('\n'));
process.exit(0);
"""


def _run() -> str:
    if not _node_deps_available():
        pytest.skip('node/jsdom not installed')
    probe = os.path.join(ROOT, 'node_modules', '.tmp_dup_msgid_harness.js')
    with open(probe, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        p = subprocess.run(['node', probe, ROOT], capture_output=True,
                           text=True, timeout=120)
    finally:
        os.unlink(probe)
    return (p.stdout + p.stderr)


@pytest.mark.unit
def test_dup_msgid_reconcile():
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'dup-msgid reconcile failures:\n' + output


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
