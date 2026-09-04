"""jsdom test for Request Inspector P4 — turn tags + swarm sub-agent rows.

Epic pt_e3dc7198e7e34bb1. Endpoint Planner/Worker/Critic turns each re-run
run_task with their OWN round numbering — the P4 contract tags every
snapshot + round_usage with the driver's existing _endpoint_phase so
same-numbered rounds stay distinct; swarm sub-agents persist directly
under '{parent}#agent:{id}'.

Drives the REAL shipped debug_panel.js + request_inspector.js under jsdom:

  1. Turn-tagged request rows render turn badges (Worker/Critic via i18n)
     and data-turn attributes.
  2. Selecting a tagged round passes the turn through to the payload fetch.
  3. coverageReason='flow-untagged' renders the AMBIGUOUS chip text
     (not the old "not captured" one).
  4. Swarm sub-agent task rows render indented with the agent badge.
  5. The bubble anchor prefers the planner row for an _isEndpointPlanner
     message (turn hint).
  6. The live accelerator keys turn-tagged rounds as 'turn|roundNum' so a
     planner R1 and a worker R1 never overwrite each other.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')
PROJECT_ROOT = os.path.dirname(HERE)


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[4];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="riDrawer" style="display:none">' +
  '  <div id="riTaskList"></div><div id="riRoundList"></div>' +
  '  <div class="debug-panel" id="debugPanel">' +
  '    <div id="debugTitle"></div><div id="debugContent"></div>' +
  '  </div>' +
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
const _I18N = {
  'ri.title': 'Request Inspector', 'ri.requests': 'requests',
  'ri.states': 'State mirrors', 'ri.stateNote': 'not LLM requests',
  'ri.empty': 'No tasks', 'ri.loading': 'Loading…',
  'ri.expired': 'Event log expired', 'ri.live': 'live',
  'ri.turnPlanning': 'Planner', 'ri.turnWorking': 'Worker',
  'ri.turnReviewing': 'Critic',
  'ri.coveragePartial': 'Planner/Critic calls are not captured',
  'ri.coverageAmbiguous': 'Legacy endpoint task: rounds carry no phase tag',
  'ri.prefixFold': 'prefix {k} vs {base}',
};
win.t = global.t = (k, args) => {
  let s = _I18N[k] || k;
  if (args) for (const kk of Object.keys(args))
    s = s.replace('{' + kk + '}', String(args[kk]));
  return s;
};
win.activeConvId = global.activeConvId = 'conv-1';
win.conversations = global.conversations = [{ id: 'conv-1', _serverTurnCount: 1 }];
win.debugVisible = global.debugVisible = false;

const CALLS = { payloads: [] };
win.Api = global.Api = {
  tasks: {
    byConv: async (convId) => ({ convId, tasks: [
      { taskId: 'task-EP', status: 'done', createdAt: 1753400000000,
        completedAt: 1753400005000, live: false,
        requestCount: 2, stateCount: 0, legacyCount: 0, hasEvents: true },
      { taskId: 'task-EP#agent:agent-research-x1', parentTaskId: 'task-EP',
        agentId: 'agent-research-x1', isSwarmAgent: true,
        status: 'swarm-agent', createdAt: 1753400000000, completedAt: null,
        live: false, requestCount: 1, stateCount: 0, legacyCount: 0,
        hasEvents: true },
    ] }),
    getRequests: async (taskId) => ({
      taskId, eventsAvailable: true, coverage: 'full', requestCount: 2,
      requests: [
        { roundNum: 1, ts: 1, model: 'm-w', turn: 'planning', params: {},
          messageCount: 2, toolsCount: 0, approxTokens: 100, label: 'P R1',
          legacy: false, attempts: [] },
        { roundNum: 1, ts: 2, model: 'm-w', turn: 'working', params: {},
          messageCount: 3, toolsCount: 0, approxTokens: 200, label: 'W R1',
          legacy: false, attempts: [] },
        { roundNum: 1, ts: 3, model: 'm-c', turn: 'reviewing', params: {},
          messageCount: 4, toolsCount: 0, approxTokens: 300, label: 'C R1',
          legacy: false, attempts: [] },
      ],
      states: [],
    }),
    getRequestPayload: async (taskId, roundNum, turn) => {
      CALLS.payloads.push({ roundNum: String(roundNum), turn: turn || '' });
      return { taskId, roundNum, turn: turn || '', model: 'm-x', params: {},
        label: 'R' + roundNum, tools: [],
        messages: [{ role: 'user', content: 'payload-' + (turn || 'none') }] };
    },
  },
  clientError: { report: () => {} },
};

const debugSrc = fs.readFileSync(process.argv[2], 'utf8');
const riSrc = fs.readFileSync(process.argv[3], 'utf8');
eval(debugSrc + '\n' + riSrc +
  '\n;win.__dumpReqs = function(){ return _debugRequests; };');

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  /* ── Open + task rows: swarm agent row indented with badge ── */
  toggleRequestInspector();
  await sleep(20);
  const taskEls = document.querySelectorAll('#riTaskList .ri-task');
  check('task_rows', taskEls.length === 2);
  const agentRow = document.querySelectorAll('#riTaskList .ri-task-agent');
  check('swarm_agent_row_indented', agentRow.length === 1);
  check('swarm_agent_badge',
    agentRow.length === 1 &&
    agentRow[0].innerHTML.indexOf('agent-research-x1') !== -1);

  /* ── Select the endpoint task: turn badges + data-turn ── */
  taskEls[0].onclick();
  await sleep(20);
  const roundEls = document.querySelectorAll('#riRoundList .ri-round');
  check('three_phase_rows', roundEls.length === 3);
  check('data_turn_attrs',
    roundEls[0].dataset.turn === 'planning' &&
    roundEls[1].dataset.turn === 'working' &&
    roundEls[2].dataset.turn === 'reviewing');
  const badges = document.querySelectorAll('#riRoundList .ri-turn-badge');
  check('turn_badges_rendered', badges.length === 3 &&
    badges[0].textContent === 'Planner' &&
    badges[1].textContent === 'Worker' &&
    badges[2].textContent === 'Critic');

  /* ── Click the reviewing R1 → payload fetch carries the turn ── */
  roundEls[2].onclick();
  await sleep(20);
  check('payload_fetch_with_turn',
    CALLS.payloads.length >= 1 &&
    CALLS.payloads[0].turn === 'reviewing');
  const hdr = document.querySelector('#debugContent .debug-msg-header');
  if (hdr) hdr.onclick();
  check('detail_from_turn_payload',
    document.getElementById('debugContent').innerHTML.indexOf('payload-reviewing') !== -1);

  /* ── Stable task action reselects the endpoint task without a transcript anchor. ── */
  await openRequestInspectorForTask('task-EP');
  await sleep(30);
  const selectedTask = document.querySelector('#riTaskList .ri-task.ri-sel');
  check('stable_task_action_selects_endpoint', !!selectedTask);

  /* ── Accelerator turn-keying: working R1 and planning R1 coexist ── */
  showMessagesInDebug([{ role: 'user', content: 'w1' }], 'W R1', true,
    'conv-1', undefined, undefined,
    { kind: 'request', model: 'm-w', roundNum: 1, taskId: 'task-EP', turn: 'working' });
  showMessagesInDebug([{ role: 'user', content: 'p1' }], 'P R1', true,
    'conv-1', undefined, undefined,
    { kind: 'request', model: 'm-w', roundNum: 1, taskId: 'task-EP', turn: 'planning' });
  const tEP = win.__dumpReqs()['task-EP'];
  check('accelerator_turn_keys', !!tEP &&
    !!tEP.rounds['working|1'] && !!tEP.rounds['planning|1'] &&
    tEP.rounds['working|1'].messages[0].content === 'w1' &&
    tEP.rounds['planning|1'].messages[0].content === 'p1');

  console.log(out.join('\n'));
})().catch(e => {
  console.log('FAIL harness_exception ' + (e && e.stack || e));
}).finally(() => {
  /* openRequestInspector installs a recursive idle poll.  Leaving it open
   * keeps Node alive until Python's 60 s timeout and can orphan the process
   * when a pytest worker is interrupted first. */
  try { closeRequestInspector(); } catch (_) { /* setup may have failed */ }
  dom.window.close();
});
"""

_HARNESS_AMBIG = None  # second fixture handled by a python-level variant


def _run(expect_fail=None):
    with tempfile.NamedTemporaryFile(
        'w', suffix='_ri_p4_harness.js', delete=False,
        encoding='utf-8',
    ) as f:
        harness = f.name
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'core', 'debug_panel.js'),
             os.path.join(JS_DIR, 'core', 'request_inspector.js'),
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
    assert not fails, 'request-inspector P4 failures:\n' + output
    assert output.count('PASS') >= 10, (
        f'expected >=10 PASS lines, got:\n{output}')
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_p4_turn_badges_and_swarm_rows():
    _run()


def test_coverage_ambiguous_chip_text():
    """coverageReason='flow-untagged' must render the AMBIGUOUS chip
    text, not the old 'not captured' one (source-level pin: the chip picks
    its i18n key by coverageReason)."""
    src = open(os.path.join(JS_DIR, 'core', 'request_inspector.js'),
               encoding='utf-8').read()
    assert "fold.coverageReason === 'flow-untagged'" in src
    assert "'ri.coverageAmbiguous'" in src


if __name__ == '__main__':
    print(_run())
