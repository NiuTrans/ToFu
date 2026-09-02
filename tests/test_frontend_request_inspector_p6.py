"""jsdom test for Request Inspector P6 — per-TOOL-ROW request anchor.

This is the phase that answers the owner's original complaint verbatim:
"I see a suspicious tool call in chatinner and I still have to go to the
debug panel and check one by one to figure out which one it might be."

P3 put ONE anchor on the assistant bubble — wrong granularity, because a
bubble holds N rounds x M tool calls. P6 puts an anchor on EVERY tool row
and resolves it to the request that PRODUCED that call.

The mapping needs no new backend field: the backend already tags each tool
round with `llmRound` (0-based orchestrator loop index) and each request
snapshot with a 1-based `roundNum`, so producing request = llmRound + 1.

Covered:
  1. Every tool row renders an anchor labelled with its producing round,
     and the label is llmRound + 1 (NOT roundNum, which is the tool-call
     ordinal — the classic off-by-one this test pins).
  2. debug_mode OFF → no anchors at all.
  3. A row that cannot be resolved to a task renders NO anchor (an anchor
     that goes nowhere is worse than none).
  4. Synthetic inject rows (inbox / peer / user-steer) get no anchor —
     they are not LLM tool calls.
  5. Clicking an anchor opens the drawer, selects that task, lands on the
     producing round, and flashes it.
  6. Endpoint tasks: when several phases share a round number, the anchor
     prefers the WORKER phase (that is where tool calls happen).
  7. Autopilot VU rows (the role:'user' _isVirtualUser bubble) render NO
     anchor even when their (roundNum, llmRound) numerically COLLIDES with
     the worker's — the VU sub-task persists no snapshots, so the round is
     owned-but-not-inspectable and must resolve to '' rather than to the
     WRONG task's mirror (the 2026-08-03 incident: a VU verification round
     opened the WORKER task's state mirror and read as data corruption).

NEUTER×2: (a) make the anchor use roundNum instead of llmRound+1 → the
mapping probe flips red, proving the anchor resolves to the right request;
(b) skip non-assistant messages BEFORE the identity check (the pre-fix
behaviour) → a VU-owned round falls through to the numeric fallback and
resolves to the WORKER task → the VU probe flips red, proving the
role-aware identity gate is load-bearing.
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
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="riDrawer" style="display:none">' +
  '  <div id="riTaskList"></div><div id="riRoundList"></div>' +
  '  <div class="debug-panel" id="debugPanel">' +
  '    <div id="debugTitle"></div><div id="debugContent"></div>' +
  '  </div>' +
  '</div><div id="chatinner"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

global.escapeHtml = win.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
global.Icon = win.Icon = (n, s) => `<svg data-icon="${n}"></svg>`;
const _I18N = {
  'ri.empty': 'No tasks', 'ri.loading': 'Loading…', 'ri.expired': 'expired',
  'ri.requests': 'requests', 'ri.live': 'live', 'ri.states': 'States',
  'ri.stateNote': 'not requests', 'ri.coveragePartial': 'partial',
  'ri.coverageAmbiguous': 'ambiguous',
  'ri.turnPlanning': 'Planner', 'ri.turnWorking': 'Worker',
  'ri.turnReviewing': 'Critic',
  'ri.toolAnchorTip': 'Inspect the request that produced this tool call (round {round})',
  'ri.prefixFold': 'prefix {k} vs {base}',
};
global.t = win.t = (k, a) => {
  let s = _I18N[k] || k;
  if (a) for (const kk of Object.keys(a)) s = s.replace('{' + kk + '}', String(a[kk]));
  return s;
};
global.activeConvId = win.activeConvId = 'conv-1';
global.debugVisible = win.debugVisible = false;
global._featureFlags = win._featureFlags = { debug_mode: true };

/* Two tool rounds in ONE assistant message:
 *   roundNum 1 (llmRound 0) → produced by request R1
 *   roundNum 2 (llmRound 2) → produced by request R3   <-- NOT R2
 * The divergence between roundNum and llmRound+1 is the whole point. */
const ROUND_A = { roundNum: 1, llmRound: 0, toolName: 'grep_search', status: 'done',
  _taskId: 'task-T1', _turnId: 'turn-T1' };
const ROUND_B = { roundNum: 2, llmRound: 2, toolName: 'read_files', status: 'done',
  _taskId: 'task-T1', _turnId: 'turn-T1' };
const ORPHAN  = { roundNum: 9, llmRound: 1, toolName: 'web_search', status: 'done' };
const INJECT  = { roundNum: 9000001, llmRound: 1, _inboxInject: true, status: 'done' };
/* Autopilot VU bubble (role:'user' + _isVirtualUser) at the TAIL, exactly
 * where autopilot places it. Its sub-task re-numbers rounds from 1/0, so
 * VU_ROUND is a deliberate numeric TWIN of ROUND_A — the 2026-08-03
 * collision, where a click on the VU's row opened the WORKER task's
 * same-numbered state mirror. */
const VU_ROUND = { roundNum: 1, llmRound: 0, toolName: 'run_command', status: 'done',
  _turnId: 'turn-VU' };

global.conversations = win.conversations = [{ id: 'conv-1', _serverTurnCount: 2 }];
global.ConversationTurnRead = win.ConversationTurnRead = {
  state: () => ({
    turnsById: {
      'turn-T1': { turnId: 'turn-T1', actor: 'assistant' },
      'turn-VU': { turnId: 'turn-VU', actor: 'virtual_user' },
    },
    attemptsById: {
      'attempt-T1': { turnId: 'turn-T1', taskId: 'task-T1', createdAt: 1 },
      'attempt-VU': { turnId: 'turn-VU', taskId: 'task-VU', createdAt: 2 },
    },
  }),
};

const CALLS = { getRequests: 0, payloads: [] };
win.Api = global.Api = {
  tasks: {
    byConv: async () => ({ convId: 'conv-1', tasks: [
      { taskId: 'task-T1', status: 'done', createdAt: 1, completedAt: 2,
        live: false, requestCount: 3, stateCount: 0, legacyCount: 0,
        hasEvents: true }] }),
    getRequests: async (taskId) => {
      CALLS.getRequests++;
      return { taskId, eventsAvailable: true, coverage: 'full', requestCount: 4,
        requests: [
          { roundNum: 1, ts: 1, model: 'm', params: {}, messageCount: 2,
            toolsCount: 1, approxTokens: 10, label: 'R1', turn: '', legacy: false, attempts: [] },
          { roundNum: 2, ts: 2, model: 'm', params: {}, messageCount: 4,
            toolsCount: 1, approxTokens: 20, label: 'R2', turn: '', legacy: false, attempts: [] },
          { roundNum: 3, ts: 3, model: 'm', params: {}, messageCount: 6,
            toolsCount: 1, approxTokens: 30, label: 'R3', turn: '', legacy: false, attempts: [] },
          { roundNum: 3, ts: 4, model: 'm', params: {}, messageCount: 7,
            toolsCount: 1, approxTokens: 31, label: 'R3-critic', turn: 'reviewing',
            legacy: false, attempts: [] },
        ], states: [] };
    },
    getRequestPayload: async (taskId, roundNum, turn) => {
      CALLS.payloads.push({ roundNum: String(roundNum), turn: turn || '' });
      return { taskId, roundNum, turn: turn || '', model: 'm', params: {},
        label: 'R' + roundNum, tools: [],
        messages: [{ role: 'user', content: 'payload-R' + roundNum }] };
    },
  },
  clientError: { report: () => {} },
};

/* Load debug_panel + request_inspector, then the ANCHOR RENDERER extracted
 * from the real tool_rounds.js (we eval only the two functions we need, since
 * tool_rounds.js as a whole pulls in a large render surface). */
const debugSrc = fs.readFileSync(path.join(ROOT,'static','js','core','debug_panel.js'), 'utf8');
const riSrc    = fs.readFileSync(path.join(ROOT,'static','js','core','request_inspector.js'), 'utf8');
const toolSrc  = fs.readFileSync(path.join(ROOT,'static','js','ui','tool_rounds.js'), 'utf8');

/* Extract _renderToolRequestAnchor + its SVG const from the SHIPPED file so
 * this test drives the real implementation, not a copy. */
function extract(name, src) {
  const i = src.indexOf('function ' + name);
  if (i < 0) throw new Error('cannot find ' + name);
  let depth = 0, started = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { depth++; started = true; }
    else if (src[j] === '}') { depth--; if (started && depth === 0) return src.slice(i, j + 1); }
  }
  throw new Error('unbalanced ' + name);
}
const svgConst = toolSrc.match(/const _RI_TOOL_ANCHOR_SVG =[\s\S]*?';/);
if (!svgConst) throw new Error('cannot find _RI_TOOL_ANCHOR_SVG');

eval(debugSrc + '\n' + riSrc + '\n' + svgConst[0] + '\n' +
     extract('_renderDebugEntry', toolSrc) + '\n' +
     ';win.__anchor = _renderDebugEntry;');

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  /* ── 1. Anchor labels the PRODUCING round (llmRound + 1) ── */
  const hA = win.__anchor(ROUND_A);
  const hB = win.__anchor(ROUND_B);
  check('anchor_rendered_per_row', !!hA && !!hB);
  check('anchor_maps_llmRound_plus_1_A', hA.indexOf('>R1<') !== -1);
  check('anchor_maps_llmRound_plus_1_B',
    hB.indexOf('>R3<') !== -1 && hB.indexOf('>R2<') === -1);
  check('anchor_calls_tool_entry',
    hB.indexOf("openToolDebugPanel('task-T1',3") !== -1);
  /* R and S were MERGED into one entry whose panel carries request/state
   * tabs. The row still addresses its post-tool state mirror so the drawer's
   * state list can locate this slot, and there must be exactly ONE control. */
  check('row_addresses_state_mirror',
    hB.indexOf('data-ri-state="task-T1:3"') !== -1);
  check('single_merged_entry_not_two',
    (hB.match(/class="ri-tool-anchor"/g) || []).length === 1);

  /* ── 2. debug_mode OFF → nothing ── */
  win._featureFlags.debug_mode = false;
  global._featureFlags.debug_mode = false;
  check('no_anchor_without_debug_mode', win.__anchor(ROUND_A) === '');
  win._featureFlags.debug_mode = true;
  global._featureFlags.debug_mode = true;

  /* ── 3. Unresolvable row → no anchor ── */
  check('no_anchor_when_task_unresolvable', win.__anchor(ORPHAN) === '');

  /* ── 4. Synthetic inject rows are not LLM tool calls ── */
  check('no_anchor_on_inject_row', win.__anchor(INJECT) === '');

  /* ── 5. Click → drawer opens, lands on the producing round, flashes ── */
  await openRequestInspectorForToolRound('task-T1', 3);
  await sleep(30);
  check('click_opens_drawer', document.body.classList.contains('ri-open'));
  check('click_fetched_task_fold', CALLS.getRequests >= 1);
  const sel = document.querySelector('#riRoundList .ri-round.ri-sel');
  check('landed_on_producing_round', !!sel && sel.dataset.round === '3');
  check('flashed_the_row', !!sel && sel.classList.contains('ri-flash'));
  check('payload_fetched_for_R3',
    CALLS.payloads.some(p => p.roundNum === '3'));

  /* ── 6. Endpoint: worker phase wins over a same-numbered critic row ── */
  check('prefers_worker_phase_on_tie', !!sel && sel.dataset.turn !== 'reviewing');

  /* ── 7. Autopilot VU round: owned-but-not-inspectable → NO anchor ──
   * The VU bubble is role:'user' and its sub-task persists NO request/state
   * snapshots, so its rounds are unresolvable BY NATURE. The numeric twin
   * trap must not resolve them to the worker task — no anchor beats a wrong
   * one (the documented contract). */
  check('vu_owned_round_renders_no_anchor', win.__anchor(VU_ROUND) === '');
  check('vu_round_resolves_to_empty_task', _riTaskIdForRound(VU_ROUND) === '');
  /* …and the refusal must NOT over-suppress: the worker's own numerically
   * identical round (identity-owned by the assistant msg) keeps its anchor. */
  check('worker_twin_still_resolves',
    win.__anchor(ROUND_A).indexOf("openToolDebugPanel('task-T1',1") !== -1);

  console.log(out.join('\n'));
})().catch(e => { console.log('FAIL harness_exception ' + (e && e.stack || e)); });
"""


def _run(tool_src_path=None, expect_fail=None):
    harness = os.path.join(HERE, '_ri_p6_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, tool_src_path or '', ROOT],
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
    assert not fails, 'P6 tool-anchor failures:\n' + output
    assert output.count('PASS') >= 18, f'expected >=18 PASS, got:\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_p6_tool_row_anchor():
    _run()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_neuter_wrong_round_mapping_flips_red():
    """NC: map the anchor to `roundNum` instead of `llmRound + 1` → the
    mapping probe MUST fail. This is the off-by-one that would send the user
    to the wrong request while still LOOKING like it works, so it is the one
    error the suite has to be able to catch."""
    shipped = os.path.join(JS_DIR, 'ui', 'tool_rounds.js')
    with open(shipped, encoding='utf-8') as f:
        src = f.read()
    anchor = '  const round = Number(lr) + 1;'
    assert anchor in src, 'NC anchor drifted — update the neuter'
    neutered = src.replace(anchor, '  const round = Number(r.roundNum);', 1)
    assert neutered != src
    tmp = os.path.join(HERE, '_tool_rounds_p6_neutered.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(neutered)
    try:
        # The harness extracts the renderer from the path it is given.
        harness = _HARNESS.replace(
            "path.join(ROOT,'static','js','ui','tool_rounds.js')",
            "process.argv[2]")
        hpath = os.path.join(HERE, '_ri_p6_nc_harness.js')
        with open(hpath, 'w') as f:
            f.write(harness)
        try:
            proc = subprocess.run(['node', hpath, tmp, ROOT],
                                  capture_output=True, text=True, timeout=60)
            out = proc.stdout.strip()
            assert 'FAIL anchor_maps_llmRound_plus_1_B' in out, (
                f'neutered mapping was NOT caught:\n{out}')
        finally:
            os.remove(hpath)
    finally:
        os.remove(tmp)
    with open(shipped, encoding='utf-8') as f:
        assert f.read() == src, 'shipped tool_rounds.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_neuter_vu_role_guard_flips_red():
    """Removing the typed actor guard exposes a virtual-user task anchor."""
    shipped = os.path.join(JS_DIR, 'core', 'request_inspector.js')
    with open(shipped, encoding='utf-8') as f:
        src = f.read()
    anchor = "    if (!turn || turn.actor === 'virtual_user') return '';"
    assert anchor in src, 'NC anchor drifted — update the neuter'
    neutered = src.replace(
        anchor,
        "    if (!turn) return '';", 1)
    assert neutered != src
    tmp = os.path.join(HERE, '_request_inspector_p6_vu_neutered.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(neutered)
    try:
        # The harness extracts request_inspector.js from the path it is given.
        harness = _HARNESS.replace(
            "path.join(ROOT,'static','js','core','request_inspector.js')",
            "process.argv[2]")
        hpath = os.path.join(HERE, '_ri_p6_vu_nc_harness.js')
        with open(hpath, 'w') as f:
            f.write(harness)
        try:
            proc = subprocess.run(['node', hpath, tmp, ROOT],
                                  capture_output=True, text=True, timeout=60)
            out = proc.stdout.strip()
            assert 'FAIL vu_owned_round_renders_no_anchor' in out, (
                f'neutered VU role guard was NOT caught:\n{out}')
        finally:
            os.remove(hpath)
    finally:
        os.remove(tmp)
    with open(shipped, encoding='utf-8') as f:
        assert f.read() == src, 'shipped request_inspector.js must be byte-identical'


def test_anchor_is_wired_at_the_single_render_chokepoint():
    """Static pin: the anchor must hang off _renderToolSlot — the ONE place
    every tool row (including swarm panels) is rendered. If a future refactor
    adds a second render path, this pin is the reminder."""
    src = open(os.path.join(JS_DIR, 'ui', 'tool_rounds.js'), encoding='utf-8').read()
    assert '_renderDebugEntry(r)' in src, 'anchor not called'
    slot = src[src.index('function _renderToolSlot'):]
    slot = slot[:slot.index('\n}')]
    assert '_renderStandaloneDebugEntry' in slot or '_renderDebugEntry' in slot, (
        'the anchor must be emitted from _renderToolSlot (the single '
        'chokepoint), not from an individual branch renderer')
    assert '_featureFlags.debug_mode' in src, 'anchor is not debug_mode-gated'
    assert "'ri.toolAnchorTip'" in src
    css = open(os.path.join(ROOT, 'static', 'styles.css'), encoding='utf-8').read()
    assert '.ri-tool-anchor' in css
    # The drawer's accommodation for the context gauge: pre-4dee9231 the gauge
    # was an absolutely-positioned ball needing its own `body.ri-open
    # .ctx-health-bar` yield hack; 4dee9231 moved it INTO #convStatusStrip
    # inside .input-area, so it now yields WITH the whole chat container as one
    # unit. The pin therefore targets the unit-yield rule plus the strip's
    # in-flow placement — a per-widget hack would be obsolete by design.
    assert 'body.ri-open .chat-container{margin-right:' in css, (
        'the drawer no longer yields the chat container — the status strip '
        '(gauge + turn-nav) would collide with it')
    html = open(os.path.join(ROOT, 'index.html'), encoding='utf-8').read()
    ia = html.index('class="input-area"') if 'class="input-area"' in html else 0
    strip = html.index('id="convStatusStrip"')
    assert ia and strip > ia, (
        '#convStatusStrip must stay INSIDE .input-area so the container-yield '
        'rule covers it (out of flow = needs its own ri-open hack again)')


if __name__ == '__main__':
    print(_run())
