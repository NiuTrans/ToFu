"""Regression test: a reloaded swarm panel must NOT show 0/N + "Unconfirmed" +
无结果 when the agents' completions were already proven by inbox-injects.

WHY (conv mr2ysg473scxv8)
-------------------------
A fire-and-forget swarm's per-agent completion lived ONLY in the live-only,
never-persisted ``round._swarmAgents`` map. After turn-rotation / reload the
panel rebuilds via ``_recoverSwarmAgents``, which — with no ``_swarmSnapshot``
and no sibling ``await_agents`` / ``get_agent_result`` rounds (there were none)
— stamped every agent ``status:"unknown"``. That drove all three symptoms:
0/N complete, the "Unconfirmed" limbo pill, and 无结果 cards — EVEN THOUGH the
model demonstrably received both agents' ``<swarm-update>`` results (the
"received … injected → context" chips prove it).

The completion proof survived reload the whole time: ``_handleSwarmInboxInject``
persists synthetic ``_inboxInject`` tool rows carrying ``inboxAgentIds`` into
the message's ``toolRounds``. The fix: ``_recoverSwarmAgents`` now reads them
(``_swarmInjectedAgentIds``) and treats an injected agentId as authoritative
``done``.

Runs the REAL shipped JS under jsdom; skips when node + jsdom aren't installed.
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
global.setInterval = win.setInterval = () => 0;
global.setTimeout = win.setTimeout = (fn) => 0;

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.t = global.t = (k) => String(k || '').split('.').pop();
win._TOOL_DISPLAY = global._TOOL_DISPLAY = {};

eval(fs.readFileSync(process.argv[4], 'utf8'));  // ui/streaming_swarm_panel.js
eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/streaming_ui.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

for (const fn of ['_buildSwarmPanelHTML', '_recoverSwarmAgents', '_swarmInjectedAgentIds']) {
  if (typeof eval(fn) !== 'function') { console.log('FAIL functions_exposed ' + fn + ' missing'); process.exit(0); }
}
check('functions_exposed', true);

const NOW = Date.now();
const FRESH = NOW - 5000;

/* Reproduce the mr2ysg473scxv8 reload state:
   - the spawn round: _swarmAgents GONE (live-only, not persisted); the
     persisted spawn handle JSON in toolContent still lists both agents;
     no _swarmSnapshot, no sibling await/get_agent_result rounds.
   - a persisted synthetic _inboxInject row per agent (what
     _handleSwarmInboxInject leaves behind) carrying inboxAgentIds. */
const spawnRound = {
  roundNum: 20, _swarm: true, _swarmActive: false, status: 'done',
  _swarmStartTime: FRESH,
  toolName: 'spawn_agents',
  toolContent: JSON.stringify({
    status: 'async_launched',
    agents: [
      { id: 'd5f50acc', role: 'coder', objective: 'audit A' },
      { id: 'b4989cda', role: 'coder', objective: 'audit B' },
    ],
  }),
  // NOTE: no _swarmAgents (lost on reload), no _swarmSnapshot.
};
const injectRowA = {
  roundNum: 9000001, status: 'done', _inboxInject: true,
  inboxAgentIds: ['b4989cda'],
};
const injectRowB = {
  roundNum: 9000002, status: 'done', _inboxInject: true,
  inboxAgentIds: ['d5f50acc'],
};
const allRounds = [spawnRound, injectRowA, injectRowB];

// ── 1. _swarmInjectedAgentIds harvests both ids from the persisted rows ──
const ids = _swarmInjectedAgentIds(allRounds);
check('injected_ids_harvested', ids.has('b4989cda') && ids.has('d5f50acc') && ids.size === 2);

// ── 2. _recoverSwarmAgents marks BOTH agents done (not unknown) ──
const recovered = _recoverSwarmAgents(spawnRound, allRounds);
check('recovered_two_agents', recovered.length === 2);
const byId = {}; for (const a of recovered) byId[a.id] = a;
check('agentA_done', byId['d5f50acc'] && byId['d5f50acc'].status === 'done');
check('agentB_done', byId['b4989cda'] && byId['b4989cda'].status === 'done');

// ── 3. The full panel: NOT 0/2, NOT Unconfirmed, NOT 无结果 ──
const html = _buildSwarmPanelHTML(spawnRound, allRounds);
check('panel_not_unconfirmed', !html.includes('Unconfirmed'));
check('panel_shows_complete', html.includes('Complete'));
check('panel_two_of_two', html.includes('2/2 agents complete'));
check('panel_not_zero_of_two', !html.includes('0/2 agents complete'));
check('panel_no_noresult', !html.includes('noResult'));  // t("swarm.phase.noResult") → "noResult"

// ── 4. Precedence: a real sibling result (failed) OVERRIDES an inbox-inject ──
//    An await/get_agent_result row proving failure must win over the coarse
//    "injected ⇒ done" signal.
const failRound = {
  roundNum: 21, toolName: 'get_agent_result', status: 'done',
  toolContent: JSON.stringify({ agent_id: 'b4989cda', found: true, error: 'boom', status: 'ok' }),
};
const recovered2 = _recoverSwarmAgents(spawnRound, [spawnRound, injectRowA, injectRowB, failRound]);
const byId2 = {}; for (const a of recovered2) byId2[a.id] = a;
check('sibling_result_overrides_inject', byId2['b4989cda'] && byId2['b4989cda'].status === 'failed');
check('other_agent_still_done_via_inject', byId2['d5f50acc'] && byId2['d5f50acc'].status === 'done');

// ── 5. NC ANCHOR: WITHOUT the inject rows, the agents recover as unknown →
//    the exact 0/2 + Unconfirmed + 无结果 bug. This documents the pre-fix
//    state; reverting the _recoverSwarmAgents inbox read reproduces it even
//    WITH the inject rows present (see the python-side NC bite). ──
const recoveredNoInject = _recoverSwarmAgents(spawnRound, [spawnRound]);
const allUnknown = recoveredNoInject.every(a => a.status === 'unknown');
check('nc_anchor_no_inject_is_unknown', recoveredNoInject.length === 2 && allUnknown);
const limboHtml = _buildSwarmPanelHTML(spawnRound, [spawnRound]);
check('nc_anchor_no_inject_unconfirmed', limboHtml.includes('Unconfirmed'));

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_swarm_inbox_inject_recovery():
    harness = os.path.join(HERE, '_swarm_inbox_recovery_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),            # argv[2]
             ROOT,                                                     # argv[3]
             os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js'),   # argv[4]
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
    assert not fails, 'Swarm inbox-recovery failures:\n' + output
    assert output.count('PASS') >= 13, f'expected >=13 PASS lines, got:\n{output}'
