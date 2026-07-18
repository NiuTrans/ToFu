#!/usr/bin/env python3
"""END-TO-END acceptance test for the agent_inbox inject-row disappearance bug.

WHY THIS TEST EXISTS (the acceptance criterion)
------------------------------------------------
The reported bug: swarm ``<swarm-update>`` / peer / user-steer injection rows
(shown to the human as in-timeline synthetic tool rows) VANISH when switching
conversations or refreshing. The prior unit tests
(``test_inbox_inject_wire_purity`` / ``test_inbox_inject_sidecar_wire_neutral``)
call the wire reconstructor DIRECTLY — they prove the wire stays byte-identical,
but they do NOT exercise the live persist→reload→rehydrate path, so they cannot
prove the rows actually REAPPEAR after reload. That reappearance IS the
objective, so this test drives the REAL shipped code end to end:

  1. BACKEND PERSIST — the real ``_persist_inject_sidecars`` (manager/_sync.py)
     copies the task's ``_inboxInjects`` / ``_peerInjects`` / ``_userSteerInjects``
     lanes onto the settled assistant message dict.
  2. DB ROUND-TRIP — the message is serialized to JSON and back (what the
     ``conversations.messages`` column does), proving the underscore sidecars
     survive storage with no whitelist stripping them.
  3. WIRE NEUTRALITY — the real ``_reconstruct_tool_call_messages``
     (conv_message_builder) rebuilds the wire ``assistant(tool_calls)+tool``
     sequence from the reloaded message. It must be BYTE-IDENTICAL to the same
     message with NO inbox sidecars — i.e. injecting the rows perturbs neither
     tool-turn continuation nor the prefix-cache bytes.
  4. FRONTEND REHYDRATE — the real shipped ``getToolRoundsFromMsg`` +
     ``_rehydrateInjectRows`` (extracted from static/js/core.js, run in node)
     reproduce the inbox/peer/steer DISPLAY rows from the reloaded sidecar, so
     the user sees them again after reload.

Acceptance = 1+2+3 (backend, python) AND 4 (frontend, node). The frontend leg
skips cleanly when node is unavailable; the python legs always run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CORE_JS = os.path.join(ROOT, 'static', 'js', 'core.js')


# ── Shared fixtures: a genuine 2-round tool turn + the three inject lanes ──

def _real_tool_rounds() -> list[dict]:
    """Two genuine, wire-valid tool rounds (the ONLY thing allowed on the wire)."""
    return [
        {'roundNum': 1, 'llmRound': 0, 'toolCallId': 'tc_1', 'toolName': 'web_search',
         'toolArgs': '{"q":"gil"}', 'toolContent': 'result A', 'status': 'done',
         'assistantContent': 'Let me search.'},
        {'roundNum': 2, 'llmRound': 1, 'toolCallId': 'tc_2', 'toolName': 'read_files',
         'toolArgs': '{"path":"a.py"}', 'toolContent': 'file body', 'status': 'done'},
    ]


def _task_with_injects() -> dict:
    """A settled task carrying all three inject lanes (what the orchestrator
    accumulates in _run.py at the SWARM_INBOX_INJECT / peer / steer emit sites)."""
    return {
        '_inboxInjects': [
            {'round': 0, 'count': 2, 'agentIds': ['a1', 'a2'],
             'previews': [{'agentId': 'a1', 'text': 'done A'}, {'agentId': 'a2', 'text': 'done B'}]},
        ],
        '_peerInjects': [
            {'round': 1, 'count': 1, 'previews': [{'fromConv': 'sib1', 'text': 'peer note'}]},
        ],
        '_userSteerInjects': [
            {'round': 1, 'count': 1, 'previews': [{'text': 'focus on X'}]},
        ],
    }


def _db_roundtrip(msg: dict) -> dict:
    """Serialize the message the way the conversations.messages JSON column does
    and read it back — proves nothing strips the underscore sidecars."""
    return json.loads(json.dumps(msg, ensure_ascii=False))


# ─────────────────────────── BACKEND legs (1 + 2 + 3) ───────────────────────

def test_sidecars_persist_and_survive_db_roundtrip():
    """Leg 1 + 2: _persist_inject_sidecars writes the lanes onto the message and
    they survive a JSON store/reload verbatim."""
    from lib.tasks_pkg.manager._sync import _persist_inject_sidecars

    task = _task_with_injects()
    msg = {'role': 'assistant', 'content': 'The answer.', 'toolRounds': _real_tool_rounds()}

    wrote = _persist_inject_sidecars(task, msg)
    assert wrote is True

    reloaded = _db_roundtrip(msg)
    for key in ('_inboxInjects', '_peerInjects', '_userSteerInjects'):
        assert reloaded.get(key) == task[key], f'{key} did not survive DB round-trip'
    # The genuine tool rounds are untouched — no synthetic row folded in.
    assert len(reloaded['toolRounds']) == 2
    assert all(r.get('toolCallId') for r in reloaded['toolRounds'])


def test_reloaded_wire_is_byte_identical_to_no_inbox_baseline():
    """Leg 3 (THE prefix-cache / tool-turn-continuation guarantee): the wire
    reconstruction of a reloaded message WITH inbox sidecars is byte-identical to
    the SAME message with NO inbox data. Injecting the rows changes zero wire
    bytes — so no cache miss, no collapsed turn."""
    from lib.tasks_pkg.manager._sync import _persist_inject_sidecars
    from lib.tasks_pkg.conv_message_builder._toolcalls import (
        _reconstruct_tool_call_messages,
    )

    # Baseline: identical turn, no inbox lanes at all.
    baseline_msg = {'role': 'assistant', 'content': 'The answer.',
                    'toolRounds': _real_tool_rounds()}
    baseline_wire = _reconstruct_tool_call_messages(baseline_msg['toolRounds'])
    assert baseline_wire is not None, 'baseline turn must reconstruct (sanity)'

    # With injects: persist → DB round-trip → reconstruct.
    inbox_msg = {'role': 'assistant', 'content': 'The answer.',
                 'toolRounds': _real_tool_rounds()}
    _persist_inject_sidecars(_task_with_injects(), inbox_msg)
    reloaded = _db_roundtrip(inbox_msg)
    inbox_wire = _reconstruct_tool_call_messages(reloaded['toolRounds'])

    assert json.dumps(inbox_wire, sort_keys=True) == json.dumps(baseline_wire, sort_keys=True), (
        'wire diverged when inbox sidecars were present — prefix cache / '
        'tool-turn continuation would break')


def test_leaked_synthetic_row_still_wire_neutral():
    """Defense-in-depth: even if a synthetic row LEAKS into toolRounds (a legacy
    row, or a mid-stream PUT that beat the belt), the reconstructor's
    is_synthetic_inbox_round filter drops it → wire STILL byte-identical to the
    baseline. This is the last line of defense behind the PUT-hygiene belt."""
    from lib.tasks_pkg.conv_message_builder._toolcalls import (
        _reconstruct_tool_call_messages,
    )

    baseline_wire = _reconstruct_tool_call_messages(_real_tool_rounds())

    polluted = _real_tool_rounds()
    polluted.insert(1, {'roundNum': 9000001, 'status': 'done', '_inboxInject': True,
                        '_inboxKey': 'inbox:0', 'inboxRound': 0, 'inboxCount': 2,
                        'inboxAgentIds': ['a1'], 'inboxPreviews': [{'text': 'x'}]})
    polluted_wire = _reconstruct_tool_call_messages(polluted)

    assert json.dumps(polluted_wire, sort_keys=True) == json.dumps(baseline_wire, sort_keys=True), (
        'a leaked synthetic row perturbed the wire — the is_synthetic_inbox_round '
        'guard is not filtering it')


# ─────────────────────────── FRONTEND leg (4) ───────────────────────────────

def _extract_rehydrate_fn() -> str:
    """Pull `getToolRoundsFromMsg` + `_rehydrateInjectRows` out of core.js so
    they eval standalone in node (module-private, only _rehydrateInjectRows is
    on window)."""
    src = open(CORE_JS, encoding='utf-8').read()
    start = src.index('function getToolRoundsFromMsg(')
    end = src.index('if (typeof window !== "undefined") {\n  window._rehydrateInjectRows')
    chunk = src[start:end]
    assert '_rehydrateInjectRows' in chunk and 'getToolRoundsFromMsg' in chunk, \
        'extraction missed the rehydrate functions'
    return chunk


_HARNESS = r"""
const fnSrc = process.env.FN_SRC;
eval(fnSrc);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Simulate the message AS RELOADED FROM DB: only real tool rounds in toolRounds,
// the inject data lives ONLY in the underscore sidecars (what the backend
// _persist_inject_sidecars wrote + survived the JSON column).
const reloaded = {
  role: 'assistant', content: 'The answer.',
  toolRounds: [
    { roundNum: 1, llmRound: 0, toolCallId: 'tc_1', toolName: 'web_search',
      toolArgs: '{"q":"gil"}', toolContent: 'result A', status: 'done' },
    { roundNum: 2, llmRound: 1, toolCallId: 'tc_2', toolName: 'read_files',
      toolArgs: '{"path":"a.py"}', toolContent: 'file body', status: 'done' },
  ],
  _inboxInjects: [{ round: 0, count: 2, agentIds: ['a1', 'a2'],
                   previews: [{ agentId: 'a1', text: 'done A' }, { agentId: 'a2', text: 'done B' }] }],
  _peerInjects: [{ round: 1, count: 1, previews: [{ fromConv: 'sib1', text: 'peer note' }] }],
  _userSteerInjects: [{ round: 1, count: 1, previews: [{ text: 'focus on X' }] }],
};

const rows = getToolRoundsFromMsg(reloaded);

// The two real rounds survive + one synthetic row per lane is rebuilt = 5.
check('real_rounds_present',
  rows.filter(r => r.toolCallId).length === 2);
const inbox = rows.filter(r => r._inboxInject);
const peer = rows.filter(r => r._peerInject);
const steer = rows.filter(r => r._userSteerInject);
check('inbox_row_rehydrated', inbox.length === 1);
check('peer_row_rehydrated', peer.length === 1);
check('steer_row_rehydrated', steer.length === 1);
// The preview `text` payload survives (the beautified card renders from it).
check('inbox_preview_text_survives',
  inbox[0] && Array.isArray(inbox[0].inboxPreviews) &&
  inbox[0].inboxPreviews.length === 2 &&
  inbox[0].inboxPreviews[0].text === 'done A');
check('peer_preview_text_survives',
  peer[0] && peer[0].peerPreviews[0].text === 'peer note');
check('steer_preview_text_survives',
  steer[0] && steer[0].steerPreviews[0].text === 'focus on X');

// CRITICAL: rehydration must NOT mutate the reloaded msg.toolRounds (else a
// later full-conv PUT re-leaks the synthetic rows into the DB).
check('source_toolRounds_not_mutated', reloaded.toolRounds.length === 2);
check('returns_a_copy', rows !== reloaded.toolRounds);

// Idempotent: a second call yields the same shape (no doubling).
const rows2 = getToolRoundsFromMsg(reloaded);
check('idempotent_no_double',
  rows2.filter(r => r._inboxInject).length === 1);

// A message with NO sidecars returns the base array untouched (fast path).
const plain = { role: 'assistant', content: 'hi', toolRounds: [reloaded.toolRounds[0]] };
check('no_sidecar_passthrough', getToolRoundsFromMsg(plain) === plain.toolRounds);

console.log(out.join('\n'));
"""

_NC_HARNESS = r"""
// NEUTER: force _rehydrateInjectRows to a no-op (return base) and prove the
// inbox rows DO NOT reappear — i.e. the rehydration is load-bearing for the
// objective (rows survive reload).
let fnSrc = process.env.FN_SRC;
// Replace the function body's first statement so it returns base immediately.
const neutered = fnSrc.replace(
  'function _rehydrateInjectRows(msg, base) {',
  'function _rehydrateInjectRows(msg, base) { return base; // NEUTERED');
if (neutered === fnSrc) { console.log('FAIL nc_pattern_matched'); process.exit(0); }
eval(neutered);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const reloaded = {
  role: 'assistant', content: 'x',
  toolRounds: [{ roundNum: 1, toolCallId: 'tc_1', toolName: 'web_search',
                 toolArgs: '{}', toolContent: 'r', status: 'done' }],
  _inboxInjects: [{ round: 0, count: 1, agentIds: ['a1'], previews: [{ text: 'x' }] }],
};
const rows = getToolRoundsFromMsg(reloaded);
// With rehydration neutered, the inbox row is GONE → confirms rehydration is
// exactly what makes the row reappear after reload.
check('nc_inbox_row_absent_without_rehydrate',
  rows.filter(r => r._inboxInject).length === 0);
console.log(out.join('\n'));
"""


def _run_node(harness: str, fn_src: str) -> str:
    env = dict(os.environ, FN_SRC=fn_src)
    proc = subprocess.run(['node', '-e', harness], capture_output=True,
                          text=True, timeout=30, env=env)
    assert proc.returncode == 0, f'node failed: {proc.stderr}'
    return proc.stdout.strip()


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_frontend_rehydrates_inbox_rows_after_reload():
    fn_src = _extract_rehydrate_fn()
    out = _run_node(_HARNESS, fn_src)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'rehydration failures:\n' + out
    assert out.count('PASS') >= 11, f'expected >=11 PASS, got:\n{out}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_NC_without_rehydrate_rows_stay_gone():
    fn_src = _extract_rehydrate_fn()
    out = _run_node(_NC_HARNESS, fn_src)
    assert 'PASS nc_inbox_row_absent_without_rehydrate' in out, (
        'NC control failed — rehydration is not load-bearing, or the pattern '
        'did not match:\n' + out)


# ═══════════════════ LIVE-SESSION adoption legs (5 + 6) ═════════════════════
# The DB-GET reload legs above prove the rows reappear after a fresh GET. But
# within a LIVE session the `done` event's committedMessage projection
# OVERWRITES assistantMsg.toolRounds with the backend's clean list, and a
# reconnect-state snapshot / poll-fallback likewise rebuilds the in-memory
# message. Unless those three seams ADOPT the sidecar onto assistantMsg, the
# chip vanishes until a manual refresh — the exact symptom, still visible
# in-session. These legs pin the three adoption seams in the SHIPPED files.

PIPE_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'sse_pipeline.js')
POLL_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'sse_poll_fallback.js')

_LANES = ('_inboxInjects', '_peerInjects', '_userSteerInjects')


def test_committedMessage_whitelist_carries_the_three_lanes():
    """Live `done` seam: the committedMessage projection loop in sse_pipeline.js
    must list all three sidecar keys, else the done event drops them off the
    in-memory message (chip vanishes mid-session until refresh)."""
    src = open(PIPE_JS, encoding='utf-8').read()
    # Isolate the projection whitelist array (from the for-loop to its close).
    i = src.index("for (const _k of ['finishReason'")
    j = src.index('if (_cm[_k] != null)', i)
    whitelist = src[i:j]
    for lane in _LANES:
        assert f"'{lane}'" in whitelist, (
            f'{lane} missing from the committedMessage whitelist — the done '
            f'projection would drop it and the chip vanishes in-session')


def test_reconnect_state_snapshot_adopts_the_three_lanes():
    """Live reconnect seam: the STATE snapshot handler in sse_pipeline.js must
    copy ev.inboxInjects/peerInjects/userSteerInjects onto assistantMsg."""
    src = open(PIPE_JS, encoding='utf-8').read()
    for camel, under in (('inboxInjects', '_inboxInjects'),
                         ('peerInjects', '_peerInjects'),
                         ('userSteerInjects', '_userSteerInjects')):
        assert f'ev.{camel}' in src and f'assistantMsg.{under} = ev.{camel}' in src, (
            f'reconnect-state snapshot does not adopt {camel} onto assistantMsg')


def test_poll_fallback_forwards_the_three_lanes():
    """Live poll seam: the poll-fallback in sse_poll_fallback.js must forward
    data.inboxInjects/peerInjects/userSteerInjects onto assistantMsg."""
    src = open(POLL_JS, encoding='utf-8').read()
    for camel, under in (('inboxInjects', '_inboxInjects'),
                         ('peerInjects', '_peerInjects'),
                         ('userSteerInjects', '_userSteerInjects')):
        assert f'data.{camel}' in src and f'assistantMsg.{under} = data.{camel}' in src, (
            f'poll-fallback does not forward {camel} onto assistantMsg')


_LIVE_HARNESS = r"""
const fnSrc = process.env.FN_SRC;
eval(fnSrc);
const whitelist = JSON.parse(process.env.WHITELIST);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Simulate the LIVE `done` event: assistantMsg accumulated a synthetic inbox
// row IN MEMORY during streaming (live push), then the done event delivers the
// backend's committedMessage whose toolRounds is the CLEAN list (no synthetic
// rows) plus the sidecar underscore fields. The projection loop below is the
// REAL whitelist extracted from the shipped sse_pipeline.js.
const assistantMsg = {
  role: 'assistant', content: 'partial',
  toolRounds: [  // live in-memory: a synthetic row was pushed during streaming
    { roundNum: 1, toolCallId: 'tc_1', toolName: 'web_search', toolArgs: '{}',
      toolContent: 'r', status: 'done' },
    { roundNum: 9000001, status: 'done', _inboxInject: true, _inboxKey: 'inbox:0',
      inboxRound: 0, inboxCount: 1, inboxAgentIds: ['a1'], inboxPreviews: [{ text: 'live' }] },
  ],
};
// The backend's committedMessage: CLEAN toolRounds (no synthetic row) + sidecars.
const _cm = {
  finishReason: 'stop',
  toolRounds: [
    { roundNum: 1, toolCallId: 'tc_1', toolName: 'web_search', toolArgs: '{}',
      toolContent: 'r', status: 'done' },
  ],
  _inboxInjects: [{ round: 0, count: 1, agentIds: ['a1'], previews: [{ text: 'live' }] }],
  _peerInjects: [{ round: 0, count: 1, previews: [{ fromConv: 's', text: 'p' }] }],
  _userSteerInjects: [{ round: 0, count: 1, previews: [{ text: 'st' }] }],
};

// Replay the REAL done projection: overwrite toolRounds, then apply the whitelist.
if (Array.isArray(_cm.toolRounds)) assistantMsg.toolRounds = _cm.toolRounds;
for (const _k of whitelist) { if (_cm[_k] != null) assistantMsg[_k] = _cm[_k]; }

// After the done projection, the synthetic row is GONE from toolRounds (backend
// clean list won) — but the sidecar was adopted, so getToolRoundsFromMsg
// rebuilds it. This is the in-session survival the objective demands.
check('done_overwrote_toolRounds_clean',
  assistantMsg.toolRounds.length === 1 && !assistantMsg.toolRounds.some(r => r._inboxInject));
check('done_adopted_inbox_sidecar', Array.isArray(assistantMsg._inboxInjects));
check('done_adopted_peer_sidecar', Array.isArray(assistantMsg._peerInjects));
check('done_adopted_steer_sidecar', Array.isArray(assistantMsg._userSteerInjects));

const rows = getToolRoundsFromMsg(assistantMsg);
check('chip_survives_in_session_inbox', rows.filter(r => r._inboxInject).length === 1);
check('chip_survives_in_session_peer', rows.filter(r => r._peerInject).length === 1);
check('chip_survives_in_session_steer', rows.filter(r => r._userSteerInject).length === 1);
check('live_preview_text_survives',
  rows.find(r => r._inboxInject).inboxPreviews[0].text === 'live');

console.log(out.join('\n'));
"""

_LIVE_NC_HARNESS = r"""
// NEUTER: drop the three lanes from the whitelist (pre-fix behaviour). The done
// projection then does NOT adopt the sidecar → after toolRounds is overwritten
// with the clean list, getToolRoundsFromMsg has nothing to rebuild from → the
// chip is GONE in-session. Proves the whitelist entries are load-bearing.
const fnSrc = process.env.FN_SRC;
eval(fnSrc);
const whitelist = JSON.parse(process.env.WHITELIST)
  .filter(k => k !== '_inboxInjects' && k !== '_peerInjects' && k !== '_userSteerInjects');

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const assistantMsg = { role: 'assistant', content: 'p', toolRounds: [] };
const _cm = {
  finishReason: 'stop',
  toolRounds: [{ roundNum: 1, toolCallId: 'tc_1', toolName: 'x', toolArgs: '{}', toolContent: 'r', status: 'done' }],
  _inboxInjects: [{ round: 0, count: 1, agentIds: ['a1'], previews: [{ text: 'live' }] }],
};
if (Array.isArray(_cm.toolRounds)) assistantMsg.toolRounds = _cm.toolRounds;
for (const _k of whitelist) { if (_cm[_k] != null) assistantMsg[_k] = _cm[_k]; }
const rows = getToolRoundsFromMsg(assistantMsg);
check('nc_chip_gone_without_whitelist_entry',
  rows.filter(r => r._inboxInject).length === 0 && !assistantMsg._inboxInjects);
console.log(out.join('\n'));
"""


def _extract_whitelist_array() -> str:
    """Extract the committedMessage whitelist keys from the shipped
    sse_pipeline.js as a JSON array, so the live harness replays the REAL loop."""
    src = open(PIPE_JS, encoding='utf-8').read()
    i = src.index("for (const _k of ['finishReason'")
    j = src.index(']) {', i)
    arr_src = src[i + src[i:].index('['): j + 1]
    import re as _re
    keys = _re.findall(r"'([^']+)'", arr_src)
    import json as _json
    return _json.dumps(keys)


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_chip_survives_live_done_projection():
    """Leg 5 (in-session): replay the REAL done committedMessage projection with
    the REAL whitelist + REAL getToolRoundsFromMsg → the chip survives without a
    refresh."""
    fn_src = _extract_rehydrate_fn()
    env = dict(os.environ, FN_SRC=fn_src, WHITELIST=_extract_whitelist_array())
    proc = subprocess.run(['node', '-e', _LIVE_HARNESS], capture_output=True,
                          text=True, timeout=30, env=env)
    assert proc.returncode == 0, f'node failed: {proc.stderr}'
    out = proc.stdout.strip()
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'live done-projection failures:\n' + out
    assert out.count('PASS') >= 8, f'expected >=8 PASS, got:\n{out}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_NC_live_done_without_whitelist_entry_loses_chip():
    """Leg 6 (NEUTER): drop the lanes from the whitelist → the chip is lost
    in-session, proving the whitelist entries are load-bearing."""
    fn_src = _extract_rehydrate_fn()
    env = dict(os.environ, FN_SRC=fn_src, WHITELIST=_extract_whitelist_array())
    proc = subprocess.run(['node', '-e', _LIVE_NC_HARNESS], capture_output=True,
                          text=True, timeout=30, env=env)
    assert proc.returncode == 0, f'node failed: {proc.stderr}'
    assert 'PASS nc_chip_gone_without_whitelist_entry' in proc.stdout, (
        'NC control failed — the whitelist entries are not load-bearing:\n' + proc.stdout)


if __name__ == '__main__':
    test_sidecars_persist_and_survive_db_roundtrip()
    test_reloaded_wire_is_byte_identical_to_no_inbox_baseline()
    test_leaked_synthetic_row_still_wire_neutral()
    test_committedMessage_whitelist_carries_the_three_lanes()
    test_reconnect_state_snapshot_adopts_the_three_lanes()
    test_poll_fallback_forwards_the_three_lanes()
    print('PASS backend + source legs')
    if shutil.which('node'):
        _src = _extract_rehydrate_fn()
        print(_run_node(_HARNESS, _src))
        print(_run_node(_NC_HARNESS, _src))
        test_chip_survives_live_done_projection()
        test_NC_live_done_without_whitelist_entry_loses_chip()
        print('PASS live-adoption legs')
    else:
        print('SKIP frontend leg — node not available')
