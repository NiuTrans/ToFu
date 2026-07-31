#!/usr/bin/env python3
"""jsdom: a reloaded swarm panel keeps the per-agent timer NODE.

This is a different failure from the paper-media panels. There, a refresh showed
a WRONG elapsed (0:03 for a ten-minute job). Here the indicator **disappears**:
`_buildSwarmPanelHTML` emits the per-agent timer only when the agent is running
AND carries `_startedAt`, and that field was minted client-side from
`Date.now()` and never persisted. After a reload `_recoverSwarmAgents` rebuilt
agents from the durable snapshot with no start, so the running agent rendered
with no timer at all — and the `else if (a.elapsed)` fallback cannot cover it,
because `elapsed` only exists once the agent has FINISHED.

So the primary assertion is the PRESENCE of the `.sw-a-timer` node for a running
agent, not merely the correctness of its number. A test that only checked the
number would still pass while the element was missing.

Also pinned: the range check (a seconds epoch or a double-converted ms value is
dropped instead of rendering a ~50-year / year-58000 elapsed), and the terminal
path (a finished agent still renders its persisted `elapsed`).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'streaming_swarm_panel.js')

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _node_deps_available():
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
win.t = global.t = (k) => k;
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
win.Icon = global.Icon = () => '';
win.renderMarkdown = global.renderMarkdown = (s) => String(s == null ? '' : s);
const warns = [];
console.warn = (...a) => { warns.push(a.join(' ')); };

eval(fs.readFileSync(process.argv[2], 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const NOW = Date.now();

/* A persisted spawn round exactly as it looks after a reload: the durable
   snapshot is present, the live _swarmAgents array is gone. */
function roundWith(agent) {
  return {
    toolName: 'spawn_agents',
    roundNum: 1,
    _swarm: true,
    _swarmActive: true,
    _swarmSnapshot: { agents: [agent], settled: false, agentCount: 1,
                      doneCount: 0, totalTokens: 0, version: 0 },
    toolContent: JSON.stringify({ agents: [{ id: agent.id }] }),
  };
}

/* ── Case 1: a RUNNING agent reloaded 5 minutes in ── */
const running = { id: 'a1', role: 'coder', status: 'running',
                  objective: 'audit the thing', model: 'm',
                  startedAt: NOW - 300000 };
let agents = _recoverSwarmAgents(roundWith(running), []);
check('recovered_one_agent', agents.length === 1);
check('startedAt_reseeded_from_snapshot',
      agents.length === 1 && Number(agents[0]._startedAt) === NOW - 300000);

let html = _buildSwarmPanelHTML(roundWith(running), []);
const holder = document.createElement('div');
holder.innerHTML = html;
const timerNode = holder.querySelector('.sw-a-timer');
// THE core assertion: the indicator must EXIST, not merely be right.
check('timer_node_present_after_reload', !!timerNode);
// 300s → "5m0s". Assert it is NOT a fresh 0s clock.
const tTxt = timerNode ? (timerNode.textContent || '').trim() : '';
check('timer_not_restarted_at_zero', tTxt !== '0s' && tTxt !== '');
check('timer_reflects_five_minutes', /^5m\d+s$/.test(tTxt));
check('timer_carries_start_attr',
      !!timerNode && timerNode.getAttribute('data-sw-start') === String(NOW - 300000));

/* ── Case 2: wrong-magnitude values are dropped, not rendered ── */
warns.length = 0;
const secs = { id: 'a2', role: 'coder', status: 'running', objective: 'x',
               startedAt: Math.floor(NOW / 1000) };   // epoch SECONDS
agents = _recoverSwarmAgents(roundWith(secs), []);
check('seconds_epoch_not_adopted',
      agents.length === 1 && !agents[0]._startedAt);
check('seconds_epoch_warns', warns.length > 0);

warns.length = 0;
const future = { id: 'a3', role: 'coder', status: 'running', objective: 'x',
                 startedAt: NOW * 1000 };             // double-converted
agents = _recoverSwarmAgents(roundWith(future), []);
check('future_epoch_not_adopted',
      agents.length === 1 && !agents[0]._startedAt);
check('future_epoch_warns', warns.length > 0);

/* ── Case 3: a finished agent still renders its persisted elapsed ── */
const done = { id: 'a4', role: 'coder', status: 'done', objective: 'x',
               elapsed: 12.5, tokens: 100, preview: 'ok' };
html = _buildSwarmPanelHTML(roundWith(done), []);
const h2 = document.createElement('div');
h2.innerHTML = html;
const doneTimer = h2.querySelector('.sw-a-timer');
check('terminal_agent_keeps_elapsed',
      !!doneTimer && /12\.5/.test(doneTimer.textContent || ''));

console.log(out.join('\n'));
process.exit(0);
"""


def _run(module_path):
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
        f.write(_HARNESS)
        hp = f.name
    try:
        r = subprocess.run(['node', hp, module_path, ROOT],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError(f'harness failed:\n{r.stdout}\n{r.stderr}')
        return r.stdout
    finally:
        os.unlink(hp)


@pytest.mark.skipif(not _node_deps_available(), reason='node/jsdom unavailable')
class TestSwarmTimerReattach(unittest.TestCase):

    def test_swarm_timer_survives_reload(self):
        out = _run(PANEL_JS)
        fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
        self.assertFalse(fails, f'failing checks: {fails}\nfull:\n{out}')

    def test_NEUTER_reseed_removed_drops_timer_node(self):
        """Without the re-seed the timer NODE must vanish — the real symptom."""
        src = open(PANEL_JS, encoding='utf-8').read()
        anchor = '        _startedAt: startedAt || undefined,'
        self.assertIn(anchor, src, 'NEUTER anchor missing — re-point it')
        poisoned = src.replace(anchor, '        /* neutered */', 1)
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                         encoding='utf-8') as f:
            f.write(poisoned)
            pp = f.name
        try:
            subprocess.run(['node', '--check', pp], check=True,
                           capture_output=True)
            out = _run(pp)
            self.assertIn('FAIL timer_node_present_after_reload', out,
                          f'NEUTER did not bite:\n{out}')
        finally:
            os.unlink(pp)
        self.assertEqual(open(PANEL_JS, encoding='utf-8').read(), src,
                         'shipped file modified!')

    def test_NEUTER_range_check_removed_adopts_seconds(self):
        src = open(PANEL_JS, encoding='utf-8').read()
        anchor = 'rawStart > 1e12 && rawStart <= Date.now()'
        self.assertIn(anchor, src, 'NEUTER anchor missing — re-point it')
        poisoned = src.replace(anchor, 'rawStart > 0', 1)
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                         encoding='utf-8') as f:
            f.write(poisoned)
            pp = f.name
        try:
            subprocess.run(['node', '--check', pp], check=True,
                           capture_output=True)
            out = _run(pp)
            self.assertIn('FAIL seconds_epoch_not_adopted', out,
                          f'NEUTER did not bite:\n{out}')
        finally:
            os.unlink(pp)
        self.assertEqual(open(PANEL_JS, encoding='utf-8').read(), src,
                         'shipped file modified!')


if __name__ == '__main__':
    unittest.main()
