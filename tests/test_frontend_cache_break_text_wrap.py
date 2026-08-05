"""Guard for the cost-popover cache-break line layout (2026-07-24 overflow bug).

The `.cp-round-break` line is `display:flex` with three children: the warning
SVG (`flex:none`), the state badge pill (`flex:none`), and the long reason
prose. The prose used to be a BARE TEXT NODE — an anonymous flex item whose
CJK min-content width is ONE character. A long badge (e.g. the 'upstream'
verdict pill) therefore squeezed the prose into a 1-character-per-line
vertical column — the "completely overflowing description" the owner reported.

Fix being guarded (two halves, both required):
  1. static/js/ui/finish_info.js wraps the reason in
     `<span class="cp-break-text">…</span>` (a real flex item, not anonymous).
  2. static/styles.css gives `.cp-round-break` `flex-wrap:wrap` and
     `.cp-break-text` `flex:1 1 12em;min-width:0` — when the badge leaves
     < 12em on the first line, the prose drops to a full-width second line.

Negative controls (proven to bite manually):
  • NC-JS:  reverting to the bare text node (no span) fails check 1/2/3.
  • NC-CSS: removing `flex-wrap:wrap` from .cp-round-break fails check 4;
            deleting the .cp-break-text rule fails check 5.

Skips the node half cleanly when node isn't installed; the CSS half is pure
Python and always runs.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
# Epic-E sub-8 (2026-08-01): _buildCostPopover moved to
# ui/finish_info_rich.js (deferred); the phrase family it calls
# (_cacheBreakReason/_translateCacheCause/…) STAYS in ui/finish_info.js.
# Eval BOTH in one script scope — mirrors production's shared global
# lexical environment across the two bundles.
JS_PATH = os.path.join(ROOT, 'static', 'js', 'ui', 'finish_info.js')
JS_RICH_PATH = os.path.join(ROOT, 'static', 'js', 'ui', 'finish_info_rich.js')
CSS_PATH = os.path.join(ROOT, 'static', 'styles.css')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ── JS half: drive the REAL _buildCostPopover and inspect the break line ──
_RENDER_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8')
  + '\n;\n' + fs.readFileSync(process.argv[3], 'utf8');

let _i18nLang = 'zh';
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
global.t = (k, o) => {
  o = o || {};
  if (k === 'finishInfo.cacheBreakLabel') return 'MISS: ' + (o.reason || '');
  if (k === 'finishInfo.cbState.upstream') return 'UPSTREAM-BADGE';
  if (k && k.indexOf('{') === -1 && o && Object.keys(o).length) {
    let s = k; for (const kk in o) s = s.replace('{'+kk+'}', o[kk]); return s;
  }
  return k;
};
global.formatCny = (n) => '¥' + Number(n||0).toFixed(4);
global.calcCostCny = () => 0.01;
global.calcCost = () => 0.01;
global.window = {}; global.document = {};

eval(src);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// The screenshot scenario: a byte-identical wire → 'upstream' verdict with the
// LONG badge, which is what squeezed the prose to one char per line.
const _round = (n, cb) => ({
  round: n,
  usage: { prompt_tokens: 100, completion_tokens: 10,
           cache_creation_input_tokens: 50000, cache_read_input_tokens: 40000,
           _dispatch: { model: 'm' } },
  cacheBreak: cb,
});
const html = _buildCostPopover({
  costInfo: { inputTokens: 100, totalInputTokens: 100, outputTokens: 10,
              costCny: 0.01, cacheReadTokens: 40000, cacheWriteTokens: 50000 },
  rounds: [_round(1, undefined), _round(2, { server_side:
    'prefix not read back though the wire bytes were byte-identical to the previous round — so this round is NOT a client-side prefix change.' })],
  numRounds: 2, u: {}, inp: 100, out: 10, cw: 50000, cr: 40000, thk: 0,
  mid: 'm', pid: '', taskId: 't1', toolRounds: [],
});

check('break_line_present', html.indexOf('cp-round-break') !== -1);
// 1. The reason prose lives inside a .cp-break-text span…
check('reason_wrapped_in_span',
  html.indexOf('<span class="cp-break-text">MISS: ') !== -1);
// 2. …and that span is the LAST child of the break line — no bare text node
//    sibling after the badge (the anonymous-flex-item trap).
check('no_bare_text_node_in_break_line',
  /<span class="cp-break-text">[^<]*<\/span><\/div>/.test(html));

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_break_reason_is_wrapped_flex_item_not_bare_text():
    harness = os.path.join(HERE, '_cache_break_wrap_harness.js')
    with open(harness, 'w') as f:
        f.write(_RENDER_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, JS_PATH, JS_RICH_PATH],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    output = proc.stdout.strip()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'break-line markup guard failures:\n' + output
    assert output.count('PASS') >= 3, f'expected >=3 PASS, got:\n{output}'
    print(output)


# ── CSS half: the flex-wrap + basis contract that keeps the prose readable ──
def _css_rules() -> list:
    """Return [(selector, declarations)] leaf rule blocks with comments
    stripped (a comment containing the words under test must not false-trip
    the guard)."""
    with open(CSS_PATH, encoding='utf-8') as f:
        css = f.read()
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    return re.findall(r'([^{}]+)\{([^{}]*)\}', css)


def _find_rule(rules, selector_suffix: str):
    """First rule whose selector list contains a selector ending exactly with
    selector_suffix (token-boundary match, so `.cp-round-break-foo` or the
    `.cp-round-break.cp-break-upstream` state overrides do NOT collide)."""
    for sel, decls in rules:
        for part in sel.split(','):
            part = part.strip()
            if part == selector_suffix or part.endswith(' ' + selector_suffix):
                return decls
    return None


def test_round_break_wraps_and_break_text_has_flex_basis():
    rules = _css_rules()

    # .cp-round-break must allow wrapping — without flex-wrap a long badge
    # pins the prose to whatever sliver remains on line 1.
    decls = _find_rule(rules, '.cp-round-break')
    assert decls is not None, 'no .cp-round-break rule found in styles.css'
    assert re.search(r'flex-wrap\s*:\s*wrap', decls), (
        '.cp-round-break lost flex-wrap:wrap — long badges will squeeze the '
        'reason prose into a 1-char-per-line column again')

    # .cp-break-text must be a growable flex item with a sane basis and
    # min-width:0 (without min-width:0 a flex item refuses to shrink below
    # its content size and overflows the popover instead of wrapping).
    decls = _find_rule(rules, '.cp-break-text')
    assert decls is not None, 'no .cp-break-text rule found in styles.css'
    assert re.search(r'flex\s*:\s*1\s+1\s+\d', decls), (
        '.cp-break-text lost its `flex:1 1 <basis>` shorthand')
    assert re.search(r'min-width\s*:\s*0', decls), (
        '.cp-break-text lost min-width:0 — it will overflow instead of wrap')
