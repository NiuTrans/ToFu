"""jsdom test for Request Inspector P3 — bubble anchor + prefix-fold diff.

Design: docs/DEBUG_PANEL_REDESIGN.md P3 (owner-ratified). Drives the REAL
shipped static/js/core/debug_panel.js + core/request_inspector.js under
jsdom:

  1. TURN ACTION: openRequestInspectorForTask(taskId) opens the drawer and
     selects the authoritative task even when it is absent from the by-conv
     convenience list.
  2. Fallback: opening the general drawer remains safe.
  3. PREFIX FOLD: selecting round N diffs its payload against round N-1 —
     the shared prefix collapses behind a .debug-prefix-fold row (hidden
     .debug-msg-prefix blocks), the increment carries .debug-msg-new;
     clicking the fold row expands the prefix.
  4. Round 1 has no diff base → NO fold row.
  5. Payload cache: re-selecting a round does not refetch.
  6. Static pins: the typed selector exposes an inspect action by task id and
     the ConversationSurface adapter opens the inspector; finish_info.js must
     not grow a duplicate entry point.

NEUTER: force _riSharedPrefix to 0 in a COPY → the fold row vanishes and
the prefix-fold probe flips red (the diff is load-bearing).
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
  id: 'conv-1', _serverTurnCount: 1,
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
    typeof openRequestInspectorForTask === 'function');

  /* ── 1. Typed Turn action: task identity opens its request list. ── */
  await openRequestInspectorForTask('task-VU9');
  check('anchor_opens_drawer', document.body.classList.contains('ri-open'));
  check('anchor_fetches_task_fold', CALLS.getRequests === 1);
  const round2 = document.querySelector('#riRoundList .ri-round[data-round="2"]');
  check('anchor_renders_task_rounds', !!round2);
  if (round2) round2.onclick();
  await sleep(20);
  check('round2_selected', !!round2 && round2.classList.contains('ri-sel'));
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

  /* ── 5. Fallback: general drawer open remains safe. ── */
  closeRequestInspector();
  openRequestInspector();
  await sleep(20);
  check('general_open_no_crash',
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
    """The typed action is debug-gated and resolves backend attempt identity."""
    selector = open(os.path.join(
        ROOT, 'frontend/src/conversation/presentation/conversation-view-model.ts'),
        encoding='utf-8').read()
    renderer = open(os.path.join(
        ROOT, 'frontend/src/conversation/ui/classic-conversation-renderers.ts'),
        encoding='utf-8').read()
    adapter = open(os.path.join(
        JS_DIR, 'main', 'conversation_turn_store.js'),
        encoding='utf-8').read()
    inspector = open(os.path.join(
        JS_DIR, 'core', 'request_inspector.js'), encoding='utf-8').read()

    assert "'inspect'" in selector
    assert 'requestInspectorEnabled && taskId' in selector
    assert "operation: taskId" in selector
    assert "button.classList.add('ri-anchor')" in renderer
    assert "'ri.openTip'" in renderer
    assert 'requestInspectorEnabled()' in adapter
    assert '_featureFlags?.debug_mode' in adapter
    assert "intent.type === 'inspect'" in adapter
    assert 'openRequestInspectorForTask(intent.operation)' in adapter
    assert 'async function openRequestInspectorForTask(taskId)' in inspector
    assert 'await _riSelectTask(String(taskId));' in inspector

    from lib.static_serving import load_static_bytes

    static_read = load_static_bytes(
        os.path.join(PROJECT_ROOT, 'static'), 'styles.css')
    assert static_read is not None
    css = static_read[0].decode('utf-8')
    assert '.ri-anchor' in css and '.debug-prefix-fold' in css
    # Key declaration is enforced centrally by ``npm run check:i18n``; this
    # feature guard pins the load-bearing call site instead of re-reading a
    # locale implementation file.
    assert "'ri.openTip'" in renderer
    assert "'msgAction.inspect'" in adapter


if __name__ == '__main__':
    print(_run())
