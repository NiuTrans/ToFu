"""jsdom test for the tool-row debug panel (ONE view: the post-tool state).

The panel mounted by the `</> R{n}` entry next to a tool row.

2026-07-29 owner directive — the panel is now ONE view, not two tabs:
  "the request button seems redundant; we don't need both a request and a
  result status button. Directly displaying just the result status button
  seems to be the correct approach."
  That is correct at the DATA level, which is what this suite pins: the
  post-tool mirror for round N is captured AFTER the tool results are
  appended to the same message list the request was built from
  (lib/tasks_pkg/tool_dispatch/_pipeline.py, same roundNum axis), so the
  request payload is a strict PREFIX of the mirror. Showing both meant two
  clicks for the same messages minus the results.
  BUT the request axis must survive as a FALLBACK: swarm sub-agents persist
  kind='request' snapshots ONLY (lib/swarm/agent.py has no state emission),
  so a state-only panel would render "mirror missing" on every sub-agent
  tool row. The fallback must also SAY which axis it fell back to.

2026-07-28 owner directive — still pinned:
  1. ROUND-SCOPED: the view renders ONLY what that round appended (the
     increment over the previous round's same-kind payload), never the full
     conversation-history dump.
  2. NO cross-round chip strip.

Kept contract (the P7 baseline):
  • The panel mounts INLINE right after the tool slot, fetches payloads with
    kind='state', and renders through the SHARED debug renderer
    (renderDebugBlocksInto / updateDebugToolsBlock).
  • At most one panel; a different round replaces, the same round toggles.
  • Live accelerator: an SSE-recorded state mirror needs NO network fetch.
  • Fallback: no tool slot in the DOM → drawer detail (kind=state).

NEUTERs:
  1. Drop the kind='state' argument → the state-kind pin flips red.
  2. Drop the round-scoping call → the previous round's history leaks back
     into the panel and the increment pin flips red.
  3. Drop the request-axis fallback → sub-agent rounds (mirror-less) render
     an empty panel, and the fallback pin flips red.
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
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="riDrawer" style="display:none">' +
  '  <div id="riTaskList"></div><div id="riRoundList"></div>' +
  '  <div class="debug-panel" id="debugPanel">' +
  '    <div id="debugTitle"></div><div id="debugContent"></div>' +
  '  </div>' +
  '</div>' +
  '<div id="chatinner">' +
  '  <div data-prn="1"><div class="ri-tool-anchor-row" data-ri-state="task-T1:2"></div></div>' +
  '  <div data-prn="2"><div class="ri-tool-anchor-row" data-ri-state="task-T1:3"></div></div>' +
  '  <div data-prn="3"><div class="ri-tool-anchor-row" data-ri-state="task-T1:5"></div></div>' +
  '</div></body>',
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
  'ri.prefixFold': 'prefix {k} vs {base}',
  'ri.toolAnchorTip': 'View request that generated this tool (Round {round})',
  'ri.stateRowTip': 'jump to the tool call',
  'ri.stateEmpty': 'State mirror expired or missing',
  'ri.stateClose': 'Close state inspector',
  'ri.tabRequest': 'Request', 'ri.tabState': 'Result state',
  'ri.stateKindTip': 'state after the tools ran',
  'ri.requestKindTip': 'no post-tool state for this round',
};
global.t = win.t = (k, a) => {
  let s = _I18N[k] || k;
  if (a) for (const kk of Object.keys(a)) s = s.replace('{' + kk + '}', String(a[kk]));
  return s;
};
global.activeConvId = win.activeConvId = 'conv-1';
global.debugVisible = win.debugVisible = false;
global._featureFlags = win._featureFlags = { debug_mode: true };
global.conversations = win.conversations = [{ id: 'conv-1', messages: [] }];

/* Payloads engineered for prefix math: every round shares the SAME leading
 * history (SYS + HIST-u1). Round N's request appends NEW-aN/NEW-tN; a state
 * mirror appends STATE-aN/STATE-tN on top of its round's request payload.
 *
 * Round 5 is the SUB-AGENT shape: request snapshots exist, state does NOT
 * (lib/swarm/agent.py persists kind='request' only). */
const HIST = [
  { role: 'system', content: 'SYS' },
  { role: 'user', content: 'HIST-u1' },
];
const NO_STATE_ROUNDS = new Set(['5']);
function reqMsgs(roundNum) {
  const base = HIST.slice();
  if (Number(roundNum) >= 2)
    base.push({ role: 'assistant', content: 'NEW-a' + roundNum },
              { role: 'tool', content: 'NEW-t' + roundNum });
  return base;
}
function stateMsgs(roundNum) {
  return reqMsgs(roundNum).concat([
    { role: 'assistant', content: 'STATE-a' + roundNum },
    { role: 'tool', content: 'STATE-t' + roundNum },
  ]);
}

const CALLS = { getRequests: 0, payloads: [] };
win.Api = global.Api = {
  tasks: {
    byConv: async () => ({ convId: 'conv-1', tasks: [
      { taskId: 'task-T1', status: 'done', createdAt: 1, completedAt: 2,
        live: false, requestCount: 3, stateCount: 2, legacyCount: 0,
        hasEvents: true }] }),
    getRequests: async (taskId) => {
      CALLS.getRequests++;
      return { taskId, eventsAvailable: true, coverage: 'full', requestCount: 3,
        requests: [
          { roundNum: 1, ts: 1, model: 'm', params: {}, messageCount: 2,
            toolsCount: 1, approxTokens: 10, label: 'R1', turn: '', legacy: false, attempts: [] },
          { roundNum: 2, ts: 2, model: 'm', params: {}, messageCount: 4,
            toolsCount: 1, approxTokens: 20, label: 'R2', turn: '', legacy: false, attempts: [] },
        ],
        states: [
          { roundNum: 1, label: 'Round 1 工具结果后', messageCount: 3, ts: 3, legacy: false },
          { roundNum: 2, label: 'Round 2 工具结果后', messageCount: 5, ts: 4, legacy: false },
        ] };
    },
    getRequestPayload: async (taskId, roundNum, turn, kind) => {
      CALLS.payloads.push({ roundNum: String(roundNum), turn: turn || '', kind: kind || '' });
      if (kind === 'state' && NO_STATE_ROUNDS.has(String(roundNum))) return null;
      const msgs = (kind === 'state') ? stateMsgs(roundNum) : reqMsgs(roundNum);
      return { taskId, roundNum, turn: turn || '', kind: kind || 'request',
        model: 'm', params: {}, label: 'R' + roundNum, tools: [],
        messages: msgs.map((m) => ({ ...m })) };
    },
  },
  clientError: { report: () => {} },
};

const debugSrc = fs.readFileSync(path.join(ROOT,'static','js','core','debug_panel.js'), 'utf8');
const riSrc    = fs.readFileSync(path.join(ROOT,'static','js','core','request_inspector.js'), 'utf8');
eval(debugSrc + '\n' + riSrc);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
/* createBlock lazy-renders the message body on first expand (header.onclick
 * → colorJson) — expand EVERY block, THEN read the text. */
function expandAll(root) {
  const hs = root ? root.querySelectorAll('.debug-msg-header') : [];
  hs.forEach((h) => h.onclick());
  return hs.length;
}

(async () => {
  /* ── 1. Inline mount next to the tool slot — the result state, scoped ── */
  await openStateInspector('task-T1', 2);
  await sleep(30);
  const slot = document.querySelector('[data-prn="1"]');
  const panel = document.querySelector('.ri-state-panel');
  check('panel_mounted', !!panel);
  check('panel_after_slot', !!panel && slot.nextElementSibling === panel);
  check('payload_fetched_as_state_kind',
    CALLS.payloads.some(p => p.roundNum === '2' && p.kind === 'state'));
  const sbody = panel && panel.querySelector('.ri-state-body');
  if (sbody) expandAll(sbody);
  const sText = sbody ? sbody.textContent : '';
  /* State R2 = [SYS, HIST-u1 | NEW-a2, NEW-t2, STATE-a2, STATE-t2]; the
   * shared prefix with state R1 is 2, so ONLY the 4 new messages render. */
  check('state_body_shows_increment', sText.indexOf('STATE-t2') !== -1);
  check('state_body_hides_history', sText.indexOf('HIST-u1') === -1);
  check('no_state_strip',
    !!panel && !panel.querySelector('.ri-state-chip') &&
    !panel.querySelector('.ri-state-strip'));

  /* ── 2. ONE view, not two: no tab strip, and the axis is NAMED ── */
  check('no_request_tab_button',
    !!panel && !panel.querySelector('[data-ri-tab="request"]'));
  check('no_tab_strip_at_all',
    !!panel && !panel.querySelector('.ri-panel-tab') &&
    !panel.querySelector('.ri-panel-tabs'));
  check('view_axis_is_state', panel.dataset.riKind === 'state');
  const kindEl = panel.querySelector('.ri-state-panel-kind');
  check('axis_named_on_screen',
    !!kindEl && kindEl.textContent === 'Result state' &&
    !kindEl.classList.contains('ri-kind-fallback'));
  /* The request payload is a PREFIX of the mirror, so the single view already
   * carries what the removed Request tab showed — nothing was lost. */
  check('state_view_carries_request_content', sText.indexOf('NEW-t2') !== -1);

  /* ── 3. At most ONE panel. Opening a DIFFERENT round replaces the open one;
   *      re-clicking the SAME round toggles it closed. With two tabs the
   *      toggle was suppressed for state opens (the caller always passed a
   *      tab); one button means one unambiguous toggle. ── */
  await openStateInspector('task-T1', 3);
  await sleep(30);
  check('other_round_replaces_panel',
    document.querySelectorAll('.ri-state-panel').length === 1 &&
    document.querySelector('.ri-state-panel').dataset.riRound === '3');
  await openStateInspector('task-T1', 3);
  await sleep(30);
  check('same_round_toggles_closed',
    document.querySelectorAll('.ri-state-panel').length === 0);
  await openStateInspector('task-T1', 2);
  await sleep(30);
  check('single_panel_instance',
    document.querySelectorAll('.ri-state-panel').length === 1);

  /* ── 4. Live accelerator: SSE-recorded state mirrors need NO fetch ── */
  showMessagesInDebug([{ role: 'user', content: 'live-state-acc-prev' }],
    'Round 2 工具结果后', true, 'conv-1', undefined, undefined,
    { kind: 'state', model: 'm-x', roundNum: 2, taskId: 'task-T1' });
  showMessagesInDebug([{ role: 'user', content: 'live-state-acc' }],
    'Round 3 工具结果后', true, 'conv-1', undefined, undefined,
    { kind: 'state', model: 'm-x', roundNum: 3, taskId: 'task-T1' });
  const fetchesBefore = CALLS.payloads.length;
  await openStateInspector('task-T1', 3);
  await sleep(30);
  check('accelerator_state_no_fetch', CALLS.payloads.length === fetchesBefore);
  const panel3 = document.querySelector('.ri-state-panel');
  check('accelerator_state_rendered',
    !!panel3 && expandAll(panel3.querySelector('.ri-state-body')) > 0 &&
    panel3.querySelector('.ri-state-body')
      .innerHTML.indexOf('live-state-acc') !== -1);

  /* ── 5. Sub-agent shape: NO state mirror → fall back to the request axis,
   *      and say so. A state-only panel would render an empty view on every
   *      swarm sub-agent tool row (agent.py persists kind='request' only). ── */
  await openStateInspector('task-T1', 5);
  await sleep(30);
  const panel5 = document.querySelector('.ri-state-panel');
  const body5 = panel5 && panel5.querySelector('.ri-state-body');
  if (body5) expandAll(body5);
  const t5 = body5 ? body5.textContent : '';
  check('mirrorless_round_falls_back_to_request',
    !!panel5 && panel5.dataset.riKind === 'request' &&
    t5.indexOf('NEW-t5') !== -1);
  check('mirrorless_round_is_not_empty',
    t5.indexOf('State mirror expired') === -1);
  const kind5 = panel5 && panel5.querySelector('.ri-state-panel-kind');
  check('fallback_axis_labelled_as_request',
    !!kind5 && kind5.textContent === 'Request' &&
    kind5.classList.contains('ri-kind-fallback'));

  /* ── 6. Fallback: tool slot not in the DOM → drawer detail (kind=state) ── */
  check('drawer_still_closed_before_fallback',
    !document.body.classList.contains('ri-open'));
  await openStateInspector('task-T1', 7);   // no data-ri-state marker for 7
  await sleep(30);
  check('fallback_opens_drawer', document.body.classList.contains('ri-open'));
  check('fallback_fetches_state_kind',
    CALLS.payloads.some(p => p.roundNum === '7' && p.kind === 'state'));
  const dbg = document.getElementById('debugContent');
  check('fallback_renders_in_drawer',
    expandAll(dbg) > 0 && dbg.innerHTML.indexOf('STATE-t7') !== -1);

  console.log(out.join('\n'));
})().catch(e => { console.log('FAIL harness_exception ' + (e && e.stack || e)); });
"""


def _run(ri_src_path=None, expect_fail=None):
    harness = os.path.join(HERE, '_ri_p7_harness.js')
    src = _HARNESS
    if ri_src_path:
        # The neuter run drives a MODIFIED copy of request_inspector.js.
        src = src.replace(
            "path.join(ROOT,'static','js','core','request_inspector.js')",
            "process.argv[2]")
    with open(harness, 'w') as f:
        f.write(src)
    try:
        proc = subprocess.run(
            ['node', harness, ri_src_path or '', ROOT],
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
    assert not fails, 'tool-row debug panel failures:\n' + output
    assert output.count('PASS') >= 23, f'expected >=23 PASS, got:\n{output}'
    return output


def _neuter_run(anchor, replacement, expect_fail, *, count=1):
    """Drive a copy of request_inspector.js with `anchor` replaced, assert the
    named probe flips red, and assert the shipped file was not touched."""
    shipped = os.path.join(JS_DIR, 'core', 'request_inspector.js')
    with open(shipped, encoding='utf-8') as f:
        src = f.read()
    assert anchor in src, 'NC anchor drifted — update the neuter'
    neutered = src.replace(anchor, replacement, count)
    assert neutered != src
    tmp = os.path.join(HERE, '_request_inspector_p7_neutered.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(neutered)
    try:
        _run(ri_src_path=tmp, expect_fail=expect_fail)
    finally:
        os.remove(tmp)
    with open(shipped, encoding='utf-8') as f:
        assert f.read() == src, 'shipped request_inspector.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_p7_inline_state_inspector():
    _run()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_neuter_state_kind_dropped_flips_red():
    """NC: drop the kind='state' argument from the payload fetch → the
    state-kind pin MUST fail. Without it the inline panel would silently
    render the PRE-REQUEST snapshot (the request axis) while claiming to
    show the post-tool state — the exact off-axis confusion this feature
    exists to kill."""
    _neuter_run("kind === 'state' ? 'state' : undefined", 'undefined',
                'payload_fetched_as_state_kind')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_neuter_scoping_dropped_leaks_history():
    """NC: render the FULL payload instead of this round's increment → the
    previous round's history leaks back into the panel and the round-scoping
    pin MUST fail. The panel exists to answer ONE round, not to dump the
    whole conversation (owner, 2026-07-28)."""
    _neuter_run(
        "const scoped = await _riRoundScopedMessages(taskId, roundNum, view.kind,\n"
        "    payload.messages);",
        'const scoped = payload.messages;',
        'state_body_hides_history')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_neuter_request_fallback_dropped_empties_subagent_rounds():
    """NC: make the single view state-ONLY (drop the request fallback) → every
    round without a post-tool mirror renders an empty panel, and the
    sub-agent pin MUST fail.

    This is the trap in collapsing the two tabs into one: swarm sub-agents
    persist kind='request' snapshots only (lib/swarm/agent.py never emits a
    state mirror), so 'just show the result state' is a dead panel for them.
    """
    _neuter_run(
        "  const req = await _riFetchPayload(taskId, roundNum, '', 'request');\n"
        "  if (req && req.messages && req.messages.length)\n"
        "    return { kind: 'request', payload: req };\n",
        '',
        'mirrorless_round_falls_back_to_request')


def test_state_inspector_wiring_pins():
    """Static pins: the inline entry, the row addressing, the i18n strings,
    the styles, and the api.js kind plumbing — the pieces a future refactor
    could silently drop while keeping every jsdom test green.

    The 2026-07-28 directive ADDED the round-scoping + no-chip-strip pins.
    The 2026-07-29 directive ADDED the single-view pins: the tab strip must
    not creep back, and the request axis must stay reachable as a fallback."""
    ri = open(os.path.join(JS_DIR, 'core', 'request_inspector.js'),
              encoding='utf-8').read()
    assert 'function openStateInspector' in ri
    assert 'function openToolDebugPanel' in ri, (
        'the single tool-row debug entry is gone')
    assert '_riMountToolPanel' in ri
    assert '_riRoundScopedMessages' in ri, (
        'the round-scoping helper is gone — the panel dumps full history again')
    assert 'ri-state-strip' not in ri, 'the cross-round strip crept back'
    assert 'ri-panel-tab' not in ri, (
        'the request|state tab strip crept back — the owner removed the second '
        'button on 2026-07-29 because the mirror is a superset of the request')
    assert '_riFetchRoundView' in ri, (
        'the state-first / request-fallback resolver is gone — mirror-less '
        'rounds (swarm sub-agents) would render an empty panel')
    assert "onclick = () => openStateInspector(taskId, s.roundNum)" in ri, (
        'drawer state rows must stay navigable')
    tr = open(os.path.join(JS_DIR, 'ui', 'tool_rounds.js'), encoding='utf-8').read()
    assert 'data-ri-state=' in tr, 'tool rows lost their state-mirror address'
    assert 'openToolDebugPanel(' in tr, (
        'tool rows lost their debug entry wiring')
    api = open(os.path.join(JS_DIR, 'api.js'), encoding='utf-8').read()
    assert 'getRequestPayload: (taskId, roundNum, turn, kind)' in api
    i18n = open(os.path.join(JS_DIR, 'i18n.js'), encoding='utf-8').read()
    for key in ("'ri.tabRequest'", "'ri.tabState'", "'ri.stateRowTip'",
                "'ri.stateEmpty'", "'ri.stateClose'", "'ri.stateKindTip'",
                "'ri.requestKindTip'"):
        assert key in i18n, f'{key} missing'
    css = open(os.path.join(ROOT, 'static', 'styles.css'), encoding='utf-8').read()
    assert '.ri-state-panel' in css
    assert '.ri-state-chip' not in css, 'the removed chip strip styles crept back'
    assert '.ri-panel-tab' not in css, 'the removed tab-strip styles crept back'
    assert '.ri-state-panel-kind' in css, (
        'the axis chip has no styles — a request-axis fallback would be '
        'indistinguishable from the post-tool mirror')


if __name__ == '__main__':
    print(_run())
