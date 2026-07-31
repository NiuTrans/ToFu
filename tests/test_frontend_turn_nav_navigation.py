"""tests/test_frontend_turn_nav_navigation.py — the sidebar turn-nav must
NAVIGATE, and the per-message action bar must not keep a dead Delete button.

WHY (three defects in one family: state that is RENDERED but not FINGERPRINTED)
------------------------------------------------------------------------------
Reported symptom: "turnnav in the sidebar often fails to navigate, some dots are
unresponsive when clicked, and the delete button sometimes behaves very
strangely." All three reduce to the same shape — a cached-render guard whose
fingerprint does NOT sample everything the render actually depends on, so the
repaint is skipped and the UI keeps showing stale, non-functional affordances.

Defect 1 — turn-nav fingerprint is under-specified (`buildTurnNav`).
  `_turnNavFp` was `userCount + lastUserContent[:40] + messages.length`. It
  contains NO conversation identity, and samples ONLY the LAST user message.
  So switching to a DIFFERENT conversation with the same shape (same length,
  same user count, same trailing user text) is a fingerprint HIT → `buildTurnNav`
  returns early → the sidebar keeps the PREVIOUS conversation's dots, whose
  `data-msg-idx` now point into a different message array. Clicking them
  navigates nowhere or to an unrelated turn. Editing / deleting any NON-last
  user message is invisible to the fingerprint for the same reason.

Defect 2 — `scrollToTurn` cannot reach a message evicted BELOW the render window.
  The bounded render window (`_MAX_RENDER_WINDOW`, streaming_render.js) evicts
  from the TAIL when the reader scrolls up, so the rendered span becomes e.g.
  [160, 240) of a 300-message conversation. `scrollToTurn` handled only the
  ABOVE case (`idx < _lazyRenderedFrom`); for anything at/after
  `_lazyRenderedTo` it fell through to a "force a re-render" fallback, but that
  full re-render paints only the TAIL window [total-_INITIAL_RENDER, total).
  A dot for index 250 is in neither range → `msg-250` still absent → the click
  is a silent no-op. Only dots that happen to land in the final 20 work, which
  is exactly the reported "some dots are unresponsive".

Defect 3 — the action-bar gate is conversation-level but the fingerprint is
  message-level (`_msgFingerprint` / `renderMessage`).
  `canDelete = conv && !activeStreams.has(conv.id) && !conv.activeTaskId` — pure
  CONVERSATION state. `_msgFingerprint` samples only the MESSAGE, so when a task
  starts or finishes, the surgical diff reads an UNCHANGED `data-mfp` and skips
  every row. Both directions are broken:
    • busy → idle: the settled turn keeps NO delete button (it only appears
      after some unrelated full re-render).
    • idle → busy: the turn keeps a VISIBLE delete button, but `deleteTurn()`
      re-reads the live gate and returns immediately — a button that silently
      does nothing. That is the "behaves very strangely" report.

NEUTER CONTROLS (each proves its fix is load-bearing)
  • NC-1: drop conv identity from the turn-nav fingerprint seed → the
    conversation-switch assertion FAILS (stale dots return).
  • NC-2: disable the new "evicted below the window" branch in `scrollToTurn`
    → the below-window navigation assertion FAILS (dead click returns).
  • NC-3: drop the conversation action-gate token from `_msgFingerprint` → the
    delete-affordance repaint assertions FAIL (stale button returns).

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import json
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


# ══════════════════════════════════════════════════════════════════════════
# Harness 1 — turn nav (fingerprint + scrollToTurn), driven against the REAL
# streaming_render.js window machinery.
#
# streaming_render.js and turn_nav.js are eval'd in ONE scope (exactly how the
# bundler concatenates them) because the render-window bounds
# `_lazyRenderedFrom` / `_lazyRenderedTo` are `let`-declared in the former and
# WRITTEN by the latter. Evaluating them separately would give turn_nav.js a
# different binding than the loaders read, and the test would prove nothing.
# ══════════════════════════════════════════════════════════════════════════

_NAV_DRIVER = r"""
;(function () {
  const out = global.__out;
  const inner = document.getElementById('chatInner');
  const nav = document.getElementById('turnNav');
  function check(name, cond, extra) {
    out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? (' ' + extra) : ''));
  }

  /* Faithful stand-in for renderChat's FULL re-render path: repaints the TAIL
   * window [total-_INITIAL_RENDER, total) and resets the lazy bounds exactly as
   * chat_render.js does. Defined INSIDE the eval scope so it writes the same
   * let-bindings the loaders read. This is the fallback scrollToTurn reaches
   * for, so modelling it honestly is what exposes defect 2. */
  window.ConvView = { replaceAll: function (cid) {
    const c = conversations.find(x => x.id === cid);
    if (!c) return false;
    const total = c.messages.length;
    const s = Math.max(0, total - _INITIAL_RENDER);
    _lazyRenderedFrom = s; _lazyRenderedTo = total; _lazyConvId = c.id;
    let h = '<div id="_lazyLoadSentinel" class="lazy-sentinel">'
          + '<span class="_lazy-count">' + s + '</span></div>';
    for (let i = s; i < total; i++) h += renderMessage(c.messages[i], i);
    inner.innerHTML = h;
    return true;
  }};

  function seedTail(conv) {
    const total = conv.messages.length;
    const START = Math.max(0, total - _INITIAL_RENDER);
    _lazyConvId = conv.id; _lazyRenderedFrom = START; _lazyRenderedTo = total;
    let html = '<div id="_lazyLoadSentinel" class="lazy-sentinel">'
             + '<span class="_lazy-count">' + START + '</span></div>';
    for (let i = START; i < total; i++) html += renderMessage(conv.messages[i], i);
    inner.innerHTML = html;
    _ensureLazyObserver();
  }
  /* Scroll up until the bounded window has evicted the tail. */
  function scrollUp(times) {
    for (let k = 0; k < times; k++) { _loadingOlder = false; _loadOlderMessages(); }
  }

  const convA = conversations[0], convB = conversations[1], TOTAL = convA.messages.length;

  /* Did buildTurnNav actually REGENERATE the dot nodes? Stamp the live nodes,
   * call it, and see whether the stamp survived. Independent of dot TEXT, which
   * matters because convA/convB are deliberately content-identical here (that is
   * what isolates conversation IDENTITY as the only distinguishing input). */
  function rebuiltBy(fn) {
    nav.querySelectorAll('.turn-dot').forEach(d => d.setAttribute('data-probe', '1'));
    const stamped = nav.querySelectorAll('.turn-dot[data-probe]').length;
    fn();
    const survived = nav.querySelectorAll('.turn-dot[data-probe]').length;
    return stamped > 0 && survived === 0;
  }

  // ══ Defect 1: conversation switch with an IDENTICAL nav fingerprint ══
  // convB is byte-identical to convA in every field the nav samples, so the
  // ONLY thing that can force a rebuild is the conversation id.
  seedTail(convA);
  buildTurnNav(convA);
  check('dots_built', nav.querySelectorAll('.turn-dot').length > 1,
        'n=' + nav.querySelectorAll('.turn-dot').length);

  activeConvId = 'cB';
  seedTail(convB);
  check('switch_rebuilds_nav', rebuiltBy(() => buildTurnNav(convB)));

  // ══ Defect 1b: a NON-last user message edited in the SAME conversation ══
  const beforeEdit = nav.querySelector('.turn-dot').getAttribute('title');
  convB.messages[0].content = 'EDITED FIRST TURN';
  buildTurnNav(convB);
  const afterEdit = nav.querySelector('.turn-dot').getAttribute('title');
  check('midhistory_edit_rebuilds_nav', beforeEdit !== afterEdit,
        'after=' + JSON.stringify(afterEdit));

  // ══ Defect 1c: the perf guard must SURVIVE — an unchanged conv is a no-op ══
  const htmlBefore = nav.innerHTML;
  buildTurnNav(convB);            // identical input → must early-return
  check('unchanged_conv_is_noop', nav.innerHTML === htmlBefore);

  // ══ Defect 2: a dot whose message was evicted BELOW the render window ══
  activeConvId = 'cA';
  seedTail(convA);
  buildTurnNav(convA);
  scrollUp(6);
  const winFrom = _lazyRenderedFrom, winTo = _lazyRenderedTo;
  check('window_is_capped', winTo < TOTAL,
        'window=' + winFrom + '..' + winTo + ' total=' + TOTAL);

  /* Target sits BELOW the capped window but ABOVE the tail the fallback
   * re-render would paint — the exact hole that made the click a no-op. */
  const target = winTo + 10;
  check('target_in_the_hole',
        target >= winTo && target < TOTAL - _INITIAL_RENDER,
        'target=' + target);
  __setScrolled(null);
  scrollToTurn(target);
  check('below_window_navigates', __getScrolled() === 'msg-' + target,
        'scrolledTo=' + __getScrolled());

  // ══ Defect 2b: the ABOVE-window path must keep working (no regression) ══
  seedTail(convA);
  scrollUp(2);
  const upTarget = Math.max(0, _lazyRenderedFrom - 5);
  __setScrolled(null);
  scrollToTurn(upTarget);
  check('above_window_navigates', __getScrolled() === 'msg-' + upTarget,
        'target=' + upTarget + ' scrolledTo=' + __getScrolled());

  // ══ Defect 2c: an already-rendered dot still scrolls directly ══
  __setScrolled(null);
  const inWin = _lazyRenderedFrom + 1;
  scrollToTurn(inWin);
  check('in_window_navigates', __getScrolled() === 'msg-' + inWin,
        'scrolledTo=' + __getScrolled());

  console.log(out.join('\n'));
})();
"""

_NAV_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NC = process.argv[3] || '';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div>'
  + '<div id="turnNav"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
global.CSS = win.CSS;

// Deterministic geometry: every bubble 100px, viewport 800px.
const CLIENT_H = 800, H = 100;
let _scrollTop = 0;
function _idxs() {
  const els = document.getElementById('chatInner').querySelectorAll('[id^="msg-"]');
  const out = [];
  els.forEach((el) => { const m = el.id.match(/^msg-(\d+)$/); if (m) out.push(parseInt(m[1], 10)); });
  return out.sort((a, b) => a - b);
}
const container = document.getElementById('chatContainer');
Object.defineProperty(container, 'clientHeight', { get: () => CLIENT_H, configurable: true });
Object.defineProperty(container, 'scrollHeight', { get: () => _idxs().length * H, configurable: true });
Object.defineProperty(container, 'scrollTop', {
  get: () => _scrollTop,
  set: (v) => { const max = Math.max(0, _idxs().length * H - CLIENT_H); _scrollTop = Math.max(0, Math.min(v, max)); },
  configurable: true,
});
win.Element.prototype.getBoundingClientRect = function () {
  if (this.id === 'chatContainer') return { top: 0, bottom: CLIENT_H, height: CLIENT_H, left: 0, right: 0, width: 0 };
  const arr = _idxs();
  const m = (this.id || '').match(/^msg-(\d+)$/);
  if (m) {
    const order = arr.indexOf(parseInt(m[1], 10));
    if (order < 0) return { top: 0, bottom: 0, height: H, left: 0, right: 0, width: 0 };
    const top = order * H - _scrollTop;
    return { top, bottom: top + H, height: H, left: 0, right: 0, width: 0 };
  }
  return { top: 0, bottom: 0, height: 0, left: 0, right: 0, width: 0 };
};
let _scrolledTo = null;
win.Element.prototype.scrollIntoView = function () { _scrolledTo = this.id; };
global.__getScrolled = () => _scrolledTo;
global.__setScrolled = (v) => { _scrolledTo = v; };

global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { if (typeof fn === 'function') fn(); return 0; };
global.setTimeout = win.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
global.escapeHtml = win.escapeHtml = (s) => String(s == null ? '' : s);
win.IntersectionObserver = global.IntersectionObserver = class {
  constructor(cb) { this.cb = cb; } observe() {} unobserve() {} disconnect() {}
};
win.renderMessage = global.renderMessage = (msg, i) =>
  '<div class="message" id="msg-' + i + '" data-msg-id="' + (msg && msg._msgId) + '">m' + i + '</div>';
win.activeStreams = global.activeStreams = new Map();
win._getChatContainer = global._getChatContainer = () => container;
win.chatInnerInsert = global.chatInnerInsert = undefined;

const N = 300;
function mkConv(id, prefix, tag) {
  const c = { id: id, messages: [] };
  for (let i = 0; i < N; i++) {
    c.messages.push({ role: i % 2 ? 'assistant' : 'user', content: tag + ' msg ' + i, _msgId: prefix + i });
  }
  return c;
}
const convA = mkConv('cA', 'a', 'AAA');
// convB is deliberately fingerprint-IDENTICAL to convA in every MESSAGE-derived
// term: same length, same user count, same per-turn text at every index. It
// differs ONLY by conversation id (and the per-message _msgId). That isolates
// conversation IDENTITY as the sole input that can force a rebuild, which is
// what NC-1 needs in order to actually bite. Real-world shape: the same prompt
// asked twice, a duplicated/templated conversation, or a branch copy.
const convB = mkConv('cB', 'b', 'AAA');
win.conversations = global.conversations = [convA, convB];
win.activeConvId = global.activeConvId = 'cA';
global.__out = [];

let SR = fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'streaming_render.js'), 'utf8');
let TN = fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'turn_nav.js'), 'utf8');
let applied = true;
if (NC === 'nc_navfp') {
  // NC-1: strip conversation identity out of the turn-nav fingerprint seed.
  const before = TN;
  TN = TN.replace('let _fpSeed = conv.id + "|"', 'let _fpSeed = "" + "|"');
  applied = TN !== before;
} else if (NC === 'nc_below') {
  // NC-2: disable the "evicted below the window" branch in scrollToTurn.
  const before = TN;
  TN = TN.replace(
    'if (Number.isFinite(_lazyRenderedTo) && idx >= _lazyRenderedTo) {',
    'if (false) {');
  applied = TN !== before;
}
if (!applied) { console.log('FAIL nc_pattern_applied ' + NC); process.exit(0); }
console.log('PASS nc_pattern_applied');

const DRIVER = __DRIVER__;
eval(SR + '\n' + TN + '\n' + DRIVER);
"""


# ══════════════════════════════════════════════════════════════════════════
# Harness 2 — the conversation-level action gate through the REAL surgical
# renderChat path (chat_render.js), asserting the delete affordance repaints.
# ══════════════════════════════════════════════════════════════════════════

_GATE_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NC = process.argv[3] || '';
const JS = path.join(ROOT, 'static', 'js');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
global.setTimeout = win.setTimeout = (fn) => 0;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { if (typeof fn === 'function') fn(); return 0; };
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };

const out = [];
function check(name, cond, extra) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? (' ' + extra) : ''));
}

const _conv = { id: 'c1', messages: [], activeTaskId: null };
win.activeStreams = global.activeStreams = new Map();
win.conversations = global.conversations = [_conv];
win.activeConvId = global.activeConvId = 'c1';
win.getActiveConv = global.getActiveConv = () => _conv;
win.t = global.t = (k) => k;
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
const _noop = () => '';
for (const n of ['renderToolRoundsHTML','renderSegmentTimelineHTML','renderErrorEnvelope',
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar','renderBranchZone',
  'renderTurnCtxNote','renderPreferenceLearnedHtml','renderFinishInfo','_buildSwarmInboxChipsHTML',
  '_injectAnchoredBranches','_stampFreshness','calcCostCny','renderTranslateIndicator',
  '_welcomePillsHtml','_forceScrollToBottom','_destroyLazyObserver','_ensureLazyObserver',
  'assertChatInnerOrder','buildTurnNav','_apSummariesFp']) {
  if (typeof win[n] === 'undefined') { win[n] = global[n] = _noop; }
}
win._apSummariesFp = global._apSummariesFp = () => '0';
win.isNearBottom = global.isNearBottom = () => true;
win._prefetchConvCosts = global._prefetchConvCosts = () => Promise.resolve(false);
win._prefetchConvFileChanges = global._prefetchConvFileChanges = () => Promise.resolve(false);
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<i>u</i>';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<i>w</i>';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<i>c</i>';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<i>p</i>';
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;
win._lazyConvId = global._lazyConvId = null;
win._lazyRenderedFrom = global._lazyRenderedFrom = Infinity;
win._lazyRenderedTo = global._lazyRenderedTo = Infinity;
win._editingMsgIdx = global._editingMsgIdx = null;
win._activeBranch = global._activeBranch = null;
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';
win._openScrollConvId = global._openScrollConvId = null;
win.chatInnerInsert = global.chatInnerInsert = undefined;
win.chatInnerHeadAnchor = global.chatInnerHeadAnchor = undefined;

(0, eval)(fs.readFileSync(path.join(JS, 'core', 'escape_html.js'), 'utf8'));
(0, eval)(fs.readFileSync(path.join(JS, 'core', 'safe_html.js'), 'utf8'));
(0, eval)(fs.readFileSync(path.join(JS, 'core', 'translation_model.js'), 'utf8'));
(0, eval)(fs.readFileSync(path.join(JS, 'ui', 'translation_indicator.js'), 'utf8'));
(0, eval)(fs.readFileSync(path.join(JS, 'core', 'turn_settlement.js'), 'utf8'));
(0, eval)(fs.readFileSync(path.join(JS, 'core.js'), 'utf8')
          .match(/function _convRenderFingerprint[\s\S]*?\n\}/)[0]);

let CR = fs.readFileSync(path.join(JS, 'ui', 'chat_render.js'), 'utf8');
if (NC === 'nc_gate') {
  // NC-3: drop the conversation action-gate token from the message fingerprint.
  const before = CR;
  CR = CR.replace('_convActionGate() + ":" +', '"" + ":" +');
  if (CR === before) { console.log('FAIL nc_pattern_applied nc_gate'); process.exit(0); }
}
console.log('PASS nc_pattern_applied');
// chat_render.js reads `_explicitBottomLatch`, `let`-declared in
// ui/streaming_render.js. A `let` does not escape its own eval, so the two are
// concatenated into ONE eval — exactly how the bundler ships them.
(0, eval)(fs.readFileSync(path.join(JS, 'ui', 'streaming_render.js'), 'utf8') + '\n;\n' + CR);

const inner = document.getElementById('chatInner');
const u = { role: 'user', _msgId: 'u1', content: 'go' };
const a = { role: 'assistant', _msgId: 'a1', content: 'answer',
            finishReason: 'stop', usage: { t: 1 } };
_conv.messages = [u, a];
const nDelete = () => inner.querySelectorAll('.msg-delete-btn').length;

/* Paint while a task is IN FLIGHT — the gate is closed, so no delete button.
 * (This is the honest starting state for "a reply just finished".) */
_conv.activeTaskId = 'task-1';
inner.innerHTML = renderMessage(u, 0) + renderMessage(a, 1);
check('busy_has_no_delete', nDelete() === 0, 'n=' + nDelete());

/* The task settles. finishStream clears activeTaskId and the settle path
 * repaints through the SURGICAL renderChat(conv,false). The delete affordance
 * must come back — otherwise the user cannot delete a finished turn at all. */
_conv.activeTaskId = null;
_lastRenderedFingerprint = '';
renderChat(_conv, false);
check('idle_surgical_restores_delete', nDelete() === 2, 'n=' + nDelete());

/* A new task starts. The surgical repaint must REMOVE the buttons — leaving
 * them is the "click does nothing" bug, because deleteTurn() re-reads the live
 * gate and returns immediately.
 * Seeded from a FULL render so this direction is measured INDEPENDENTLY of the
 * previous assertion: if the restore above silently did nothing, a "removal"
 * check starting from 0 buttons would pass trivially and mask the regression. */
_conv.activeTaskId = null;
renderChat(_conv, true);
check('full_render_idle_has_delete', nDelete() === 2, 'n=' + nDelete());
_conv.activeTaskId = 'task-2';
_lastRenderedFingerprint = '';
renderChat(_conv, false);
check('busy_surgical_removes_delete', nDelete() === 0, 'n=' + nDelete());

/* Same for a live STREAM (the other half of the gate), also full-render seeded. */
_conv.activeTaskId = null;
renderChat(_conv, true);
check('idle_again_restores_delete', nDelete() === 2, 'n=' + nDelete());
activeStreams.set('c1', { taskId: 'x' });
_lastRenderedFingerprint = '';
renderChat(_conv, false);
check('stream_surgical_removes_delete', nDelete() === 0, 'n=' + nDelete());
activeStreams.delete('c1');

/* Guard the perf intent: with the gate UNCHANGED, an untouched conversation
 * must still be a no-op (the fingerprint must not churn every render). */
_lastRenderedFingerprint = '';
renderChat(_conv, false);
const mfpBefore = inner.querySelector('#msg-1').getAttribute('data-mfp');
_lastRenderedFingerprint = '';
renderChat(_conv, false);
check('stable_gate_fingerprint_is_stable',
      inner.querySelector('#msg-1').getAttribute('data-mfp') === mfpBefore);

console.log(out.join('\n'));
process.exit(0);
"""


def _run(harness_src: str, name: str, nc: str = '') -> str:
    harness = os.path.join(HERE, f'_turnnav_{name}_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(harness_src)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT, nc],
            capture_output=True, text=True, timeout=90,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


def _nav(nc: str = '') -> str:
    return _run(_NAV_HARNESS.replace('__DRIVER__', json.dumps(_NAV_DRIVER)), 'nav', nc)


def _gate(nc: str = '') -> str:
    return _run(_GATE_HARNESS, 'gate', nc)


def _verdicts(output: str) -> dict:
    return {ln[5:].split(' ')[0]: ln[:4].strip()
            for ln in output.splitlines() if ln[:4].strip() in ('PASS', 'FAIL')}


# ── Defect 1 + 2: the sidebar turn nav ────────────────────────────────────

@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_turn_nav_rebuilds_and_navigates():
    """The nav rebuilds on a conversation switch and on a mid-history edit, and
    every dot navigates — including one whose message the bounded render window
    evicted BELOW the rendered span."""
    output = _nav()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'turn-nav failures:\n' + output
    for needed in ('dots_built', 'switch_rebuilds_nav', 'midhistory_edit_rebuilds_nav',
                   'unchanged_conv_is_noop', 'window_is_capped', 'target_in_the_hole',
                   'below_window_navigates', 'above_window_navigates',
                   'in_window_navigates'):
        assert f'PASS {needed}' in output, f'missing PASS {needed} in:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_nav_fingerprint_without_conv_identity_goes_stale():
    """NC-1: strip conversation identity from the turn-nav fingerprint seed →
    switching conversations must leave the PREVIOUS conversation's dots up."""
    v = _verdicts(_nav('nc_navfp'))
    assert v.get('nc_pattern_applied') == 'PASS', v
    assert v.get('switch_rebuilds_nav') == 'FAIL', (
        'Removing conv identity did NOT reintroduce stale dots — the identity '
        f'term is not load-bearing: {v}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_scroll_to_turn_without_below_window_branch_is_a_dead_click():
    """NC-2: disable the evicted-below branch → a dot below the render window
    must go back to being a silent no-op."""
    v = _verdicts(_nav('nc_below'))
    assert v.get('nc_pattern_applied') == 'PASS', v
    assert v.get('below_window_navigates') == 'FAIL', (
        'Disabling the below-window branch did NOT break navigation — the '
        f'branch is not load-bearing: {v}')
    # The other directions must still work, proving the NC is surgical.
    assert v.get('above_window_navigates') == 'PASS', v
    assert v.get('in_window_navigates') == 'PASS', v


# ── Defect 3: the conversation-level action gate ──────────────────────────

@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_delete_affordance_tracks_conversation_busy_state():
    """The surgical repaint must add the Delete button when a turn settles and
    remove it when a task/stream takes over — never leave a button whose
    handler will refuse to act."""
    output = _gate()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'action-gate failures:\n' + output
    for needed in ('busy_has_no_delete', 'idle_surgical_restores_delete',
                   'full_render_idle_has_delete', 'busy_surgical_removes_delete',
                   'idle_again_restores_delete', 'stream_surgical_removes_delete',
                   'stable_gate_fingerprint_is_stable'):
        assert f'PASS {needed}' in output, f'missing PASS {needed} in:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_message_fingerprint_without_action_gate_keeps_stale_delete():
    """NC-3: drop the action-gate token from _msgFingerprint → the surgical
    repaint must go back to skipping the row, stranding a stale button."""
    v = _verdicts(_gate('nc_gate'))
    assert v.get('nc_pattern_applied') == 'PASS', v
    assert v.get('idle_surgical_restores_delete') == 'FAIL', (
        'Dropping the action-gate token did NOT strand the stale affordance — '
        f'the token is not load-bearing: {v}')
    assert v.get('busy_surgical_removes_delete') == 'FAIL', v
