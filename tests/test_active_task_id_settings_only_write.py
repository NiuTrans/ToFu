#!/usr/bin/env python3
"""tests/test_active_task_id_settings_only_write.py — the /api/chat/send (and
regenerate / continue) step-6 ``activeTaskId`` write must be SETTINGS-ONLY and
must NOT clobber a concurrent ``messages`` checkpoint written by the task thread.

WHY
---
`chat_send` (routes/chat.py) does, after spawning the task thread:

    task_id = _start_task_for_conv(...)        # step 5 — thread now running
    # step 6 — record activeTaskId

Between step 5 and step 6 the task thread can ALREADY write a partial
checkpoint (``_sync_partial_to_conversation`` → ``messages`` now carries the
assistant slot with streamed content). The old step 6 called
``_persist_conv_messages(db, conv_id, messages, title, {'activeTaskId': task_id})``
— a FULL-ROW upsert that rewrites the ``messages`` column with the route's
STALE, user-only ``messages`` list (the assistant slot doesn't exist yet in the
route's local copy) and bumps ``updated_at``. That CLOBBERS the checkpoint back
to the pre-start snapshot → on a poor network (where the frontend's finishStream
may never sync) the reload reads ``conversations.messages`` and shows "Waiting…"
over an answer that was actually generated.

The fix routes step 6 through ``set_conversation_settings(conv_id,
{'activeTaskId': task_id}, db=db)`` — a per-conv serialized SETTINGS-ONLY merge
that never touches ``messages`` / ``updated_at``.

Tests (drive the REAL helpers against a real DB):
  1. ``test_activeTaskId_write_preserves_checkpoint`` — THE FIX. Seed the stale
     user-only tail, simulate the task-thread checkpoint (assistant slot with
     content), then run the fix path. Assert the checkpointed assistant content
     SURVIVES and activeTaskId is recorded.
  2. ``test_old_fullrow_write_clobbers_checkpoint`` — ★ the double-neuter /
     contrast: the OLD path (``persist_conv_messages`` with the stale
     user-only messages) DESTROYS the assistant checkpoint. This proves the
     test discriminates the fix (fix preserves, neuter clobbers).

Env note (see project memory): run DIRECTLY
(``python tests/test_active_task_id_settings_only_write.py``) — bare pytest may
lack the schema.
"""

import json as _json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _seed(db, conv_id, messages):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'atid-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now, 'updated_at': now, 'search_text': '',
        'settings': json_dumps_pg({'model': 'x'}),
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'search_text', 'settings'],
       retry=True)
    db.commit()


def _read(db, conv_id):
    row = db.execute(
        'SELECT messages, settings FROM conversations WHERE id=? AND user_id=1',
        (conv_id,)).fetchone()
    if not row:
        return None, None
    msgs = _json.loads(row[0]) if isinstance(row[0], str) else (row[0] or [])
    s = _json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
    return msgs, s


def _write_checkpoint(db, conv_id, messages):
    """Simulate the task thread's _sync_partial_to_conversation write."""
    from lib.database import db_execute_with_retry, json_dumps_pg
    db_execute_with_retry(
        db, 'UPDATE conversations SET messages=?, msg_count=? WHERE id=? AND user_id=1',
        (json_dumps_pg(messages), len(messages), conv_id))
    db.commit()


def _cleanup(db, conv_id):
    from lib.database import db_execute_with_retry
    db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db.commit()


# The route's local copy at step 6: ONLY the user message (the assistant slot
# is created later, inside the task thread).
_STALE_ROUTE_MSGS = [{'role': 'user', 'content': 'hello', 'timestamp': '2026-07-06T00:00:00Z'}]
# What the task thread checkpoints between step 5 and step 6.
_CHECKPOINT_MSGS = _STALE_ROUTE_MSGS + [
    {'role': 'assistant', 'content': 'a substantial streamed partial answer',
     'timestamp': '2026-07-06T00:00:01Z'}]


def test_activeTaskId_write_preserves_checkpoint():
    from lib.conversations import set_conversation_settings
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-atid-fix'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _STALE_ROUTE_MSGS)
    try:
        # Task thread checkpoints the assistant slot between step 5 and step 6.
        _write_checkpoint(db, conv_id, _CHECKPOINT_MSGS)
        # ★ THE FIX: settings-only activeTaskId write.
        set_conversation_settings(conv_id, {'activeTaskId': 'task-1'}, db=db)

        msgs, s = _read(db, conv_id)
        assert s.get('activeTaskId') == 'task-1', f'activeTaskId not recorded: {s}'
        assert len(msgs) == 2, f'checkpoint clobbered — messages={msgs}'
        assert msgs[1]['role'] == 'assistant', msgs
        assert msgs[1]['content'] == 'a substantial streamed partial answer', (
            f'assistant checkpoint content lost: {msgs[1]!r}')
        assert s.get('model') == 'x', f'unrelated settings key lost: {s}'
    finally:
        _cleanup(db, conv_id)
    _ok('settings-only activeTaskId write PRESERVES the task-thread checkpoint')


def test_old_fullrow_write_clobbers_checkpoint():
    """★ Double-neuter contrast: the OLD full-row write DESTROYS the checkpoint.

    Reproduces exactly what step 6 used to do — a full-row persist with the
    route's stale user-only ``messages`` list. Demonstrates the bug the fix
    removes, and proves the preserve-assertion above is discriminating.
    """
    from lib.chat.persistence import persist_conv_messages
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-atid-neuter'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _STALE_ROUTE_MSGS)
    try:
        _write_checkpoint(db, conv_id, _CHECKPOINT_MSGS)
        # OLD behaviour: full-row upsert with the STALE user-only messages.
        persist_conv_messages(db, conv_id, list(_STALE_ROUTE_MSGS), 'atid-test',
                              {'activeTaskId': 'task-1'})
        db.commit()

        msgs, s = _read(db, conv_id)
        assert s.get('activeTaskId') == 'task-1', s
        # The bug: the assistant checkpoint is GONE (rewound to user-only).
        assert len(msgs) == 1, (
            f'expected the old full-row write to clobber the checkpoint down to '
            f'1 msg, got {len(msgs)} — if this ever becomes 2, the fix is no '
            f'longer necessary AND test #1 is no longer discriminating')
    finally:
        _cleanup(db, conv_id)
    _ok('old full-row write CLOBBERS the checkpoint (contrast — proves the fix bites)')


def main():
    print()
    print(_color('═══ activeTaskId settings-only write tests ═══', '36'))
    print()
    tests = [
        test_activeTaskId_write_preserves_checkpoint,
        test_old_fullrow_write_clobbers_checkpoint,
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
    print(_color(f'═══ ALL {len(tests)} activeTaskId-WRITE TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_active_task_id_settings_only_write.__main__')
    main()
