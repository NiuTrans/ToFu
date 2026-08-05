#!/usr/bin/env python3
"""Frontend: compaction_done must fold the live "compacting" phase HUD
(epic pt_f222e9ed288a44b3).

WHY
---
The stream-phase HUD (streaming_ui.js) renders `msg.phase` — a phase of
'compacting' shows "正在压缩早期上下文以适配窗口…" with animated dots. The
phase has NO terminal event of its own: live, it only gets replaced when the
NEXT round emits a phase (waiting_model / tool_exec) — and if the tab misses
that round (disconnect, background), the pill stays up for HOURS, reading as
"compressing right now" when the compaction finished long ago (user-reported
2026-08-01, screenshot: 20:10's compacting pill still up at 22:22).

The fold lives in `_handleCompaction` (sse_handlers_misc.js): on
`compaction_done`, if the conv's stream-session phase IS the compacting one,
clear it (setStreamPhase(convId, null)). Only the compacting phase is folded
— an unrelated live phase is never clobbered.

jsdom drives the real shipped stream_session.js + sse_handlers_misc.js.
NEUTER mode strips the fold call and proves the pill then survives.

Run DIRECTLY (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_frontend_compaction_phase_fold.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest

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
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

const out = [];
function check(name, cond, note) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : (' — ' + (note || ''))));
}

/* NEUTER mode (argv[3] === 'neuter-fold'): strip the fold line from a SCRATCH
 * copy of sse_handlers_misc.js — the pill must then survive compaction_done,
 * proving the fold line is what retires it. */
const _NEUTER = process.argv[3] === 'neuter-fold';

eval(fs.readFileSync(path.join(ROOT, 'static/js/ui/stream_session.js'), 'utf8'));

let _src = fs.readFileSync(path.join(ROOT, 'static/js/ui/sse_handlers_misc.js'), 'utf8');
if (_NEUTER) {
  const _target = "foldStreamPhaseIf(convId, 'compacting');";
  if (!_src.includes(_target)) throw new Error('neuter target line missing from sse_handlers_misc.js');
  _src = _src.replace(_target, '/* NEUTERED phase fold */');
}
eval(_src);

const convId = 'c1';
function _ctx() {
  return { convId, taskId: 't1',
           assistantMsg: { _compactions: [{ archiveId: 1, status: 'in_progress' }] } };
}

// ── 1. compaction_done folds a live compacting phase (+ upgrades marker) ──
{
  const c = _ctx();
  getStreamSession(convId).phase = {
    phase: 'compacting', detail: 'Compressing earlier context…',
    detailKey: 'stream.phase.compactingWindow' };
  _handleCompaction({ type: 'compaction_done', archiveId: 1,
                      tokensAfter: 100, msgsAfter: 3, reductionPct: 95 }, c);
  const ph = getStreamSession(convId).phase;
  check('compaction_done_folds_compacting_phase', ph === null,
        JSON.stringify(ph));
  check('marker_upgraded_to_done',
        c.assistantMsg._compactions[0].status === 'done'
        && c.assistantMsg._compactions[0].tokensAfter === 100,
        JSON.stringify(c.assistantMsg._compactions[0]));
}

// ── 2. compaction_done does NOT clobber an unrelated live phase ──
{
  const c = _ctx();
  getStreamSession(convId).phase = { phase: 'tool_exec', detail: 'run_command' };
  _handleCompaction({ type: 'compaction_done', archiveId: 1 }, c);
  const ph = getStreamSession(convId).phase;
  check('unrelated_phase_preserved',
        ph && ph.phase === 'tool_exec', JSON.stringify(ph));
}

// ── 3. compaction START keeps the compacting phase (honestly in progress) ──
{
  const c = _ctx();
  getStreamSession(convId).phase = {
    phase: 'compacting', detail: 'Compressing…',
    detailKey: 'stream.phase.compactingWindow' };
  _handleCompaction({ type: 'compaction', archiveId: 2, tokensBefore: 2198193 }, c);
  const ph = getStreamSession(convId).phase;
  check('compaction_start_keeps_phase',
        ph && ph.phase === 'compacting', JSON.stringify(ph));
  check('start_marker_in_progress',
        c.assistantMsg._compactions.some(m => m.archiveId === 2
                                          && m.status === 'in_progress'),
        JSON.stringify(c.assistantMsg._compactions));
}

// ── 4. no session entry → fold must not create one (Map-leak guard) ──
{
  streamSessions.delete('c9');
  _handleCompaction({ type: 'compaction_done', archiveId: 9 },
                    { convId: 'c9', taskId: 't9', assistantMsg: {} });
  check('no_session_no_leak', !streamSessions.has('c9'),
        'fold must not create a session entry for a conv without one');
}

console.log(out.join('\n'));
"""


def _run_harness(neuter=False):
    harness = os.path.join(HERE, '_compaction_phase_fold_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT, 'neuter-fold' if neuter else ''],
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
def test_compaction_done_folds_phase():
    """FAILING-FIRST: without the fold, probe 1 fails (phase survives)."""
    output = _run_harness()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'compaction phase fold failures:\n' + output
    assert output.count('PASS') >= 6, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_compaction_done_fold_neuter():
    """NEUTER proof: stripping the fold line makes the pill survive
    compaction_done — causality evidence."""
    output = _run_harness(neuter=True)
    assert 'FAIL compaction_done_folds_compacting_phase' in output, output


if __name__ == '__main__':
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    unittest.main(verbosity=2)
