"""Sibling title-collision guarantee for parallel tool batches.

Incident (conversation ``mtc9t5qiubhy2k``): one LLM round fired several
``hope/get_log_file`` calls whose backend-composed titles were byte-identical
(same tool, same pod resource — the distinguishing args were elided). The
rows rendered as what looked like duplicate executions.

Two-layer fix, frontend half pinned here:

1. ``siblingTitleDiscriminators`` (tool-execution-groups.ts) — for every
   within-batch cluster of rounds sharing (toolName, query), derives a
   `` · key=value`` suffix from the args that ACTUALLY differ across the
   cluster (at most two keys present in every sibling), falling back to the
   occurrence index (`` #2``, `` #3``) when even the args are byte-equal.
   Keyed by durable toolCallId; batch-scoped so identical titles in
   DIFFERENT llmRounds (already separated by R-badges) stay untouched.
2. ``_renderToolSlot`` (ui/tool_rounds.js) — applies the suffix to a shallow
   clone of the round before rendering, so the collision can never reach the
   DOM even for a future tool whose arg keys no backend list knows about.

All tests skip cleanly without node.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
TOOL_ROUNDS = Path(runtime_section_path('ui/tool_rounds.js'))

_STUBS = r"""
const fs = require('fs');
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.t = (k) => k;
global.Icon = () => '';
global.renderMarkdown = (s) => s;
global._shortUrl = (u) => u;
global.formatNumber = (n) => String(n);
global.window = { location: { href: 'http://localhost/' },
  addEventListener() {}, removeEventListener() {} };
global.document = { addEventListener() {}, removeEventListener() {},
  createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }) };
eval(fs.readFileSync(process.argv[1], 'utf8'));
"""


def _node(harness: str, *paths: str) -> dict:
    if not shutil.which('node'):
        pytest.skip('node is required')
    result = subprocess.run(
        ['node', '-e', harness, *paths], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_pure_helper_clusters_and_fallbacks():
    harness = _STUBS + r"""
const title = 'hope/get_log_file — stdout @ c/n';
let seq = 0;
const mk = (id, llmRound, args, q) => ({
  roundNum: (seq += 1), llmRound, toolCallId: id,
  attemptId: 'att-1', taskId: 'task-1',  // production rounds carry scope
  toolName: 'mcp__hope__get_log_file',
  query: q || title, toolArgs: args, results: [{}], status: 'done',
});
const rounds = [
  mk('gw_a', 0, { regex: 'A', method: 'tail' }),
  mk('gw_b', 0, { regex: 'B', method: 'tail' }),
  mk('gw_c', 0, { regex: 'A', method: 'tail' }, 'hope/get_log_file — stderr @ c/n'),
  mk('gw_d', 1, { regex: 'A', method: 'tail' }),  // same title, OTHER batch
  mk('gw_e', 2, { regex: 'X', method: 'head' }),  // byte-equal twin below
  mk('gw_f', 2, { regex: 'X', method: 'head' }),
  // 3-way cluster: args differ across the cluster, but x/y agree on the
  // two surfaced diff keys — the residual collision must STILL split.
  mk('gw_x', 3, { regex: 'X', method: 'head' }),
  mk('gw_y', 3, { regex: 'X', method: 'head' }),
  mk('gw_z', 3, { regex: 'A', method: 'tail' }),
];
const m = siblingTitleDiscriminators(rounds);
console.log(JSON.stringify({
  a: m.get('gw_a') || '', b: m.get('gw_b') || '',
  c: m.get('gw_c') || '', d: m.get('gw_d') || '',
  e: m.get('gw_e') || '', f: m.get('gw_f') || '',
  x: m.get('gw_x') || '', y: m.get('gw_y') || '', z: m.get('gw_z') || '',
  size: m.size,
}));
process.exit(0);
"""
    result = _node(harness, str(TOOL_ROUNDS))
    # Same-batch collision, args differ in `regex` only → chip suffixes.
    assert result['a'] == ' · regex=A', result
    assert result['b'] == ' · regex=B', result
    # Different query in the same batch is NOT a collision.
    assert result['c'] == '', result
    # Same title but a different llmRound → different batch → untouched.
    assert result['d'] == '', result
    # Byte-equal args → occurrence index, first occurrence unmarked.
    assert result['e'] == '', result
    assert result['f'] == ' #2', result
    # Residual collision: x/y agree on the surfaced diff keys, so the second
    # one gets the occurrence index APPENDED to its chips — the no-identical-
    # sibling-titles guarantee holds even when chips alone cannot split.
    assert result['x'] == ' · regex=X · method=head', result
    assert result['y'] == ' · regex=X · method=head #2', result
    assert result['z'] == ' · regex=A · method=tail', result
    assert result['size'] == 6, result


def test_render_tool_slot_applies_suffix():
    harness = _STUBS + r"""
const title = 'hope/get_log_file — stdout @ c/n';
let seq = 0;
const mk = (id, args) => ({
  roundNum: (seq += 1), llmRound: 0, toolCallId: id,
  attemptId: 'att-1', taskId: 'task-1',  // production rounds carry scope
  toolName: 'mcp__hope__get_log_file',
  query: title, toolArgs: args, results: [{}], status: 'done',
});
const rounds = [mk('gw_a', { regex: 'SMOKE' }), mk('gw_b', { regex: 'AGENTIX' })];
const htmlA = _renderToolSlot(rounds[0], rounds);
const htmlB = _renderToolSlot(rounds[1], rounds);
const solo = _renderToolSlot(
  { ...mk('gw_solo', { regex: 'SMOKE' }), query: 'hope/list_log_files — p @ c/n' },
  rounds,
);
console.log(JSON.stringify({
  aHasChip: htmlA.includes('regex=SMOKE'),
  bHasChip: htmlB.includes('regex=AGENTIX'),
  aNoCross: !htmlA.includes('regex=AGENTIX'),
  originalQueryUntouched: rounds[0].query === title,
  soloUnsuffixed: !solo.includes('regex='),
}));
process.exit(0);
"""
    result = _node(harness, str(TOOL_ROUNDS))
    assert result == {
        'aHasChip': True,
        'bHasChip': True,
        'aNoCross': True,
        'originalQueryUntouched': True,
        'soloUnsuffixed': True,
    }, result
