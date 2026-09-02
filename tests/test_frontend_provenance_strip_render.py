"""jsdom regression for the unified turn-provenance strip rendering.

WHY
---
The expanded provenance strip (`renderTurnProvenanceHtml` in
static/js/ui/tool_rounds.js) showed three rendering defects (user screenshot):

  1. Memory descriptions were clipped server-side to 120 chars, so the
     expanded panel could never show the full text. (Backend fix in
     lib/memory/prefetch/ — the frontend now just renders whatever it gets,
     so this harness asserts the strip emits the FULL description it is given.)
  2. Preference bullets carry lightweight markdown (`**bold**`, `*italic*`,
     `` `code` ``). They were piped through bare escapeHtml(), so the literal
     asterisks/backticks showed ("markdown 渲染, 字体不好看"). `_tpInlineMd`
     now renders the three inline emphasis spans (XSS-safe: escape first).
  3. Related-conversation titles were clipped with an ellipsis; they must wrap.

This harness loads the REAL shipped tool_rounds.js under jsdom and asserts the
emitted HTML for each segment. Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

// ── Minimal globals tool_rounds.js touches at load / inside the builders ──
win.escapeHtml = global.escapeHtml = (t) =>
  String(t == null ? '' : t)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
win.t = global.t = (k, vars) => {
  // Echo a usable string; the builders only need .replace('{n}', n) to work.
  if (vars && typeof vars.n !== 'undefined') return String(vars.n) + ' ' + k;
  return k;
};
win.Icon = global.Icon = () => '<svg></svg>';
win.projectState = global.projectState = { active: false };

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/tool_rounds.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof renderTurnProvenanceHtml !== 'function') {
  console.log('FAIL fn_exposed renderTurnProvenanceHtml missing');
  process.exit(0);
}
check('fn_exposed', true);

// ════════════════════════════════════════════════════════════════════
// Case 1 — memory description renders IN FULL and as inline markdown.
// ════════════════════════════════════════════════════════════════════
{
  const longDesc = 'Centralized tool-arg repair (lib/tool_input_repair.py): '
    + '6 value-repair patterns + **param-KEY alias** layer (`file_path`->path) '
    + 'that fixes a whole class of cross-harness tool calls without rejection.';
  const html = renderTurnProvenanceHtml({
    _memoryPrefetch: {
      phase: 'done', selected: 1,
      memories: [{ name: 'tool-input-repair', scope: 'project', description: longDesc }],
    },
  });
  // Full description present (NOT clipped at 120 chars).
  check('c1_full_desc', html.indexOf('without rejection.') !== -1);
  // Markdown rendered: **param-KEY alias** -> <strong>, `file_path` -> <code>.
  check('c1_bold_rendered', html.indexOf('<strong>param-KEY alias</strong>') !== -1);
  check('c1_code_rendered', html.indexOf('<code>file_path</code>') !== -1);
  // Raw markdown tokens must NOT survive as literal text.
  check('c1_no_literal_stars', html.indexOf('**param-KEY alias**') === -1);
}

// ════════════════════════════════════════════════════════════════════
// Case 2 — preference bullets render inline markdown, not literal asterisks.
// ════════════════════════════════════════════════════════════════════
{
  const html = renderTurnProvenanceHtml({
    _preferencesApplied: {
      chars: 200,
      items: ['**Language:** Output in Chinese; profile file in English.',
              'Use `listings` with *tofucode* for traces.'],
    },
  });
  check('c2_pref_list', html.indexOf('class="mp-mem-list pa-list"') !== -1);
  check('c2_bold_rendered', html.indexOf('<strong>Language:</strong>') !== -1);
  check('c2_code_rendered', html.indexOf('<code>listings</code>') !== -1);
  check('c2_italic_rendered', html.indexOf('<em>tofucode</em>') !== -1);
  check('c2_no_literal_stars', html.indexOf('**Language:**') === -1);
}

// ════════════════════════════════════════════════════════════════════
// Case 3 — XSS: a hostile preference/desc is escaped before emphasis.
// ════════════════════════════════════════════════════════════════════
{
  const html = renderTurnProvenanceHtml({
    _preferencesApplied: { chars: 1, items: ['<script>alert(1)</script> **x**'] },
  });
  check('c3_script_escaped', html.indexOf('<script>alert(1)</script>') === -1
        && html.indexOf('&lt;script&gt;') !== -1);
  check('c3_still_renders_bold', html.indexOf('<strong>x</strong>') !== -1);
}

// ════════════════════════════════════════════════════════════════════
// Case 4 — related-conversation title kept verbatim (no JS-side ellipsis);
//          full long CJK title survives into the markup so CSS can wrap it.
// ════════════════════════════════════════════════════════════════════
{
  const longTitle = '阅读报告模式如果我点击重新生成再强制刷新回来之后又只呈现旧报告了，'
    + '但是后台搜索根本没停相当于出现了一个孤立任务';
  const html = renderTurnProvenanceHtml({
    _relatedConversations: {
      count: 1,
      items: [{ id: 'abc123', title: longTitle, summary: 'a long summary that must wrap fully and never get clipped' }],
    },
  });
  check('c4_full_title', html.indexOf(longTitle) !== -1);
  check('c4_no_ellipsis_token', html.indexOf(longTitle + '…') === -1);
  check('c4_summary_present', html.indexOf('never get clipped') !== -1);
}

// ════════════════════════════════════════════════════════════════════
// Case 5 — AUTO-APPLIED learned preferences fold into the quiet strip
//          (a segment), NOT the prominent box; the box renders ONLY the
//          actionable `pending` rows.
// ════════════════════════════════════════════════════════════════════
{
  const learned = [
    { kind: 'added', summary: 'Reply in **Chinese** by default', pending: false },
    { kind: 'reinforced', summary: 'Use `pytest` for tests', pending: false },
  ];
  const stripHtml = renderTurnProvenanceHtml({ _preferencesLearned: learned });
  // Informational learned prefs appear as a folded segment in the strip.
  check('c5_strip_has_learned_seg', stripHtml.indexOf('tp-seg-prefs-learned') !== -1);
  check('c5_strip_bold_rendered', stripHtml.indexOf('<strong>Chinese</strong>') !== -1);
  check('c5_strip_no_literal_stars', stripHtml.indexOf('**Chinese**') === -1);

  // The prominent box renders NOTHING for purely informational learned prefs.
  const boxHtml = renderPreferenceLearnedHtml(learned);
  check('c5_box_empty_for_informational', boxHtml === '');

  // A pending (actionable) row DOES render in the box, with Confirm/Dismiss.
  const withPending = learned.concat([{ kind: 'pending', summary: 'X', pending: true, id: 'p1' }]);
  const boxHtml2 = renderPreferenceLearnedHtml(withPending);
  check('c5_box_renders_pending', boxHtml2.indexOf('pl-pending') !== -1
        && boxHtml2.indexOf('pl-confirm') !== -1);
  // The informational rows are NOT duplicated into the box.
  check('c5_box_no_informational', boxHtml2.indexOf('pl-reinforced') === -1);
}

console.log(out.join('\n'));
// tool_rounds.js:3730 installs a load-time 1Hz _cmdTimerTicker that keeps the
// node event loop alive — force a clean exit after the synchronous checks.
process.exit(0);
"""


def _run():
    harness = os.path.join(HERE, '_provenance_strip_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),   # argv[2]
             ROOT,                                            # argv[3]
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
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'provenance-strip failures:\n' + output
    assert output.count('PASS') >= 14, f'expected >=14 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_provenance_strip_render():
    _run()
