"""tests/test_frontend_toolset_diff_collapse.py — regression for the
tool-schema-latch banner NOT overflowing the composer when a big MCP diff
lands.

WHY
---
MCP auto-connect runs on a background thread after boot, so a server's tools
arrive seconds LATE. When they land the global tool-schema latch is cleared,
so the next round's `done` SSE reports every newly-connected MCP tool in
`toolsetDiff.added`. `_renderToolsetDiff` used to emit ONE chip per tool name
→ `mcp__github__*` ×28 + `mcp__hope__*` ×40 flooded and overflowed the input
box (see the user report screenshot).

The fix aggregates MCP tools into ONE entry per server carrying a count
(`MCP github ×28`) and caps the visible chips at `_MAX_TOOL_CHIPS`, collapsing
the remainder into a single "…and N more" chip. This harness loads the REAL
shipped toolset-apply.js under bare node and asserts that behaviour.

NEUTER (on a mutated copy; shipped file untouched): stripping the MCP-grouping
branch so every tool renders individually makes the chip-count assertion FAIL
— proving the aggregation is load-bearing.
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


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

global.escapeHtml = (s) => String(s);
global.t = (k) => {
  if (k === 'toolset.moreChips') return '…and %n more';
  return k;
};

// Minimal DOM: one text element the renderer writes into.
function El() {
  return { _html: '', textContent: '',
    set innerHTML(v){ this._html = v; }, get innerHTML(){ return this._html; } };
}
const textEl = El();
global.document = { getElementById: (id) => (id === 'toolsetApplyBannerText' ? textEl : null) };

const SRC = fs.readFileSync(process.argv[2], 'utf8');
function loadModule(src){ (0, eval)(src); }
loadModule(SRC);

if (typeof _collapseToolNames !== 'function') {
  console.log('FAIL fn_exposed'); console.log(out.join('\n')); process.exit(0);
}
check('fn_exposed', true);

// ── 1. _collapseToolNames aggregates MCP tools per server, keeps plain ones ──
const gh = []; for (let i=0;i<28;i++) gh.push('mcp__github__t'+i);
const hope = []; for (let i=0;i<40;i++) hope.push('mcp__hope__t'+i);
const names = ['spawn_agents'].concat(gh).concat(hope).concat(['mcp__github-batch__batch_commit']);
const collapsed = _collapseToolNames(names);
const ghEntry = collapsed.find((e) => e.label === 'MCP github');
const hopeEntry = collapsed.find((e) => e.label === 'MCP hope');
const batchEntry = collapsed.find((e) => e.label === 'MCP github-batch');
const plainEntry = collapsed.find((e) => e.label === 'spawn_agents');
check('github_grouped', !!ghEntry && ghEntry.count === 28);
check('hope_grouped', !!hopeEntry && hopeEntry.count === 40);
// server names with a single dash stay one group (split on the 2nd __)
check('dashed_server_grouped', !!batchEntry && batchEntry.count === 1);
check('plain_tool_kept', !!plainEntry && plainEntry.count === 1);
// 28+40 MCP tools + 1 dashed + 1 plain collapse to just 4 entries.
check('collapsed_to_4', collapsed.length === 4);

// ── 2. _renderToolsetDiff caps chips and never emits 68 raw tool chips ──
// added = the 68 mcp + spawn_agents + batch = 70 names; removed = none.
window.showToolsetApplyBanner({ added: names, removed: [] });
const html = textEl.innerHTML;
const chipCount = (html.match(/toolset-diff-chip /g) || []).length;
// Must be tiny (4 grouped entries here), NOT ~70.
check('few_chips', chipCount <= 6);
check('shows_count_x28', html.indexOf('×28') >= 0);
check('shows_count_x40', html.indexOf('×40') >= 0);
check('no_raw_mcp_name', html.indexOf('mcp__github__t0') < 0);

// ── 3. Overflow cap: many DISTINCT plain tools collapse into "…and N more" ──
const many = []; for (let i=0;i<40;i++) many.push('tool_'+i);
window.showToolsetApplyBanner({ added: many, removed: [] });
const html2 = textEl.innerHTML;
const chipCount2 = (html2.match(/toolset-diff-chip /g) || []).length;
check('cap_enforced', chipCount2 <= 25);   // _MAX_TOOL_CHIPS(24) + 1 "more"
check('more_chip_present', html2.indexOf('toolset-diff-chip more') >= 0);
check('more_count_text', html2.indexOf('more') >= 0);

// ── 4. NEUTER: remove the MCP-grouping branch → 68 tools render individually ──
{
  const NEEDLE = "if (name.indexOf('mcp__') === 0) {";
  const neutered = SRC.replace(NEEDLE, "if (false) {");
  check('neuter_applied', neutered !== SRC);
  loadModule(neutered);
  const c2 = _collapseToolNames(names);
  // Without grouping, all 70 names become individual entries (>> 4).
  check('neuter_no_grouping', c2.length >= 68);
}

console.log(out.join('\n'));
process.exit(0);
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_toolset_diff_collapse():
    harness = os.path.join(HERE, '_toolset_diff_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, 'toolset-apply.js')],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [l for l in output.splitlines() if l.startswith('FAIL')]
    assert not fails, 'toolset-diff collapse failures:\n' + output
    assert output.count('PASS') >= 15, f'expected >=15 PASS lines, got:\n{output}'
