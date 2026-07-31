"""tests/test_frontend_async_writeback_version.py — RENDER_CONTRACT Phase 2b / L2.

WHY (owner point 3)
-------------------
Phase 2a folded `msg.cost` / `msg.modifiedFileList` into the per-message content
version. But cost lands in the `_costCache` (keyed by usage) and file-change
extraction in the `_fcResultByMsg` WeakMap — NEITHER is on `msg`. So on the
LAZY path (a message the server did not pre-stamp), the async data landed in a
side cache the field-version could not see → the version stayed put → the row
only repainted via the separate `_bgRefreshChat` output-diff path. L2 was
therefore only HALF closed.

Phase 2b closes it by writing the async result back onto `msg` when it lands:
  • cost → `msg.cost` (the SAME server-authoritative field the backend stamps;
    same shape, already folded + preferred by renderFinishInfo);
  • file-change extraction → a display-only `msg._fcResolvedFp` version signal
    (the extracted fallback has a DIFFERENT shape than the git-backed
    `modifiedFileList`, so it must NOT overwrite that authoritative field; the
    WeakMap stays the data store, this stamp only moves the version).

So the field-version now MOVES when the lazy async data lands → the one
surgical trigger repaints the row, no background path needed.

Asserts (drives the REAL shipped _prefetchConvCosts + _msgContentVersion):
  A. after _prefetchConvCosts resolves, a message that lacked cost has
     `msg.cost` stamped AND its content version MOVED;
  B. cost write-back never stamps a null/no-charge cost (no false version move);
  C. the file-change async land stamps `_fcResolvedFp` and moves the version
     (checked via _msgContentVersion directly with the stamp present/absent).

NEUTER: strip the cost write-back line → `msg.cost` stays unset after the
prefetch and the version does NOT move — proving the write-back is what closes
the lazy-path half of L2.

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
COST = os.path.join(JS_DIR, 'core', 'cost.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
const NC = process.argv[5] || '';
global.window = global;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
global.t = (k) => String(k || '').split('.').pop();
global._TOOL_DISPLAY = {};
// cost.js JSON parse helper (sibling-module symbol not eval'd by this harness).
global._safeJsonParse = (raw, fallback) => fallback;
global.serverModel = 'aws.claude-opus-4.8';
// cost.js migration preamble reads config.* at load — seed a minimal config.
global.config = { model: 'aws.claude-opus-4.8' };
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };

// Stub the batch cost API: returns one cost dict per item (non-zero).
let _batchCalls = 0;
global.Api = { conversations: {
  costBatch: async (items) => {
    _batchCalls++;
    return { costs: items.map((it, i) => ({ costCny: 0.1 * (i + 1), costUsd: 0.014 * (i + 1),
      inputTokens: 10, outputTokens: 20, cacheWriteTokens: 0, cacheReadTokens: 0,
      thinkingTokens: 0, inputCostCny: 0.05, outputCostCny: 0.05,
      cacheWriteCostCny: 0, cacheReadCostCny: 0, cacheSavingsCny: 0 })) };
  },
  cost: async () => null,
} };

// translation_model.js for translationFingerprint; then chat_render for the
// version function; then cost.js for _prefetchConvCosts.
(0, eval)(fs.readFileSync(process.argv[2], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[2].replace('escape_html.js', 'translation_model.js'), 'utf8'));
try { (0, eval)(fs.readFileSync(process.argv[2].replace('core/escape_html.js', 'ui/translation_indicator.js'), 'utf8')); } catch (e) {}
(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // chat_render.js

let costSrc = fs.readFileSync(process.argv[4], 'utf8');
if (NC === 'nowriteback') {
  const before = costSrc;
  // Strip the cost write-back pass (bounded by the two sentinel comments; the
  // opener carries a descriptive tail after the marker word, so match loosely).
  costSrc = costSrc.replace(/\/\* PHASE2B-COST-WRITEBACK[\s\S]*?END-PHASE2B-COST-WRITEBACK \*\//,
    '/* neutered */');
  if (costSrc === before) { console.log('FAIL neuter_not_applied (PHASE2B-COST-WRITEBACK sentinel absent)'); process.exit(0); }
}
(0, eval)(costSrc);  // cost.js (real / neutered)

const out = [];
function check(name, cond, extra) { out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? (' ' + extra) : '')); }

const _ver = (typeof _msgContentVersion === 'function') ? _msgContentVersion : _msgFingerprint;
if (typeof _prefetchConvCosts !== 'function' || typeof _ver !== 'function') {
  console.log('FAIL fn_exposed _prefetchConvCosts/_msgContentVersion missing'); console.log(out.join('\n')); process.exit(0);
}
check('fn_exposed', true);

(async () => {
  // ── A) Cost write-back stamps msg.cost + moves the version on the lazy path. ──
  {
    const msg = { role: 'assistant', content: 'hi', usage: { input_tokens: 10, output_tokens: 20 },
                  model: 'aws.claude-opus-4.8' };
    const conv = { id: 'c1', messages: [msg] };
    const vBefore = _ver(msg);
    const noCostBefore = (msg.cost === undefined || msg.cost === null);
    await _prefetchConvCosts(conv);
    const stamped = !!(msg.cost && msg.cost.costCny > 0);
    check('A_cost_stamped_on_msg', noCostBefore && stamped, 'cost=' + JSON.stringify(msg.cost));
    check('A_version_moved_after_cost', _ver(msg) !== vBefore);
  }

  // ── B) A no-charge (null) cost is NOT stamped (no false version move). ──
  {
    // Force the batch to return a null cost for this one.
    const savedBatch = Api.conversations.costBatch;
    Api.conversations.costBatch = async (items) => ({ costs: items.map(() => null) });
    const msg = { role: 'assistant', content: 'x', usage: { input_tokens: 5, output_tokens: 5 },
                  model: 'aws.claude-opus-4.8' };
    const conv = { id: 'c2', messages: [msg] };
    const vBefore = _ver(msg);
    await _prefetchConvCosts(conv);
    check('B_null_cost_not_stamped', !msg.cost);
    check('B_version_unchanged_on_null_cost', _ver(msg) === vBefore);
    Api.conversations.costBatch = savedBatch;
  }

  // ── C) File-change resolution stamp (_fcResolvedFp) moves the version. This
  //       is the version SIGNAL for the lazy file-change path; the WeakMap
  //       remains the data store, modifiedFileList stays authoritative. ──
  {
    const base = { role: 'assistant', content: 'did work',
                   toolRounds: [{ roundNum: 1, toolName: 'write_file', status: 'done' }] };
    const vBefore = _ver(base);
    const after = Object.assign({}, base, { _fcResolvedFp: 'abc123' });
    check('C_fc_stamp_moves_version', _ver(after) !== vBefore);
    // Same stamp value → stable (no needless repaint).
    const after2 = Object.assign({}, base, { _fcResolvedFp: 'abc123' });
    check('C_fc_stamp_stable', _ver(after) === _ver(after2));
    // Different resolution → moves again.
    const after3 = Object.assign({}, base, { _fcResolvedFp: 'def456' });
    check('C_fc_stamp_change_moves', _ver(after) !== _ver(after3));
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_async_writeback_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ESCAPE_HTML, CHAT_RENDER, COST, nc],
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
def test_async_writeback_makes_version_cover_lazy_path():
    """Cost lands → msg.cost stamped → version moves; file-change land stamp →
    version moves. Closes the lazy-path half of L2 (owner point 3)."""
    src = open(COST, encoding='utf-8').read()
    assert 'PHASE2B-COST-WRITEBACK' in src, \
        'Phase-2b cost write-back missing from cost.js — test stale'
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'async-writeback failures:\n' + output
    lines = _lines(output)
    for key in ('A_cost_stamped_on_msg', 'A_version_moved_after_cost',
                'B_null_cost_not_stamped', 'B_version_unchanged_on_null_cost',
                'C_fc_stamp_moves_version', 'C_fc_stamp_stable',
                'C_fc_stamp_change_moves'):
        assert lines.get(key) == 'PASS', f'{key} not PASS:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_no_writeback_leaves_version_blind():
    """NEUTER: strip the cost write-back → msg.cost stays unset and the version
    does not move — proving the write-back closes the lazy-path L2 half."""
    output = _run('nowriteback')
    assert 'FAIL neuter_not_applied' not in output, (
        'the PHASE2B-COST-WRITEBACK sentinel is absent — write-back has not '
        f'landed yet:\n{output}')
    lines = _lines(output)
    assert lines.get('A_cost_stamped_on_msg') == 'FAIL', (
        'without write-back, msg.cost was still stamped — write-back is not '
        f'load-bearing:\n{output}')
    assert lines.get('A_version_moved_after_cost') == 'FAIL', (
        f'without write-back, the version still moved on the lazy path:\n{output}')
