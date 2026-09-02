"""jsdom test for the Request Inspector drawer (P2).

Design: docs/DEBUG_PANEL_REDESIGN.md (owner-ratified form A). Drives the
REAL shipped static/js/core/debug_panel.js + core/request_inspector.js
under jsdom:

  1. toggleDebug() now opens the RIGHT-SIDE DRAWER (body.ri-open +
     #riDrawer visible) — the global floating box is retired.
  2. Task rows render from Api.tasks.byConv (SERVER-authoritative), with
     plain-language status / action / expired state (no request-count jargon).
  3. Selecting a task folds request rows (R1/R2) + attempts + the coverage
     chip for endpoint-driven tasks; state mirrors render SEPARATELY
     (never mixed into the request list).
  4. Selecting a round renders the detail via showMessagesInDebug (the ONE
     renderer — no second JSON viewer) from the on-demand payload fetch.
  5. LIVE ACCELERATOR: a round already in _debugRequests (SSE-fed) renders
     WITHOUT any server fetch.
  6. MEMORY CAP (owner P2 constraint): recording a round for task B strips
     task A's payloads to metadata-only (`_stripped` flag).
  7. closeDebug() hides the drawer.

NEUTER: make _riSelectRound return early in a COPY → the detail-render
probe flips red, proving the delegation is load-bearing.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')


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
  'ri.title': 'Run details',
  'ri.states': 'Tool-result records',
  'ri.stateNote': 'for troubleshooting only',
  'ri.empty': 'No tasks recorded',
  'ri.loading': 'Loading…',
  'ri.expired': 'Event log expired',
  'ri.coveragePartial': 'endpoint partial',
  'ri.selectTask': 'Select a run to see what it did',
  'ri.detailTitle': 'Turn details',
  'ri.selectRound': 'Expand Technical details and select a turn.',
  'ri.viewProcess': 'View what happened',
  'ri.summaryLabel': 'Run summary',
  'ri.statusRunning': 'Task in progress',
  'ri.statusDone': 'Task completed',
  'ri.statusFailed': 'Task did not complete',
  'ri.statusStopped': 'Task stopped',
  'ri.statusUnknown': 'Status unknown',
  'ri.operationCountOne': 'Completed 1 operation',
  'ri.operationCount': 'Completed {n} operations',
  'ri.operationCountApproxOne': 'Completed about 1 operation',
  'ri.operationCountApprox': 'Completed about {n} operations',
  'ri.noOperations': 'No tool operations were run',
  'ri.operationUnavailable': 'Operation count unavailable',
  'ri.operationHelp': 'Counts real tool actions. System logs are not counted.',
  'ri.taskLabel': 'Task {id}',
  'ri.technicalDetails': 'View technical details',
  'ri.modelTurns': '{n} model turns',
  'ri.roundNumber': 'Turn {n}',
  'ri.roundMeta': '{messages} messages · ~{tokens} tokens',
  'ri.availableTools': '{n} tools available',
};
win.t = global.t = (k, vars) => {
  let value = _I18N[k] || k;
  for (const [name, replacement] of Object.entries(vars || {}))
    value = value.replaceAll('{' + name + '}', String(replacement));
  return value;
};
win.activeConvId = global.activeConvId = 'conv-1';
win.conversations = global.conversations = [{ id: 'conv-1' }];
win.debugVisible = global.debugVisible = false;

/* ── Api stub (server-authoritative fixtures) ── */
const CALLS = { byConv: 0, getRequests: 0, getRequestPayload: 0 };
win.Api = global.Api = {
  tasks: {
    byConv: async (convId) => {
      CALLS.byConv++;
      return { convId, tasks: [
        { taskId: 'task-AAA', status: 'done', createdAt: 1753400000000,
          completedAt: 1753400005000, live: false,
          requestCount: 2, stateCount: 1, legacyCount: 0, hasEvents: true },
        { taskId: 'task-OLD', status: 'done', createdAt: 1753390000000,
          completedAt: 1753390005000, live: false,
          requestCount: 0, stateCount: 0, legacyCount: 0, hasEvents: false },
      ] };
    },
    getRequests: async (taskId) => {
      CALLS.getRequests++;
      if (taskId === 'task-OLD') return {
        taskId, eventsAvailable: false, requestCount: 0,
        operationCount: 0, operationCountAvailable: false,
        operationCountApproximate: false,
        requests: [], states: [], coverage: 'full',
      };
      return {
        taskId, eventsAvailable: true, coverage: 'partial', requestCount: 2,
        operationCount: 2, operationCountApproximate: false,
        operationCountAvailable: true,
        requests: [
          { roundNum: 1, ts: 1753400001000, model: 'm-x',
            params: { maxTokens: 1000 }, messageCount: 3, toolsCount: 2,
            approxTokens: 1200, label: 'Round 1 请求前 · 3条', legacy: false,
            attempts: [
              { tag: 'R1', model: 'm-x', tokensIn: 500, tokensOut: 100,
                traceId: 'tr-1', streamElapsedMs: 2100, cacheRead: 0,
                cacheWrite: 0, ts: 1753400002000 },
              { tag: 'R1-FALLBACK', model: 'm-fb', tokensIn: 500,
                tokensOut: 90, traceId: 'tr-1fb', streamElapsedMs: 3100,
                cacheRead: 0, cacheWrite: 0, ts: 1753400003000 },
            ] },
          { roundNum: 2, ts: 1753400004000, model: 'm-x',
            params: { maxTokens: 1000 }, messageCount: 5, toolsCount: 2,
            approxTokens: 2400, label: 'Round 2 请求前 · 5条', legacy: false,
            attempts: [] },
        ],
        states: [
          { roundNum: 'final', label: '最终回复后 · 6条', messageCount: 6,
            ts: 1753400005000, legacy: false },
        ],
      };
    },
    getRequestPayload: async (taskId, roundNum) => {
      CALLS.getRequestPayload++;
      return { taskId, roundNum, model: 'm-x', params: {},
        label: 'Round ' + roundNum + ' 请求前', tools: [],
        messages: [{ role: 'user', content: 'payload-from-server' }] };
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

(async () => {
  check('fn_present', typeof toggleDebug === 'function' &&
    typeof toggleRequestInspector === 'function');

  /* ── 1. toggleDebug opens the DRAWER, not the floating box ── */
  toggleDebug();
  await new Promise(r => setTimeout(r, 10));
  check('drawer_opens', document.body.classList.contains('ri-open') &&
    document.getElementById('riDrawer').style.display === 'flex');
  check('byconv_called', CALLS.byConv === 1);
  check('detail_empty_state_is_actionable',
    document.getElementById('debugTitle').textContent === 'Turn details' &&
    document.getElementById('debugContent').textContent.indexOf('Expand Technical details') !== -1);

  /* ── 2. Task rows (server-authoritative) ── */
  const taskEls = document.querySelectorAll('#riTaskList .ri-task');
  check('task_rows_rendered', taskEls.length === 2);
  check('task_request_count_hidden',
    taskEls.length && taskEls[0].textContent.indexOf('2 requests') === -1);
  check('task_action_is_plain_language',
    taskEls.length && taskEls[0].textContent.indexOf('View what happened') !== -1);
  check('expired_task_marked',
    taskEls.length === 2 && taskEls[1].innerHTML.indexOf('Event log expired') !== -1);

  /* ── 3. Select task → request rows + attempts + coverage chip + states ── */
  taskEls[0].onclick();
  await new Promise(r => setTimeout(r, 10));
  check('getrequests_called', CALLS.getRequests === 1);
  const summary = document.querySelector('#riRoundList .ri-summary');
  check('human_summary_is_primary', !!summary &&
    summary.querySelector('.ri-summary-count').textContent === 'Completed 2 operations' &&
    summary.querySelector('.ri-summary-help').textContent.indexOf('System logs are not counted') !== -1);
  const technical = document.querySelector('#riRoundList .ri-technical');
  check('technical_details_start_collapsed', !!technical && !technical.open &&
    technical.querySelector('summary').textContent.indexOf('2 model turns') !== -1);
  const roundEls = document.querySelectorAll('#riRoundList .ri-round');
  check('round_rows_rendered', roundEls.length === 2);
  check('available_tools_are_not_executed_tools', roundEls.length &&
    roundEls[0].textContent.indexOf('2 tools available') !== -1);
  check('round1_two_attempts',
    roundEls.length &&
    roundEls[0].querySelectorAll('.ri-attempt').length === 2);
  check('fallback_attempt_shown',
    roundEls.length && roundEls[0].innerHTML.indexOf('R1-FALLBACK') !== -1);
  check('coverage_chip_partial',
    !!document.querySelector('#riRoundList .ri-coverage-chip'));
  const stateRows = document.querySelectorAll('#riRoundList .ri-state-row');
  check('states_separate', stateRows.length === 1 &&
    stateRows[0].textContent.indexOf('最终回复后') !== -1);

  /* ── 4. Select round → detail via the ONE renderer, server payload ── */
  roundEls[1].onclick();
  await new Promise(r => setTimeout(r, 10));
  /* P3 note: selecting round 2 ALSO fetches round 1 for the prefix-fold
   * diff, so the counter is >= 1 (r2 + r1), not exactly 1. */
  check('payload_fetched_for_round2', CALLS.getRequestPayload >= 1);
  const hdr1 = document.querySelector('#debugContent .debug-msg-header');
  check('detail_rendered',
    document.getElementById('debugTitle').innerHTML.indexOf('Messages') !== -1 &&
    !!hdr1 && document.getElementById('riDrawer').classList.contains('ri-detail-active'));
  if (hdr1) hdr1.onclick();
  check('detail_payload_shown',
    document.getElementById('debugContent').innerHTML.indexOf('payload-from-server') !== -1);

  /* ── 5. Live accelerator: SSE-recorded round needs NO fetch ── */
  showMessagesInDebug([{ role: 'user', content: 'live-acc' }], 'Round 1 请求前',
    true, 'conv-1', undefined, undefined,
    { kind: 'request', model: 'm-x', roundNum: 1, taskId: 'task-AAA' });
  const fetchesBefore = CALLS.getRequestPayload;
  roundEls[0].onclick();
  await new Promise(r => setTimeout(r, 10));
  check('accelerator_no_fetch', CALLS.getRequestPayload === fetchesBefore);
  const hdr2 = document.querySelector('#debugContent .debug-msg-header');
  if (hdr2) hdr2.onclick();
  check('accelerator_renders_live',
    document.getElementById('debugContent').innerHTML.indexOf('live-acc') !== -1);

  /* ── 6. Memory cap: recording task B strips task A payloads ── */
  showMessagesInDebug([{ role: 'user', content: 'other-task' }], 'Round 1',
    true, 'conv-1', undefined, undefined,
    { kind: 'request', model: 'm-y', roundNum: 1, taskId: 'task-BBB' });
  const tA = win.__dumpReqs()['task-AAA'];
  check('memory_cap_strips_others',
    !!tA && tA.rounds['1'] && tA.rounds['1'].messages === null &&
    tA.rounds['1']._stripped === true &&
    tA.rounds['1'].messageCount === 1);

  /* ── 7. Expired logs must say unavailable, never invent zero work ── */
  taskEls[1].onclick();
  await new Promise(r => setTimeout(r, 10));
  const expiredSummary = document.querySelector('#riRoundList .ri-summary-count');
  check('expired_count_is_unavailable', !!expiredSummary &&
    expiredSummary.textContent === 'Operation count unavailable' &&
    expiredSummary.textContent.indexOf('No tool operations') === -1);

  /* ── 8. closeDebug hides the drawer ── */
  closeDebug();
  check('drawer_closes', !document.body.classList.contains('ri-open') &&
    document.getElementById('riDrawer').style.display === 'none');

  console.log(out.join('\n'));
})().catch(e => { console.log('FAIL harness_exception ' + (e && e.stack || e)); });
"""


def _run(ri_path=None, expect_fail=None):
    harness = os.path.join(HERE, '_ri_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'core', 'debug_panel.js'),
             ri_path or os.path.join(JS_DIR, 'core', 'request_inspector.js'),
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
    assert not fails, 'request-inspector drawer failures:\n' + output
    assert output.count('PASS') >= 17, (
        f'expected >=17 PASS lines, got:\n{output}')
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_request_inspector_drawer():
    _run()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_neuter_detail_delegation_flips_red():
    """Negative control: _riSelectRound returns early in a COPY → the
    'detail_rendered' probe MUST fail (the delegation is load-bearing)."""
    shipped = os.path.join(JS_DIR, 'core', 'request_inspector.js')
    with open(shipped, encoding='utf-8') as f:
        src = f.read()
    anchor = 'async function _riSelectRound(taskId, roundNum, el, turn) {'
    assert anchor in src, 'delegation anchor drifted — update the neuter'
    neutered = src.replace(anchor, anchor + '\n  if (true) return;', 1)
    assert neutered != src
    tmp = os.path.join(HERE, '_request_inspector_neutered.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(neutered)
    try:
        _run(ri_path=tmp, expect_fail='detail_rendered')
    finally:
        os.remove(tmp)
    with open(shipped, encoding='utf-8') as f:
        assert f.read() == src, (
            'shipped request_inspector.js must be byte-identical')


if __name__ == '__main__':
    print(_run())
