#!/usr/bin/env python3
# Incident anchor: born in commit ab99ef8b — checkpoint: accumulated work since last commit
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Phase-1 (parity-gap closure): the terminal `done` event must ship the EXACT
assistant dict committed to ``conversations.messages`` — no parallel
reconstruction, no keep-longer/snapshot papering-over.

WHY
---
Historically the settled bubble record was built FOUR separate times (DB dict /
`done` SSE event / `state` snapshot / `/poll`), synced "by convention" (the
``extract_task_meta`` docstring warns 4 paths must match). Worse, the
non-autopilot path emitted the `done` event (orchestrator.py:1014) BEFORE
``persist_task_result`` committed the DB row (orchestrator.py:1023) — so the
terminal event a client received was NOT the committed record, and the record
did not even exist yet. That divergence is exactly what the frontend
keep-longer / snapshot / poll-merge scaffolding exists to paper over.

The fix: ``_sync_result_to_conversation`` stamps ``task['_committedMsg']`` with
the EXACT dict it wrote (the trailing assistant post-CAS, or the fresh row's
authoritative tail on a genuine frontend-won race). The orchestrator hoists
this sync BEFORE ``append_event(done_evt)`` for ALL paths and attaches
``done_evt['committedMessage'] = task['_committedMsg']``. So the terminal event
ships the DB truth verbatim.

Tests (drive the REAL shipped ``_sync_result_to_conversation``):
  1. ``test_committed_msg_equals_db_tail`` — after a successful sync,
     ``task['_committedMsg']`` is byte-equal to the assistant dict now in
     ``conversations.messages``. ★ THE FIX (parity). Double-neuter: remove the
     ``task['_committedMsg'] = ...`` stamp and this FAILS (no committed frame).
  2. ``test_committed_msg_carries_terminal_metadata`` — the committed frame
     carries finishReason/usage/model (what the done event needs), proving the
     frontend can project it verbatim without re-deriving.
  3. ``test_no_committed_msg_on_skip_path`` — behaviour preservation: an
     inline-message task (no conv row) does NOT stamp ``_committedMsg`` → the
     done event omits it → the client keeps its transient buffer (offline
     fallback invariant).
"""

import json as _json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

# ci_serial: real create_task + _sync_result_to_conversation writes through
# the shared sqlite pool — hit 'database is locked' under the CI parallel
# lane's write storms (4034072 3.12 leg) while passing in ~1s uncontended.
pytestmark = [pytest.mark.unit, pytest.mark.ci_serial]


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _seed_conv(db, conv_id, messages):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'parity-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _read_tail(db, conv_id):
    row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    msgs = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
    return msgs[-1]


def test_committed_msg_equals_db_tail():
    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry
    from lib.tasks_pkg.manager import (create_task, _sync_result_to_conversation,
                                        _conv_latest_task, _conv_latest_task_lock)

    conv_id = 'cv-parity-eq'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'timestamp': 2},
    ])
    try:
        task = create_task(conv_id, [{'role': 'user', 'content': 'U1'}], {})
        task['content'] = 'The committed answer must equal the DB tail verbatim.'
        task['thinking'] = 'some reasoning'
        task['finishReason'] = 'stop'
        task['model'] = 'test-model'
        task['usage'] = {'input_tokens': 10, 'output_tokens': 20}
        with _conv_latest_task_lock:
            _conv_latest_task[conv_id] = task['id']

        from lib.tasks_pkg.manager import build_result_meta
        _sync_result_to_conversation(task, build_result_meta(task))

        committed = task.get('_committedMsg')
        assert committed is not None, (
            'task["_committedMsg"] was NOT stamped — the done event has no '
            'verbatim committed dict to ship (parity gap OPEN).')
        db_tail = _read_tail(db, conv_id)
        # The stamped frame must equal the row actually written.
        assert committed.get('content') == db_tail.get('content') == task['content'], (
            f'content parity broken: committed={committed.get("content")!r} '
            f'db={db_tail.get("content")!r}')
        assert committed.get('thinking') == db_tail.get('thinking') == 'some reasoning'
        assert committed.get('finishReason') == db_tail.get('finishReason') == 'stop'
        assert committed.get('model') == db_tail.get('model') == 'test-model'
        assert committed.get('usage') == db_tail.get('usage')
    finally:
        with _conv_latest_task_lock:
            _conv_latest_task.pop(conv_id, None)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    _ok('committed frame is byte-equal to the DB-committed assistant tail (parity)')


def test_committed_msg_carries_terminal_metadata():
    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry
    from lib.tasks_pkg.manager import (create_task, _sync_result_to_conversation,
                                        build_result_meta,
                                        _conv_latest_task, _conv_latest_task_lock)

    conv_id = 'cv-parity-meta'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'timestamp': 2},
    ])
    try:
        task = create_task(conv_id, [{'role': 'user', 'content': 'U1'}], {})
        task['content'] = 'answer'
        task['finishReason'] = 'stop'
        task['model'] = 'm'
        task['usage'] = {'output_tokens': 5}
        with _conv_latest_task_lock:
            _conv_latest_task[conv_id] = task['id']
        _sync_result_to_conversation(task, build_result_meta(task))
        cm = task.get('_committedMsg') or {}
        # These are the fields the frontend done handler projects verbatim.
        assert cm.get('finishReason') == 'stop'
        assert cm.get('model') == 'm'
        assert cm.get('usage') == {'output_tokens': 5}
        assert cm.get('role') == 'assistant'
    finally:
        with _conv_latest_task_lock:
            _conv_latest_task.pop(conv_id, None)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    _ok('committed frame carries terminal metadata the done event projects verbatim')


def test_committed_msg_segments_refreshed_at_sync():
    """pt_687b87ac: the terminal sync must RE-ASSEMBLE the segment timeline
    before writing last_msg / stamping _committedMsg — never persist whatever
    mid-stream checkpoint assembly happens to sit on ``task['segments']``.

    The race this pins: the pre-emit sync stamps the done frame's
    committedMessage while ``task['segments']`` still holds a mid-round
    checkpoint timeline (terminal text segment = only the first streamed
    word); persist_task_result's later re-assembly then completes the DB
    tail — the done frame already left with the stale prefix. Seed that
    exact stale state and demand the sync converge BOTH channels to the
    final text. RED before the fix (the sync used to persist the stale
    list verbatim), GREEN after.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry
    from lib.tasks_pkg.manager import (create_task, _sync_result_to_conversation,
                                        build_result_meta,
                                        _conv_latest_task, _conv_latest_task_lock)

    conv_id = 'cv-parity-segments'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'timestamp': 2},
    ])
    try:
        task = create_task(conv_id, [{'role': 'user', 'content': 'U1'}], {})
        task['content'] = 'Studio iteration complete — the file is on disk.'
        task['finishReason'] = 'stop'
        # The stale mid-stream checkpoint timeline: terminal segment holds
        # ONLY the first streamed word — exactly what the e2e caught riding
        # the done frame.
        task['segments'] = [
            {'type': 'text', 'text': 'Studio ',
             'deliverable': True, 'terminal': True},
        ]
        with _conv_latest_task_lock:
            _conv_latest_task[conv_id] = task['id']
        _sync_result_to_conversation(task, build_result_meta(task))

        committed = task.get('_committedMsg')
        assert committed is not None, '_committedMsg not stamped'
        c_segs = committed.get('segments') or []
        c_text = next((s.get('text') for s in c_segs
                       if s.get('type') == 'text' and s.get('terminal')), None)
        assert c_text == task['content'], (
            'committedMessage carries a STALE segment timeline: '
            f'terminal text={c_text!r} (expected {task["content"]!r}) — the '
            'done frame ships a prefix of the final answer (pt_687b87ac).')
        db_segs = (_read_tail(db, conv_id).get('segments') or [])
        d_text = next((s.get('text') for s in db_segs
                       if s.get('type') == 'text' and s.get('terminal')), None)
        assert d_text == task['content'], (
            f'DB tail carries the stale timeline: terminal text={d_text!r}')
    finally:
        with _conv_latest_task_lock:
            _conv_latest_task.pop(conv_id, None)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    _ok('terminal sync re-assembles segments — committed frame + DB tail carry the FINAL timeline')


def test_no_committed_msg_on_skip_path():
    """Inline-message tasks (no conversation row) legitimately skip the write.
    The frame MUST stay unset so the done event omits committedMessage and the
    client keeps its transient buffer (offline/skip fallback invariant)."""
    from lib.tasks_pkg.manager import (create_task, _sync_result_to_conversation,
                                        build_result_meta)
    task = create_task('cv-parity-inline', [{'role': 'user', 'content': 'U1'}], {})
    task['content'] = 'inline answer read from task_results by the caller'
    task['finishReason'] = 'stop'
    task['_inline_messages'] = True   # external-caller short-circuit
    _sync_result_to_conversation(task, build_result_meta(task))
    assert task.get('_committedMsg') is None, (
        'skip path stamped _committedMsg — a done event would ship a dict that '
        'was NOT committed, defeating the offline-fallback invariant.')
    _ok('skip path leaves _committedMsg unset (done event omits it → buffer fallback)')


def main():
    print()
    print(_color('═══ Phase-1 committed-message parity tests ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_committed_message_parity.__main__')
    tests = [
        test_committed_msg_equals_db_tail,
        test_committed_msg_carries_terminal_metadata,
        test_committed_msg_segments_refreshed_at_sync,
        test_no_committed_msg_on_skip_path,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} PARITY TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
