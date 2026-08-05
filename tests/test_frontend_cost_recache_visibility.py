"""Regression harness: the cost popover must SURFACE the write-breakdown's
re-cache WASTE (``recacheBody``) even when the banner-level cache-break
detector stayed silent, and must NOT print the legacy heuristic "write source"
note on top of the authoritative ``writeBreakdown`` equation.

WHY
---
The Stage-1 backend traceability fix makes a turn's round-1 attribute an
evicted-tail re-bill to ``recacheBody`` (with a ``readDrop``) instead of the
benign ``contextWrite`` — but the banner detector (detect_cache_break) stays
SILENT on that sub-threshold / cross-turn drop, so ``rd.cacheBreak`` is absent
and the old popover showed NO explanation for the "重新缓存正文" term: the
money-losing term appeared bare in the equation. This harness asserts:

  1. When ``_wb.recacheBody > 0`` and there is NO ``cacheBreak`` banner, a
     dedicated waste line (`.cp-round-waste`) is rendered from the breakdown
     data (readDrop), so the user SEES why the round cost money.
  2. The legacy ``.cp-round-note`` "write source" heuristic is SUPPRESSED when
     the authoritative ``writeBreakdown`` equation was shown (it used to fire
     redundantly on round-1, claiming "上一轮产出" for a round with none).
  3. When a banner ``cacheBreak`` IS present, the waste line is NOT added
     (the existing `.cp-round-break` line explains it — no duplication).

Loads the REAL shipped i18n.js + finish_info.js under jsdom. A NEUTER removes
the waste line to prove the guard has teeth.

Skips cleanly when node + jsdom aren't installed.
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
const I18N = process.argv[2];
const FINISH = process.argv[3];
const RICH = process.argv[4];   // _buildCostPopover lives here now (Epic-E split)
const ROOT = process.argv[5];
const NEUTER = process.argv[6] === '1';

const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.localStorage = win.localStorage;

win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.debugLog = global.debugLog = () => {};
win.Icon = global.Icon = () => '';
win.calcCostCny = global.calcCostCny = () => null;
win.formatCny = global.formatCny = (v) => '¥' + v;

eval(fs.readFileSync(I18N, 'utf8'));
win.t = global.t = t;

let finishSrc = fs.readFileSync(FINISH, 'utf8');
let richSrc = fs.readFileSync(RICH, 'utf8');
if (NEUTER) {
  // Remove the waste-line emission — the exact fix under test.
  richSrc = richSrc.replace(
    /html \+= `<div class="cp-round-waste"[^\n]*\n/,
    '/* NEUTERED waste line */\n');
}
// ONE eval: the rich module closes over finish_info.js's top-level consts.
eval(finishSrc + '\n' + richSrc);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _buildCostPopover !== 'function') {
  console.log('FAIL popover_exposed');
  process.exit(0);
}
check('popover_exposed', true);

// ── Scenario A: a turn's round-1 whose write is a cross-turn EVICTION re-bill.
//    recacheBody>0 + readDrop>0, but NO cacheBreak banner (detector silent on
//    the sub-threshold / round-1 cross-turn drop). Second round is a normal
//    tail so the popover renders a table (numRounds>1). ──
const roundsA = [
  { round: 1, usage: { completion_tokens: 20, cache_write_tokens: 40100,
                       cache_read_tokens: 79200 },
    writeBreakdown: { write: 40100, prevOutput: 0, toolResults: 0,
                      contextWrite: 500, recacheBody: 38800, envelope: 800,
                      readDrop: 38800,
                      recacheCause: { no_cache_reuse: 'x' } } },
  { round: 2, usage: { completion_tokens: 30, cache_write_tokens: 900,
                       cache_read_tokens: 120000 },
    writeBreakdown: { write: 900, prevOutput: 20, toolResults: 0,
                      contextWrite: 0, recacheBody: 0, envelope: 800 } },
];
const ctxA = {
  costInfo: { costCny: 1 }, rounds: roundsA, numRounds: roundsA.length,
  u: {}, inp: 0, out: 0, cw: 0, cr: 0, thk: 0,
  mid: 'm', pid: 'p', taskId: 't', toolRounds: [],
};
const htmlA = _buildCostPopover(ctxA);

// The re-cache WASTE line must be present (round-1's recacheBody explained).
check('A_waste_line_present', htmlA.includes('cp-round-waste'));
// The equation still shows the recacheBody term ("重新缓存正文").
check('A_recache_term_present', htmlA.includes('重新缓存正文'));
// The legacy heuristic note must NOT fire on round-1 (authoritative breakdown
// was shown) — no "write source: prev output + tool results" contradiction.
check('A_no_legacy_writenote', !htmlA.includes('cp-round-note'));

// ── Scenario B: a round WITH a banner cacheBreak → the existing cp-round-break
//    line explains it; the waste line must NOT be added (no duplication). ──
const roundsB = [
  { round: 1, usage: { completion_tokens: 20, cache_write_tokens: 5000,
                       cache_read_tokens: 100 } },
  { round: 2, usage: { completion_tokens: 30, cache_write_tokens: 37700,
                       cache_read_tokens: 0 },
    cacheBreak: { no_cache_reuse: 'stochastic server-side cache miss' },
    writeBreakdown: { write: 37700, prevOutput: 20, toolResults: 0,
                      contextWrite: 0, recacheBody: 36880, envelope: 800,
                      readDrop: 0,
                      recacheCause: { no_cache_reuse: 'stochastic server-side cache miss' } } },
];
const ctxB = {
  costInfo: { costCny: 1 }, rounds: roundsB, numRounds: roundsB.length,
  u: {}, inp: 0, out: 0, cw: 0, cr: 0, thk: 0,
  mid: 'm', pid: 'p', taskId: 't', toolRounds: [],
};
const htmlB = _buildCostPopover(ctxB);
// The banner break line is present …
check('B_break_line_present', htmlB.includes('cp-round-break'));
// … and the waste line is NOT duplicated (recacheBody explained by the break).
check('B_no_duplicate_waste', !htmlB.includes('cp-round-waste'));

console.log(out.join('\n'));
"""


def _run(neuter: bool) -> str:
    harness = os.path.join(HERE, '_cost_recache_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'i18n.js'),
             os.path.join(JS_DIR, 'ui', 'finish_info.js'),
             os.path.join(JS_DIR, 'ui', 'finish_info_rich.js'),
             ROOT,
             '1' if neuter else '0'],
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
def test_recache_waste_visible_and_no_double_note():
    output = _run(neuter=False)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'cost-recache-visibility failures:\n' + output
    assert output.count('PASS') >= 6, f'expected >=6 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_without_waste_line_the_guard_fails():
    # Remove the waste-line emission → scenario A's waste check MUST fail,
    # proving it is the new line (not incidental text) that satisfies it.
    output = _run(neuter=True)
    assert 'FAIL A_waste_line_present' in output, (
        'NEUTER did not break the waste-line check — guard has no teeth:\n'
        + output)
