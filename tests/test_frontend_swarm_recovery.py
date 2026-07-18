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
// i18n shim: the panel's agent-card renderer calls t("swarm.phase.*").
// Return the real English labels (mirrors static/js/i18n.js) so the
// panel renders the same user-visible text production ships.
const _SWARM_PHASE_EN = {
  'swarm.phase.thinking': 'Thinking…', 'swarm.phase.tool_use': 'Using tools',
  'swarm.phase.writing': 'Writing…', 'swarm.phase.searching': 'Searching…',
  'swarm.phase.coding': 'Coding…', 'swarm.phase.analyzing': 'Analyzing…',
  'swarm.phase.complete': 'Complete', 'swarm.phase.failed': 'Failed',
  'swarm.phase.error': 'Error', 'swarm.phase.queued': 'Queued',
  'swarm.phase.running': 'Working…', 'swarm.phase.noResult': 'No result',
};
win.t = global.t = (k) => _SWARM_PHASE_EN[k] || String(k || '').split('.').pop();
win._TOOL_DISPLAY = global._TOOL_DISPLAY = {};

eval(fs.readFileSync(process.argv[4], 'utf8'));  // ui/streaming_swarm_panel.js (swarm builders moved here 2026-06-27)
eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/streaming_ui.js
// ui/tool_rounds.js defines _isRoundSwarm (the panel render GATE). Guarded:
// it pulls in a lot of unrelated render helpers, but only _isRoundSwarm is
// needed here and it's a self-contained function declaration. A load error
// must not fail the whole harness — the gate check below is conditional.
try { eval(fs.readFileSync(process.argv[5], 'utf8')); } catch (e) {
  console.error('[harness] tool_rounds.js load skipped: ' + (e && e.message));
}

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

// ── 4. DURABLE SNAPSHOT (root-cause fix): a reloaded FIRE-AND-FORGET spawn
//      round — NO live _swarmAgents, NO await_agents sibling — carries only
//      the backend-persisted _swarmSnapshot. It must render REAL agent cards
//      (status/preview/tokens/modifiedFiles), NOT 'unknown' stubs. This is the
//      exact case the old recovery path could never satisfy. ──
const fafSpawnRound = {
  roundNum: 1, toolName: 'spawn_agents', _swarm: true, status: 'done',
  toolContent: JSON.stringify({
    status: 'async_launched', swarm_id: 'sw-faf',
    agents: [
      { id: 'aa11', role: 'researcher', objective: 'Survey A', output_file: '/x/a.log' },
      { id: 'bb22', role: 'coder', objective: 'Patch B', output_file: '/x/b.log' },
    ],
  }),
  // The durable snapshot the backend wrote on settle (lib/swarm/snapshot.py).
  _swarmSnapshot: {
    settled: true, agentCount: 2, totalTokens: 1600,
    agents: [
      { id: 'aa11', role: 'researcher', model: 'm', objective: 'Survey A',
        status: 'done', elapsed: 1.2, tokens: 700, preview: 'A FINDINGS BODY',
        modifiedFiles: 0, error: '' },
      { id: 'bb22', role: 'coder', model: 'm', objective: 'Patch B',
        status: 'done', elapsed: 2.5, tokens: 900, preview: 'B PATCH BODY',
        modifiedFiles: 2, error: '' },
    ],
  },
};
// Only the spawn round survives — no sibling await/get rounds at all.
const fafRounds = [fafSpawnRound];
const fafAgents = _recoverSwarmAgents(fafSpawnRound, fafRounds);
check('faf_recovers_both', fafAgents.length === 2);
const fafById = {}; for (const a of fafAgents) fafById[a.id] = a;
check('faf_real_status_not_unknown',
  fafById['aa11'] && fafById['aa11'].status === 'done'
  && fafById['bb22'] && fafById['bb22'].status === 'done');
check('faf_preview_from_snapshot',
  fafById['aa11'] && fafById['aa11'].preview === 'A FINDINGS BODY');
check('faf_modified_files', fafById['bb22'] && fafById['bb22'].modifiedFiles === 2);
const fafHtml = _buildSwarmPanelHTML(fafSpawnRound, fafRounds);
check('faf_panel_complete_pill', fafHtml.includes('Complete'));
check('faf_panel_renders_body', fafHtml.includes('B PATCH BODY') && fafHtml.includes('sw-agent'));
check('faf_panel_pencil_pill', fafHtml.includes('sw-a-edited'));   // bb22 edited 2 files
// The gate must consider a snapshot-only round renderable.
if (typeof _isRoundSwarm === 'function') {
  check('faf_isRoundSwarm_true', _isRoundSwarm(fafSpawnRound) === true);
}

// ── 5. SETTLED SNAPSHOT clears the stale-guard footguns (#9) ──
//   A round saved mid-flight (_swarmActive:true, ancient _swarmStartTime, no
//   _swarmEndTime) but carrying a settled:true snapshot must, after one
//   _buildSwarmPanelHTML, be stamped settled: _swarmActive cleared +
//   _swarmEndTime set — so the 30-min stale guard and the 1Hz ticker can
//   NEVER mis-fire it to "Stale"/runaway-timer. ──
const settledStaleRound = {
  roundNum: 1, toolName: 'spawn_agents', _swarm: true, status: 'searching',
  _swarmActive: true, _asyncRunning: true,
  _swarmStartTime: Date.now() - (40 * 60 * 1000),   // 40 min ago — past _SW_STALE_MS
  toolContent: JSON.stringify({ status: 'async_launched', swarm_id: 'sw-set',
    agents: [{ id: 'zz1', role: 'coder', objective: 'O' }] }),
  _swarmSnapshot: {
    settled: true, version: 100001, agentCount: 1, totalTokens: 10,
    agents: [{ id: 'zz1', role: 'coder', objective: 'O', status: 'done',
               elapsed: 1.0, tokens: 10, preview: 'OK', modifiedFiles: 0, error: '' }],
  },
};
const setHtml = _buildSwarmPanelHTML(settledStaleRound, [settledStaleRound]);
check('settled_clears_active', settledStaleRound._swarmActive === false
  && settledStaleRound._asyncRunning === false);
check('settled_stamps_endtime', !!settledStaleRound._swarmEndTime);
check('settled_renders_complete_not_stale',
  setHtml.includes('Complete') && !setHtml.includes('Stale'));
check('settled_no_live_ticker', !setHtml.includes('data-sw-start'));

// ── 6. BATCH get_agent_result: a single round whose payload carries a
//      `results` array (agent_ids batch mode) must enrich EVERY agent, same
//      as N separate single-agent get_agent_result rounds would. ──
const batchSpawnRound = {
  roundNum: 1, toolName: 'spawn_agents', _swarm: true, status: 'done',
  toolContent: JSON.stringify({
    status: 'async_launched', swarm_id: 'sw-batch',
    agents: [
      { id: 'ext', role: 'coder', objective: 'audit ext', output_file: '/x/ext.log' },
      { id: 'route', role: 'coder', objective: 'audit route', output_file: '/x/route.log' },
      { id: 'fe', role: 'coder', objective: 'audit fe', output_file: '/x/fe.log' },
    ],
  }),
};
const batchGarRound = {
  roundNum: 2, toolName: 'get_agent_result', status: 'done',
  toolContent: JSON.stringify({
    status: 'ok',
    results: [
      { found: true, agent_id: 'ext', role: 'coder', objective: 'audit ext',
        status: 'ok', final_answer: 'EXT REPORT', error: '', elapsed: '10', tokens: '100' },
      { found: true, agent_id: 'route', role: 'coder', objective: 'audit route',
        status: 'ok', final_answer: 'ROUTE REPORT', error: '', elapsed: '20', tokens: '200' },
      { found: true, agent_id: 'fe', role: 'coder', objective: 'audit fe',
        status: 'ok', final_answer: 'FE REPORT', error: '', elapsed: '30', tokens: '300' },
    ],
  }),
};
const batchRounds = [batchSpawnRound, batchGarRound];
const batchAgents = _recoverSwarmAgents(batchSpawnRound, batchRounds);
const batchById = {}; for (const a of batchAgents) batchById[a.id] = a;
check('batch_recovers_all_three', batchAgents.length === 3);
check('batch_ext_body', batchById['ext'] && batchById['ext'].preview === 'EXT REPORT'
  && batchById['ext'].status === 'completed');
check('batch_route_body', batchById['route'] && batchById['route'].preview === 'ROUTE REPORT');
check('batch_fe_body', batchById['fe'] && batchById['fe'].preview === 'FE REPORT');
const batchHtml = _buildSwarmPanelHTML(batchSpawnRound, batchRounds);
check('batch_panel_renders_all_bodies',
  batchHtml.includes('EXT REPORT') && batchHtml.includes('ROUTE REPORT') && batchHtml.includes('FE REPORT'));

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
             os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js'),  # argv[4]
             os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),            # argv[5]
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
    assert output.count('PASS') >= 25, f'expected >=25 PASS lines, got:\n{output}'
