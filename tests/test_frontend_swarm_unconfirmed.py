"""Regression test: the swarm panel's "Unconfirmed"（无结果）limbo must
self-resolve against backend truth — and genuine errors must surface.

WHY
---
The "Parallel Execution" panel rendered "Unconfirmed" whenever it had agent
cards but no terminal answer and no settled snapshot. Three root causes are
pinned here (backend contract lives in tests/test_swarm_status_truthfulness.py):

  1. FALSE SETTLE — the reconciler probed /api/v1/swarm/status with a guessed
     task id; an alias miss answered active:false for a LIVE swarm and the
     one-shot latch settled every agent to unknown. The probe key must be the
     backend-stamped ``_swarmKey`` (falling back to the conv id, which IS the
     conv-scoped session key), and an ambiguous answer (``active:null /
     known:false``) must NEVER settle — it keeps probing, and only settles as
     honestly-unknown after repeated unknowns over a real time window.
  2. RELOAD BLINDNESS — post-reload panels lost the live flags and were never
     probed at all; the reconciler now recovers the roster (and writes it
     back) and probes unsettled panels too.
  3. SILENT DRIVER CRASH — a crashed driver emitted no UI event; the panel
     now renders Failed-with-reason from ``_swarmError`` (wired by the
     ``swarm_phase:error`` event), outranking the Unconfirmed branch.

Runs the REAL shipped JS under jsdom; skips cleanly when node + jsdom aren't
installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = [pytest.mark.unit, pytest.mark.serial]

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')
_NODE_HARNESS_TIMEOUT_S = 180


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
global.setInterval = win.setInterval = () => 0;   // neuter tickers; we drive the reconciler manually
let scheduledTimeouts = 0;
global.setTimeout = win.setTimeout = (fn) => { scheduledTimeouts += 1; return 0; };

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
win.t = global.t = (k) => String(k || '').split('.').pop();
win._TOOL_DISPLAY = global._TOOL_DISPLAY = {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/streaming_swarm_panel.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

for (const fn of ['_buildSwarmPanelHTML', '_settleStuckSwarmRound',
                  '_reconcileStuckSwarmPanels', '_swarmRoundTaskId']) {
  if (typeof eval(fn) !== 'function') { console.log('FAIL functions_exposed ' + fn + ' missing'); process.exit(0); }
}
check('functions_exposed', true);

const NOW = Date.now();
const OLD = NOW - 300000;   // 5 min ago — comfortably past the 60s unknown-settle age

// ── 1. Driver-error pill: Failed-with-reason, NEVER Unconfirmed ──
const errRound = {
  roundNum: 1, _swarm: true, _swarmActive: false, status: 'done',
  _swarmStartTime: OLD, _swarmEndTime: OLD + 30000,
  _swarmError: 'RuntimeError: driver boom',
  _swarmAgents: [{ id: 'e1', role: 'coder', objective: 'x', status: 'unknown', phase: 'unknown' }],
};
const errHtml = _buildSwarmPanelHTML(errRound, [errRound]);
check('error_pill_failed', errHtml.includes('Failed'));
check('error_pill_reason', errHtml.includes('driver boom'));
check('error_pill_not_unconfirmed', !errHtml.includes('Unconfirmed'));

// ── 2. Probe-key precedence: _swarmKey → conv.id → spawning task id ──
check('probekey_prefers_swarmkey',
  _swarmRoundTaskId({ _taskId: 'task-spawn' }, { id: 'conv1', activeTaskId: 'task-new' },
                    { _swarmKey: 'conv-key-1' }) === 'conv-key-1');
check('probekey_conv_id_is_swarm_key',
  _swarmRoundTaskId({ _taskId: 'task-spawn' }, { id: 'conv1', activeTaskId: 'task-new' }, {}) === 'conv1');
check('probekey_falls_back_spawn_task',
  _swarmRoundTaskId({ _taskId: 'task-spawn' }, null, {}) === 'task-spawn');

// ── 3. Reconciler three-state behavior ──
let probeIds = [];
let answer = { active: null, known: false };
win.Api = global.Api = { swarm: { status: async (id) => { probeIds.push(id); return answer; } } };
win.activeStreams = global.activeStreams = new Set();
win.activeConvId = 'convA';
win.saveConversations = global.saveConversations = () => {};

/* The reconciler consumes Turn-native presentation ownership. Keep the old
   message fixtures only as convenient input builders; this adapter snapshots
   each one into an immutable durable Turn and records copied overlays. */
const durableTurns = new Map();
const overlayTurns = new Map();
function clone(value) { return JSON.parse(JSON.stringify(value)); }
function durableTurn(conv) {
  let turn = durableTurns.get(conv.id);
  if (turn) return turn;
  const message = conv._testProjections[0] || {};
  const projection = clone(message);
  delete projection.role;
  delete projection._turnId;
  turn = {
    turnId: 'turn-' + conv.id, actor: 'assistant', kind: 'reply', laneId: 'main',
    parentTurnId: null, status: conv._turnStatus || 'completed',
    currentAttemptId: null, projectionRevision: 1,
    projection, settlement: {}, createdAt: 1,
  };
  durableTurns.set(conv.id, turn);
  return turn;
}
const swarmPresentation = {
  candidates(conv) {
    const durable = durableTurn(conv);
    return [overlayTurns.get(conv.id) || durable];
  },
  update(conv, turnId, updateProjection) {
    const durable = durableTurn(conv);
    const source = overlayTurns.get(conv.id) || durable;
    if (source.turnId !== turnId) return null;
    const overlay = clone(source);
    overlay.projectionRevision += 1;
    if (updateProjection(overlay.projection, overlay) === false) return null;
    overlayTurns.set(conv.id, overlay);
    return overlay;
  },
};
win.ConversationSwarmPresentation = global.ConversationSwarmPresentation = swarmPresentation;
function currentRound(conv, index = 0) {
  return swarmPresentation.candidates(conv)[0].projection.toolRounds[index];
}

// Post-reload limbo panel: no live flags, unresolved agents, no settled snapshot.
const convA = { id: 'convA', _testProjections: [{ role: 'assistant', _taskId: 'task-spawnA',
  toolRounds: [{ roundNum: 1, _swarm: true, status: 'done', _swarmStartTime: OLD,
    _swarmAgents: [{ id: 'u1', role: 'researcher', objective: 'x', status: 'unknown', phase: 'unknown' }] }] }] };
win.conversations = global.conversations = [convA];
const roundA = convA._testProjections[0].toolRounds[0];

(async () => {
  // 3a. ambiguous answer → probe used the conv-scoped key; NO settle, NO latch
  await _reconcileStuckSwarmPanels();
  check('probe_uses_conv_key', probeIds[probeIds.length - 1] === 'convA');
  check('ambiguous_not_settled', !currentRound(convA)._swarmEndTime);
  check('ambiguous_durable_unchanged', !roundA._swarmEndTime);
  check('ambiguous_counted', probeIds.filter(id => id === 'convA').length === 1);

  // 3b. second ambiguous → still open; third (age > 60s) → honestly-unknown settle
  await _reconcileStuckSwarmPanels();
  check('second_ambiguous_still_open', !currentRound(convA)._swarmEndTime);
  await _reconcileStuckSwarmPanels();
  check('third_unknown_settles', !!currentRound(convA)._swarmEndTime);
  check('third_unknown_agent_honest', currentRound(convA)._swarmAgents[0].status === 'unknown');

  // 3c. definitive terminated answer carries truth incl. the failure reason
  const convB = { id: 'convB', _testProjections: [{ role: 'assistant', _taskId: 'task-spawnB',
    toolRounds: [{ roundNum: 1, _swarm: true, _swarmActive: true, status: 'searching',
      _swarmStartTime: NOW - 5000,
      _swarmAgents: [{ id: 'f1', role: 'coder', objective: 'y', status: 'running', phase: 'tool_use' }] }] }] };
  conversations.push(convB);
  answer = { active: false, known: true, terminated: true,
             agents: [{ id: 'f1', status: 'failed', error: 'provider 429 storm' }] };
  await _reconcileStuckSwarmPanels();
  const roundB = currentRound(convB);
  check('terminated_settles_first_answer', !!roundB._swarmEndTime);
  check('terminated_agent_failed', roundB._swarmAgents[0].status === 'failed'
        && roundB._swarmAgents[0].phase === 'error');
  check('terminated_error_carried', roundB._swarmAgents[0].preview === 'provider 429 storm');

  // 3d. active:true stamps the liveness confirmation and resets the streak
  const convC = { id: 'convC', _testProjections: [{ role: 'assistant',
    toolRounds: [{ roundNum: 1, _swarm: true, _swarmActive: true, status: 'searching',
      _swarmStartTime: NOW - 5000, _swReconcileUnknowns: 2,
      _swarmAgents: [{ id: 'r1', role: 'coder', objective: 'z', status: 'running' }] }] }] };
  conversations.push(convC);
  answer = { active: true, known: true };
  await _reconcileStuckSwarmPanels();
  const roundC = currentRound(convC);
  check('active_confirms_liveness', typeof roundC._swActiveConfirmedAt === 'number');
  check('active_uses_overlay', overlayTurns.has('convC'));
  check('active_not_settled', !roundC._swarmEndTime);

  // 3e. post-reload roster recovery: no _swarmAgents, only the persisted
  //     spawn handle → the reconciler recovers the roster, probes, settles
  //     with the backend's real per-agent outcome.
  const convD = { id: 'convD', _testProjections: [{ role: 'assistant',
    toolRounds: [{ roundNum: 1, _swarm: true, toolName: 'spawn_agents', status: 'done',
      _swarmStartTime: OLD,
      toolContent: JSON.stringify({ agents: [{ id: 'h1', objective: 'recovered roster' }] }) }] }] };
  conversations.push(convD);
  answer = { active: false, known: true, terminated: true,
             agents: [{ id: 'h1', status: 'completed' }] };
  await _reconcileStuckSwarmPanels();
  const roundD = currentRound(convD);
  check('reload_roster_recovered', (roundD._swarmAgents || []).some(a => a.id === 'h1'));
  check('reload_settled_with_truth', !!roundD._swarmEndTime
        && roundD._swarmAgents.find(a => a.id === 'h1').status === 'done');

  // 3f. RESURRECT — a reloaded panel (no live flags, no start time) probing
  //     active:true re-attaches: live flag restored, start time backfilled
  //     from created_at, backend per-agent rows applied, spawn_more agent
  //     grafted, and the pill leaves the Unconfirmed limbo for Running.
  const convE = { id: 'convE', _testProjections: [{ role: 'assistant',
    toolRounds: [{ roundNum: 1, _swarm: true, status: 'done',
      _swarmAgents: [{ id: 'g1', role: 'researcher', objective: 'x', status: 'unknown', phase: 'unknown' }] }] }] };
  conversations.push(convE);
  answer = { active: true, known: true, created_at: (NOW - 35000) / 1000,
             agents: [{ id: 'g1', status: 'running' },
                      { id: 'g2', role: 'coder', objective: 'wave2', status: 'pending' }] };
  await _reconcileStuckSwarmPanels();
  const roundE = currentRound(convE);
  check('resurrect_active_flag', roundE._swarmActive === true);
  check('resurrect_not_settled', !roundE._swarmEndTime);
  check('resurrect_starttime_backfilled',
        Math.abs(roundE._swarmStartTime - (NOW - 35000)) < 2000);
  check('resurrect_agent_running', roundE._swarmAgents[0].status === 'running'
        && roundE._swarmAgents[0].phase === 'running');
  check('resurrect_graft_spawn_more',
        (roundE._swarmAgents || []).some(a => a.id === 'g2' && a.status === 'pending'));
  check('resurrect_pill_running',
        _buildSwarmPanelHTML(roundE, [roundE]).includes('sw-pill-running'));

  // 3g. The resurrected panel stays a probe candidate and settles on the
  //     terminal answer through the normal path — with the failure reason.
  answer = { active: false, known: true, terminated: true,
             agents: [{ id: 'g1', status: 'completed' },
                      { id: 'g2', status: 'failed', error: 'boom' }] };
  await _reconcileStuckSwarmPanels();
  const settledRoundE = currentRound(convE);
  check('resurrect_then_settles', !!settledRoundE._swarmEndTime);
  check('resurrect_settle_done',
        settledRoundE._swarmAgents.find(a => a.id === 'g1').status === 'done');
  check('resurrect_settle_error_carried',
        settledRoundE._swarmAgents.find(a => a.id === 'g2').error === 'boom');

  // 3h. Rendering the Unconfirmed branch schedules a fast first probe (the
  //     throttle stamp is set; the harness-neutered setTimeout never fires).
  const convF = { id: 'convF', _testProjections: [{ role: 'assistant',
    toolRounds: [{ roundNum: 1, _swarm: true, status: 'done', _swarmStartTime: NOW - 40000,
      _swarmAgents: [{ id: 'q1', role: 'researcher', objective: 'x', status: 'unknown', phase: 'unknown' }] }] }] };
  const roundF = convF._testProjections[0].toolRounds[0];
  const timeoutsBefore = scheduledTimeouts;
  const htmlF = _buildSwarmPanelHTML(roundF, [roundF]);
  check('unconfirmed_pill_rendered', htmlF.includes('Unconfirmed'));
  check('unconfirmed_fast_probe_scheduled', scheduledTimeouts === timeoutsBefore + 1);
  check('unconfirmed_render_is_pure', roundF._swFastProbeAt === undefined);

  // 3i. STREAMING-conv per-round gate (the 2026-08-23 flapping defect's
  //     second half): a round whose live flags were never stamped (or were
  //     wiped by a projection frame) on a STREAMING conv is owned by no one
  //     — the old conv-level skip left it limbo for the whole stream. It
  //     must now be probed; a live-flagged round on the SAME conv stays
  //     SSE-owned and is NOT probed.
  const convG = { id: 'convG', _turnStatus: 'running', _testProjections: [{ role: 'assistant',
    toolRounds: [
      { roundNum: 1, _swarm: true, status: 'done', _swarmStartTime: OLD,
        _swarmAgents: [{ id: 's1', role: 'coder', objective: 'x', status: 'unknown', phase: 'unknown' }] },
      { roundNum: 2, _swarm: true, _swarmActive: true, status: 'searching',
        _swarmStartTime: NOW - 5000,
        _swarmAgents: [{ id: 's2', role: 'coder', objective: 'y', status: 'running' }] },
    ] }] };
  conversations.push(convG);
  activeStreams.add('convG');
  answer = { active: false, known: true, terminated: true,
             agents: [{ id: 's1', status: 'completed' }] };
  await _reconcileStuckSwarmPanels();
  const roundG1 = currentRound(convG, 0);
  const roundG2 = currentRound(convG, 1);
  check('streaming_flagless_round_probed', probeIds.includes('convG'));
  check('streaming_flagless_settles_with_truth',
        !!roundG1._swarmEndTime
        && roundG1._swarmAgents.find(a => a.id === 's1').status === 'done');
  check('streaming_flagged_round_stays_sse_owned',
        roundG2._swarmActive === true && !roundG2._swarmEndTime
        && !overlayTurns.get('convG').projection.toolRounds[1]._swarmEndTime);
  check('streaming_flagged_agent_untouched',
        roundG2._swarmAgents[0].status === 'running');
  check('durable_turn_never_mutated',
        durableTurn(convG).projection.toolRounds[0]._swarmEndTime === undefined);
  activeStreams.delete('convG');
  console.log(out.join('\n'));
})();
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_swarm_unconfirmed_reconcile_three_state():
    with tempfile.TemporaryDirectory(prefix='tofu-swarm-unconfirmed-') as temp_dir:
        harness = os.path.join(temp_dir, 'swarm_unconfirmed_harness.js')
        with open(harness, 'w') as f:
            f.write(_HARNESS)
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js'),  # argv[2]
             ROOT,                                            # argv[3]
             ],
            capture_output=True, text=True,
            timeout=_NODE_HARNESS_TIMEOUT_S,
        )
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'Swarm unconfirmed-reconcile failures:\n' + output
    assert output.count('PASS') >= 32, f'expected >=32 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_ambiguous_answer_settle_guard_is_load_bearing():
    """NEUTER: restore two-state probing (any non-active answer settles) and
    prove the ambiguous-answer case then FALSE-settles on the first probe —
    the exact live-swarm-mislabeled-Unconfirmed bug this repair removes."""
    src_path = os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js')
    with open(src_path, encoding='utf-8') as f:
        src = f.read()
    needle = 'if (status.active === false && status.known !== false) {'
    assert needle in src, 'settle guard shape changed — update this neuter test'
    neutered_src = src.replace(needle, 'if (status.active !== true) {', 1)
    assert neutered_src != src, 'neuter did not modify the source'

    with tempfile.TemporaryDirectory(
            prefix='tofu-swarm-unconfirmed-neuter-') as temp_dir:
        neutered_path = os.path.join(temp_dir, 'streaming_swarm_panel.js')
        harness = os.path.join(temp_dir, 'swarm_unconfirmed_neuter_harness.js')
        with open(neutered_path, 'w', encoding='utf-8') as f:
            f.write(neutered_src)
        with open(harness, 'w') as f:
            f.write(_HARNESS)
        proc = subprocess.run(
            ['node', harness,
             neutered_path,                                   # argv[2] — neutered
             ROOT,                                            # argv[3]
             ],
            capture_output=True, text=True,
            timeout=_NODE_HARNESS_TIMEOUT_S,
        )
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    # Two-state probing settles the ambiguous answer immediately → the
    # "ambiguous_not_settled" check must FAIL.
    assert 'FAIL ambiguous_not_settled' in output, \
        'NC (two-state probing) should false-settle on an ambiguous answer:\n' + output
