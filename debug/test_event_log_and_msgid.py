"""Smoke test for the persisted SSE event log + stable per-message IDs.

Covers:
  * lib/tasks_pkg/event_log.py — append, coalesce, replay, Last-Event-ID
  * lib/tasks_pkg/manager.py   — _assign_message_ids / find_message_by_id
  * routes/conversations.py    — PATCH .../messages/by-id/<msg_id>
  * routes/translate.py        — id-resolution fallback in _commit_translation_inner

Run with: python debug/test_event_log_and_msgid.py
"""

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/tofu_event_log_test.db')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/chatui_event_log_test.db')

# Werkzeug shim for editable Flask snapshot
import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '0.0.0'

# Reset DB before import
if os.path.exists(os.environ['TOFU_DB_PATH']):
    os.unlink(os.environ['TOFU_DB_PATH'])

from flask import Flask  # noqa: E402

from lib.database import DOMAIN_CHAT, get_thread_db, init_db, json_dumps_pg  # noqa: E402

init_db()

from lib.tasks_pkg.event_log import (  # noqa: E402
    append_persistent_event, flush_pending, has_terminal_event, read_events,
)
from lib.tasks_pkg.manager import _assign_message_ids, find_message_by_id  # noqa: E402
from routes.conversations import conversations_bp  # noqa: E402
from routes.translate import _commit_translation_inner  # noqa: E402

app = Flask(__name__)
app.register_blueprint(conversations_bp)
client = app.test_client()


def _create_conv(messages):
    conv_id = 'evtest-' + uuid.uuid4().hex[:10]
    now = int(time.time() * 1000)
    db = get_thread_db(DOMAIN_CHAT)
    db.execute(
        '''INSERT INTO conversations (id, user_id, title, messages, created_at, updated_at,
                                      settings, msg_count, search_text)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        (conv_id, 1, 'EvTest', json_dumps_pg(messages), now, now,
         '{}', len(messages), ''),
    )
    db.commit()
    return conv_id


def _load_messages(conv_id):
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT messages FROM conversations WHERE id=? AND user_id=1', (conv_id,)
    ).fetchone()
    return json.loads(row['messages'] or '[]')


def test_event_log_replay():
    tid = 'tid-replay-' + uuid.uuid4().hex[:6]
    seq = [
        {'type': 'phase', 'phase': 'planning', 'detail': 'go'},
        {'type': 'delta', 'content': 'Hello '},
        {'type': 'delta', 'content': 'world'},
        {'type': 'delta', 'content': '!'},
        {'type': 'phase', 'phase': 'tool', 'detail': 'web_search'},
        {'type': 'done', 'finishReason': 'stop'},
    ]
    for i, ev in enumerate(seq):
        append_persistent_event(tid, i, ev)
    flush_pending(tid)

    all_events = read_events(tid)
    # Each delta is its own row — no coalescing
    assert len(all_events) == 6, f'expected 6 rows, got {len(all_events)}'
    delta_rows = [e for e in all_events if e['payload']['type'] == 'delta']
    assert len(delta_rows) == 3
    assert delta_rows[0]['payload']['content'] == 'Hello '
    assert delta_rows[1]['payload']['content'] == 'world'
    assert delta_rows[2]['payload']['content'] == '!'

    # Last-Event-ID semantics
    since1 = read_events(tid, since_event_id=1)
    assert [e['event_id'] for e in since1] == [2, 3, 4, 5]
    since3 = read_events(tid, since_event_id=3)
    assert [e['event_id'] for e in since3] == [4, 5]

    assert has_terminal_event(tid) is True
    assert has_terminal_event('does-not-exist') is False
    print('[OK] test_event_log_replay')


def test_assign_message_ids_idempotent():
    msgs = [
        {'role': 'user', 'content': 'a'},
        {'role': 'assistant', 'content': 'b'},
        {'role': 'user', 'content': 'c', '_msgId': 'fixed'},
    ]
    assert _assign_message_ids(msgs) is True
    ids = [m['_msgId'] for m in msgs]
    assert all(ids), 'all messages should get an id'
    assert msgs[2]['_msgId'] == 'fixed', 'preserved id should not change'

    # Idempotent
    assert _assign_message_ids(msgs) is False, 'second pass must be a no-op'
    assert [m['_msgId'] for m in msgs] == ids, 'ids must be stable across calls'

    # Lookup
    i, m = find_message_by_id(msgs, 'fixed')
    assert i == 2 and m['content'] == 'c'
    assert find_message_by_id(msgs, 'no-such-id') == (None, None)
    print('[OK] test_assign_message_ids_idempotent')


def test_patch_message_by_id_survives_insert():
    mid = str(uuid.uuid4())
    conv_id = _create_conv([
        {'role': 'user', 'content': 'q1', '_msgId': mid, 'timestamp': 1},
        {'role': 'assistant', 'content': 'a1', 'timestamp': 2},
    ])

    # Direct PATCH by id
    r = client.patch(
        f'/api/conversations/{conv_id}/messages/by-id/{mid}',
        json={'content': 'edited q1'},
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['msg']['content'] == 'edited q1'

    # Simulate concurrent insert at idx 0 (shifts the original message to idx 1)
    db = get_thread_db(DOMAIN_CHAT)
    msgs = _load_messages(conv_id)
    msgs.insert(0, {'role': 'user', 'content': 'inserted before', 'timestamp': 0})
    now = int(time.time() * 1000)
    db.execute(
        'UPDATE conversations SET messages=?, msg_count=?, updated_at=? WHERE id=? AND user_id=1',
        (json_dumps_pg(msgs), len(msgs), now, conv_id),
    )
    db.commit()

    # PATCH by-id still finds it, despite the shift (would have failed by index)
    r = client.patch(
        f'/api/conversations/{conv_id}/messages/by-id/{mid}',
        json={'content': 'still finds it'},
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['idx'] == 1, f'expected idx=1, got {r.get_json()["idx"]}'

    final = _load_messages(conv_id)
    assert final[0]['content'] == 'inserted before'  # unchanged
    assert final[1]['content'] == 'still finds it'   # the targeted edit landed
    assert final[2]['content'] == 'a1'

    # Unknown id → 404
    r = client.patch(
        f'/api/conversations/{conv_id}/messages/by-id/{uuid.uuid4()}',
        json={'content': 'nope'},
    )
    assert r.status_code == 404
    print('[OK] test_patch_message_by_id_survives_insert')


def test_translate_commit_resolves_by_id():
    """The msg_idx-out-of-range warning class is fixed by id resolution."""
    mid = str(uuid.uuid4())
    conv_id = _create_conv([
        {'role': 'user', 'content': 'hello'},
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': 'orig answer in english',
         '_msgId': mid, 'timestamp': 100},
    ])

    # Caller passes a stale index (msg_idx=7) AND the stable id — the id wins.
    _commit_translation_inner(
        conv_id, msg_idx=7, field='translatedContent',
        translated_text='你好',
        original_text='orig answer in english',
        model='test-model', msg_id=mid,
    )
    final = _load_messages(conv_id)
    target = next(m for m in final if m.get('_msgId') == mid)
    assert target.get('translatedContent') == '你好', target
    assert target.get('_translateModel') == 'test-model'
    assert target.get('_showingTranslation') is True
    print('[OK] test_translate_commit_resolves_by_id')


def test_translate_commit_falls_back_to_content():
    """When neither id nor idx hit, fall back to content match before giving up."""
    mid = str(uuid.uuid4())
    # Note: assistant message has no _msgId on the conv yet
    conv_id = _create_conv([
        {'role': 'user', 'content': 'hello'},
        {'role': 'assistant', 'content': 'unique-target-content-xyz'},
    ])
    # Caller has a stale id and a stale idx — content match must save it.
    _commit_translation_inner(
        conv_id, msg_idx=99, field='translatedContent',
        translated_text='translated',
        original_text='unique-target-content-xyz',
        model='test', msg_id=mid,
    )
    final = _load_messages(conv_id)
    # Content match resolves to the assistant; backfilled _msgId from caller
    target = next(m for m in final if m['role'] == 'assistant')
    assert target.get('translatedContent') == 'translated'
    assert target.get('_msgId') == mid, 'caller-supplied id should backfill onto content-matched message'
    print('[OK] test_translate_commit_falls_back_to_content')


def main():
    test_event_log_replay()
    test_assign_message_ids_idempotent()
    test_patch_message_by_id_survives_insert()
    test_translate_commit_resolves_by_id()
    test_translate_commit_falls_back_to_content()
    print('\nAll event-log + msgId smoke tests passed.')


if __name__ == '__main__':
    main()
