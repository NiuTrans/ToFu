"""Regression harness: the cost popover must make the write-breakdown's
offset-by-one batch correspondence EXPLICIT on the row.

WHY
---
A round's prompt-cache ``write`` includes the tool RESULTS that flowed in
from the PREVIOUS round's tool batch (the model calls tools in round N; the
results are appended and cached in round N+1). So the cost popover's "第N轮"
and the tool panel's "批次N" are offset by exactly one — a user cross-checking
"第3轮 write" against "批次3" will NEVER match, and the mismatch used to be
buried in a tooltip.

Defect A fix (static/js/ui/finish_info.js + static/js/i18n.js):
  1. The main-row tool-results term reads "上一轮工具结果 {v}" (was the
     ambiguous "工具结果 {v}"), stating the offset in words.
  2. The term carries an explicit batch reference "（工具批次{n}流入）" where
     n = display-round-index i (round label i+1 → inflow batch label i), so
     the two panels can be cross-checked directly.

This harness loads the REAL shipped ``i18n.js`` (for the actual translations)
and ``finish_info.js`` under jsdom, drives ``_buildCostPopover`` with the
round-2 / round-3 shapes from the reported conversation, and asserts the
offset is visible. A NEUTER negative control removes the batch-ref call and
proves the guard fails without it (teeth).

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


# argv[2] = i18n.js, argv[3] = finish_info.js, argv[4] = ROOT,
# argv[5] = '1' to NEUTER (strip the batch-ref annotation from finish_info.js
# source before eval, proving the guard fails without the fix).
_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const I18N = process.argv[2];
const FINISH = process.argv[3];
const ROOT = process.argv[4];
const NEUTER = process.argv[5] === '1';

const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
// i18n.js reads localStorage at load time; expose jsdom's.
global.localStorage = win.localStorage;

// finish_info.js references these at load / call time.
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.debugLog = global.debugLog = () => {};
win.Icon = global.Icon = () => '';
win.calcCostCny = global.calcCostCny = () => null;
win.formatCny = global.formatCny = (v) => '¥' + v;

// Load the REAL i18n so t() returns the shipped translations (default zh).
eval(fs.readFileSync(I18N, 'utf8'));
win.t = global.t = t;

let finishSrc = fs.readFileSync(FINISH, 'utf8');
if (NEUTER) {
  // Remove the batch-ref annotation line — the exact fix under test.
  finishSrc = finishSrc.replace(
    /if \(i > 0\) _tr \+= t\('finishInfo\.wbBatchRef', \{ n: i \}\);/,
    '/* NEUTERED */');
}
eval(finishSrc);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _buildCostPopover !== 'function') {
  console.log('FAIL popover_exposed');
  process.exit(0);
}
check('popover_exposed', true);

// ── The reported conversation's shape (5 API rounds). We care about the
//    write-breakdown rows for round 3 (display index 2) and round 4
//    (display index 3): each carries a writeBreakdown whose toolResults
//    flowed in from the PREVIOUS tool batch. ──
const rounds = [
  // round 1: prefix warm-up (contextWrite), no prior tools.
  { round: 1, usage: { completion_tokens: 595, cache_write_tokens: 117700 },
    writeBreakdown: { write: 117700, prevOutput: 0, toolResults: 0,
                      contextWrite: 116900, recacheBody: 0, envelope: 800 } },
  // round 2: write 1.1k, tool est overshoots → proportional split (both survive).
  { round: 2, usage: { completion_tokens: 358, cache_write_tokens: 1100 },
    writeBreakdown: { write: 1100, prevOutput: 410, toolResults: 690,
                      contextWrite: 0, recacheBody: 0, envelope: 0, capped: true } },
  // round 3: write 2.2k = prevOutput 358 + tool batch-2 results 1.4k + envelope 425.
  { round: 3, usage: { completion_tokens: 308, cache_write_tokens: 2200 },
    writeBreakdown: { write: 2200, prevOutput: 358, toolResults: 1400,
                      contextWrite: 0, recacheBody: 0, envelope: 442 } },
  // round 4: write 124.9k, tool batch-3 results 823 flow in.
  { round: 4, usage: { completion_tokens: 67, cache_write_tokens: 124900 },
    writeBreakdown: { write: 124900, prevOutput: 308, toolResults: 823,
                      contextWrite: 122969, recacheBody: 0, envelope: 800 } },
];

const ctx = {
  costInfo: { costCny: 1 }, rounds, numRounds: rounds.length,
  u: {}, inp: 0, out: 0, cw: 0, cr: 0, thk: 0,
  mid: 'm', pid: 'p', taskId: 't', toolRounds: [],
};
const html = _buildCostPopover(ctx);

// ── The main-row tool-results term must state the offset in WORDS. ──
check('label_says_last_round', html.includes('上一轮工具结果'));
// The old ambiguous bare "工具结果 {n}" main-row label must be gone. (The
// TOOLTIP still contains "工具结果 …" — that's fine — but the term chips use
// the "上一轮工具结果" form now. We assert the new form is present, which is
// the substantive guarantee.)

// ── Round 3 (display index 2) carries the batch-2 inflow reference. ──
check('round3_batchref_present', html.includes('工具批次2流入'));
// ── Round 4 (display index 3) carries the batch-3 inflow reference. ──
check('round4_batchref_present', html.includes('工具批次3流入'));
// ── Round 1 (display index 0) has NO tool results → NO batch ref. ──
check('round1_no_batchref', !html.includes('工具批次0流入'));

// ── Sanity: the write-source equation for round 3 still shows the parts. ──
check('round3_terms_present',
      html.includes('上一轮回复') && html.includes('消息开销'));

console.log(out.join('\n'));
"""


def _run(neuter: bool) -> str:
    harness = os.path.join(HERE, '_wb_offset_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'i18n.js'),               # argv[2]
             os.path.join(JS_DIR, 'ui', 'finish_info.js'),  # argv[3]
             ROOT,                                          # argv[4]
             '1' if neuter else '0'],                       # argv[5]
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
def test_offset_batch_correspondence_is_explicit():
    output = _run(neuter=False)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'offset-visibility failures:\n' + output
    assert output.count('PASS') >= 6, f'expected >=6 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_without_batchref_the_guard_fails():
    # Remove the batch-ref annotation → the batch-correspondence checks MUST
    # fail, proving the guard has teeth (it is the annotation, not incidental
    # text, that satisfies the assertions).
    output = _run(neuter=True)
    assert 'FAIL round3_batchref_present' in output, (
        'NEUTER did not break the round-3 batch-ref check — guard has no '
        f'teeth:\n{output}')
    assert 'FAIL round4_batchref_present' in output, (
        'NEUTER did not break the round-4 batch-ref check:\n' + output)
