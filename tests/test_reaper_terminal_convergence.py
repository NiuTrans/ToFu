"""tests/test_reaper_terminal_convergence.py — terminal-state writers converge
on the MESSAGE, not on the persist log line (epic pt_bf93496e98b9441e).

WHY
---
A task force-failed by the stuck-task reaper has THREE terminal-state writers
that used to race with no shared verdict:

  1. the reaper (``_maintenance.reap_stuck_running_tasks``) — stamps
     ``finishReason='error'`` + a ``worker_lost`` envelope and syncs the conv;
  2. the wedged worker's own late ``_finalize_and_emit_done`` — which used to
     UNCONDITIONALLY collapse ``finishReason`` to ``'aborted'`` because it
     never read ``_abort_reason='stuck_no_progress'``;
  3. the conv-sync append path, which used to append the reaper's error
     bubble onto whatever turn happened to be the tail — including a NEWER
     turn's prompt — so the next task's sync adopted the foreign bubble and
     inherited the stale error onto its clean answer.

Measured on the production library (message terminal state, NOT persist log
lines): 8 same-path reaps → 4 'error' / 2 'aborted' messages (winner decided
purely by timing), and 9 messages wearing a reaper error whose ``_taskId``
was never reaped (2 of them successful 'stop' answers showing ✓ + an error).

The fix (all asserted here on the MESSAGE state):

  * F1 ``_finalize.py`` — a reap (``_abort_reason='stuck_no_progress'``)
    settles ``finishReason='error'`` (the reaper's verdict), NOT 'aborted';
    a user Stop still settles 'aborted'.
  * F2 ``_sync.py`` — a reaped task's error bubble answers ONLY its own
    trailing user turn; when the conv has moved on, the sync converges onto
    the task's OWN slot (``_taskId`` scan) or drops — never appends onto a
    newer turn.
  * F3 ``_sync.py`` — a clean settle CLEARS a stale error from its own slot;
    the CAS-graft converges 'error' absence too; a tail bubble owned by a
    foreign ``_taskId`` is never filled.

Each test names the NEUTER it survives: reverting the named fix turns THIS
test red (verified in a throwaway clone before shipping).
"""

import json as _json
import threading
import time

import pytest

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────

def _seed_conv(db, conv_id, messages, settings=None):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now_ms = int(time.time() * 1000)
    row = {
        'id': conv_id, 'user_id': 1, 'title': 'reaper-conv-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }
    cols = ['id', 'user_id', 'title', 'messages', 'msg_count',
            'created_at', 'updated_at']
    if settings is not None:
        row['settings'] = _json.dumps(settings)
        cols.append('settings')
    upsert(db, CONVERSATIONS, row, insert_cols=cols, retry=True)
    db.commit()


def _cleanup(db, conv_id):
    from lib.database import db_execute_with_retry
    db_execute_with_retry(db, 'DELETE FROM message_queue WHERE conv_id=?', (conv_id,))
    db_execute_with_retry(db, 'DELETE FROM task_results WHERE conv_id=?', (conv_id,))
    db_execute_with_retry(db, 'DELETE FROM task_events WHERE task_id LIKE ?', ('t-conv-%',))
    db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db.commit()
    try:
        from lib.tasks_pkg.manager import _conv_latest_task, _conv_latest_task_lock
        with _conv_latest_task_lock:
            _conv_latest_task.pop(conv_id, None)
    except Exception:
        pass


def _read_msgs(db, conv_id):
    row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    msgs = row[0]
    return _json.loads(msgs) if isinstance(msgs, str) else msgs


def _reaper_envelope(model='aws.claude-opus-4.8'):
    from lib.error_envelope import make_envelope
    return make_envelope(
        'worker_lost',
        detail='Task made no progress for 1812 seconds and was terminated as wedged.',
        model=model, context='stuck-task-reaper', source='lib.tasks_pkg.manager')


def _mk_task(task_id, conv_id, **fields):
    t = {
        'id': task_id, 'convId': conv_id, 'status': 'error',
        'aborted': True, '_abort_reason': 'stuck_no_progress',
        'content': '', 'thinking': '', 'finishReason': 'error',
        'config': {'model': 'aws.claude-opus-4.8'},
        'created_at': time.time(), 'finished_at': time.time(),
        'events': [], 'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
        'toolRounds': [], 'usage': {}, 'apiRounds': [],
        'error': _reaper_envelope(),
    }
    t.update(fields)
    return t


def _assert_reaper_invariants(msgs, owner_task_id):
    """The ticket's two acceptance predicates on the MESSAGE terminal state:
    ① every reaper-context error message carries system-reap finishReason
       ('error' — never 'aborted'/'stop');
    ② the error sits on a message OWNED by the reaped task (_taskId match)."""
    for m in msgs:
        e = m.get('error')
        if isinstance(e, dict) and e.get('context') == 'stuck-task-reaper':
            assert m.get('finishReason') == 'error', (
                f'reaper error message must settle finishReason=error, '
                f'got {m.get("finishReason")!r} (content={m.get("content", "")[:40]!r})')
            assert m.get('_taskId') == owner_task_id, (
                f'reaper error must belong to its owning task {owner_task_id}, '
                f'got _taskId={m.get("_taskId")!r}')


# ─────────────────────────────────────────────────────────────────────────
# F1 — the late finalize of a REAPED task converges on the reaper's verdict
# ─────────────────────────────────────────────────────────────────────────

def test_finalize_converges_reaped_task_to_error_verdict(monkeypatch):
    """Drive the REAL ``_finalize_and_emit_done`` for a task the reaper already
    settled (aborted + _abort_reason='stuck_no_progress' + worker_lost
    envelope). NEUTER: reverting F1 (unconditional 'aborted') turns this red —
    the message, the task, the done event and the task_results row would all
    read 'aborted' (the exact production race outcome for d2805477)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.orchestrator._finalize import _finalize_and_emit_done

    conv_id = 'cv-conv-finalize-reap'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q that wedged', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '',
         '_msgId': 'amid-reap-1'},
    ])
    task = _mk_task('t-conv-reap-1', conv_id,
                    _assistantMsgId='amid-reap-1',
                    _initial_msg_count=2,
                    status='error', content='partial work before the wedge')
    manager._record_latest_task(conv_id, task['id'])
    try:
        _finalize_and_emit_done(
            task, model='aws.claude-opus-4.8', preset='medium',
            thinking_depth=None, cfg={},
            last_finish_reason='stop', last_usage=None,
            accumulated_usage={}, api_rounds=[],
            tool_call_happened=False, messages=[], original_messages=[],
            all_search_results_text=[], max_tokens=None,
            thinking_enabled=False, temperature=None,
            _loop_exit_reason='reaped_mid_tool', _abort_detected_phase=None,
            project_path='', project_enabled=False,
            round_num=1, assistant_msg=None)

        # ① the task itself settles with the SYSTEM-REAP verdict…
        assert task['finishReason'] == 'error', (
            f'a reaped task must converge on the reaper verdict, '
            f'got finishReason={task["finishReason"]!r}')
        # ② …the DONE wire event carries it…
        done_evts = [e for e in task['events'] if e.get('type') == 'done']
        assert done_evts and done_evts[-1].get('finishReason') == 'error', (
            f'done event must carry finishReason=error, got {done_evts}')
        assert done_evts[-1].get('error'), 'done event must carry the envelope'
        # ③ …the MESSAGE terminal state carries it…
        msgs = _read_msgs(db, conv_id)
        bubble = next(m for m in msgs if m.get('role') == 'assistant')
        assert bubble.get('finishReason') == 'error', (
            f'message must settle finishReason=error, got {bubble.get("finishReason")!r}')
        assert (bubble.get('error') or {}).get('context') == 'stuck-task-reaper'
        _assert_reaper_invariants(msgs, task['id'])
        # ④ …and the task_results row agrees (poll path).
        row = db.execute('SELECT metadata FROM task_results WHERE task_id=?',
                         (task['id'],)).fetchone()
        meta = _json.loads(row[0]) if row and row[0] else {}
        assert meta.get('finishReason') == 'error', (
            f'task_results metadata must read finishReason=error, got {meta}')
    finally:
        _cleanup(db, conv_id)


def test_finalize_user_stop_still_settles_aborted(monkeypatch):
    """SCOPE GUARD (NEUTER-complement): a USER Stop — ``_abort_reason`` NOT
    'stuck_no_progress' — must still settle 'aborted'. Proves F1 is keyed on
    the reaper reason, not on a blanket 'error for every abort'."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.orchestrator._finalize import _finalize_and_emit_done

    conv_id = 'cv-conv-finalize-stop'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '',
         '_msgId': 'amid-stop-1'},
    ])
    task = _mk_task('t-conv-stop-1', conv_id,
                    _assistantMsgId='amid-stop-1',
                    _initial_msg_count=2,
                    _abort_reason='user-stop', error=None,
                    finishReason=None, status='running',
                    content='partial answer the user cut off')
    manager._record_latest_task(conv_id, task['id'])
    try:
        _finalize_and_emit_done(
            task, model='aws.claude-opus-4.8', preset='medium',
            thinking_depth=None, cfg={},
            last_finish_reason='stop', last_usage=None,
            accumulated_usage={}, api_rounds=[],
            tool_call_happened=False, messages=[], original_messages=[],
            all_search_results_text=[], max_tokens=None,
            thinking_enabled=False, temperature=None,
            _loop_exit_reason='user_abort', _abort_detected_phase='llm_stream',
            project_path='', project_enabled=False,
            round_num=1, assistant_msg=None)

        assert task['finishReason'] == 'aborted', (
            f'a user Stop must still settle aborted, got {task["finishReason"]!r}')
        msgs = _read_msgs(db, conv_id)
        bubble = next(m for m in msgs if m.get('role') == 'assistant')
        assert bubble.get('finishReason') == 'aborted'
        assert not bubble.get('error'), (
            'a clean user-Stop bubble must not invent an error envelope')
    finally:
        _cleanup(db, conv_id)


# ─────────────────────────────────────────────────────────────────────────
# F2 — the reaper's error bubble never lands on a NEWER turn
# ─────────────────────────────────────────────────────────────────────────

def test_reaper_sync_converges_onto_own_slot_when_turn_moved_on():
    """The ce514dce chain, replayed: sync #1 (reaper thread) appends the error
    bubble for the unanswered trailing turn; a NEWER user turn lands; sync #2
    (the wedged worker's late finalize) must NOT append a second bubble —
    it converges onto the task's OWN slot by _taskId.
    NEUTER: reverting F2 re-appends → len grows to 6 → red."""
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.tasks_pkg import manager

    conv_id = 'cv-conv-moved-on'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'earlier q', 'timestamp': 1},
        {'role': 'assistant', 'content': 'earlier a', 'timestamp': 2,
         'finishReason': 'stop', '_taskId': 't-old-settled'},
        {'role': 'user', 'content': 'the prompt that wedged', 'timestamp': 3},
    ])
    task = _mk_task('t-conv-moved-1', conv_id, _initial_msg_count=3)
    manager._record_latest_task(conv_id, task['id'])
    meta = manager.build_result_meta(task)
    try:
        # sync #1 — the reaper thread: tail IS the task's own prompt → append.
        manager._sync_result_to_conversation(task, meta)
        msgs = _read_msgs(db, conv_id)
        assert len(msgs) == 4, f'sync #1 must append the error bubble, got {len(msgs)}'
        assert (msgs[3].get('error') or {}).get('context') == 'stuck-task-reaper'
        assert msgs[3].get('_taskId') == task['id']

        # a NEWER turn's prompt lands before the wedged worker unwinds.
        msgs.append({'role': 'user', 'content': 'the next turn', 'timestamp': 4})
        db.execute('UPDATE conversations SET messages=?, msg_count=? '
                   'WHERE id=? AND user_id=1',
                   (json_dumps_pg(msgs), len(msgs), conv_id))
        db.commit()

        # sync #2 — the late finalize (same task): must converge, NOT append.
        manager._sync_result_to_conversation(task, manager.build_result_meta(task))
        msgs = _read_msgs(db, conv_id)
        assert len(msgs) == 5, (
            f'the late sync must NOT append a second bubble onto the newer '
            f'turn, got {len(msgs)} messages')
        err_bubbles = [m for m in msgs
                       if (m.get('error') or {}).get('context') == 'stuck-task-reaper']
        assert len(err_bubbles) == 1, (
            f'exactly one reaper error bubble must exist, got {len(err_bubbles)}')
        assert msgs[3] is err_bubbles[0] or msgs.index(err_bubbles[0]) == 3
        assert msgs[4].get('role') == 'user', 'the newer turn must stay untouched'
        _assert_reaper_invariants(msgs, task['id'])
    finally:
        _cleanup(db, conv_id)


def test_reaper_sync_drops_when_no_slot_and_turn_moved_on():
    """Drop branch: the reaped task has NO slot anywhere and its own turn is
    no longer the tail — the sync must NOT append the error bubble onto the
    newer turn's prompt. NEUTER: reverting F2 appends → len grows → red."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import manager

    conv_id = 'cv-conv-drop'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'earlier q', 'timestamp': 1},
        {'role': 'assistant', 'content': 'earlier a', 'timestamp': 2,
         'finishReason': 'stop', '_taskId': 't-old-settled'},
        {'role': 'user', 'content': 'the prompt that wedged', 'timestamp': 3},
        {'role': 'user', 'content': 'a newer turn that moved on', 'timestamp': 4},
    ])
    task = _mk_task('t-conv-drop-1', conv_id, _initial_msg_count=3)
    manager._record_latest_task(conv_id, task['id'])
    try:
        manager._sync_result_to_conversation(task, manager.build_result_meta(task))
        msgs = _read_msgs(db, conv_id)
        assert len(msgs) == 4, (
            f'a reaped task whose turn moved on must NOT append, got {len(msgs)}')
        assert not any((m.get('error') or {}).get('context') == 'stuck-task-reaper'
                       for m in msgs), 'no reaper error may land on a foreign turn'
    finally:
        _cleanup(db, conv_id)


# ─────────────────────────────────────────────────────────────────────────
# F3 — provenance: a clean task never adopts a foreign error bubble, and a
#      stale error never survives a clean settle
# ─────────────────────────────────────────────────────────────────────────

def test_successful_sync_never_adopts_foreign_error_bubble():
    """The idx=15 hijack, replayed: the tail is a FOREIGN error bubble (owned
    by the reaped task) sitting above THIS task's prompt. The clean task's
    sync must append its OWN bubble and leave the foreign tombstone intact.
    NEUTER: reverting F3a fills the foreign bubble (content + _taskId
    overwritten, error inherited) → red on both halves."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import manager

    conv_id = 'cv-conv-hijack'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'earlier q', 'timestamp': 1},
        {'role': 'assistant', 'content': 'earlier a', 'timestamp': 2,
         'finishReason': 'stop', '_taskId': 't-old-settled'},
        {'role': 'user', 'content': 'the prompt that wedged', 'timestamp': 3},
        {'role': 'user', 'content': 'the next turn (ours)', 'timestamp': 4},
        # the wrong-turn error bubble the reaper's sync appended one turn late:
        {'role': 'assistant', 'content': '', 'thinking': '',
         'error': _reaper_envelope(), 'finishReason': 'error',
         '_taskId': 't-reaped-foreign'},
    ])
    task = _mk_task('t-conv-clean-1', conv_id,
                    _abort_reason='', aborted=False, status='done',
                    error=None, finishReason='stop',
                    content='the clean answer', _initial_msg_count=4)
    manager._record_latest_task(conv_id, task['id'])
    try:
        manager._sync_result_to_conversation(task, manager.build_result_meta(task))
        msgs = _read_msgs(db, conv_id)
        assert len(msgs) == 6, (
            f'the clean task must append its OWN bubble, got {len(msgs)}')
        foreign, own = msgs[4], msgs[5]
        # the foreign tombstone is UNTOUCHED…
        assert foreign.get('_taskId') == 't-reaped-foreign'
        assert (foreign.get('error') or {}).get('context') == 'stuck-task-reaper'
        assert foreign.get('finishReason') == 'error'
        assert not (foreign.get('content') or ''), (
            'the foreign bubble must not be filled with the clean answer')
        # …and the clean answer carries NO inherited error.
        assert own.get('_taskId') == task['id']
        assert own.get('content') == 'the clean answer'
        assert own.get('finishReason') == 'stop'
        assert 'error' not in own, (
            f'a clean answer must not wear another task\'s error: {own.get("error")}')
    finally:
        _cleanup(db, conv_id)


def test_clean_settle_clears_stale_error_on_own_slot():
    """F3b: OUR OWN slot carries a stale error (a transient mid-stream
    envelope, or a verdict that rode in via a shared bubble); the task
    settles clean → the error is cleared. NEUTER: reverting F3b leaves the
    stale error → red."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import manager

    conv_id = 'cv-conv-stale-err'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '',
         '_msgId': 'amid-stale-1', 'error': _reaper_envelope()},
    ])
    task = _mk_task('t-conv-stale-1', conv_id,
                    _abort_reason='', aborted=False, status='done',
                    error=None, finishReason='stop',
                    content='recovered and answered',
                    _assistantMsgId='amid-stale-1', _initial_msg_count=2)
    manager._record_latest_task(conv_id, task['id'])
    try:
        manager._sync_result_to_conversation(task, manager.build_result_meta(task))
        msgs = _read_msgs(db, conv_id)
        bubble = msgs[1]
        assert bubble.get('content') == 'recovered and answered'
        assert bubble.get('_taskId') == task['id']
        assert 'error' not in bubble, (
            f'a clean settle must clear the stale error, got {bubble.get("error")}')
    finally:
        _cleanup(db, conv_id)


def test_merge_terminal_fields_converges_error_absence():
    """Unit: 'error' is an OWNED field, so its ABSENCE in the terminal message
    is the verdict — the graft must DELETE a stale error from the fresh tail,
    and still COPY a present one. NEUTER: reverting the F3 merge arm leaves
    the stale error → red."""
    from lib.tasks_pkg.manager._sync import _merge_terminal_fields

    fresh = {'role': 'assistant', 'content': 'old', 'error': _reaper_envelope()}
    terminal_clean = {'role': 'assistant', 'content': 'new', 'finishReason': 'stop'}
    out = _merge_terminal_fields(fresh, terminal_clean)
    assert out.get('content') == 'new'
    assert 'error' not in out, 'a clean terminal verdict must evict the stale error'

    fresh2 = {'role': 'assistant', 'content': 'old'}
    terminal_err = {'role': 'assistant', 'content': '', 'finishReason': 'error',
                    'error': _reaper_envelope()}
    out2 = _merge_terminal_fields(fresh2, terminal_err)
    assert (out2.get('error') or {}).get('context') == 'stuck-task-reaper', (
        'a present error must still be grafted')
