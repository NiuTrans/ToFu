"""Regression test: after a page reload the swarm "Parallel Execution" panel
must show each sub-agent's REAL execution status + result — not an empty body
or objective-only stubs.

WHY
---
The live ``round._swarmAgents`` array (synthesized from ``swarm_*`` SSE events)
is frontend-only and never persisted. After a reload only the ``spawn_agents``
round survives (with the launch handle in ``toolContent``); the agent RESULTS
were persisted on the SIBLING ``await_agents`` / ``get_agent_result`` rounds.

``_recoverSwarmAgents(round, allRounds)`` in ``static/js/ui/streaming_ui.js``
cross-references those sibling rounds so the rebuilt panel shows real status,
elapsed, tokens and the final result text. This test locks that contract using
the exact persisted JSON shape (verified against PG conv ``mqc2nzy6h1xka6``).

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
global.setInterval = win.setInterval = () => 0;   // neuter the timer ticker
global.setTimeout = win.setTimeout = (fn) => 0;

// Globals the file touches at load / render time.
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win._TOOL_DISPLAY = global._TOOL_DISPLAY = {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/streaming_ui.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _recoverSwarmAgents !== 'function' || typeof _buildSwarmPanelHTML !== 'function') {
  console.log('FAIL functions_exposed _recoverSwarmAgents/_buildSwarmPanelHTML missing');
  process.exit(0);
}
check('functions_exposed', true);

// ── Persisted shape after reload (mirrors PG conv mqc2nzy6h1xka6) ──
// spawn round: only the launch handle survives.
const spawnRound = {
  roundNum: 1, toolName: 'spawn_agents', _swarm: true, status: 'done',
  toolContent: JSON.stringify({
    status: 'async_launched', swarm_id: 'sw-1',
    agents: [
      { id: 'b775ff8c', role: 'researcher', objective: 'Survey diffusion LMs', output_file: '/x/b.log' },
      { id: 'cd485e5c', role: 'researcher', objective: 'Co-evolution research', output_file: '/x/c.log' },
    ],
  }),
};
// await_agents round: completed[] carries status/elapsed/tokens/preview.
const awaitRound = {
  roundNum: 2, toolName: 'await_agents', status: 'done',
  toolContent: JSON.stringify({
    completed: [{ agent_id: 'cd485e5c', role: 'researcher', objective: 'Co-evolution research',
                  status: 'completed', elapsed: '227.8', tokens: '6453',
                  preview: 'Co-Evolution briefing preview', output_file: '/x/c.log', error: '' }],
    still_running: ['b775ff8c'], mode: 'all', timed_out: true, status: 'ok',
  }),
};
// get_agent_result round: full final_answer for one agent.
const garRound = {
  roundNum: 3, toolName: 'get_agent_result', status: 'done',
  toolContent: JSON.stringify({
    found: true, agent_id: 'b775ff8c', role: 'researcher', objective: 'Survey diffusion LMs',
    status: 'ok', final_answer: 'FULL DIFFUSION REPORT BODY ...', error: '',
    elapsed: '266', tokens: '7650', tool_calls: 5, rounds: 4, output_file: '/x/b.log',
  }),
};
const allRounds = [spawnRound, awaitRound, garRound];

// ── 1. recovery cross-references sibling rounds (not objective-only stubs) ──
const agents = _recoverSwarmAgents(spawnRound, allRounds);
check('recovers_both_agents', agents.length === 2);
const byId = {}; for (const a of agents) byId[a.id] = a;
check('agent1_status_from_gar', byId['b775ff8c'] && byId['b775ff8c'].status === 'completed');
check('agent1_result_text', byId['b775ff8c'] && byId['b775ff8c'].preview === 'FULL DIFFUSION REPORT BODY ...');
check('agent1_tokens', byId['b775ff8c'] && byId['b775ff8c'].tokens === '7650');
check('agent2_status_from_await', byId['cd485e5c'] && byId['cd485e5c'].status === 'completed');
check('agent2_preview_from_await', byId['cd485e5c'] && byId['cd485e5c'].preview === 'Co-Evolution briefing preview');
check('agent2_elapsed', byId['cd485e5c'] && byId['cd485e5c'].elapsed === '227.8');

// ── 2. rendered panel HTML actually contains status + result (the user-visible fix) ──
const html = _buildSwarmPanelHTML(spawnRound, allRounds);
check('panel_has_complete_pill', html.includes('Complete'));
check('panel_renders_result_body', html.includes('FULL DIFFUSION REPORT BODY'));
check('panel_renders_await_preview', html.includes('Co-Evolution briefing preview'));
check('panel_not_empty_body', html.includes('sw-agent'));

// ── 3. an agent with NO sibling result row stays visibly unresolved (no fake "done") ──
const agentsNoResults = _recoverSwarmAgents(spawnRound, [spawnRound]);
check('no_result_status_unknown', agentsNoResults.every(a => a.status === 'unknown'));
const htmlNoRes = _buildSwarmPanelHTML(spawnRound, [spawnRound]);
check('no_result_shows_no_result_pill', htmlNoRes.includes('No result'));

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_swarm_panel_recovery_from_sibling_rounds():
    harness = os.path.join(HERE, '_swarm_recovery_harness.js')
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
    assert not fails, 'Swarm recovery failures:\n' + output
    assert output.count('PASS') >= 13, f'expected >=13 PASS lines, got:\n{output}'
