"""tests/test_frontend_stream_identity_projection.py — regression guards for the
「等待中… ↔ 推理中 N 字符」 flip-flop (epic pt_44e985ec82014e6d).

PRODUCTION INCIDENT (2026-07-31, conv ms8x5blr9hb4tq, task a0b0d9d2)
-------------------------------------------------------------------
On a VS Code tunnel the SSE died into the poll fallback within 0.1s, while a
second SSE reader (gen 1→2 supersede in logs/app.log:381305-381314) stayed
alive — TWO lanes (poll + SSE delta) ran concurrently. The lanes write the
assistant message the stream entry is BOUND to (identity: _msgId/_taskId);
the rAF render frame `_streamFrameArg` (health_stream_timer.js) projected
POSITIONALLY from `conv.messages[length-1]`. A duplicate empty placeholder at
the tail meant the projection read an EMPTY message while the session phase
counter `_thinkingLen` (set ONLY by the SSE delta branch, sse_pipeline.js:991)
advanced — the status zone flipped between 等待中… (poll's phase=null) and
推理中 2.5k 字符 (SSE's thinking_active), with the thinking block NEVER
visible. Verified by driving the REAL updateStreamingUI under jsdom: only the
split-identity shape reproduces the screenshots; poll-only and same-object
shapes are stable.

THE FIXES GUARDED HERE
----------------------
  1a. connectToTask duplicate-connect guard: a second connect for the SAME
      conv+task is a no-op (the supersede→poll-fallback lane war at the origin).
  1b. `_adoptTaskPlaceholder` (core/conv_reducers.js): the send pipeline adopts
      an already-bound placeholder instead of pushing a duplicate, re-stamping
      the canonical client-minted _msgId so client/server identity agree.
  2.  `_streamFrameArg` projects the stream-entry's BOUND message (identity),
      falling back to the tail — the render reads what the lanes write.
  3.  The placeholder-push + duplicate-skip events report through
      `_reportClientError` so the NEXT occurrence is pinned from server logs
      (this incident's exact pusher was unprovable because console-only).

DISCIPLINE: every guard here was written FAILING-FIRST against the pre-fix
source, and each fix has a NEUTER proving it is load-bearing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
HST = os.path.join(ROOT, 'static', 'js', 'core', 'health_stream_timer.js')
SSE = os.path.join(ROOT, 'static', 'js', 'ui', 'sse_pipeline.js')
REDUCERS = os.path.join(ROOT, 'static', 'js', 'core', 'conv_reducers.js')
SEND = os.path.join(ROOT, 'static', 'js', 'main', 'main_send_pipeline.js')
CL = os.path.join(ROOT, 'static', 'js', 'ui', 'conversation_list.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r'function %s\s*\(' % re.escape(name), src)
    assert m, f'{name} not found in source'
    i = src.index('{', m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f'unbalanced braces extracting {name}')


def _run_node(script: str) -> str:
    proc = subprocess.run(['node', '-e', script], capture_output=True,
                          text=True, timeout=60, cwd=ROOT)
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


# ══════════════════════════════════════════════════════════════════════
#  Part A — _streamFrameArg identity projection (fix 2)
# ══════════════════════════════════════════════════════════════════════

_A_HARNESS = r"""
'use strict';
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

/* Real helpers, lifted verbatim so the frame runs against production code. */
__RESOLVE_BY_ID__

const streamSessions = new Map([['c1', { phase: null }]]);
let conversations = [];
let activeStreams = new Map();
function getToolRoundsFromMsg(m) { return (m && m.toolRounds) || []; }

/* The REAL _streamFrameArg, extracted from health_stream_timer.js */
__FRAME_ARG__

/* Scenario A1: the lanes write the BOUND message Q (stream entry), while an
 * empty duplicate placeholder P sits at the tail. The frame must read Q's
 * thinking — the tail's emptiness must not blank the projection. */
conversations = [{ id: 'c1', messages: [
  { role: 'user', content: 'q', _msgId: 'u1' },
  { role: 'assistant', content: '', thinking: 'THINK', toolRounds: [], _msgId: 'q1', _taskId: 'T' },
  { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'p1' },
] }];
activeStreams = new Map([['c1', { controller: {}, taskId: 'T',
  assistantMsg: conversations[0].messages[1] }]]);
{
  const f = _streamFrameArg('c1');
  check('A1_identity_reads_bound_not_empty_tail', f && f.thinking === 'THINK');
}

/* Scenario A2: the entry's assistantMsg is a STALE reference (an array
 * replacement orphaned it) — resolve live by _msgId to the NEW object. */
{
  const live = { role: 'assistant', content: '', thinking: 'LIVE', toolRounds: [], _msgId: 'q1', _taskId: 'T' };
  conversations = [{ id: 'c1', messages: [
    { role: 'user', content: 'q', _msgId: 'u1' },
    live,
    { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'p1' },
  ] }];
  const staleRef = { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'q1', _taskId: 'T' };
  activeStreams = new Map([['c1', { controller: {}, taskId: 'T', assistantMsg: staleRef }]]);
  const f = _streamFrameArg('c1');
  check('A2_stale_entry_ref_resolves_live_by_msgid', f && f.thinking === 'LIVE');
}

/* Scenario A3: endpoint critic at the tail — the critic lane legitimately owns
 * the projection; the identity rule must NOT steal it back to the worker. */
{
  const worker = { role: 'assistant', content: '', thinking: 'WORK', toolRounds: [], _msgId: 'w1', _taskId: 'T' };
  conversations = [{ id: 'c1', messages: [
    { role: 'user', content: 'q', _msgId: 'u1' },
    worker,
    { role: 'user', content: '', thinking: 'CRIT', toolRounds: [], _msgId: 'c1r', _isEndpointReview: true },
  ] }];
  activeStreams = new Map([['c1', { controller: {}, taskId: 'T', assistantMsg: worker }]]);
  const f = _streamFrameArg('c1');
  check('A3_endpoint_review_tail_not_overridden', f && f.thinking === 'CRIT');
}

/* Scenario A4: VU-carrier binds a DETACHED dummy (never in conv.messages) —
 * no live match → fall back to the tail exactly as before. */
{
  const tail = { role: 'assistant', content: '', thinking: 'TAIL', toolRounds: [], _msgId: 't9' };
  conversations = [{ id: 'c1', messages: [
    { role: 'user', content: 'q', _msgId: 'u1' },
    tail,
  ] }];
  const dummy = { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'd1' };
  activeStreams = new Map([['c1', { controller: {}, taskId: 'T', assistantMsg: dummy }]]);
  const f = _streamFrameArg('c1');
  check('A4_detached_dummy_falls_back_to_tail', f && f.thinking === 'TAIL');
}

console.log(out.join('\n'));
"""


def _part_a_results(frame_src: str) -> str:
    cl_src = open(CL, encoding='utf-8').read()
    script = (_A_HARNESS
              .replace('__RESOLVE_BY_ID__', _extract_fn(cl_src, '_resolveAssistantById'))
              .replace('__FRAME_ARG__', frame_src))
    return _run_node(script)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_stream_frame_arg_identity_projection():
    """Fix 2: the frame projects the stream-entry's BOUND message (identity),
    never an empty duplicate at the tail; the critic lane and the detached
    dummy keep their existing behaviour (anchors)."""
    src = _extract_fn(open(HST, encoding='utf-8').read(), '_streamFrameArg')
    output = _part_a_results(src)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'identity projection failures:\n' + output
    # Anchors must hold on BOTH sides of the fix:
    assert 'PASS A3_endpoint_review_tail_not_overridden' in output, output
    assert 'PASS A4_detached_dummy_falls_back_to_tail' in output, output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_strip_identity_block():
    """NEUTER-A: gate the identity block off → the projection falls back to the
    empty tail again (A1/A2 must go RED)."""
    src = _extract_fn(open(HST, encoding='utf-8').read(), '_streamFrameArg')
    assert '_bound' in src, (
        '_streamFrameArg has no identity-first block to neuter — '
        'fix 2 is missing from health_stream_timer.js')
    neutered = src.replace(
        'if (_bound && _bound !== ckpt && !(last && last._isEndpointReview)) {',
        'if (false && _bound && _bound !== ckpt) {')
    assert neutered != src, 'neuter replacement did not land'
    output = _part_a_results(neutered)
    assert 'FAIL A1_identity_reads_bound_not_empty_tail' in output, output
    assert 'FAIL A2_stale_entry_ref_resolves_live_by_msgid' in output, output
    # Anchors survive the neuter (they do not depend on the identity block):
    assert 'PASS A3_endpoint_review_tail_not_overridden' in output, output
    assert 'PASS A4_detached_dummy_falls_back_to_tail' in output, output


# ══════════════════════════════════════════════════════════════════════
#  Part B — connectToTask duplicate-connect guard + push reporting (1a, 3)
# ══════════════════════════════════════════════════════════════════════

_B_HARNESS = r"""
'use strict';
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const calls = { trySSE: 0, reports: [], poll: 0 };

/* ── sandbox globals the real file expects ── */
const window = globalThis;
let conversations = [];
let activeConvId = 'SOMEWHERE_ELSE';   // keeps connectToTask off every DOM path
const activeStreams = new Map();
const document = { getElementById: () => null };
const sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
let _msgCounter = 0;
function _ensureMsgId(m) { if (!m._msgId) m._msgId = 'mid_' + (++_msgCounter); return m; }
function renderConversationList() {}
function updateSendButton() {}
function saveConversations() {}
function debugLog() {}
function twStart() {}
function twStop() {}
function finishStream() {}
function getConvById(id) { return conversations.find(c => c.id === id) || null; }
/* The REAL _trySSE runs — count its SSE-open attempts by stubbing the
 * transport it awaits (a dead proxy makes it throw before any event). */
const Api = { chat: { streamResponse: async () => { calls.trySSE++; throw new Error('proxy dead'); } } };
async function _pollFallback() { calls.poll++; }             // keeps the entry
function _reportClientError(msg) { calls.reports.push(String(msg)); }
function _resolveAssistantById(conv, msgId) {
  if (!conv || !msgId || !Array.isArray(conv.messages)) return null;
  for (let i = conv.messages.length - 1; i >= 0; i--) {
    const m = conv.messages[i];
    if (m && m._msgId === msgId) return m;
  }
  return null;
}
function _resolveAssistantByTaskId(conv, taskId) {
  if (!conv || !taskId || !Array.isArray(conv.messages)) return null;
  for (let i = conv.messages.length - 1; i >= 0; i--) {
    const m = conv.messages[i];
    if (m && m.role === 'assistant' && m._taskId === taskId) return m;
  }
  return null;
}
__PRIOR_TURN_REDUCER__

/* ── the REAL sse_pipeline.js (defines connectToTask et al.) ── */
__SSE_SRC__

(async () => {
  /* B1: duplicate connect for the SAME conv+task must be a no-op — the
   * second _trySSE reader is what the server supersedes into the poll war. */
  conversations = [{ id: 'c1', messages: [
    { role: 'user', content: 'q', _msgId: 'u1' },
    { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'a1' },
  ] }];
  await connectToTask('c1', 'T1');
  check('B1_first_connect_ran_sse', calls.trySSE === 1);
  await connectToTask('c1', 'T1');
  check('B1_duplicate_connect_no_second_sse', calls.trySSE === 1);
  check('B1_duplicate_connect_reported',
        calls.reports.some(r => /already active|duplicate/i.test(r)));

  /* B2: a DIFFERENT task for the same conv must still connect (anchor). */
  await connectToTask('c1', 'T2');
  check('B2_different_task_connects', calls.trySSE === 2);

  /* B3: tail is a USER message → defensive recovery pushes ONE placeholder
   * and reports it (the production duplicate's exact forensic signal). */
  conversations = [{ id: 'c2', messages: [
    { role: 'user', content: 'q', _msgId: 'u9' },
  ] }];
  const _reportsBefore = calls.reports.length;
  await connectToTask('c2', 'T9');
  const _c2 = conversations[0];
  check('B3_recovery_push_exactly_one_placeholder',
        _c2.messages.length === 2 && _c2.messages[1].role === 'assistant');
  check('B3_recovery_push_reported',
        calls.reports.slice(_reportsBefore).some(r => /placeholder/i.test(r)));

  console.log(out.join('\n'));
})().catch(e => { console.log('HARNESS-ERROR ' + (e && e.stack || e)); });
"""


def _part_b_results(sse_src: str) -> str:
    red_src = open(REDUCERS, encoding='utf-8').read()
    script = (_B_HARNESS
              .replace('__PRIOR_TURN_REDUCER__', _extract_fn(red_src, 'assistantTailIsPriorTurn'))
              .replace('__SSE_SRC__', sse_src))
    return _run_node(script)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_connect_to_task_duplicate_guard_and_reporting():
    """Fixes 1a+3: a duplicate same-task connect opens no second reader and is
    reported; a different task still connects; the recovery placeholder push is
    exactly one message and is reported."""
    output = _part_b_results(open(SSE, encoding='utf-8').read())
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'connectToTask guard failures:\n' + output
    assert 'PASS B2_different_task_connects' in output, output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_strip_duplicate_guard():
    """NEUTER-B: drop the duplicate-connect early return → the second connect
    opens a second SSE reader again (B1 must go RED)."""
    src = open(SSE, encoding='utf-8').read()
    assert '_dupStream' in src, (
        'connectToTask has no duplicate-connect guard (_dupStream) to neuter — '
        'fix 1a is missing from sse_pipeline.js')
    neutered = src.replace('_dupStream && _dupStream.taskId === taskId',
                           '_dupStream && false')
    assert neutered != src, 'neuter replacement did not land'
    output = _part_b_results(neutered)
    assert 'FAIL B1_duplicate_connect_no_second_sse' in output, output
    # NOTE: B2's exact-count pin is NOT asserted under the neuter — with the
    # duplicate un-suppressed its SSE-open shifts every later count, so B2
    # cannot be an independent control here (by design it is one post-fix).


# ══════════════════════════════════════════════════════════════════════
#  Part C — _adoptTaskPlaceholder helper + send-path call site (1b)
# ══════════════════════════════════════════════════════════════════════

_C_HARNESS = r"""
'use strict';
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const window = globalThis;
__REDUCERS_SRC__

/* C1: nothing bound → the candidate comes back untouched, not adopted. */
{
  const conv = { id: 'c1', messages: [{ role: 'user', content: 'q', _msgId: 'u1' }] };
  const cand = { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'CANON' };
  const r = _adoptTaskPlaceholder(conv, 'T', cand);
  check('C1_mints_when_nothing_bound', r && r.msg === cand && r.adopted === false);
}

/* C2: a placeholder already bound to THIS task exists → adopt it and re-stamp
 * the canonical _msgId so client mint + backend DB slot keep ONE identity. */
{
  const bound = { role: 'assistant', content: '', thinking: 'x', toolRounds: [], _msgId: 'EARLY', _taskId: 'T' };
  const conv = { id: 'c2', messages: [
    { role: 'user', content: 'q', _msgId: 'u1' }, bound] };
  const cand = { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'CANON' };
  const r = _adoptTaskPlaceholder(conv, 'T', cand);
  check('C2_adopts_bound_placeholder', r && r.msg === bound && r.adopted === true);
  check('C2_restamps_canonical_msgid', bound._msgId === 'CANON');
  check('C2_preserves_accumulated_thinking', bound.thinking === 'x');
  check('C2_no_duplicate_pushed', conv.messages.length === 2);
}

/* C3: a message bound to a DIFFERENT task must NOT be adopted. */
{
  const foreign = { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'F', _taskId: 'T_OLD' };
  const conv = { id: 'c3', messages: [
    { role: 'user', content: 'q', _msgId: 'u1' }, foreign] };
  const cand = { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'CANON' };
  const r = _adoptTaskPlaceholder(conv, 'T_NEW', cand);
  check('C3_foreign_task_not_adopted', r && r.msg === cand && r.adopted === false);
  check('C3_foreign_msgid_untouched', foreign._msgId === 'F');
}

/* C4: the recover/queue/autopilot variant — a candidate WITHOUT _msgId must
 * adopt WITHOUT re-stamping (those paths minted no canonical id, so the
 * existing message's identity is the only one the backend knows). */
{
  const bound = { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'KEEP', _taskId: 'T' };
  const conv = { id: 'c4', messages: [
    { role: 'user', content: 'q', _msgId: 'u1' }, bound] };
  const cand = { role: 'assistant', content: '', thinking: '', toolRounds: [] };
  const r = _adoptTaskPlaceholder(conv, 'T', cand);
  check('C4_adopts_without_restamping', r && r.msg === bound && r.adopted === true);
  check('C4_existing_msgid_preserved', bound._msgId === 'KEEP');
}

console.log(out.join('\n'));
"""


def _part_c_results(red_src: str) -> str:
    return _run_node(_C_HARNESS.replace('__REDUCERS_SRC__', red_src))


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_adopt_task_placeholder_helper():
    """Fix 1b: adopt the already-bound placeholder (re-stamping the canonical
    id) instead of minting a duplicate; foreign tasks are never adopted."""
    output = _part_c_results(open(REDUCERS, encoding='utf-8').read())
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, '_adoptTaskPlaceholder failures:\n' + output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_helper_always_mints():
    """NEUTER-C: force the helper to always mint → C2 (adoption) goes RED."""
    src = open(REDUCERS, encoding='utf-8').read()
    assert 'function _adoptTaskPlaceholder(' in src, (
        '_adoptTaskPlaceholder missing from conv_reducers.js — fix 1b not implemented')
    fn = _extract_fn(src, '_adoptTaskPlaceholder')
    neutered_fn = fn.replace(
        'if (existing) {',
        'if (false && existing) {')
    assert neutered_fn != fn, 'neuter replacement did not land'
    output = _part_c_results(src.replace(fn, neutered_fn))
    assert 'FAIL C2_adopts_bound_placeholder' in output, output
    assert 'PASS C1_mints_when_nothing_bound' in output, output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_helper_always_restamps():
    """NEUTER-C4: force the re-stamp to fire even when the candidate has no id
    → the recover/queue/autopilot variant loses the existing identity (C4 RED)."""
    src = open(REDUCERS, encoding='utf-8').read()
    fn = _extract_fn(src, '_adoptTaskPlaceholder')
    neutered_fn = fn.replace(
        'if (candidate && candidate._msgId) existing._msgId = candidate._msgId;',
        'existing._msgId = candidate && candidate._msgId;')
    assert neutered_fn != fn, 'neuter replacement did not land'
    output = _part_c_results(src.replace(fn, neutered_fn))
    assert 'FAIL C4_existing_msgid_preserved' in output, output
    # The canonical re-stamp (C2) still works under this neuter:
    assert 'PASS C2_restamps_canonical_msgid' in output, output


def test_send_pipeline_routes_through_the_helper():
    """The send path's placeholder push must route through the shared helper —
    a re-inlined push re-opens the duplicate class this epic closes."""
    src = open(SEND, encoding='utf-8').read()
    assert '_adoptTaskPlaceholder(conv, taskId' in src, (
        'main_send_pipeline.js does not call _adoptTaskPlaceholder — '
        'the send path still pushes its placeholder unconditionally')


# ══════════════════════════════════════════════════════════════════════
#  Part D — class closure: every reachable placeholder push routes the
#  helper (owner directive 2026-08-01). Wiring asserts anchor on the
#  behavior-producing call text (helper semantics are pinned in Part C).
# ══════════════════════════════════════════════════════════════════════

REGEN = os.path.join(ROOT, 'static', 'js', 'main', 'main_regen_continue.js')
EDIT = os.path.join(ROOT, 'static', 'js', 'ui', 'edit_message.js')


def test_regen_routes_through_helper_with_canonical_id():
    """regenerateFromUser: adopt-dedupe with the canonical _regenAssistantMsgId
    (shipped as config.assistantMsgId) — same class, same close as send."""
    src = open(REGEN, encoding='utf-8').read()
    assert '_adoptTaskPlaceholder(conv, taskId, _mintedPlaceholder)' in src, (
        'regenerateFromUser pushes its placeholder unconditionally — '
        'the duplicate class survives on the regen path')
    assert '_msgId: _regenAssistantMsgId' in src, (
        'regen candidate lost its canonical _regenAssistantMsgId — '
        'adoption would re-stamp a wrong identity')


def test_edit_resend_routes_through_helper_with_canonical_id():
    """saveEditAndResend — regen's twin (same atomic endpoint + minted id)."""
    src = open(EDIT, encoding='utf-8').read()
    assert '_adoptTaskPlaceholder(conv, taskId, _mintedPlaceholder)' in src, (
        'saveEditAndResend pushes its placeholder unconditionally — '
        'the duplicate class survives on the edit-resend path')
    assert '_msgId: _editAssistantMsgId' in src, (
        'edit-resend candidate lost its canonical _editAssistantMsgId')


def test_recover_queue_autopilot_attach_route_helper_no_restamp():
    """_recoverTimedOutChatTask / queue-dispatch / autopilot follow-up attach:
    route the helper with a candidate that carries NO _msgId (no canonical id
    exists on these paths → adoption must not re-stamp)."""
    src = open(SEND, encoding='utf-8').read()
    assert '_adoptTaskPlaceholder(conv, task.id, _mintedPlaceholder)' in src, (
        '_recoverTimedOutChatTask does not route the helper — '
        'the recovery path still pushes unconditionally')
    assert '_adoptTaskPlaceholder(conv, newTask.id, _mintedPlaceholder)' in src, (
        'the queue-dispatch attach does not route the helper')
    assert '_adoptTaskPlaceholder(conv, nextTaskId, _mintedPlaceholder)' in src, (
        'the autopilot follow-up attach does not route the helper')
