"""jsdom test for the Request Inspector per-task round log (P1 data plane).

Design: docs/DEBUG_PANEL_REDESIGN.md §4 — SSE ``messages_snapshot`` events must
APPEND into a per-task round log (``_debugRequests[taskId]``), never overwrite
the previous round. The legacy ``_debugCache`` single-slot render source is
untouched. kind='state' snapshots are NOT LLM requests and route to ``.states``;
a missing kind (legacy event) defaults to 'request'.

Loads the REAL shipped static/js/core/debug_panel.js under jsdom and drives
``showMessagesInDebug`` with the new 7th ``meta`` param (the envelope the SSE
handler now forwards: kind / model / params / roundNum / taskId).

NEUTER (negative control): patching a COPY of debug_panel.js so the recording
block never runs MUST flip the retention assertions red — proving the log is
load-bearing, not vacuous.
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
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="debugPanel">' +
  '<div id="debugTitle"></div>' +
  '<div id="debugContent"></div>' +
  '</div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
win.Icon = global.Icon = (name, size) => `<svg data-icon="${name}" width="${size||14}"></svg>`;
win.t = global.t = (k) => k;

win.activeConvId = global.activeConvId = 'conv-1';
win.conversations = global.conversations = [{ id: 'conv-1' }];
win.debugVisible = global.debugVisible = true;

/* eval the shipped file + append a getter INSIDE the eval scope so the
 * harness can inspect the const-scoped _debugRequests (const does not leak
 * out of a direct eval — function declarations do). */
const src = fs.readFileSync(process.argv[2], 'utf8');
eval(src + '\n;win.__dumpReqs = function(){ return _debugRequests; };');

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

check('fn_present', typeof showMessagesInDebug === 'function');
check('store_present', typeof win.__dumpReqs() === 'object');

const msgs1 = [
  { role: 'system', content: 'SYS' },
  { role: 'user', content: 'hi' },
];
const msgs2 = msgs1.concat([
  { role: 'assistant', content: 'a1' },
  { role: 'user', content: 'u2' },
]);
const META = (round) => ({
  kind: 'request', model: 'm-x',
  params: { maxTokens: 1000, temperature: 1 },
  roundNum: round, taskId: 'task-A',
});

/* ── Two request snapshots on the SAME task → BOTH rounds retained ── */
showMessagesInDebug(msgs1, 'Round 1 请求前 · 2条', true, 'conv-1', undefined, undefined, META(1));
showMessagesInDebug(msgs2, 'Round 2 请求前 · 4条', true, 'conv-1', undefined, undefined, META(2));
const tA = win.__dumpReqs()['task-A'];
check('two_rounds_retained', !!tA && tA.roundOrder.length === 2);
check('round1_not_overwritten',
  !!tA && tA.rounds['1'] && tA.rounds['1'].messageCount === 2);
check('round2_recorded',
  !!tA && tA.rounds['2'] && tA.rounds['2'].messageCount === 4);
check('rounds_ordered', !!tA &&
  tA.roundOrder[0] === '1' && tA.roundOrder[1] === '2');
check('model_params_stored', !!tA &&
  tA.rounds['1'].model === 'm-x' &&
  tA.rounds['1'].params && tA.rounds['1'].params.maxTokens === 1000);
check('messages_ref_kept', !!tA &&
  Array.isArray(tA.rounds['1'].messages) && tA.rounds['1'].messages.length === 2);

/* ── kind='state' → .states, NOT the request rounds ── */
showMessagesInDebug(msgs2, '最终回复后 · 4条', true, 'conv-1', undefined, undefined,
  { kind: 'state', model: 'm-x', roundNum: 'final', taskId: 'task-A' });
check('state_not_in_rounds', !!tA && tA.roundOrder.length === 2);
check('state_routed_to_states', !!tA && tA.states.length === 1 &&
  tA.states[0].roundNum === 'final' && tA.states[0].kind === 'state');

/* ── Legacy event WITHOUT kind → treated as 'request' (back-compat) ── */
showMessagesInDebug(msgs1, 'legacy', true, 'conv-1', undefined, undefined,
  { roundNum: 1, taskId: 'task-B' });
const tB = win.__dumpReqs()['task-B'];
check('legacy_kind_defaults_request', !!tB &&
  tB.roundOrder.length === 1 && tB.rounds['1'].kind === 'request');

/* ── No meta (cold /debug-messages path) → nothing recorded ── */
const taskCountBefore = Object.keys(win.__dumpReqs()).length;
showMessagesInDebug(msgs1, '2 msgs (server)', false, 'conv-1', undefined, true);
check('no_meta_no_record',
  Object.keys(win.__dumpReqs()).length === taskCountBefore);

/* ── Legacy panel render still works (title summary on the latest) ── */
check('panel_still_renders',
  document.getElementById('debugTitle').innerHTML.indexOf('Messages') !== -1);

console.log(out.join('\n'));
"""


def _run(src_path=None, expect_fail=None):
    """Drive the harness against a debug_panel.js source.

    ``expect_fail``: when set (a probe name), the run MUST report that probe
    as FAIL (negative-control mode for the neutered copy).
    """
    harness = os.path.join(HERE, '_debug_request_log_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             src_path or os.path.join(JS_DIR, 'core', 'debug_panel.js'),
             ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    if expect_fail:
        assert f'FAIL {expect_fail}' in output, (
            f'neutered copy did NOT flip {expect_fail} red:\n{output}')
        return output
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'request-log data-plane failures:\n' + output
    assert output.count('PASS') >= 13, f'expected >=13 PASS lines, got:\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_request_log_appends_per_round():
    _run()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_neuter_recording_stripped_flips_red():
    """Negative control: disable the recording block in a COPY → the
    'two_rounds_retained' probe MUST fail. Proves the log is load-bearing."""
    shipped = os.path.join(JS_DIR, 'core', 'debug_panel.js')
    with open(shipped, encoding='utf-8') as f:
        src = f.read()
    anchor = 'if (meta && typeof meta === "object") {'
    assert anchor in src, 'recording-block anchor drifted — update the neuter'
    neutered = src.replace(anchor, 'if (false) {', 1)
    assert neutered != src
    tmp = os.path.join(HERE, '_debug_panel_reqlog_neutered.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(neutered)
    try:
        _run(src_path=tmp, expect_fail='two_rounds_retained')
    finally:
        os.remove(tmp)
    # The shipped file must be byte-identical after the copy-patched run.
    with open(shipped, encoding='utf-8') as f:
        assert f.read() == src, 'shipped debug_panel.js must be byte-identical'


if __name__ == '__main__':
    print(_run())
