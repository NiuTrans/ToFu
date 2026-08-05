"""jsdom regression: a SERVER-SIDE-INJECTED turn (Project-Brain kickoff /
peer message / drained queue turn) must become visible on an open tab WHILE
the task it spawned is still streaming — not only after completion + a
manual refresh.

INCIDENT (conv msebjymx5b4a25, 2026-08-05)
------------------------------------------
The owner answered a blocked Project-Brain epic on the board. The answer
re-dispatched the epic: the queue drain appended the kickoff user message
server-side and spawned the task within the same second. On the open tab:

  1. NOTHING refetched the body — the dispatch notify went out with
     ``rev=None`` ("metadata-only") even though the append had bumped the
     content rev (DB trigger), so the rev-gated verify never fired;
  2. the busy signal attached the stream — an Agent bubble appeared "out of
     nowhere", with no triggering message above it;
  3. even a manual refresh did not surface the kickoff: Phase-2's
     MERGE_ACTIVE_TASK append is gated on ``!activeStreams.has`` and the
     boot-attach had already registered the stream;
  4. the previous turn's settle bar showed only the model tag: the local
     copy was cached before its terminal sync, and every top-up channel is
     closed while a task stays busy.

THE FIX
-------
  * lib/message_queue.py — the dispatch notify carries the REAL post-append
    rev (and engine-built turns get a server-minted ``_msgId``);
  * core/conv_reducers.js — ``_adoptInjectedSettledPrefix``: inserts settled
    server messages missing locally immediately BEFORE the stream's bound
    tail (identity-anchored; the bound object is never touched; the live
    turn's own partial checkpoint rows are excluded);
  * core/cross_tab_sync.js — the live-stream guard diverts to a new
    ``_streamActiveVerify`` lane: insertion + terminal-field top-up (never on
    the live tail) + the translation merge, keyed by ``_msgId``;
  * core/conversations.js — Phase-2 MERGE_ACTIVE_TASK runs the same insertion
    when a stream is live instead of refusing.

HARNESS — drives the REAL conv family + cross_tab_sync.js under bare node:
  A. notify lane: rev-carrying frame during a live stream inserts the
     kickoff before the bound placeholder, tops up the stale settle bar,
     keeps translations working, repaints via showStreamingUIForConv only;
  D. idempotency: a second frame inserts nothing, repaints nothing;
  C. anchor-miss (window doesn't reach the settled tail): refuse, and do NOT
     advance _serverRev (a later read must still reconcile);
  B. Phase-2 lane: loadConversationMessages with a live stream inserts the
     kickoff through the MERGE_ACTIVE_TASK arm.
NC: neuter the reducer's splice → both lanes go dark (A and B fail), while
    the field-level top-ups (lane-side, not reducer-side) keep working.
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')

sys.path.insert(0, HERE)
from _jsdom import run_harness, frontend_module_guard  # noqa: E402
from _conv_bundle_sources import conv_family_sources  # noqa: E402

frontend_module_guard(need_jsdom=False)

_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.addEventListener = () => {};
global._syncChannel = null;
global.TAB_ID = 'tab-test';
global.document = { addEventListener: () => {}, visibilityState: 'visible',
                    getElementById: () => null, querySelector: () => null,
                    querySelectorAll: () => [] };
global.activeConvId = 'c1';
global.activeStreams = new Map();
global._editingMsgIdx = null;
global.debugLog = () => {};
global.config = {};
global.Icon = () => '';
global.AbortSignal = { timeout: () => undefined };
global.apiUrl = (p) => p;
global.renderConversationList = () => {};
global.renderChat = () => {};
global.updateSendButton = () => {};
global.loadConversationsFromServer = async () => {};
global.loadConversation = () => {};
global.newChat = () => {};
global.pushIsConnected = () => true;
global.pushSubscribe = () => {};
global._restoreConvToolState = () => {};
global._frameIsOurs = () => true;
global.convWindowParam = () => '';
global.getActiveConv = () => conversations.find((c) => c.id === activeConvId) || null;
global._convSorter = (a, b) => (b.updatedAt || 0) - (a.updatedAt || 0);
global.ConvCache = {
  isAvailable: () => true,
  get: async () => null,
  getMeta: async () => null,
  getAllMeta: async () => [],
  put: async () => {},
  remove: async () => {},
};
let saveCalls = [];
global.saveConversations = (id) => { saveCalls.push(id); };
let streamUICalls = [];
let replaceAllCalls = [];
let applyMsgCalls = [];
global.showStreamingUIForConv = (id) => { streamUICalls.push(id); };
global.ConvView = {
  replaceAll: (id) => { replaceAllCalls.push(id); },
  applyMessage: (id, msg, opts) => { applyMsgCalls.push(opts && opts.idx); return true; },
};
let SERVER = null;
global.Api = {
  conversations: {
    get: async () => JSON.parse(JSON.stringify(SERVER)),
    getResponse: async () => ({
      ok: true, status: 200,
      headers: { get: () => null },
      json: async () => JSON.parse(JSON.stringify(SERVER)),
    }),
  },
};

global.conversations = [];
const _files = [process.argv[2], ...process.argv.slice(4)];  // argv[3] is ROOT
for (const f of _files) eval(fs.readFileSync(f, 'utf8'));
global.conversations = conversations;
/* The family's REAL saveConversations (core/conv_save.js) shadows the global
 * stub inside the eval'd functions — and it needs the DOM, so it throws in
 * bare node; the lane's .catch then swallowed the failure AFTER the adoption
 * had already landed, silently skipping the repaint. Override the lexical
 * binding so repaint side-effects stay observable. */
try { saveConversations = (id) => { saveCalls.push(id); }; } catch (e) {}

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let phObj = null;
function seed() {
  saveCalls = []; streamUICalls = []; replaceAllCalls = []; applyMsgCalls = [];
  activeStreams.clear();
  conversations.length = 0;
  /* The stream's BOUND placeholder (connectToTask pushed it on attach). */
  phObj = { role: 'assistant', content: '', thinking: '', toolRounds: [],
            _msgId: 'ph1', timestamp: 3000, _taskId: 'tNEW' };
  conversations.push({
    id: 'c1', title: 'c1', _serverRev: 8, _needsLoad: false,
    createdAt: 1000, updatedAt: 2000, activeTaskId: 'tNEW',
    messages: [
      { role: 'user', content: 'q', _msgId: 'm0', timestamp: 1000 },
      /* Stale local copy — cached BEFORE the terminal sync: no finishReason
       * (the model-only settle bar half of the incident). */
      { role: 'assistant', content: 'first answer', _msgId: 'm1',
        timestamp: 2000, _taskId: 'tOLD' },
      phObj,
    ],
  });
  activeStreams.set('c1', { taskId: 'tNEW', assistantMsg: phObj, controller: {} });
  SERVER = {
    rev: 9, updatedAt: 4000, settings: {},
    messages: [
      { role: 'user', content: 'q', _msgId: 'm0', timestamp: 1000 },
      { role: 'assistant', content: 'first answer', _msgId: 'm1',
        timestamp: 2000, _taskId: 'tOLD', finishReason: 'stop',
        usage: { total_tokens: 42 }, translatedContent: '第一个回答' },
      /* The brain kickoff — persisted server-side by the queue drain. */
      { role: 'user', content: '[Project Brain — autonomous dispatch] …',
        _msgId: 'kick1', timestamp: 3500, _brainDispatch: true,
        _brainEpic: { epicId: 'pt_x', route: 'creator', method: 'answered' } },
      /* The live turn's partial checkpoint — must NEVER be statically
       * inserted (its content is owned by the stream/poll lanes). */
      { role: 'assistant', content: 'partial streamed', _msgId: 'srvLive',
        timestamp: 3600, _taskId: 'tNEW' },
    ],
  };
}

(async () => {
  if (typeof _onConvNotifyPush !== 'function'
      || typeof loadConversationMessages !== 'function'
      || typeof _adoptInjectedSettledPrefix !== 'function') {
    console.log('FAIL fns_exposed'); process.exit(0);
  }
  check('fns_exposed', true);

  /* ── A. NOTIFY LANE: a rev-carrying frame lands while the stream is live ── */
  {
    seed();
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 9, userId: 1 });
    await sleep(250);
    const msgs = conversations[0].messages;
    check('A_kickoff_inserted_before_live_tail',
      msgs.length === 4 && msgs[2]._msgId === 'kick1');
    check('A_bound_placeholder_untouched', msgs[3] === phObj);
    check('A_kickoff_marker_intact', msgs[2]._brainDispatch === true);
    check('A_live_partial_not_inserted_statically',
      !msgs.some((m) => m._msgId === 'srvLive'));
    check('A_stale_settle_bar_topped_up',
      msgs[1].finishReason === 'stop' && msgs[1].usage
      && msgs[1].usage.total_tokens === 42);
    check('A_translation_still_lands', msgs[1].translatedContent === '第一个回答');
    check('A_repaint_via_streaming_composer',
      streamUICalls.length === 1 && replaceAllCalls.length === 0);
    check('A_rev_advanced', conversations[0]._serverRev === 9);
  }

  /* ── D. IDEMPOTENT: a second frame (same body, newer rev) inserts nothing ── */
  {
    /* continues A's state */
    SERVER.rev = 10;
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 10, userId: 1 });
    await sleep(250);
    const msgs = conversations[0].messages;
    check('D_no_duplicate_insert',
      msgs.length === 4
      && msgs.filter((m) => m._msgId === 'kick1').length === 1);
    check('D_no_second_repaint', streamUICalls.length === 1);
    check('D_rev_advanced', conversations[0]._serverRev === 10);
  }

  /* ── C. ANCHOR MISS: the server window does not reach our settled tail →
   *      refuse, and leave _serverRev unread so a later read reconciles ── */
  {
    seed();
    SERVER = { rev: 9, updatedAt: 4000, settings: {},
      messages: [SERVER.messages[2], SERVER.messages[3]] };  // [kick, live] only
    _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 9, userId: 1 });
    await sleep(250);
    const msgs = conversations[0].messages;
    check('C_anchor_miss_refuses_insert',
      msgs.length === 3 && !msgs.some((m) => m._msgId === 'kick1'));
    check('C_anchor_miss_rev_not_advanced', conversations[0]._serverRev === 8);
    check('C_anchor_miss_no_repaint',
      streamUICalls.length === 0 && replaceAllCalls.length === 0);
  }

  /* ── B. PHASE-2 LANE: loadConversationMessages with a live stream ── */
  {
    seed();
    conversations[0]._needsLoad = true;   // force the Phase-2 fetch
    await loadConversationMessages('c1');
    const msgs = conversations[0].messages;
    check('B_phase2_kickoff_inserted',
      msgs.length === 4 && msgs[2]._msgId === 'kick1');
    check('B_phase2_bound_placeholder_untouched', msgs[3] === phObj);
    check('B_phase2_live_partial_not_static',
      !msgs.some((m) => m._msgId === 'srvLive'));
    check('B_phase2_settle_bar_topped_up', msgs[1].finishReason === 'stop');
    check('B_phase2_repaint_via_streaming_composer',
      streamUICalls.length === 1 && replaceAllCalls.length === 0);
  }

  console.log(out.join('\n'));
  console.log('__JSDOM_RESULT__ ' + JSON.stringify({
    pass: out.filter(l => l.startsWith('PASS')).length,
    fail: out.filter(l => l.startsWith('FAIL')).length,
  }));
  process.exit(0);
})();
"""


def _sources(*, neuter=False):
    override = None
    if neuter:
        target = os.path.join(JS_DIR, 'core', 'conv_reducers.js')
        with open(target, encoding='utf-8') as f:
            src = f.read()
        needle = ('  if (!candidates.length) return 0;\n'
                  '  localMsgs.splice(settledEnd, 0, ...candidates);\n'
                  '  return candidates.length;')
        assert src.count(needle) == 1, \
            'injected-prefix splice drifted — update the neuter target'
        src = src.replace(needle, '  return 0;  // NEUTERED: no insertion', 1)
        copy = os.path.join(HERE, '_conv_reducers_neutered_inject.js')
        with open(copy, 'w', encoding='utf-8') as f:
            f.write(src)
        override = {'core/conv_reducers.js': copy}
    fam = conv_family_sources(override=override)
    cts = os.path.join(JS_DIR, 'core', 'cross_tab_sync.js')
    return fam, fam[0], fam[1:] + [cts]


def test_injected_turn_visible_during_stream():
    _fam, target, extras = _sources()
    run_harness(
        target_js=target,
        extra_targets=extras,
        body_js=_HARNESS,
        expect_pass=20,
        label='injected-turn-stream-visible',
    )


def test_NC_injected_turn_reducer_is_load_bearing(tmp_path):
    """NEUTER (the reducer's splice removed): both adoption lanes go dark —
    the kickoff stays invisible during the stream (the incident shape), while
    the lane-side field top-ups (finishReason / translation — not routed
    through the reducer) keep passing, pinning the two-layer story."""
    import subprocess
    _fam, target, extras = _sources(neuter=True)
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.js', dir=HERE, delete=False) as fh:
        hp = fh.name
        fh.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', hp, target, ROOT, *extras], capture_output=True, text=True,
            timeout=90,
            env={**os.environ, 'JSDOM_HARNESS': os.path.join(HERE, '_jsdom_harness.js')})
    finally:
        os.remove(hp)
        _neu = os.path.join(HERE, '_conv_reducers_neutered_inject.js')
        if os.path.exists(_neu):
            os.remove(_neu)
    outtxt = proc.stdout
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{outtxt}'
    assert 'FAIL A_kickoff_inserted_before_live_tail' in outtxt, (
        'NEUTER did not bite on the notify lane — kickoff appeared anyway.\n' + outtxt)
    assert 'FAIL B_phase2_kickoff_inserted' in outtxt, (
        'NEUTER did not bite on the Phase-2 lane — kickoff appeared anyway.\n' + outtxt)
    assert 'PASS A_stale_settle_bar_topped_up' in outtxt, (
        'the lane-side terminal top-up must survive the reducer neuter.\n' + outtxt)
    assert 'PASS A_translation_still_lands' in outtxt, (
        'the lane-side translation merge must survive the reducer neuter.\n' + outtxt)
