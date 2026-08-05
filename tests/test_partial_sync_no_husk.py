#!/usr/bin/env python3
"""Regression test: ``_sync_partial_to_conversation`` must NOT materialize a
brand-new trailing assistant row from a thinking-only first checkpoint.

WHY
---
An autopilot follow-up (or any) turn streams a stray reasoning fragment
(``thinking:'I'``) before any content, then dies (server crash) while
``task_results.status='running'``.  The partial-checkpoint sync used to
append ``{role:'assistant', content:'', thinking:'I'}`` onto the
conversation — a husk with no finishReason that renders as a blank,
finish-tag-less bubble and slips past the frontend ghost-cleanup.

The fix: when the last message is NOT already an assistant row (i.e. the
sync would CREATE a new trailing row), require real ``content`` before
appending.  A thinking-only checkpoint is deferred; once content arrives the
row is created with the accumulated thinking written alongside, so nothing is
dropped.  Updating an EXISTING assistant row (the frontend placeholder) is
unaffected — that's the normal in-flight path.

Both directions are asserted (deferred husk; content materializes; existing
placeholder still updated thinking-only).
"""

import os
import sys
import time
import json as _json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Flask→Quart shim (mirrors test_chat_manager_migration.py).
import quart as _quart
sys.modules['flask'] = _quart

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.ci_serial]


@pytest.fixture(scope='module', autouse=True)
def _bootstrap_db():
    """Ensure the (conftest-forced temp sqlite) DB has its tables created.

    Conftest forces a fresh temp ``TOFU_DB_PATH`` but does not always run
    schema init for a test that touches the DB directly via get_thread_db.
    """
    from lib.database import init_db
    init_db()
    yield


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _seed_conv(db, conv_id, messages):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'husk-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _read_msgs(db, conv_id):
    row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    return _json.loads(row[0]) if isinstance(row[0], str) else row[0]


def test_thinking_only_checkpoint_defers_new_assistant_row():
    """Last message is a USER turn (autopilot VU); a thinking-only checkpoint
    must NOT append a new trailing assistant husk."""
    from lib.database import (DOMAIN_CHAT, get_thread_db, db_execute_with_retry)
    from lib.tasks_pkg.manager import (create_task, _sync_partial_to_conversation,
                                       _conv_latest_task, _conv_latest_task_lock)

    conv_id = 'cv-husk-defer'
    db = get_thread_db(DOMAIN_CHAT)
    # Trailing user (virtual-user reply) — the follow-up assistant hasn't been
    # materialized yet; the frontend placeholder didn't sync in this scenario.
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'go on', '_isVirtualUser': True, 'timestamp': 1},
    ])
    try:
        task = create_task(conv_id, [{'role': 'user', 'content': 'go on'}], {})
        task['content'] = ''
        task['thinking'] = 'I'  # one stray reasoning token, then "crash"
        task['_memoryPrefetch'] = {'phase': 'done', 'selected': 1}
        with _conv_latest_task_lock:
            _conv_latest_task[conv_id] = task['id']

        _sync_partial_to_conversation(task)

        msgs = _read_msgs(db, conv_id)
        assert len(msgs) == 1, f'husk was appended! messages={msgs}'
        assert msgs[-1]['role'] == 'user', 'trailing message should still be the VU user turn'
    finally:
        with _conv_latest_task_lock:
            _conv_latest_task.pop(conv_id, None)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    _ok('thinking-only checkpoint does NOT append a trailing assistant husk')


def test_content_checkpoint_materializes_row_with_thinking():
    """Once REAL content arrives, the row is created — and the thinking
    accumulated by then is written alongside it (nothing dropped)."""
    from lib.database import (DOMAIN_CHAT, get_thread_db, db_execute_with_retry)
    from lib.tasks_pkg.manager import (create_task, _sync_partial_to_conversation,
                                       _conv_latest_task, _conv_latest_task_lock)

    conv_id = 'cv-husk-content'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'go on', '_isVirtualUser': True, 'timestamp': 1},
    ])
    try:
        task = create_task(conv_id, [{'role': 'user', 'content': 'go on'}], {})
        task['content'] = 'Here is the real reply.'
        task['thinking'] = 'I reasoned about it'
        with _conv_latest_task_lock:
            _conv_latest_task[conv_id] = task['id']

        _sync_partial_to_conversation(task)

        msgs = _read_msgs(db, conv_id)
        assert len(msgs) == 2, f'expected the assistant row to be created, got {msgs}'
        assert msgs[-1]['role'] == 'assistant'
        assert msgs[-1]['content'] == 'Here is the real reply.'
        assert msgs[-1]['thinking'] == 'I reasoned about it', 'accumulated thinking dropped'
    finally:
        with _conv_latest_task_lock:
            _conv_latest_task.pop(conv_id, None)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    _ok('content checkpoint materializes the assistant row + keeps accumulated thinking')


def test_existing_placeholder_still_takes_thinking_only_update():
    """When the frontend already pushed an empty assistant placeholder, a
    thinking-only checkpoint must still update IT (the normal in-flight path is
    unaffected — we only refuse to CREATE a new husk)."""
    from lib.database import (DOMAIN_CHAT, get_thread_db, db_execute_with_retry)
    from lib.tasks_pkg.manager import (create_task, _sync_partial_to_conversation,
                                       _conv_latest_task, _conv_latest_task_lock)

    conv_id = 'cv-husk-placeholder'
    db = get_thread_db(DOMAIN_CHAT)
    # Frontend placeholder already present as the trailing assistant row.
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'go on', '_isVirtualUser': True, 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'timestamp': 2},
    ])
    try:
        task = create_task(conv_id, [{'role': 'user', 'content': 'go on'}], {})
        task['content'] = ''
        task['thinking'] = 'partial reasoning'
        with _conv_latest_task_lock:
            _conv_latest_task[conv_id] = task['id']

        _sync_partial_to_conversation(task)

        msgs = _read_msgs(db, conv_id)
        assert len(msgs) == 2, f'must not append a new row, got {msgs}'
        assert msgs[-1]['role'] == 'assistant'
        assert msgs[-1]['thinking'] == 'partial reasoning', 'existing placeholder not updated'
    finally:
        with _conv_latest_task_lock:
            _conv_latest_task.pop(conv_id, None)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    _ok('existing assistant placeholder still receives a thinking-only update')


def main():
    print()
    print(_color('═══ partial-sync no-husk tests ═══', '36'))
    print()
    # Standalone runs (python tests/x.py) skip conftest → force sqlite + guard
    # the prod DB + bootstrap the schema, all via the shared helper.
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_partial_sync_no_husk.__main__')
    tests = [
        test_thinking_only_checkpoint_defers_new_assistant_row,
        test_content_checkpoint_materializes_row_with_thinking,
        test_existing_placeholder_still_takes_thinking_only_update,
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
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
