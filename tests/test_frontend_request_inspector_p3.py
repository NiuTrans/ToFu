"""jsdom test for Request Inspector P3 — bubble anchor + prefix-fold diff.

Design: docs/DEBUG_PANEL_REDESIGN.md P3 (owner-ratified). Drives the REAL
shipped static/js/core/debug_panel.js + core/request_inspector.js under
jsdom:

  1. BUBBLE ANCHOR: openRequestInspectorForMessage(msgId) opens the drawer,
     selects msg._taskId (even a task NOT in the by-conv list — the VU
     case), picks the bubble's last apiRound.round (1-based == snapshot
     roundNum), flashes + renders the detail.
  2. Fallback: an unknown msgId opens the drawer without crashing.
  3. PREFIX FOLD: selecting round N diffs its payload against round N-1 —
     the shared prefix collapses behind a .debug-prefix-fold row (hidden
     .debug-msg-prefix blocks), the increment carries .debug-msg-new;
     clicking the fold row expands the prefix.
  4. Round 1 has no diff base → NO fold row.
  5. Payload cache: re-selecting a round does not refetch.
  6. Static pins: chat_render.js carries the debug_mode-gated ri-anchor
     (a `.msg-action-btn` in the unified `.message-actions` bar) calling
     openRequestInspectorForMessage (the bubble entry); finish_info.js
     must NOT grow a duplicate back.

NEUTER: force _riSharedPrefix to 0 in a COPY → the fold row vanishes and
the prefix-fold probe flips red (the diff is load-bearing).
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
  'ri.expired': 'Event log expired', 'ri.coveragePartial': 'partial',
  'ri.live': 'live',
  'ri.prefixFold': 'Prefix of {k} message(s) identical to {base} collapsed — click to expand',
};
win.t = global.t = (k, args) => {
  let s = _I18N[k] || k;
  if (args) for (const kk of Object.keys(args))
    s = s.replace('{' + kk + '}', String(args[kk]));
  return s;
};
win.activeConvId = global.activeConvId = 'conv-1';
win.conversations = global.conversations = [{
  id: 'conv-1',
  messages: [
    { _msgId: 'm1', role: 'assistant', content: 'reply', _taskId: 'task-VU9',
      apiRounds: [{ round: 1 }, { round: 2 }] },
  ],
}];
win.debugVisible = global.debugVisible = false;

const R1_MSGS = [{ role: 'user', content: 'shared-u1' }];
const R2_MSGS = [
  { role: 'user', content: 'shared-u1' },
  { role: 'assistant', content: 'a1-new' },
  { role: 'user', content: 'u2-new' },
];
const CALLS = { byConv: 0, getRequests: 0, payloads: [] };
win.Api = global.Api = {
  tasks: {
    byConv: async (convId) => {
      CALLS.byConv++;
      // NOTE: task-VU9 deliberately ABSENT (VU sub-tasks are not in the
      // by-conv list — the anchor must reach them directly).
      return { convId, tasks: [] };
    },
    getRequests: async (taskId) => {
      CALLS.getRequests++;
      return {
        taskId, eventsAvailable: true, coverage: 'full', requestCount: 2,
        requests: [
          { roundNum: 1, ts: 1753400001000, model: 'm-x', params: {},
            messageCount: 1, toolsCount: 0, approxTokens: 100,
            label: 'Round 1 请求前', legacy: false, attempts: [] },
          { roundNum: 2, ts: 1753400002000, model: 'm-x', params: {},
            messageCount: 3, toolsCount: 0, approxTokens: 300,
            label: 'Round 2 请求前', legacy: false, attempts: [] },
        ],
        states: [],
      };
    },
    getRequestPayload: async (taskId, roundNum) => {
      CALLS.payloads.push(String(roundNum));
      const msgs = String(roundNum) === '1' ? R1_MSGS : R2_MSGS;
      return { taskId, roundNum, model: 'm-x', params: {},
        label: 'Round ' + roundNum, tools: [], messages: msgs };
    },
  },
  clientError: { report: () => {} },
};

const debugSrc = fs.readFileSync(process.argv[2], 'utf8');
const riSrc = fs.readFileSync(process.argv[3], 'utf8');
eval(debugSrc + '\n' + riSrc);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  check('anchor_fn_present',
    typeof openRequestInspectorForMessage === 'function');

  /* ── 1. Bubble anchor: msg → task-VU9 → round 2 (last apiRound) ── */
  openRequestInspectorForMessage('m1');
  await sleep(30);
  check('anchor_opens_drawer', document.body.classList.contains('ri-open'));
  check('anchor_fetches_task_fold', CALLS.getRequests === 1);
  const sel = document.querySelector('#riRoundList .ri-round[data-round="2"]');
  check('anchor_selects_last_apiround', !!sel && sel.classList.contains('ri-sel'));
  check('anchor_flashes_row', !!sel && sel.classList.contains('ri-flash'));
  check('anchor_detail_rendered',
    document.getElementById('debugTitle').innerHTML.indexOf('Messages') !== -1);

  /* ── 2. Prefix fold: r2 vs r1 share 1 leading message ── */
  const foldRow = document.querySelector('#debugContent .debug-prefix-fold');
  check('prefix_fold_row', !!foldRow &&
    foldRow.textContent.indexOf('R1') !== -1);
  const prefixBlocks = document.querySelectorAll('#debugContent .debug-msg-prefix');
  check('prefix_blocks_hidden', prefixBlocks.length === 1 &&
    prefixBlocks[0].style.display === 'none');
  const newBlocks = document.querySelectorAll('#debugContent .debug-msg-new');
  check('increment_marked_new', newBlocks.length === 2);
  /* Expand the fold → prefix becomes visible. */
  if (foldRow) foldRow.onclick();
  check('fold_expands_prefix',
    prefixBlocks.length === 1 && prefixBlocks[0].style.display === '');

  /* ── 3. Payload cache: re-select round 2 → NO refetch ── */
  const fetched = CALLS.payloads.slice();
  document.querySelector('#riRoundList .ri-round[data-round="2"]').onclick();
  await sleep(20);
  check('payload_cache_no_refetch',
    CALLS.payloads.length === fetched.length);

  /* ── 4. Round 1: no diff base → NO fold row ── */
  document.querySelector('#riRoundList .ri-round[data-round="1"]').onclick();
  await sleep(20);
  check('round1_no_fold',
    !document.querySelector('#debugContent .debug-prefix-fold'));

  /* ── 5. Fallback: unknown msgId → drawer opens, no crash ── */
  closeRequestInspector();
  openRequestInspectorForMessage('no-such-msg');
  await sleep(20);
  check('unknown_msgid_no_crash',
    document.body.classList.contains('ri-open'));

  console.log(out.join('\n'));
})().catch(e => { console.log('FAIL harness_exception ' + (e && e.stack || e)); });
"""


def _run(ri_path=None, expect_fail=None):
    harness = os.path.join(HERE, '_ri_p3_harness.js')
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
    assert not fails, 'request-inspector P3 failures:\n' + output
    assert output.count('PASS') >= 13, (
        f'expected >=13 PASS lines, got:\n{output}')
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_p3_anchor_and_prefix_fold():
    _run()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_neuter_shared_prefix_flips_red():
    """Negative control: _riSharedPrefix forced to 0 in a COPY → the
    'prefix_fold_row' probe MUST fail (the diff is load-bearing)."""
    shipped = os.path.join(JS_DIR, 'core', 'request_inspector.js')
    with open(shipped, encoding='utf-8') as f:
        src = f.read()
    anchor = 'function _riSharedPrefix(prevMsgs, curMsgs) {'
    assert anchor in src, 'diff anchor drifted — update the neuter'
    neutered = src.replace(anchor, anchor + '\n  return 0;', 1)
    assert neutered != src
    tmp = os.path.join(HERE, '_request_inspector_p3_neutered.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(neutered)
    try:
        _run(ri_path=tmp, expect_fail='prefix_fold_row')
    finally:
        os.remove(tmp)
    with open(shipped, encoding='utf-8') as f:
        assert f.read() == src, (
            'shipped request_inspector.js must be byte-identical')


def test_action_bar_anchor_static_pins():
    """Static pins: the bubble `</>` entry lives in chat_render.js as a
    `.msg-action-btn ri-anchor` inside the unified `.message-actions` bar,
    gated on _featureFlags.debug_mode + msg._taskId, calling
    openRequestInspectorForMessage with the message id. The finish meta row
    (finish_info.js) must not carry a duplicate anchor."""
    cr = os.path.join(JS_DIR, 'ui', 'chat_render.js')
    with open(cr, encoding='utf-8') as f:
        src = f.read()
    assert 'openRequestInspectorForMessage' in src, (
        'chat_render.js lost the Request Inspector anchor call')
    assert 'msg-action-btn ri-anchor' in src, (
        'ri-anchor must be a .msg-action-btn inside .message-actions')
    # The anchor must sit inside a debug_mode + task gate.
    idx = src.index('openRequestInspectorForMessage')
    gate_window = src[max(0, idx - 700):idx]
    assert '_featureFlags.debug_mode' in gate_window, (
        'ri-anchor is NOT gated on debug_mode')
    assert 'msg._taskId' in gate_window, (
        'ri-anchor must require msg._taskId (no task → no anchor)')
    # The move is load-bearing: exactly ONE anchor, in the action bar.
    fi = os.path.join(JS_DIR, 'ui', 'finish_info.js')
    with open(fi, encoding='utf-8') as f:
        assert 'ri-anchor' not in f.read(), (
            'finish_info.js re-grew an ri-anchor — the entry lives in '
            '.message-actions now; keep exactly one')
    with open(os.path.join(ROOT, 'static', 'styles.css'),
              encoding='utf-8') as f:
        css = f.read()
    assert '.ri-anchor' in css and '.debug-prefix-fold' in css
    # i18n keys for the anchor + fold row
    with open(os.path.join(JS_DIR, 'i18n.js'), encoding='utf-8') as f:
        i18n = f.read()
    assert "'ri.openTip'" in i18n and "'ri.prefixFold'" in i18n
    assert "'msgAction.inspect'" in i18n


if __name__ == '__main__':
    print(_run())
