"""jsdom test for the Request Inspector FLAT WIRE FEED (2026-09-22 owner
directive — "直接平铺展示所有 messages，turn 之间画个分界线，跳转就直接
跳转，不要再搞任务详情、运行详情、单轮内容").

Replaces the retired drill-down suites (test_frontend_request_inspector.py
P1, _p3 prefix-fold, _p4 turn badges, _turn_nav filtering): the task list,
round list and single-round detail pane are GONE. What remains is ONE flat
message scroll with turn/agent/rebuild dividers, a SCROLL-model turn nav,
the per-turn Turn Trace, and the inline tool panel (covered by P7).

Drives the REAL shipped debug_panel.js + request_inspector.js under jsdom:

  1. The feed renders every message exactly once, dividers interleaved in
     wire order (turn → rebuild → turn → agent).
  2. The turn nav (‹ 回复 #N ›) is a SCROLL model: stepper and jump list
     scrollIntoView the divider and flash it; they never load a view.
  3. Anchor entries land on the producing turn (pending scroll survives
     the wire not being loaded yet).
  4. Silent poll refreshes preserve expanded blocks and scroll position,
     and append live messages.
  5. SSE evidence frames still record into the per-task log but never
     paint over the feed (deferRender).
  6. Per-turn trace entry renders the trace in place with a back-to-feed
     bar that polls must not clobber.
  7. Honest states: loading → feed, fetch failure → retry affordance,
     empty conversation → empty note (never "failed").
  8. A partial or failed read retains the last complete feed, marks retryable
     gaps (not expired), and a successful retry clears the warning.
  9. closeDebug hides the drawer.

NEUTER: _riFeedDividerEl returns a bare div in a COPY → the divider
probes flip red, proving the dividers are load-bearing.
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
  '  <div class="ri-head">' +
  '    <div class="ri-turn-nav" id="riTurnNav" hidden>' +
  '      <button id="riTurnPrev"></button>' +
  '      <button id="riTurnPos"><span id="riTurnReadout"></span></button>' +
  '      <button id="riTurnNext"></button>' +
  '    </div>' +
  '  </div>' +
  '  <div class="ri-main"><div class="debug-panel" id="debugPanel">' +
  '    <div id="debugTitle"></div><div id="debugContent"></div>' +
  '  </div></div>' +
  '  <div class="ri-turn-jump" id="riTurnJump" hidden></div>' +
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
  'ri.title': 'Debug messages',
  'ri.empty': 'No tasks recorded for this conversation',
  'ri.loading': 'Loading…',
  'ri.loadFailed': 'Failed to load',
  'ri.eventWindowPartial': 'Incomplete execution window; latest snapshot unconfirmed',
  'ri.retry': 'Retry',
  'ri.expired': 'Event log expired',
  'ri.expiredHint': 'Detailed records are kept for about 30 days',
  'ri.feedTurns': '{n} replies',
  'ri.feedTruncated': 'Earliest {n} replies not expanded',
  'ri.rebuildNote': 'Context rebuilt here: first {k} continuous, {d} rewritten',
  'ri.agentSection': 'Sub-agent execution',
  'ri.backToFeed': 'Back to message feed',
  'ri.turnChip': 'Reply #{n}',
  'ri.turnChipTip': 'Jump to the reply in the chat',
  'ri.earlierShort': 'Earlier',
  'ri.jumpToTurn': 'Jump to a turn',
  'ri.turns': 'Turns',
  'ri.prevTurn': 'Previous turn',
  'ri.nextTurn': 'Next turn',
  'ri.statusRunning': 'Task in progress',
  'ri.statusDone': 'Task completed',
  'ri.statusFailed': 'Task did not complete',
  'ri.statusStopped': 'Task stopped',
  'ri.statusUnknown': 'Status unknown',
  'ri.timeJustNow': 'just now',
  'ri.timeMinAgo': '{n} min ago',
  'ri.timeHoursAgo': '{n} h ago',
  'ri.timeDaysAgo': '{n} d ago',
  'ri.traceEntry': 'Timing',
  'ri.traceEntryHint': 'Where the time went',
  'ri.traceTitle': 'Timing analysis',
  'ri.traceTotal': 'Total',
  'ri.trKindLlm': 'Model',
  'ri.trKindTool': 'Tool',
  'ri.trRowTurn': 'Task',
  'ri.trRowPhase': 'Phase',
  'ri.trRowDetail': 'Detail',
  'ri.trRowSub': 'Sub',
  'stream.roundMessages': 'Round {round} · {n} msgs',
  'debug.structContent': 'Content', 'debug.structReasoning': 'Reasoning',
  'debug.structToolCalls': 'Tool calls', 'debug.structToolResult': 'Tool result',
  'debug.structFields': 'Fields', 'debug.structRawJson': 'Raw JSON',
  'debug.structImage': 'Image',
};
win.t = global.t = (k, vars) => {
  let value = _I18N[k] || k;
  for (const [name, replacement] of Object.entries(vars || {}))
    value = value.replaceAll('{' + name + '}', String(replacement));
  return value;
};
win.activeConvId = global.activeConvId = 'conv-1';
win.conversations = global.conversations = [{ id: 'conv-1', _serverTurnCount: 2 }];
win.debugVisible = global.debugVisible = false;
win._safeClipboardWrite = global._safeClipboardWrite = async (text) => {
  win.__copied = text;
};

global.ConversationTurnRead = win.ConversationTurnRead = {
  state: () => ({
    turnsById: {
      t1: { turnId: 't1', actor: 'assistant' },
      t2: { turnId: 't2', actor: 'assistant' },
    },
    attemptsById: {},
  }),
  ordered: () => [
    { turnId: 't1', actor: 'assistant' },
    { turnId: 't2', actor: 'assistant' },
  ],
};
const CHAT_SCROLLS = [];
global._findRenderedNativeTurnNode = win._findRenderedNativeTurnNode =
  (turnId) => ({ scrollIntoView: () => CHAT_SCROLLS.push(turnId) });
const FEED_SCROLLS = [];
win.Element.prototype.scrollIntoView = function () { FEED_SCROLLS.push(this); };

const NOW = 1753400000000;
const U = (text) => ({ role: 'user', content: text });
const A = (text) => ({ role: 'assistant', content: text });
const WIRE = () => ({
  convId: 'conv-1',
  messages: [U('q1'), A('a1'), U('q2'), A('a2'), U('agent brief'), A('agent work')],
  sections: [
    { type: 'turn', msgIndex: 0, turnId: 't1', taskId: 'task-1',
      createdAt: NOW - 5000, live: false, status: 'done', preview: 'q1' },
    { type: 'rebuild', msgIndex: 2, kept: 1, dropped: 1 },
    { type: 'turn', msgIndex: 2, turnId: 't2', taskId: 'task-2',
      createdAt: NOW - 1000, live: false, status: 'done', preview: 'q2' },
    { type: 'agent', msgIndex: 4, agentId: 'w1',
      taskId: 'task-1#agent:w1', keptPrefix: 2 },
  ],
  turns: [
    { turnId: 't1', taskId: 'task-1', msgIndex: 0, createdAt: NOW - 5000,
      live: false, status: 'done', preview: 'q1' },
    { turnId: 't2', taskId: 'task-2', msgIndex: 2, createdAt: NOW - 1000,
      live: false, status: 'done', preview: 'q2' },
  ],
  coverage: 'full',
});

const CALLS = { convWire: 0, payloads: 0, traces: [] };
let nextWire = null;
win.Api = global.Api = {
  tasks: {
    convWire: async () => {
      CALLS.convWire++;
      return nextWire || WIRE();
    },
    getTrace: async (taskId) => {
      CALLS.traces.push(taskId);
      return { eventsAvailable: true, tStart: 1000, totalMs: 5000,
        summary: { llmMs: 3000, toolMs: 1000 }, spans: [], gaps: [],
        statusHistory: [], clientObservations: [], running: false };
    },
    getRequestPayload: async () => { CALLS.payloads++; return null; },
  },
  clientError: { report: () => {} },
};

const debugSrc = fs.readFileSync(process.argv[2], 'utf8');
const riSrc = fs.readFileSync(process.argv[3], 'utf8');
eval(debugSrc + '\n' + riSrc +
  '\n;win.__dumpReqs = function(){ return _debugRequests; };' +
  '\n;win.__ingest = function(f){ return DebugPresentationState.ingestEvidenceFrame(\'conv-1\', f); };');

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const kinds = () => Array.from(document.getElementById('debugContent').children)
  .map((el) => el.classList.contains('ri-feed-turn') ? 'turn'
    : el.classList.contains('ri-feed-rebuild') ? 'rebuild'
    : el.classList.contains('ri-feed-agent') ? 'agent'
    : el.classList.contains('debug-msg-block') ? 'msg' : '?');

(async () => {
  check('fn_present', typeof toggleDebug === 'function' &&
    typeof openRequestInspectorForTask === 'function');

  /* ── 1. Open: drawer + loading → flat feed with dividers in order ── */
  const opened = toggleDebug();
  check('drawer_opens', document.body.classList.contains('ri-open') &&
    document.getElementById('riDrawer').style.display === 'flex');
  check('loading_paints_first',
    document.getElementById('debugContent').querySelector('.ri-loading') !== null);
  await sleep(10);
  check('convwire_fetched', CALLS.convWire === 1);
  const seq = kinds();
  check('feed_order', JSON.stringify(seq) === JSON.stringify(
    ['turn', 'msg', 'msg', 'rebuild', 'turn', 'msg', 'msg', 'agent', 'msg', 'msg']));
  check('feed_every_message_once',
    document.querySelectorAll('#debugContent .debug-msg-block').length === 6);
  const chips = Array.from(document.querySelectorAll('.ri-feed-turn .ri-turn-chip'))
    .map((el) => el.textContent);
  check('turn_chips_ordinal', JSON.stringify(chips) === JSON.stringify(
    ['Reply #1', 'Reply #2']));
  const rebuildEl = document.querySelector('.ri-feed-rebuild');
  check('rebuild_note',
    !!rebuildEl && rebuildEl.textContent.indexOf('rewritten') !== -1);
  const agentEl = document.querySelector('.ri-feed-agent');
  check('agent_section',
    !!agentEl && agentEl.textContent.indexOf('w1') !== -1);
  check('title_summary',
    document.getElementById('debugTitle').textContent.indexOf('Messages (6)') !== -1 &&
    document.getElementById('debugTitle').textContent.indexOf('2 replies') !== -1);
  check('trace_entry_per_turn',
    document.querySelectorAll('.ri-feed-turn .ri-feed-trace').length === 2);

  /* ── 2. Nav: SCROLL model, defaults to the newest turn ── */
  const nav = document.getElementById('riTurnNav');
  check('nav_visible', nav.hidden === false);
  check('nav_defaults_newest',
    document.getElementById('riTurnReadout').textContent === 'Reply #2');
  check('nav_edges',
    document.getElementById('riTurnPrev').disabled === false &&
    document.getElementById('riTurnNext').disabled === true);
  FEED_SCROLLS.length = 0;
  document.getElementById('riTurnPrev').onclick();
  await sleep(1);
  check('stepper_scrolls_and_flashes',
    FEED_SCROLLS.length === 1 &&
    FEED_SCROLLS[0].classList.contains('ri-feed-turn') &&
    FEED_SCROLLS[0].dataset.turnId === 't1' &&
    FEED_SCROLLS[0].classList.contains('ri-flash'));
  check('stepper_updates_readout',
    document.getElementById('riTurnReadout').textContent === 'Reply #1');
  document.getElementById('riTurnPos').onclick();
  await sleep(1);
  const jump = document.getElementById('riTurnJump');
  const rows = jump.querySelectorAll('.ri-turn-jump-row');
  check('jump_newest_first', rows.length === 2 &&
    rows[0].textContent.indexOf('q2') !== -1);
  FEED_SCROLLS.length = 0;
  rows[0].onclick();
  await sleep(1);
  check('jump_scrolls', FEED_SCROLLS.length === 1 &&
    FEED_SCROLLS[0].dataset.turnId === 't2' && jump.hidden === true);

  /* ── 3. Locate chip scrolls the CHAT without moving the feed ── */
  document.getElementById('riTurnPos').onclick();
  await sleep(1);
  const chip = jump.querySelector('.ri-turn-jump-row .ri-turn-chip');
  if (chip) chip.onclick({ stopPropagation: () => {} });
  check('locate_scrolls_chat', CHAT_SCROLLS.length === 1 && CHAT_SCROLLS[0] === 't2');
  check('locate_keeps_readout',
    document.getElementById('riTurnReadout').textContent === 'Reply #2');

  /* ── 4. SSE evidence frames record but never paint over the feed ── */
  const before = document.getElementById('debugContent').children.length;
  win.__ingest({
    type: 'messages_snapshot', kind: 'request', roundNum: 3, taskId: 'task-2',
    messageCount: 2, messages: [U('live'), A('live-a')], tools: [],
  });
  await sleep(1);
  check('evidence_does_not_paint',
    document.getElementById('debugContent').children.length === before);
  check('evidence_still_records',
    !!win.__dumpReqs()['task-2'] && !!win.__dumpReqs()['task-2'].rounds['3']);

  /* ── 5. Silent refresh preserves open blocks and appends live ── */
  const firstHeader = document.querySelector('#debugContent .debug-msg-header');
  firstHeader.onclick();
  check('block_opens', firstHeader.closest('.debug-msg-block')
    .classList.contains('open'));
  const grown = WIRE();
  grown.messages.push(U('q2-followup'));
  grown.sections = grown.sections.map((s) => Object.assign({}, s));
  nextWire = grown;
  await _riLoadWire('conv-1', { silent: true });
  check('silent_refresh_appends',
    document.querySelectorAll('#debugContent .debug-msg-block').length === 7);
  check('open_block_survives_refresh',
    !!document.querySelector('#debugContent .debug-msg-block.open'));
  nextWire = null;

  /* ── 6. Anchor lands on the producing turn (pending scroll) ── */
  FEED_SCROLLS.length = 0;
  closeRequestInspector();
  check('drawer_closes', !document.body.classList.contains('ri-open'));
  const anchorOpen = openRequestInspectorForTask('task-1');
  await sleep(15);
  check('anchor_scrolls_to_turn',
    FEED_SCROLLS.some((el) => el.dataset && el.dataset.turnId === 't1'));

  /* ── 7. Trace entry: renders in place, back bar returns, poll-safe ── */
  const traceBtn = document.querySelector('.ri-feed-turn[data-turn-id="t2"] .ri-feed-trace');
  check('trace_button_present', !!traceBtn);
  if (traceBtn) traceBtn.onclick();
  await sleep(15);
  check('trace_fetched', CALLS.traces.length === 1 && CALLS.traces[0] === 'task-2');
  check('trace_rendered',
    !!document.querySelector('#debugContent .tr-wrap'));
  check('back_bar_present',
    !!document.querySelector('#debugContent .ri-feed-back'));
  await _riLoadWire('conv-1', { silent: true });
  check('poll_keeps_trace',
    !!document.querySelector('#debugContent .tr-wrap'));
  const backBtn = document.querySelector('.ri-feed-back button');
  if (backBtn) backBtn.onclick();
  await sleep(1);
  check('back_restores_feed',
    document.querySelectorAll('#debugContent .ri-feed-turn').length === 2);

  /* ── 8. Partial reads never erase the last complete wire. ── */
  const partial = { convId: 'conv-1', messages: [],
    sections: WIRE().sections.filter((s) => s.type === 'turn').map((s) =>
      Object.assign({}, s, { readError: true })),
    turns: WIRE().turns.map((row) => Object.assign({}, row, { readError: true })),
    readError: true, coverage: 'partial', coverageReason: 'event-limit',
    eventReadBounds: [{ taskId: 'task-2', reason: 'event-limit', afterSequence: 9999 }] };
  win.Api.tasks.convWire = async () => { CALLS.convWire++; return partial; };
  await _riLoadWire('conv-1', { silent: true });
  check('partial_preserves_last_good',
    document.querySelectorAll('#debugContent .debug-msg-block').length === 6 &&
    document.querySelectorAll('#debugContent .ri-feed-turn').length === 2 &&
    !!document.querySelector('#debugContent .ri-warn-line .ri-retry') &&
    document.querySelector('#debugContent .ri-feed-truncated').textContent.includes('latest snapshot unconfirmed'));
  win.Api.tasks.convWire = async () => { CALLS.convWire++; return WIRE(); };
  await _riLoadWire('conv-1', { silent: true });
  check('partial_recovers_without_stale_warning',
    document.querySelectorAll('#debugContent .debug-msg-block').length === 6 &&
    !document.querySelector('#debugContent .ri-warn-line') &&
    !document.querySelector('#debugContent .ri-feed-truncated'));

  closeRequestInspector();
  win.Api.tasks.convWire = async () => { CALLS.convWire++; return partial; };
  openRequestInspector();
  await sleep(15);
  check('first_partial_marks_retryable_gap',
    document.querySelectorAll('#debugContent .ri-feed-turn').length === 2 &&
    !document.querySelector('#debugContent .debug-msg-block') &&
    document.querySelector('#debugContent .ri-feed-turn .ri-feed-turn-preview')
      .textContent.includes('Failed to load') &&
    !document.querySelector('#debugContent .ri-feed-turn .ri-feed-turn-preview')
      .textContent.includes('expired'));
  win.Api.tasks.convWire = async () => { CALLS.convWire++; return WIRE(); };
  await _riLoadWire('conv-1', { silent: false });

  /* ── 8. Honest states: failure → retry; empty → empty note ── */
  closeRequestInspector();
  win.Api.tasks.convWire = async () => { CALLS.convWire++; return null; };
  openRequestInspector();
  await sleep(15);
  check('failure_state_retry',
    document.getElementById('debugContent').textContent.indexOf('Failed to load') !== -1 &&
    !!document.querySelector('#debugContent .ri-retry'));
  win.Api.tasks.convWire = async () => { CALLS.convWire++;
    throw new Error('storage temporarily unavailable'); };
  await _riLoadWire('conv-1', { silent: true });
  check('throwing_fetch_stays_retryable',
    !!document.querySelector('#debugContent .ri-retry'));
  win.Api.tasks.convWire = async () => { CALLS.convWire++;
    return { convId: 'conv-1', messages: [null], sections: [], turns: [] }; };
  await _riLoadWire('conv-1', { silent: true });
  check('malformed_message_retry_without_crash',
    !!document.querySelector('#debugContent .ri-retry') &&
    document.getElementById('debugTitle').textContent.includes('Failed to load') &&
    document.getElementById('debugContent')._rawMessages.length === 0);
  win.Api.tasks.convWire = async () => { CALLS.convWire++;
    return { convId: 'conv-1', messages: [], sections: [], turns: [] }; };
  await _riLoadWire('conv-1', { silent: false });
  check('empty_state_honest',
    document.getElementById('debugContent').textContent.indexOf('No tasks recorded') !== -1 &&
    document.getElementById('riTurnNav').hidden === true);
  win.Api.tasks.convWire = async () => { CALLS.convWire++; return WIRE(); };
  await _riLoadWire('conv-1', { silent: false });
  check('retry_recovers',
    document.querySelectorAll('#debugContent .debug-msg-block').length === 6);

  console.log(out.join('\n'));
})().catch(e => { console.log('FAIL harness_exception ' + (e && e.stack || e)); });
"""


def _run(ri_path=None, expect_fail=None):
    harness = os.path.join(HERE, '_ri_feed_harness.js')
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
    assert not fails, 'request-inspector wire-feed failures:\n' + output
    assert output.count('PASS') >= 25, (
        f'expected >=25 PASS lines, got:\n{output}')
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_wire_feed_drawer():
    _run()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_neuter_dividers_flip_red():
    """Negative control: _riFeedDividerEl returns a bare div in a COPY →
    the feed-order probe MUST fail (the dividers are load-bearing)."""
    shipped = os.path.join(JS_DIR, 'core', 'request_inspector.js')
    with open(shipped, encoding='utf-8') as f:
        src = f.read()
    anchor = 'function _riFeedDividerEl(s) {'
    assert anchor in src, 'divider anchor drifted — update the neuter'
    neutered = src.replace(
        anchor,
        anchor + "\n  return document.createElement('div');",
        1)
    assert neutered != src
    tmp = os.path.join(HERE, '_request_inspector_feed_neutered.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(neutered)
    try:
        _run(ri_path=tmp, expect_fail='feed_order')
    finally:
        os.remove(tmp)
    with open(shipped, encoding='utf-8') as f:
        assert f.read() == src, (
            'shipped request_inspector.js must be byte-identical')


def test_bubble_anchor_static_pins():
    """The typed chat-bubble inspect action resolves backend attempt identity
    and opens the inspector — the entry point the feed's turn landing serves.
    (Carried over from the retired P3 suite; .debug-prefix-fold is gone with
    the single-round pane.)"""
    selector = open(os.path.join(
        ROOT, 'frontend/src/conversation/presentation/view-model/turn-metadata.ts'),
        encoding='utf-8').read()
    renderer = open(os.path.join(
        ROOT, 'frontend/src/conversation/ui/renderers/turn-chrome.ts'),
        encoding='utf-8').read()
    adapter = open(os.path.join(
        JS_DIR, 'main', 'conversation_turn_store.js'), encoding='utf-8').read()
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

    from lib.static_serving import load_static_bytes

    static_read = load_static_bytes(
        os.path.join(os.path.dirname(os.path.dirname(HERE)), 'static'), 'styles.css')
    assert static_read is not None
    css = static_read[0].decode('utf-8')
    drawer_css = open(os.path.join(ROOT, 'frontend/src/styles/application/02-inspector-drawer.css'), encoding='utf-8').read()
    assert '.ri-anchor' in drawer_css and '.ri-feed-turn' in drawer_css
    assert "'ri.openTip'" in renderer


if __name__ == '__main__':
    print(_run())
