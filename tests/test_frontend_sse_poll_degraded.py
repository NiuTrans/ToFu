"""tests/test_frontend_sse_poll_degraded.py — poll mode as a FIRST-CLASS
degraded state (2026-08-06, epic pt_6cb1607e).

WHY (measured incident, 2026-08-06 14:02:25)
--------------------------------------------
The VS Code tunnel killed all four live SSE streams in the same second. The
resume budget burned out inside the ~3s flap window (zero backoff — fixed in
tests/test_frontend_sse_resume_retry.py), and every conv surrendered to
`_pollFallback` — PERMANENTLY. Three defects this suite pins:

  1. FALSE STALL BANNER — the stall watch (ui/stall_watch.js) is fed ONLY by
     SSE frames, but heartbeat self-ticks only ever EXIST on the SSE lane, so
     the moment a conv surrendered to poll, "not even heartbeat frames
     arrived" became structurally true and the 「已停滞 · 疑似卡死」 banner
     fired 300s later against a HEALTHY, actively-polling turn (the Stop
     affordance nearly killed a live 60-round task: server-side it kept
     producing past 15:10). Poll mode must clear the watch at entry; poll
     responses ARE the liveness proof there.
  2. NO SELF-HEAL — the surrendered conv polled full snapshots (~968KB every
     ~2s) for the rest of the turn, even after the tunnel healed seconds
     later. Poll mode now re-probes the SSE lane every
     `_SSE_REPROBE_INTERVAL_MS` (30s): CURSOR-LESS (poll snapshots overwrite
     content wholesale, so a stale-cursor replay would double the text — the
     probe takes the backend-folded `state` snapshot, the page-reload shape),
     and on attach `_trySSE` owns the stream to done while poll yields.
  3. GHOST WRITES — the poll loop held its closure `assistantMsg` for the
     rest of the turn; a Phase-2 reload replacing conv.messages mid-poll
     would ghost the ref (merges land on an object nothing renders — the
     frozen-bubble mechanism). The merge now re-resolves by stable `_msgId`
     (then `_taskId`) on every successful poll, mirroring dispatchSSEEvent's
     `_rebindAssistant`.

Harness: bare node (the same pattern as test_frontend_poll_null_resp.py),
evaluating the REAL shipped sse_poll_fallback.js with scripted Api.chat.poll /
_trySSE stubs. Scenarios:
  A — entry clears the watch; interval not elapsed ⇒ ZERO probes; poll
      snapshots paint into the IN-TREE placeholder (identity); done path
      finalizes exactly once.
  B — a wholesale conv.messages replacement between polls (the Phase-2
      reload shape) ⇒ the NEXT poll rebinds by _msgId and writes into the
      NEW in-tree object; the ghost ref freezes.
  C — interval 0 ⇒ a failed probe resumes polling, a later probe that
      attaches hands the turn back (poll yields without finalizing); every
      probe enters CURSOR-LESS and clears the persisted cursor.
Plus source-wiring ratchets so the seams rot with the code.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_frontend_sse_poll_degraded.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
POLL_JS = runtime_section_path('ui/sse_poll_fallback.js')
ENDPOINT_PROJECTION_JS = runtime_section_path('ui/endpoint_turn_projection.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
/* NOTE: no 'use strict' here — strict-mode eval gets its own scope and the
 * eval'd `_pollFallback` would NOT leak into module scope (bare-eval harness
 * pattern shared with test_frontend_poll_null_resp.py). */
const fs = require('fs');
const CFG = JSON.parse(process.argv[3]);
global.window = global;
if (CFG.reprobeMs != null) global._SSE_REPROBE_INTERVAL_MS = CFG.reprobeMs;

const calls = { stallWatchClear: [], clearSseCursor: [], trySSE: [],
                pollQueries: [],
                turnSnapshots: [], startStreaming: [], replaceAll: 0,
                twUpdate: 0, twStop: 0, finishStream: 0, save: 0 };
global.stallWatchClear = (t) => { calls.stallWatchClear.push(t); };
global._clearSseCursor = (t) => { calls.clearSseCursor.push(t); };
global.twUpdate = () => {
  calls.twUpdate++;
  if (typeof conv !== 'undefined')
    calls.turnSnapshots.push(JSON.parse(JSON.stringify(conv.messages)));
};
global.twStop = () => { calls.twStop++; };
global.finishStream = () => { calls.finishStream++; };
global.saveConversations = () => { calls.save++; };
global.debugLog = () => {};
global.showToast = () => {};
global._checkServerHealth = async () => true;
global._reportClientError = () => {};
global.setStreamPhase = () => {};
global._maskVuMachineTokens = (s) =>
  String(s || '').replace(/\[(?:VU|PROGRESS):[^\]]*\]/gi, '').trim();
let _mid = 0;
global._ensureMsgId = (m) => {
  if (m && !m._msgId) m._msgId = 'poll-mid-' + (++_mid);
  return m;
};
global.ConvView = {
  replaceAll: () => { calls.replaceAll++; },
  startStreaming: (convId, opts) => {
    calls.startStreaming.push({ convId, opts });
  },
};
global.projectColdSnapshot = (s) => ({ content: s.content || '', thinking: s.thinking || '',
                                      toolRounds: s.toolRounds || [] });
global._resolveAssistantById = (conv, id, fb) => {
  for (let i = conv.messages.length - 1; i >= 0; i--) {
    const m = conv.messages[i];
    if (m && m._msgId === id) return m;
  }
  return fb;
};
global._resolveAssistantByTaskId = (conv, tid) => {
  for (let i = conv.messages.length - 1; i >= 0; i--) {
    const m = conv.messages[i];
    if (m && m.role === 'assistant' && m._taskId === tid) return m;
  }
  return null;
};

const conv = { id: 'conv-dg', activeTaskId: 'task-dg-1', messages: [
  { role: 'user', content: 'q', _msgId: 'm-user' },
  { role: 'assistant', content: '', thinking: '', toolRounds: [],
    _msgId: 'm-ph', _taskId: 'task-dg-1' },
] };
global.conversations = [conv];
global.activeConvId = 'conv-dg';
const placeholder = conv.messages[1];   // the connectToTask recovery placeholder

let pi = 0;
global.Api = { chat: { poll: async (tid, opts) => {
  calls.pollQueries.push(opts && opts.query
    ? { incr: opts.query.incr, ifp: opts.query.ifp || null } : null);
  const step = CFG.polls[Math.min(pi, CFG.polls.length - 1)];
  pi++;
  if (step.swapMessages) {
    /* Simulate a Phase-2 reload BETWEEN polls: wholesale replace
     * conv.messages with fresh server objects (same _msgId, server-side
     * snapshot content). The closure placeholder is now a GHOST. */
    conv.messages = [
      { role: 'user', content: 'q', _msgId: 'm-user' },
      { role: 'assistant', content: step.swapContent, thinking: '', toolRounds: [],
        _msgId: 'm-ph', _taskId: 'task-dg-1' },
    ];
  }
  return { ok: true, status: 200, headers: { get: () => null },
           json: async () => step.data };
} } };

let ti = 0;
global._trySSE = async (convId, taskId, strm, msg) => {
  calls.trySSE.push({ cursorNull: strm._lastEventId == null });
  const step = (CFG.probes && CFG.probes.length)
    ? CFG.probes[Math.min(ti, CFG.probes.length - 1)] : 'fail';
  ti++;
  return step === 'done';
};

eval(fs.readFileSync(process.argv[4], 'utf8'));   // endpoint_turn_projection.js
eval(fs.readFileSync(process.argv[2], 'utf8'));   // ui/sse_poll_fallback.js (real)

(async () => {
  /* The stale surrender-time cursor the tunnel left behind. */
  const stream = { controller: { signal: { aborted: false } }, _lastEventId: '8502' };
  await _pollFallback('conv-dg', 'task-dg-1', stream, placeholder);
  const inTree = conv.messages[conv.messages.length - 1];
  process.stdout.write('\n' + JSON.stringify({
    calls,
    pollCount: pi,
    ghostContent: placeholder.content,
    inTreeContent: inTree.content,
    inTreeIsPlaceholder: inTree === placeholder,
    messages: conv.messages,
  }));
})();
"""


def _js(obj) -> str:
    """JSON.stringify wire shape (no spaces) — what the JS echo produces."""
    return json.dumps(obj, separators=(',', ':'))


def _run(cfg: dict) -> dict:
    probe = os.path.join(ROOT, 'node_modules', '.tmp_poll_degraded_harness.js')
    os.makedirs(os.path.dirname(probe), exist_ok=True)
    with open(probe, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        p = subprocess.run([
            'node', probe, POLL_JS, json.dumps(cfg),
            ENDPOINT_PROJECTION_JS,
        ],
                           capture_output=True, text=True, timeout=60, cwd=ROOT)
    finally:
        os.unlink(probe)
    assert p.returncode == 0, f'node failed: {p.stderr}\n{p.stdout}'
    return json.loads(p.stdout.strip().splitlines()[-1])


_RUN1 = {'data': {'status': 'running', 'content': 'x' * 10, 'thinking': 't1',
                  'contentEpoch': 0, 'toolRounds': [],
                  'taskId': 'task-dg-1', 'phase': None}}
_RUN2 = {'data': {'status': 'running', 'content': 'y' * 30, 'thinking': 't1t2',
                  'contentEpoch': 0, 'toolRounds': [],
                  'taskId': 'task-dg-1', 'phase': None}}
_DONE = {'data': {'status': 'done', 'content': 'z' * 40, 'thinking': 't1t2',
                  'contentEpoch': 0, 'toolRounds': [],
                  'taskId': 'task-dg-1', 'finishReason': 'stop'}}


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_entry_clears_stall_watch_and_poll_paints_in_tree():
    """Scenario A: entering poll mode clears the stall watch (the banner can
    never fire against an actively-polling turn); the reprobe interval keeps
    SSE probes at bay; snapshots paint into the SAME in-tree placeholder
    (identity) and the terminal poll finalizes exactly once."""
    r = _run({'reprobeMs': 1e15, 'polls': [_RUN1, _RUN2, _DONE]})
    assert all(q and q['incr'] == 1 and q['ifp'] is None for q in r['calls']['pollQueries']), \
        f"every degraded poll opts into incr mode; no fp to echo when responses carry none: {r}"
    assert r['calls']['stallWatchClear'] == ['task-dg-1'], \
        f"poll entry must clear the stall watch for the surrendered task: {r}"
    assert r['calls']['trySSE'] == [], \
        f"no probe before the interval elapses: {r}"
    assert r['inTreeIsPlaceholder'] is True and r['inTreeContent'] == 'z' * 40, \
        f"poll snapshots must paint into the in-tree bubble: {r}"
    assert r['calls']['twUpdate'] >= 2, f"the paint seam runs per poll: {r}"
    assert r['calls']['finishStream'] == 1 and r['calls']['twStop'] == 1, \
        f"terminal poll finalizes exactly once: {r}"


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_rebind_after_phase2_replace_writes_in_tree_not_ghost():
    """Scenario B: conv.messages is wholesale-replaced BETWEEN polls (the
    Phase-2 reload shape). The next poll must re-resolve by _msgId and write
    into the NEW in-tree object — the ghost ref must freeze."""
    fp_a = {'c': 11, 't': 22, 'r': [0, 0]}
    fp_b = {'c': 33, 't': 22, 'r': [0, 0]}
    run1 = {'data': dict(_RUN1['data'], fp=fp_a)}
    swap2 = {'swapMessages': True, 'swapContent': 's' * 20,
             'data': {'status': 'running', 'content': 'y' * 30, 'thinking': '',
                      'toolRounds': [], 'taskId': 'task-dg-1', 'phase': None,
                      'fp': fp_b}}
    done = {'data': dict(_DONE['data'], fp=fp_b)}
    r = _run({'reprobeMs': 1e15, 'polls': [run1, swap2, done]})
    assert r['ghostContent'] == 'x' * 10, \
        f"the ghost ref must stop receiving merges after the replace: {r}"
    assert r['inTreeIsPlaceholder'] is False, \
        f"after the replace the live object is the server copy: {r}"
    assert r['inTreeContent'] == 'z' * 40, \
        f"post-rebind merges land on the in-tree object: {r}"
    assert r['calls']['finishStream'] == 1, r
    # Incr-poll echo discipline across the rebind: poll #2 echoes fp_a; the
    # swap fires the rebind, which must FORCE the next pull full (ifp gone) —
    # the fp describes bytes only the ghost object holds.
    qs = r['calls']['pollQueries']
    assert qs[1]['ifp'] == _js(fp_a), f'poll #2 echoes the last fp: {qs}'
    assert qs[2]['ifp'] is None, f'a rebind must force the next pull FULL: {qs}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_incr_omissions_keep_state_and_echo_fp():
    """Scenario D (epic pt_688f9783): the echo-fingerprint incremental merge.
    Poll 1 is full and carries `fp`; poll 2 omits every fat section behind
    `*Same` markers — the merge must KEEP the accumulated state (not clobber
    it with undefined); poll 3's terminal full body still settles the turn.
    The echo itself is pinned: poll 2's request carries poll 1's fp verbatim.
    """
    fp = {'c': 111, 't': 222, 'r': [1, 5]}
    full1 = {'data': {'status': 'running', 'content': 'x' * 10, 'thinking': 't1',
                      'toolRounds': [{'round': 1}], 'taskId': 'task-dg-1',
                      'phase': None, 'fp': fp}}
    omit2 = {'data': {'status': 'running', 'contentSame': True, 'thinkingSame': True,
                      'roundsSame': True, 'taskId': 'task-dg-1', 'phase': None,
                      'fp': fp}}
    done3 = {'data': {'status': 'done', 'content': 'x' * 10 + '!final', 'thinking': 't1',
                      'toolRounds': [{'round': 1}], 'taskId': 'task-dg-1',
                      'finishReason': 'stop', 'fp': fp}}
    r = _run({'reprobeMs': 1e15, 'polls': [full1, omit2, done3]})
    qs = r['calls']['pollQueries']
    assert qs[0]['incr'] == 1 and qs[0]['ifp'] is None, f'first contact is full: {qs}'
    assert qs[1]['ifp'] == _js(fp), f'the echo is verbatim: {qs}'
    assert r['inTreeContent'] == 'x' * 10 + '!final', \
        f'omissions must KEEP state, the terminal full body must settle: {r}'
    assert r['calls']['finishStream'] == 1 and r['calls']['twStop'] == 1, r


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_incr_tool_round_deltas_replace_tail_and_append_without_losing_prefix():
    """The bounded tool-round wire patch must reconstruct the exact live list:
    a mutable-tail update replaces that row and a later append preserves both
    sealed rows.  This is the client half of the large-poll regression."""
    fp1 = {'c': [0, 1], 't': [0, 2], 'r': [2, 10]}
    fp2 = {'c': [0, 1], 't': [0, 2], 'r': [2, 11]}
    fp3 = {'c': [0, 1], 't': [0, 2], 'r': [3, 12]}
    closed = {'roundNum': 1, 'status': 'done', 'result': 'sealed'}
    live_a = {'roundNum': 2, 'status': 'running', 'result': 'partial-a'}
    live_b = {'roundNum': 2, 'status': 'done', 'result': 'partial-b'}
    appended = {'roundNum': 3, 'status': 'running', 'result': 'new'}
    full = {'data': {
        'status': 'running', 'content': '', 'thinking': '',
        'toolRounds': [closed, live_a], 'taskId': 'task-dg-1', 'fp': fp1,
    }}
    replace_tail = {'data': {
        'status': 'running', 'contentSame': True, 'thinkingSame': True,
        'toolRoundsDelta': {'start': 1, 'rows': [live_b]},
        'taskId': 'task-dg-1', 'fp': fp2,
    }}
    append = {'data': {
        'status': 'running', 'contentSame': True, 'thinkingSame': True,
        'toolRoundsDelta': {'start': 2, 'rows': [appended]},
        'taskId': 'task-dg-1', 'fp': fp3,
    }}
    done = {'data': {
        'status': 'done', 'content': 'done', 'thinking': '',
        'toolRounds': [closed, live_b, appended], 'taskId': 'task-dg-1',
        'finishReason': 'stop', 'fp': fp3,
    }}
    r = _run({'reprobeMs': 1e15, 'polls': [full, replace_tail, append, done]})
    snaps = r['calls']['turnSnapshots']
    assert snaps[1][-1]['toolRounds'] == [closed, live_b], r
    assert snaps[2][-1]['toolRounds'] == [closed, live_b, appended], r
    assert r['calls']['pollQueries'][1]['ifp'] == _js(fp1), r
    assert r['calls']['pollQueries'][2]['ifp'] == _js(fp2), r


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_incr_tool_round_delta_with_missing_local_base_forces_full_retry():
    """A reload/rebind race may erase the local patch base.  The client must
    not apply a gapped delta and must omit ifp on the next request so the
    server self-heals with a full snapshot."""
    fp = {'c': [0, 1], 't': [0, 2], 'r': [4, 10]}
    bad_patch = {'data': {
        'status': 'running', 'content': '', 'thinking': '',
        'toolRoundsDelta': {'start': 3, 'rows': [{'roundNum': 4}]},
        'taskId': 'task-dg-1', 'fp': fp,
    }}
    full = {'data': {
        'status': 'done', 'content': 'done', 'thinking': '',
        'toolRounds': [{'roundNum': i} for i in range(1, 5)],
        'taskId': 'task-dg-1', 'finishReason': 'stop', 'fp': fp,
    }}
    r = _run({'reprobeMs': 1e15, 'polls': [bad_patch, full]})
    assert r['calls']['pollQueries'][1]['ifp'] is None, r
    assert [x['roundNum'] for x in r['messages'][-1]['toolRounds']] == [1, 2, 3, 4], r


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_poll_adopts_authoritative_inter_round_reset():
    """A tool round may replace its pre-tool narration with an empty live
    deliverable while retaining the narration on toolRounds. Poll snapshots
    are authoritative for a live turn: the empty/shorter reset must replace
    the bottom content immediately, not remain duplicated until later text
    grows past the old narration length."""
    narration = 'I will inspect the current implementation before changing it.'
    prior_thinking = 'I need to inspect the data flow carefully.'
    round_after_reset = {
        'roundNum': 1, 'llmRound': 0, 'toolCallId': 'tc-reset',
        'toolName': 'read_files', 'status': 'searching',
        'assistantContent': narration, 'thinking': prior_thinking,
    }
    first = {'data': {
        'status': 'running', 'content': narration, 'thinking': prior_thinking,
        'contentEpoch': 0, 'toolRounds': [],
        'taskId': 'task-dg-1', 'phase': None,
    }}
    reset = {'data': {
        'status': 'running', 'content': '', 'thinking': '',
        'contentEpoch': 1, 'toolRounds': [round_after_reset],
        'taskId': 'task-dg-1', 'phase': {'phase': 'tool_exec'},
    }}
    new_tail = {'data': {
        'status': 'running', 'content': 'Final', 'thinking': '',
        'contentEpoch': 1,
        'toolRounds': [dict(round_after_reset, status='done')],
        'taskId': 'task-dg-1', 'phase': None,
    }}
    done = {'data': {
        'status': 'done', 'content': 'Final answer', 'thinking': '',
        'contentEpoch': 1,
        'toolRounds': [dict(round_after_reset, status='done')],
        'taskId': 'task-dg-1', 'finishReason': 'stop',
    }}
    r = _run({'reprobeMs': 1e15, 'polls': [first, reset, new_tail, done]})
    contents = [snap[-1].get('content') for snap in r['calls']['turnSnapshots']]
    thinking = [snap[-1].get('thinking') for snap in r['calls']['turnSnapshots']]
    assert contents[:3] == [narration, '', 'Final'], (
        'poll mode kept stale pre-tool narration in the bottom content zone; '
        f'expected narration → empty reset → new tail, got {contents}')
    assert thinking[:3] == [prior_thinking, '', ''], (
        'poll mode kept stale per-round thinking in the bottom thinking zone; '
        f'expected thinking → empty reset, got {thinking}')
    reset_msg = r['calls']['turnSnapshots'][1][-1]
    assert reset_msg['toolRounds'][0].get('assistantContent') == narration, (
        'inter-round narration must remain preserved on its tool round')
    assert reset_msg['toolRounds'][0].get('thinking') == prior_thinking, (
        'inter-round thinking must remain preserved on its tool round')


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_poll_keeps_longer_text_within_same_epoch_and_rejects_stale_epoch():
    """A shorter poll is authoritative only after an explicit reset epoch.
    Same-epoch lag and an older generation must not erase newer client text."""
    first = {'data': {
        'status': 'running', 'content': 'already streamed text', 'thinking': 'reasoning',
        'contentEpoch': 2, 'toolRounds': [], 'taskId': 'task-dg-1', 'phase': None,
    }}
    lagging = {'data': {
        'status': 'running', 'content': 'old', 'thinking': 'r',
        'contentEpoch': 2, 'toolRounds': [], 'taskId': 'task-dg-1', 'phase': None,
    }}
    reset = {'data': {
        'status': 'running', 'content': '', 'thinking': '',
        'contentEpoch': 3, 'toolRounds': [], 'taskId': 'task-dg-1', 'phase': None,
    }}
    stale = {'data': {
        'status': 'running', 'content': 'resurrected old narration', 'thinking': 'old think',
        'contentEpoch': 2, 'toolRounds': [], 'taskId': 'task-dg-1', 'phase': None,
    }}
    done = {'data': {
        'status': 'done', 'content': 'final answer', 'thinking': '',
        'contentEpoch': 3, 'toolRounds': [], 'taskId': 'task-dg-1',
        'finishReason': 'stop',
    }}
    r = _run({'reprobeMs': 1e15, 'polls': [first, lagging, reset, stale, done]})
    contents = [snap[-1].get('content') for snap in r['calls']['turnSnapshots']]
    assert contents[:4] == [
        'already streamed text', 'already streamed text', '', '',
    ], f'same/stale epoch snapshot clobbered client state: {contents}'
    assert r['inTreeContent'] == 'final answer', r


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_reprobe_cursorless_and_hands_back_to_sse():
    """Scenario C (interval 0): a failed probe resumes polling; a later probe
    that attaches runs the turn to done and poll YIELDS (no poll-lane
    finalize). Every probe enters cursor-less AND clears the persisted
    cursor — poll snapshots made the stale cursor's incremental replay
    unsafe (it would double the text)."""
    r = _run({'reprobeMs': 0, 'probes': ['fail', 'done'],
              'polls': [_RUN1, _RUN2, _DONE]})
    assert len(r['calls']['trySSE']) == 2, f"failed probe ⇒ keep polling, later probe re-fires: {r}"
    assert all(c['cursorNull'] for c in r['calls']['trySSE']), \
        f"every re-probe must connect CURSOR-LESS (snapshot sync, not delta replay): {r}"
    assert r['calls']['clearSseCursor'] == ['task-dg-1', 'task-dg-1'], \
        f"the persisted cursor must be cleared per probe: {r}"
    assert r['pollCount'] == 1, \
        f"the successful probe hands back immediately — no further polls: {r}"
    assert r['calls']['finishStream'] == 0 and r['calls']['twStop'] == 0, \
        f"on handback _trySSE owns finalization; the poll lane must not double-finalize: {r}"


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_flow_autopilot_poll_projects_vu_and_discards_task_done_placeholder():
    """SSE-down parity: poll must show a VU, never a Critic, and clean it on
    TASK_DONE even when endpointTurns is unchanged in the terminal response."""
    running = {'data': {
        'status': 'running', 'endpointMode': True, 'flowMode': True,
        'flowProjection': 'autopilot', 'endpointPhase': 'reviewing',
        'endpointIteration': 1, 'turnRole': 'virtual_user', 'emits': 'user',
        'vuMsgId': 'vu-poll-1', 'autopilotRunId': 'run-poll-1',
        'endpointTurns': [{
            'role': 'assistant', 'content': 'worker result',
            '_epIteration': 1, '_msgId': 'worker-poll-1',
        }],
        'content': '[PROGRESS: resolved=1 remaining=1] Keep going',
        'thinking': '', 'toolRounds': [], 'taskId': 'task-dg-1',
    }}
    terminal = {'data': {
        'status': 'done', 'endpointMode': True, 'flowMode': True,
        'flowProjection': 'autopilot', 'endpointPhase': 'done',
        'endpointStopReason': 'completed', 'endpointIteration': 1,
        'turnRole': 'virtual_user', 'emits': 'user',
        'vuMsgId': 'vu-poll-1', 'autopilotRunId': 'run-poll-1',
        'endpointTurnsSame': True, 'contentSame': True,
        'thinkingSame': True, 'roundsSame': True,
        'taskId': 'task-dg-1', 'finishReason': 'stop',
    }}
    r = _run({'reprobeMs': 1e15, 'polls': [running, terminal]})
    first = r['calls']['turnSnapshots'][0]
    vu = next((m for m in first if m.get('_msgId') == 'vu-poll-1'), None)
    assert vu and vu.get('_isVirtualUser') is True, r
    assert not vu.get('_isEndpointReview'), r
    assert vu.get('_autopilotRunId') == 'run-poll-1', r
    assert vu.get('content') == 'Keep going', r
    assert any(c['opts'].get('role') == 'autopilot'
               and c['opts'].get('msgId') == 'vu-poll-1'
               for c in r['calls']['startStreaming']), r
    assert not any(m.get('_msgId') == 'vu-poll-1' for m in r['messages']), r
    assert r['calls']['finishStream'] == 1 and r['calls']['twStop'] == 1, r


def test_source_wires_degraded_mode_seams():
    """Source-scan ratchets: strip a seam and the behavioural harnesses above
    could rot green. Pin the four load-bearing lines."""
    src = open(POLL_JS, encoding='utf-8').read()
    assert 'stallWatchClear(taskId)' in src, \
        'poll entry no longer clears the stall watch — the false 「已停滞」 banner returns'
    assert 'const _SSE_REPROBE_INTERVAL_MS' in src, \
        'reprobe cadence const removed — degraded mode is permanent again'
    assert '_lastSseReprobe = Date.now();' in src, \
        'reprobe clock removed'
    assert '_clearSseCursor(taskId)' in src and 'stream._lastEventId = null;' in src, \
        'cursor-less probe discipline removed — stale-cursor replay doubles poll-snapshot text'
    assert src.count('await _trySSE(convId, taskId, stream, assistantMsg)') >= 1, \
        'the SSE re-probe call was removed from the poll loop'
    assert '_resolveAssistantById' in src and '_resolveAssistantByTaskId' in src, \
        'the stable-id rebind was removed — ghost writes after a Phase-2 replace return'
    # Incr-poll seams (pt_688f9783): drop any of these and every poll silently
    # goes back to hauling the full ~1MB snapshot through the tunnel.
    assert "const _pollQuery = { incr: 1 };" in src, 'poll requests no longer opt into incr mode'
    assert '_pollQuery.ifp = JSON.stringify(_pollFP)' in src, 'the fp echo was removed'
    assert 'data.contentSame' in src and 'data.roundsSame' in src, 'the omission merge guards were removed'
    assert '_pollFP = null;' in src, 'the rebind→force-full reset was removed'
    assert 'data.contentEpoch' in src and 'epochAdvanced' in src and 'epochStale' in src, \
        'the generation-aware shrink contract was removed from degraded polling'
    assert 'projectEndpointChatTurn(' in src, \
        'poll fallback no longer consumes shared Flow turn identity policy'
    assert '_flowPollInFlight' in src, \
        'poll fallback lost stable in-flight flow-turn projection/cleanup'


if __name__ == '__main__':
    test_entry_clears_stall_watch_and_poll_paints_in_tree()
    print('PASS test_entry_clears_stall_watch_and_poll_paints_in_tree')
    test_rebind_after_phase2_replace_writes_in_tree_not_ghost()
    print('PASS test_rebind_after_phase2_replace_writes_in_tree_not_ghost')
    test_incr_omissions_keep_state_and_echo_fp()
    print('PASS test_incr_omissions_keep_state_and_echo_fp')
    test_reprobe_cursorless_and_hands_back_to_sse()
    print('PASS test_reprobe_cursorless_and_hands_back_to_sse')
    test_source_wires_degraded_mode_seams()
    print('PASS test_source_wires_degraded_mode_seams')
    print('ALL GREEN')
