"""tests/test_frontend_action_index_decoupled.py — RENDER_CONTRACT Phase 2b / L10.

WHY
---
`renderMessage` baked the array index into every action-button `onclick`
(`deleteTurn(3)` / `copyMessage(3)` / `regenerateFromUser(3)` / …). Two harms:

  • CORRECTNESS (shipped by Phase 1): the id-keyed reconcile REUSES a drifted-
    but-unchanged node (re-stamping only its `id="msg-N"` handle). The baked
    onclick string is NOT rebuilt (fingerprint unchanged), so a bubble that
    drifted from index 3 → 4 still runs `deleteTurn(3)` — Delete/Regen hits the
    WRONG turn (data loss).
  • BLOCKS Phase 2b (L10): because the rendered output DIFFERS on pure index
    drift, a rendered-output content-version (the only forgettable-field-proof
    version, needed to retire `_bgRefreshChat`) would force-rebuild every
    drifted node and undo Phase 1's node reuse.

The fix: `_msgElIndex(el)` resolves the CURRENT array index from the nearest
`.message` node's stable `data-msg-id` (positional `msg-N` legacy fallback) at
click time; every onclick becomes `fn(_msgElIndex(this))`. Output is now
index-independent.

Asserts (drives the REAL shipped renderMessage + _msgElIndex):
  A. no action onclick bakes a numeric index — they all use `_msgElIndex(this)`;
  B. `_msgElIndex(el)` resolves the live index by `data-msg-id` from
     getActiveConv() (and follows a splice), positional fallback for id-less;
  C. INDEX-INDEPENDENCE — renderMessage(msg, 3) and renderMessage(msg, 5),
     after normalising the `id="msg-N"` handle, are byte-identical (the load-
     bearing property for a future output-hash version).

NEUTER: re-bake the index (`_msgElIndex(this)` → `${idx}` in source) and prove
(A) and (C) then FAIL — i.e. the live-resolve is load-bearing.

Skips cleanly when node isn't installed.
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
TOOL_ROUNDS = os.path.join(JS_DIR, 'ui', 'tool_rounds.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
const NC = process.argv[6] || '';
global.window = global;
global.document = {
  addEventListener: function () {}, removeEventListener: function () {},
  querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
  createElement: () => ({ style: {}, classList: { add(){}, remove(){}, toggle(){} }, setAttribute(){}, appendChild(){} }),
};
const out = [];
function check(name, cond, extra) { out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? (' ' + extra) : '')); }

global.config = { segmentTimeline: true };
global.t = (k) => k;
global._fmtAbsoluteDateTime = () => '';
global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
global.renderMcpLoginHintHtml = () => '';
global.renderTurnProvenanceHtml = () => '';
global.renderFileChangesBar = () => '';
global.renderErrorEnvelope = () => '';
global.renderBranchZone = () => '';
global.renderTurnCtxNote = () => '';
global.renderPreferenceLearnedHtml = () => '';
global.activeStreams = new Set();
global._USER_AVATAR_SVG = '<img data-avatar="onigiri">';
global._buildSwarmPanelHTML = () => '<swarm/>';
global._buildSwarmInboxChipsHTML = () => '';
global._isRoundSwarm = () => false;
global._TOOL_DISPLAY = {};
global._toolPanelHeaderLabel = () => 'HDR';
global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
global.renderFinishInfo = () => '';
global._TOFU_WORKER_SVG = '<img data-avatar="worker">';
global._TOFU_PLANNER_SVG = '<img data-avatar="planner">';
global._TOFU_CRITIC_SVG = '<img data-avatar="critic">';
global.calcCostCny = () => 0;

// A committed conv where the message under test is NOT the last (so the
// Continue affordance's isLastAssistant is false → deterministic) and not
// streaming. renderMessage's getActiveConv reads this.
let CONV = null;
global.getActiveConv = () => CONV;

function loadAll(chatSrc) {
  (0, eval)(fs.readFileSync(process.argv[2], 'utf8'));  // escape_html.js
  (0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // safe_html.js
  (0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // tool_rounds.js
  (0, eval)(fs.readFileSync(process.argv[2].replace('escape_html.js', 'translation_model.js'), 'utf8'));
  (0, eval)(fs.readFileSync(process.argv[2].replace('core/escape_html.js', 'ui/translation_indicator.js'), 'utf8'));
  (0, eval)(chatSrc);
}

let CHAT = fs.readFileSync(process.argv[5], 'utf8');
if (NC === 'bake') {
  // NEUTER: re-bake the array index back into the action onclicks. The token
  // `_msgElIndex(this)` is the Phase-2b sentinel; absent → not landed.
  const before = CHAT;
  CHAT = CHAT.split('_msgElIndex(this)').join('${idx}');
  if (CHAT === before) { console.log('FAIL neuter_not_applied (Phase-2b _msgElIndex sentinel absent)'); process.exit(0); }
}
loadAll(CHAT);

if (typeof renderMessage !== 'function') { console.log('FAIL fn_exposed renderMessage missing'); console.log(out.join('\n')); process.exit(0); }
check('fn_exposed', true);

// An assistant message with content + thinking (exercises the action bar AND
// the _toggleThinking onclick). 10-msg conv so idx 3/5 are both mid-history.
function mkConv() {
  const msgs = [];
  for (let i = 0; i < 10; i++) msgs.push({ role: 'assistant', _msgId: 'm' + i, content: 'body ' + i });
  return { id: 'c1', messages: msgs };
}
function mkAsst() { return { role: 'assistant', _msgId: 'mX', content: 'hello', thinking: 'reasoning here' }; }

CONV = mkConv();

// ── A) No action onclick bakes a numeric index; all use _msgElIndex(this). ──
{
  const html = renderMessage(mkAsst(), 3);
  // Baked-index calls that MUST be gone from the action bar / body affordances.
  const bakedPatterns = [
    /deleteTurn\(\d+\)/, /copyMessage\(\d+\)/, /regenerateFromUser\(\d+\)/,
    /translateMessage\(\d+\)/, /startEditMessage\(\d+\)/, /promptNewBranch\(\d+\)/,
    /exportMessageWithPreview\(\d+\)/, /_toggleThinking\(this,\d+\)/,
  ];
  const stillBaked = bakedPatterns.filter(re => re.test(html)).map(re => re.source);
  check('A_no_baked_index_in_actions', stillBaked.length === 0, 'baked=' + stillBaked.join(','));
  check('A_uses_msgElIndex', html.indexOf('_msgElIndex(this)') !== -1);
  // Spot-check specific handlers route through the resolver.
  check('A_delete_uses_resolver', html.indexOf('deleteTurn(_msgElIndex(this))') !== -1);
  check('A_thinking_uses_resolver', html.indexOf('_toggleThinking(this,_msgElIndex(this))') !== -1);
}

// ── B) _msgElIndex resolves the live index by data-msg-id (+ splice + fallback). ──
{
  const okFn = typeof _msgElIndex === 'function'
    || (typeof window !== 'undefined' && typeof window._msgElIndex === 'function');
  check('B_resolver_exposed', okFn);
  const fn = (typeof _msgElIndex === 'function') ? _msgElIndex : window._msgElIndex;
  // Fake button whose .closest('.message') carries data-msg-id="m7".
  const mkEl = (mid, posId) => ({
    closest: (sel) => (sel === '.message'
      ? { getAttribute: (a) => (a === 'data-msg-id' ? mid : null), id: posId || '' }
      : null),
  });
  CONV = mkConv();
  check('B_resolves_by_msgid', fn(mkEl('m7')) === 7, 'got=' + fn(mkEl('m7')));
  // Splice a new message at the front → m7 shifts to index 8; resolver follows.
  CONV.messages.splice(0, 0, { role: 'user', _msgId: 'mNEW', content: 'q' });
  check('B_follows_splice', fn(mkEl('m7')) === 8, 'got=' + fn(mkEl('m7')));
  // Unknown id → -1 (handlers guard on conv.messages[-1] === undefined).
  check('B_unknown_is_neg1', fn(mkEl('nope')) === -1, 'got=' + fn(mkEl('nope')));
  // Legacy id-less node → positional fallback from id="msg-N".
  check('B_positional_fallback', fn(mkEl(null, 'msg-4')) === 4, 'got=' + fn(mkEl(null, 'msg-4')));
  CONV = mkConv();
}

// ── C) INDEX-INDEPENDENCE: renderMessage(msg,3) === renderMessage(msg,5) after
//       normalising the positional id handle. This is the load-bearing property
//       for a future output-hash version (drift no longer changes output). ──
{
  const norm = (h) => h.replace(/id="msg-\d+"/g, 'id="msg-X"');
  const a = norm(renderMessage(mkAsst(), 3));
  const b = norm(renderMessage(mkAsst(), 5));
  check('C_output_index_independent', a === b,
    a === b ? '' : ('len ' + a.length + ' vs ' + b.length));
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_action_index_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ESCAPE_HTML, SAFE_HTML, TOOL_ROUNDS, CHAT_RENDER, nc],
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
    return {ln[5:].split(' ')[0]: ln[:4].strip()
            for ln in output.splitlines() if ln[:4].strip() in ('PASS', 'FAIL')}


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_action_onclick_decoupled_from_index():
    """Action-button onclicks resolve the live index via _msgElIndex(this), so
    renderMessage output is index-independent (L10 fix)."""
    src = open(CHAT_RENDER, encoding='utf-8').read()
    assert 'deleteTurn(_msgElIndex(this))' in src, \
        'Phase-2b action-index decoupling missing from chat_render.js — test stale'
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'action-index-decoupled failures:\n' + output
    lines = _lines(output)
    for key in ('A_no_baked_index_in_actions', 'A_uses_msgElIndex',
                'A_delete_uses_resolver', 'A_thinking_uses_resolver',
                'B_resolver_exposed', 'B_resolves_by_msgid', 'B_follows_splice',
                'B_unknown_is_neg1', 'B_positional_fallback',
                'C_output_index_independent'):
        assert lines.get(key) == 'PASS', f'{key} not PASS:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_baked_index_breaks_independence():
    """NEUTER: re-bake the array index into the onclicks. Output then DIFFERS on
    index drift and baked-index calls reappear — proving the live-resolve is
    load-bearing for L10."""
    output = _run('bake')
    assert 'FAIL neuter_not_applied' not in output, (
        'the Phase-2b `_msgElIndex(this)` sentinel is absent — decoupling has '
        f'not landed yet:\n{output}')
    lines = _lines(output)
    assert lines.get('C_output_index_independent') == 'FAIL', (
        'baked index did NOT break index-independence — the resolver is not '
        f'load-bearing:\n{output}')
    assert lines.get('A_no_baked_index_in_actions') == 'FAIL', (
        f'baked index did NOT reappear in the action onclicks:\n{output}')
