"""Regression test: a swarm "Parallel Execution" panel must NOT tick "Running"
forever when the terminal ``swarm_phase:complete`` event never reaches the tab.

WHY
---
``_swarmActive`` / ``_asyncRunning`` and the wall-clock timer are frontend-only
state cleared ONLY by a terminal SSE event. If the server restarts (or the SSE
stream drops) after the swarm finished, an OPEN tab is stuck showing "Running /
408m9s" with no poll loop to fix it — the reported zombie panel.

Two defenses live in ``static/js/ui/streaming_ui.js``:
  • Option 1 (symptom): ``_buildSwarmPanelHTML`` flips a panel older than
    ``_SW_STALE_MS`` (with no ``_swarmEndTime``) to a muted "Stale" pill and
    stops rendering the runaway elapsed; ``_tickSwarmTimers`` freezes any
    ``[data-sw-start]`` past the cap.
  • Option 2 (root): ``_settleStuckSwarmRound`` reconciles a panel the backend
    reports inactive — clears the flags, freezes the end time, and marks any
    still-mid-flight agent ``unknown`` (no fabricated green "done").

Runs the REAL shipped JS under jsdom; skips cleanly when node + jsdom aren't
installed.
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
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setInterval = win.setInterval = () => 0;   // neuter the timer ticker + reconciler
global.setTimeout = win.setTimeout = (fn) => 0;

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win._TOOL_DISPLAY = global._TOOL_DISPLAY = {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/streaming_ui.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

for (const fn of ['_buildSwarmPanelHTML', '_settleStuckSwarmRound', '_tickSwarmTimers']) {
  if (typeof eval(fn) !== 'function') { console.log('FAIL functions_exposed ' + fn + ' missing'); process.exit(0); }
}
check('functions_exposed', true);
// _SW_STALE_MS is a `const` inside the eval'd module scope — function closures
// see it, but it does NOT leak to this harness scope. Mirror its known value
// (30 min, streaming_ui.js) so the test exercises the same threshold.
const STALE_MS = 30 * 60 * 1000;

const NOW = Date.now();
const OLD = NOW - (STALE_MS + 60000);   // comfortably past the cap
const FRESH = NOW - 5000;

// ── 1. A fresh active panel still reads "Running" with a live timer ──
const freshRound = {
  roundNum: 1, _swarm: true, _swarmActive: true, status: 'searching',
  _swarmStartTime: FRESH,
  _swarmAgents: [{ id: 'a1', role: 'analyst', objective: 'wait + verify', status: 'running', phase: 'tool_use' }],
};
const freshHtml = _buildSwarmPanelHTML(freshRound, [freshRound]);
check('fresh_shows_running', freshHtml.includes('Running'));
check('fresh_not_stale', !freshHtml.includes('Stale'));
check('fresh_has_ticker_attr', freshHtml.includes('data-sw-start'));

// ── 2. An OLD unsettled active panel flips to "Stale" and drops the runaway timer ──
const staleRound = {
  roundNum: 1, _swarm: true, _swarmActive: true, status: 'searching',
  _swarmStartTime: OLD,
  _swarmAgents: [{ id: 'a1', role: 'analyst', objective: 'wait + verify', status: 'running', phase: 'tool_use' }],
};
const staleHtml = _buildSwarmPanelHTML(staleRound, [staleRound]);
check('stale_shows_stale_pill', staleHtml.includes('Stale'));
check('stale_no_running_pill', !staleHtml.includes('>Running<') && !staleHtml.includes('Running</span>'));
// runaway elapsed (hundreds of minutes) must NOT be present
check('stale_no_runaway_timer', !/\d{3,}m\d+s/.test(staleHtml));

// ── 3. An OLD panel that DID settle (has _swarmEndTime) is NOT stale ──
const settledRound = {
  roundNum: 1, _swarm: true, _swarmActive: false, status: 'done',
  _swarmStartTime: OLD, _swarmEndTime: OLD + 30000,
  _swarmAgents: [{ id: 'a1', role: 'analyst', objective: 'x', status: 'done', phase: 'done' }],
};
const settledHtml = _buildSwarmPanelHTML(settledRound, [settledRound]);
check('settled_not_stale', !settledHtml.includes('Stale'));
check('settled_shows_complete', settledHtml.includes('Complete'));

// ── 4. _tickSwarmTimers freezes a runaway [data-sw-start], ticks a fresh one ──
document.body.innerHTML =
  '<div class="sw-panel">' +
    '<span class="sw-header-timer" id="old" data-sw-start="' + OLD + '">408m9s</span>' +
    '<span class="sw-header-timer" id="new" data-sw-start="' + FRESH + '">0s</span>' +
  '</div>';
_tickSwarmTimers();
check('ticker_freezes_runaway', document.getElementById('old').textContent === '408m9s');
check('ticker_updates_fresh', /^\d+s$/.test(document.getElementById('new').textContent));

// ── 5. _settleStuckSwarmRound: backend-confirmed inactive → settle + honest agent status ──
const zombie = {
  roundNum: 1, _swarm: true, _swarmActive: true, _asyncRunning: true, status: 'searching',
  _swarmStartTime: OLD,
  _swarmAgents: [
    { id: 'g1', role: 'analyst', objective: 'still on screen as running', status: 'running', phase: 'tool_use' },
    { id: 'g2', role: 'coder', objective: 'backend says done', status: 'running', phase: 'tool_use' },
  ],
};
// Backend session evicted for g1 (no row), but still knows g2 completed.
_settleStuckSwarmRound(zombie, [{ id: 'g2', status: 'completed' }]);
check('settle_clears_active', zombie._swarmActive === false && zombie._asyncRunning === false);
check('settle_marks_done_status', zombie.status === 'done');
check('settle_freezes_end', typeof zombie._swarmEndTime === 'number' && zombie._swarmEndTime > 0);
const z = {}; for (const a of zombie._swarmAgents) z[a.id] = a;
check('settle_no_fake_done', z['g1'].status === 'unknown' && z['g1'].phase === 'unknown');
check('settle_applies_backend_status', z['g2'].status === 'done' && z['g2'].phase === 'done');

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_swarm_stale_panel_guard_and_reconcile():
    harness = os.path.join(HERE, '_swarm_stale_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),   # argv[2]
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
    assert not fails, 'Swarm stale-panel failures:\n' + output
    assert output.count('PASS') >= 15, f'expected >=15 PASS lines, got:\n{output}'
