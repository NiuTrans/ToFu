#!/usr/bin/env python3
"""FloorRetry first-attempt RESIDUE — the mrxij7q34xm070 "abrupt stop" root fix.

THE BUG (live evidence, 2026-07-23 conv mrxij7q34xm070, task 341349f5):
  R3's first attempt streamed a ~4344-char draft for 133s; the ~5s streaming
  checkpoint mirrored it into conversations.messages. usage then judged a
  byte-stable cache-floor collapse → FloorRetry discarded that attempt, resent
  the identical body, and adopted the resend (which streamed with
  on_content=None). The converge step fixed task['content'] (208-char adopted
  narration) but the conv ROW still held the 4344-char discarded draft. R4-R7
  — including the 3751-char final answer — never EXCEEDED 4344, so:

    * the grew-only checkpoint guard never wrote again (residue pinned);
    * the terminal content guard read existing=4344 > new=3751 as "frontend
      genuinely won" and refused to overwrite at done;
    * task['_committedMsg'] was stamped from the residue row and the client
      projected it verbatim → the bubble showed the discarded draft, cut off
      mid-sentence, with a stop finish-tag. The "abrupt stop" was an illusion.

THE FIX (three seams):
  A. ``_stream.py`` — at adoption, record the discarded first-attempt
     (content, thinking) onto ``task['_floor_retry_residue']`` (bounded).
  B. ``_sync.py::_sync_partial_to_conversation`` — the checkpoint mirror
     converges to the authoritative task text on DIFFERENCE (not just growth);
     a shrink bypasses delta coalescing.
  C. ``_sync.py::_sync_result_to_conversation`` — the terminal content guard
     and the terminal-CAS re-read guard exempt a row that BYTE-MATCHES a
     recorded residue entry: that is our own discarded attempt, never a
     frontend win, so the authoritative final answer overwrites it.

Failing-first / NEUTER discipline:
  * test_adoption_shrinks_checkpoint_mirror is RED without fix B.
  * test_terminal_guard_overwrites_exact_residue is RED without fix C
    (pre-write guard).
  * test_cas_reread_guard_also_exempts_residue is RED without fix C
    (CAS re-read guard).
  * test_terminal_guard_still_protects_genuine_frontend_win is the NEUTER
    control: a longer existing row that does NOT byte-match the residue must
    STILL be protected — proving the exemption is surgical, not a blanket
    shrink.

Run directly (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_floor_retry_residue.py
"""
from __future__ import annotations

import json
import os
import sys
import threading as _thr
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

pytestmark = pytest.mark.unit


_FLOOR_USAGE = {'prompt_tokens': 10, 'cache_read_tokens': 28654,
                'cache_creation_input_tokens': 42000, '_wire_fp': [{'k': 'a'}]}
_HIT_USAGE = {'prompt_tokens': 10, 'cache_read_tokens': 150000,
              'cache_creation_input_tokens': 1200, '_wire_fp': [{'k': 'a'}]}


def _seed_wire_fp(conv_id, fp):
    from lib.tasks_pkg.cache_tracking import _cache_lock, _cache_states
    from lib.tasks_pkg.cache_tracking._state import CacheState, _state_key
    key = _state_key(conv_id)
    with _cache_lock:
        st = _cache_states.get(key)
        if st is None:
            st = CacheState()
            _cache_states[key] = st
        st.wire_fp = list(fp)


def _seed_conv(db, conv_id, messages):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'fr-residue-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _read_messages(db, conv_id):
    row = db.execute(
        'SELECT messages FROM conversations WHERE id=? AND user_id=1',
        (conv_id,)).fetchone()
    return json.loads(row[0] or '[]') if row else None


def _mk_task(conv_id, content='', thinking=''):
    from lib.tasks_pkg.manager import (
        create_task, _conv_latest_task, _conv_latest_task_lock)
    task = create_task(conv_id, [{'role': 'user', 'content': 'U1'}], {})
    task['content'] = content
    task['thinking'] = thinking
    with _conv_latest_task_lock:
        _conv_latest_task[conv_id] = task['id']
    return task


def _cleanup(db, conv_id, task_id):
    from lib.database import db_execute_with_retry
    from lib.tasks_pkg.manager import _conv_latest_task, _conv_latest_task_lock
    with _conv_latest_task_lock:
        _conv_latest_task.pop(conv_id, None)
    db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db_execute_with_retry(db, 'DELETE FROM task_results WHERE task_id=?', (task_id,))
    db.commit()


def _run_stream_with_script(task, monkeypatch, dispatch_seq, *, enabled=True):
    """Drive the REAL stream_llm_response with a scripted dispatch sequence.
    Each seq item: {'stream': <text to emit via on_content>, 'final': <msg
    content returned>, 'thinking_final': <reasoning_content>, 'usage': {...}}.
    The primary attempt (seq[0]) may stream deltas; resends stream nothing
    (mirrors the production Layer-1 discipline: on_content=None)."""
    import lib.tasks_pkg.manager as _mgr
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '1' if enabled else '0')
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY_MAX', '2')
    calls = {'n': 0}
    seq = list(dispatch_seq)

    def _fake_dispatch(body, **kwargs):
        i = calls['n']
        calls['n'] += 1
        item = seq[min(i, len(seq) - 1)]
        oc = kwargs.get('on_content')
        if oc and item.get('stream'):
            for chunk in item['stream']:
                oc(chunk)
        ot = kwargs.get('on_thinking')
        if ot and item.get('stream_thinking'):
            for chunk in item['stream_thinking']:
                ot(chunk)
        return ({'role': 'assistant',
                 'content': item.get('final', ''),
                 'reasoning_content': item.get('thinking_final', '')},
                item.get('finish', 'stop'), dict(item['usage']))

    _orig = _mgr.dispatch_stream
    _mgr.dispatch_stream = _fake_dispatch
    try:
        return _mgr.stream_llm_response(
            task, {'model': 'aws.claude-opus-4.8',
                   'messages': [{'role': 'system', 'content': 'S'},
                                {'role': 'user', 'content': 'go'}]},
            tag='R3')
    finally:
        _mgr.dispatch_stream = _orig


# ── Fix A: residue recording at adoption ────────────────────────────────────

def test_stream_records_discarded_attempt_and_converges(monkeypatch):
    """Fix A: adopting a resend must (1) converge task content/thinking to the
    adopted text AND (2) record the discarded first-attempt verbatim on
    task['_floor_retry_residue'] for the downstream guard exemption.
    RED without the recording block (residue list absent); GREEN with it."""
    conv_id = 'frr-record'
    _seed_wire_fp(conv_id, [{'k': 'a'}])
    draft = 'DRAFT-' + 'x' * 300
    adopted = 'FINAL-answer'
    task = {'id': 'task-frr-1', 'convId': conv_id, 'content': '', 'thinking': '',
            'config': {}, 'events': [], 'toolRounds': [],
            'content_lock': _thr.Lock(), 'events_lock': _thr.Lock()}
    _run_stream_with_script(task, monkeypatch, [
        {'stream': [draft[:100], draft[100:]], 'stream_thinking': ['think-draft'],
         'final': draft, 'thinking_final': 'think-draft', 'usage': _FLOOR_USAGE},
        {'final': adopted, 'thinking_final': 'think-final', 'usage': _HIT_USAGE},
    ])
    assert task['content'] == adopted, 'converge must replace content with adopted text'
    assert task['thinking'] == 'think-final'
    residue = task.get('_floor_retry_residue') or []
    assert len(residue) == 1, f'exactly one discarded attempt recorded; got {residue}'
    assert residue[0]['content'] == draft, (
        'residue must byte-match the streamed first-attempt draft')
    assert residue[0]['thinking'] == 'think-draft'


def test_no_residue_recorded_when_nothing_streamed(monkeypatch):
    """An adoption whose first attempt produced ZERO streamed text leaves no
    residue entry (nothing was mirrored, nothing to exempt)."""
    conv_id = 'frr-empty'
    _seed_wire_fp(conv_id, [{'k': 'a'}])
    task = {'id': 'task-frr-2', 'convId': conv_id, 'content': '', 'thinking': '',
            'config': {}, 'events': [], 'toolRounds': [],
            'content_lock': _thr.Lock(), 'events_lock': _thr.Lock()}
    _run_stream_with_script(task, monkeypatch, [
        {'final': '', 'usage': _FLOOR_USAGE},
        {'final': 'adopted', 'usage': _HIT_USAGE},
    ])
    assert not task.get('_floor_retry_residue'), (
        f'empty discard must not record residue; got {task.get("_floor_retry_residue")}')


# ── Fix B: checkpoint mirror converges on shrink ────────────────────────────

def test_adoption_shrinks_checkpoint_mirror(monkeypatch):
    """Fix B (failing-first): the ~5s checkpoint mirrors attempt#1's LONG draft
    into conversations.messages; after the adoption converges the task to a
    SHORTER adopted text, the next checkpoint must SHRINK the row to the
    adopted text. Pre-fix the grew-only guard kept the residue pinned
    (mrxij7q34xm070: 4344-char draft beat the 3751-char final answer)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'frr-shrink'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1'},
        {'role': 'assistant', 'content': ''},
    ])
    _seed_wire_fp(conv_id, [{'k': 'a'}])
    task = _mk_task(conv_id)
    draft = 'DRAFT-' + 'y' * 400          # long first-attempt draft
    adopted = 'ADOPTED-narration'          # short adopted resend text
    try:
        # R3: primary streams the long draft (first delta checkpoints it into
        # the row via the REAL stream path), floors; resend adopted (short).
        _run_stream_with_script(task, monkeypatch, [
            {'stream': [draft], 'final': draft, 'usage': _FLOOR_USAGE},
            {'final': adopted, 'usage': _HIT_USAGE},
        ])
        row_msgs = _read_messages(db, conv_id)
        assert row_msgs[-1]['content'] == draft, (
            'precondition: the first attempt must have been mirrored mid-stream')

        # Adoption converged the task; the NEXT checkpoint must shrink the row.
        mgr.checkpoint_task_partial(task)
        row_after = _read_messages(db, conv_id)
        assert row_after[-1]['content'] == adopted, (
            f'checkpoint mirror must converge on shrink; '
            f'row still holds {len(row_after[-1]["content"])} chars '
            f'(residue={len(draft)}, adopted={len(adopted)})')
    finally:
        _cleanup(db, conv_id, task['id'])


def test_growth_coalescing_unaffected_by_shrink_logic(monkeypatch):
    """NEUTER-adjacent: ordinary sub-threshold GROWTH must still be coalesced
    (the shrink path must not accidentally make every tiny delta write)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'frr-coalesce'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1'},
        {'role': 'assistant', 'content': ''},
    ])
    task = _mk_task(conv_id)
    try:
        task['content'] = 'A' * 200
        mgr.checkpoint_task_partial(task)
        assert _read_messages(db, conv_id)[-1]['content'] == 'A' * 200
        # 10-char growth (< default 160 threshold) → still withheld.
        task['content'] = 'A' * 200 + 'B' * 10
        mgr.checkpoint_task_partial(task)
        assert _read_messages(db, conv_id)[-1]['content'] == 'A' * 200, (
            'sub-threshold growth must still coalesce (shrink logic must not '
            'turn every small delta into a full messages write)')
    finally:
        _cleanup(db, conv_id, task['id'])


# ── Fix C: terminal guards exempt byte-matched residue ──────────────────────

def test_terminal_guard_overwrites_exact_residue(monkeypatch):
    """Fix C (failing-first, pre-write guard): the row holds the discarded
    attempt (longer than the final answer on BOTH content and thinking) and
    the task recorded it as residue. The terminal sync must overwrite with
    the authoritative final answer AND stamp a truthful _committedMsg.
    Pre-fix the guard read this as 'frontend genuinely won' and kept the
    residue — which then rode committedMessage verbatim to the client."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'frr-term'
    db = get_thread_db(DOMAIN_CHAT)
    residue_c = 'DISCARDED-draft-' + 'z' * 500
    residue_t = 'discarded-thinking-' + 'q' * 60
    final_c = 'The real final answer.'
    final_t = 'real thinking'
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1'},
        {'role': 'assistant', 'content': residue_c, 'thinking': residue_t},
    ])
    task = _mk_task(conv_id, content=final_c, thinking=final_t)
    task['_floor_retry_residue'] = [{'content': residue_c, 'thinking': residue_t}]
    try:
        task['finishReason'] = 'stop'
        mgr._sync_result_to_conversation(task, mgr.build_result_meta(task))
        row = _read_messages(db, conv_id)[-1]
        assert row['content'] == final_c, (
            f'residue must be overwritten by the final answer; '
            f'row holds {len(row["content"])} chars')
        assert row['thinking'] == final_t
        cm = task.get('_committedMsg') or {}
        assert cm.get('content') == final_c, (
            'committedMessage must carry the truthful final answer, not the '
            'residue the client would otherwise project verbatim')
    finally:
        _cleanup(db, conv_id, task['id'])


def test_terminal_guard_still_protects_genuine_frontend_win(monkeypatch):
    """NEUTER control: the row holds LONGER content that does NOT byte-match
    the recorded residue (e.g. a real frontend PUT). The guard must STILL
    protect it — proving the exemption is surgical, not a blanket shrink."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'frr-protect'
    db = get_thread_db(DOMAIN_CHAT)
    frontend_c = 'FRONTEND fuller content ' + 'f' * 500
    frontend_t = 'frontend thinking ' + 'g' * 60
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1'},
        {'role': 'assistant', 'content': frontend_c, 'thinking': frontend_t},
    ])
    task = _mk_task(conv_id, content='short final', thinking='short think')
    # Residue recorded from a DIFFERENT attempt — must not match the row.
    task['_floor_retry_residue'] = [{'content': 'some other draft',
                                     'thinking': 'some other thinking'}]
    try:
        task['finishReason'] = 'stop'
        mgr._sync_result_to_conversation(task, mgr.build_result_meta(task))
        row = _read_messages(db, conv_id)[-1]
        assert row['content'] == frontend_c, (
            'a genuine frontend win (no residue byte-match) must still be '
            'protected — the exemption must not shrink it')
        assert row['thinking'] == frontend_t
    finally:
        _cleanup(db, conv_id, task['id'])


def test_cas_reread_guard_also_exempts_residue(monkeypatch):
    """Fix C (CAS re-read guard): force exactly ONE terminal CAS miss whose
    fresh row still holds the residue. Pre-fix the re-read concluded
    'frontend genuinely won' and abandoned the final answer; with the
    exemption it grafts the final answer and retries to success."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr
    import lib.tasks_pkg.manager._sync as _sync_mod

    conv_id = 'frr-cas'
    db = get_thread_db(DOMAIN_CHAT)
    residue_c = 'DISCARDED-draft-' + 'w' * 500
    residue_t = 'discarded-thinking-' + 'v' * 60
    final_c = 'Authoritative final.'
    final_t = 'authoritative thinking'
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1'},
        {'role': 'assistant', 'content': residue_c, 'thinking': residue_t},
    ])
    task = _mk_task(conv_id, content=final_c, thinking=final_t)
    task['_floor_retry_residue'] = [{'content': residue_c, 'thinking': residue_t}]

    class _CASOnceShim:
        """Pass-through db wrapper that bumps the row's rev (via a genuine
        messages change) right before the sync's FIRST terminal UPDATE, so the
        CAS misses exactly once and the re-read still sees the residue."""
        fired = False

        def execute(self, sql, params=()):
            if (not self.fired and isinstance(sql, str)
                    and ' '.join(sql.split()).upper().startswith(
                        'UPDATE CONVERSATIONS SET MESSAGES')):
                self.fired = True
                db.execute("UPDATE conversations SET messages = messages || ' ' "
                           'WHERE id=? AND user_id=1', (conv_id,))
                db.commit()
            return db.execute(sql, params)

        def __getattr__(self, name):
            return getattr(db, name)

    shim = _CASOnceShim()
    monkeypatch.setattr(_sync_mod, 'get_thread_db', lambda *a, **k: shim)
    try:
        task['finishReason'] = 'stop'
        mgr._sync_result_to_conversation(task, mgr.build_result_meta(task))
        assert shim.fired, 'test setup: the forced CAS miss must have fired'
        row = _read_messages(db, conv_id)[-1]
        assert row['content'] == final_c, (
            'after a CAS miss whose fresh row byte-matches the discarded '
            'attempt, the retry must graft the final answer, not abandon it '
            f'as a frontend win; row holds {len(row["content"])} chars')
    finally:
        _cleanup(db, conv_id, task['id'])


# ── E2E: the mrxij7q34xm070 scenario end-to-end ─────────────────────────────

def test_mrxij7q34xm070_scenario_e2e(monkeypatch):
    """E2E replay of the live bug through the REAL stream + checkpoint +
    terminal sync path:
      R3 streams a LONG draft and floor-collapses → resend adopted (SHORT
      narration); R4-R7 follow with short texts, R7 produces the final answer
      (shorter than the R3 draft). At done the row — and the committedMessage
      the client projects — must carry the FINAL answer, not the R3 residue.
    Pre-fix RED: the row keeps the R3 draft (grew-only) and the terminal
    guard protects it (existing > new)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'frr-e2e'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1'},
        {'role': 'assistant', 'content': ''},
    ])
    _seed_wire_fp(conv_id, [{'k': 'a'}])
    task = _mk_task(conv_id)
    draft = 'DRAFT ' * 550                 # ~3300-char R3 first attempt
    draft_think = 'td ' * 300              # ~900-char R3 draft thinking
    adopted = 'Short adopted narration.'   # R3 resend (208-char class)
    final_answer = 'FINAL ' * 500          # ~3000-char R7 answer (< draft)
    final_think = 'tf ' * 200              # ~600-char R7 thinking (< draft's)
    try:
        # R3: draft streamed + floored → resend adopted. The draft leads on
        # BOTH axes (content AND thinking) — the real conv's guard condition.
        _run_stream_with_script(task, monkeypatch, [
            {'stream': [draft], 'stream_thinking': [draft_think],
             'final': draft, 'thinking_final': draft_think, 'usage': _FLOOR_USAGE},
            {'final': adopted, 'thinking_final': 'short', 'usage': _HIT_USAGE},
        ])
        # The live conv's byte-stable-detection checkpoint persisted BOTH the
        # draft content AND the draft thinking (4344+491). The 5s stream cadence
        # coalesces the thinking delta here, so flush it explicitly — the row
        # must hold the draft on BOTH axes for the terminal guard to engage.
        task['content'] = draft
        task['thinking'] = draft_think
        mgr.checkpoint_task_partial(task)
        assert _read_messages(db, conv_id)[-1]['content'] == draft, (
            'precondition: R3 draft mirrored mid-stream')
        assert _read_messages(db, conv_id)[-1]['thinking'] == draft_think

        # R4-R6: per-round reset + short rounds (emulate the orchestrator's
        # content/thinking reset between rounds).
        for round_text in ('R4 text', 'R5 txt', 'R6 text!!'):
            task['content'] = ''
            task['thinking'] = ''
            _seed_wire_fp(conv_id, [{'k': 'a'}])
            _run_stream_with_script(task, monkeypatch, [
                {'stream': [round_text], 'final': round_text, 'usage': _HIT_USAGE},
            ])
            mgr.checkpoint_task_partial(task)

        # R7: the final answer round (shorter than the R3 draft on BOTH
        # axes, exactly like the live conv: 3751<4344 content, 442<491 thinking).
        task['content'] = ''
        task['thinking'] = ''
        _seed_wire_fp(conv_id, [{'k': 'a'}])
        _run_stream_with_script(task, monkeypatch, [
            {'stream': [final_answer], 'stream_thinking': [final_think],
             'final': final_answer, 'thinking_final': final_think,
             'usage': _HIT_USAGE},
        ])
        mgr.checkpoint_task_partial(task)

        # Done: the terminal sync must persist the FINAL answer.
        task['finishReason'] = 'stop'
        mgr._sync_result_to_conversation(task, mgr.build_result_meta(task))
        row = _read_messages(db, conv_id)[-1]
        assert row['content'] == final_answer, (
            f'E2E: the settled row must hold the R7 final answer, not the R3 '
            f'discarded draft; row holds {len(row["content"])} chars')
        assert row['thinking'] == final_think
        cm = task.get('_committedMsg') or {}
        assert cm.get('content') == final_answer, (
            'the done event\'s committedMessage must be truthful — the client '
            'projects it verbatim')
    finally:
        _cleanup(db, conv_id, task['id'])


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
