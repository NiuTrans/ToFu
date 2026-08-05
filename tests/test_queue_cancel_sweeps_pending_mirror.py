"""Cancel / clear must sweep the ``_pendingQueued`` mirror row from the
conversation body (epic pt_cfdfd30c8699407b, 2026-08-05).

The backend mirrors the FIRST queued user message into the conversation
body as a display-only ``_pendingQueued`` row (cross-device visibility,
``lib/chat/persistence.py::append_pending_user_msg``). Before this fix,
``remove_from_queue`` / ``clear_queue`` deleted ONLY the ``message_queue``
row — stranding the mirror forever: a greyed "queued" bubble for a message
that will never run, on every device, until someone hand-edited history.

These tests drive the REAL queue library against the worker-isolated test
DB: seed a queue row whose payload carries ``_user_msg`` (exactly what
/api/v1/chat/send stores) + a conversation body holding the mirror row,
then cancel/clear and assert the body is swept.
"""

from __future__ import annotations

import json
import time

import pytest

pytestmark = pytest.mark.unit


def _cid():
    return f'test-pqsweep-{time.time_ns()}'


def _db():
    from lib.database import DOMAIN_CHAT, get_thread_db
    return get_thread_db(DOMAIN_CHAT)


def _seed_conv(conv_id, messages):
    db = _db()
    now = int(time.time() * 1000)
    db.execute(
        'INSERT INTO conversations (id, user_id, title, messages, created_at,'
        ' updated_at, settings, msg_count, search_text)'
        ' VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)',
        (conv_id, 't', json.dumps(messages, ensure_ascii=False),
         now, now, '{}', len(messages), ''))
    db.commit()


def _body(conv_id):
    row = _db().execute(
        'SELECT messages FROM conversations WHERE id=?', (conv_id,)).fetchone()
    return json.loads(row['messages']) if row else None


def _enqueue(conv_id, text, ts):
    """Mirror /api/v1/chat/send's queued payload (carries the built user msg)."""
    from lib.message_queue import enqueue_message
    return enqueue_message(conv_id, {
        'text': text, 'timestamp': ts,
        '_user_msg': {'role': 'user', 'content': text, 'timestamp': ts},
    }, {'model': 'test-model'})


BASE = [
    {'role': 'user', 'content': 'q1', 'timestamp': 1, '_msgId': 'a'},
    {'role': 'assistant', 'content': 'a1', 'timestamp': 2, '_msgId': 'b'},
]


def test_cancel_sweeps_the_mirror_row():
    conv_id = _cid()
    ts = 777000
    res = _enqueue(conv_id, 'later', ts)
    _seed_conv(conv_id, BASE + [
        {'role': 'user', 'content': 'later', 'timestamp': ts,
         '_msgId': 'c', '_pendingQueued': True}])

    from lib.message_queue import remove_from_queue
    assert remove_from_queue(conv_id, res['queueId']) is True

    body = _body(conv_id)
    assert [m['_msgId'] for m in body] == ['a', 'b'], (
        'the cancelled message’s _pendingQueued mirror row was left in the '
        'conversation body — a ghost queued bubble on every device')


def test_cancel_without_user_msg_leaves_body_untouched():
    """Legacy/peer queue rows carry no ``_user_msg`` — nothing to match, so
    the sweep must no-op rather than nuke unrelated rows."""
    conv_id = _cid()
    from lib.message_queue import enqueue_message, remove_from_queue
    res = enqueue_message(conv_id, {'text': 'plain', 'timestamp': 42},
                          {'model': 'test-model'})
    _seed_conv(conv_id, BASE + [
        {'role': 'user', 'content': 'unrelated pending', 'timestamp': 99,
         '_msgId': 'c', '_pendingQueued': True}])

    assert remove_from_queue(conv_id, res['queueId']) is True
    body = _body(conv_id)
    assert [m['_msgId'] for m in body] == ['a', 'b', 'c'], (
        'the sweep removed a row it had no identity match for')


def test_clear_sweeps_all_pending_rows():
    conv_id = _cid()
    _enqueue(conv_id, 'one', 111)
    _enqueue(conv_id, 'two', 222)
    _seed_conv(conv_id, BASE + [
        {'role': 'user', 'content': 'one', 'timestamp': 111,
         '_msgId': 'c', '_pendingQueued': True},
        {'role': 'user', 'content': 'two', 'timestamp': 222,
         '_msgId': 'd', '_pendingQueued': True}])

    from lib.message_queue import clear_queue
    assert clear_queue(conv_id) == 2

    body = _body(conv_id)
    assert [m['_msgId'] for m in body] == ['a', 'b'], (
        'clear_queue left pending mirror rows behind')


def test_dispatched_row_is_never_swept():
    """The marker guard: a row whose turn DISPATCHED (marker cleared by the
    dispatch reconcile) is a real turn — a later cancel of a DIFFERENT queue
    item with a colliding timestamp must not remove it."""
    conv_id = _cid()
    ts = 555000
    res = _enqueue(conv_id, 'real turn', ts)
    _seed_conv(conv_id, BASE + [
        {'role': 'user', 'content': 'real turn', 'timestamp': ts,
         '_msgId': 'c'}])   # dispatched — no _pendingQueued marker

    from lib.message_queue import remove_from_queue
    assert remove_from_queue(conv_id, res['queueId']) is True

    body = _body(conv_id)
    assert [m['_msgId'] for m in body] == ['a', 'b', 'c'], (
        'the sweep removed a dispatched (unmarked) row — only _pendingQueued '
        'rows may ever be swept')


def test_route_cancel_sweeps_and_notifies(flask_client):
    """End-to-end over HTTP: DELETE the queue row → the body mirror goes,
    and the response is the api_ok envelope (charter #0)."""
    conv_id = _cid()
    ts = 888000
    res = _enqueue(conv_id, 'later', ts)
    _seed_conv(conv_id, BASE + [
        {'role': 'user', 'content': 'later', 'timestamp': ts,
         '_msgId': 'c', '_pendingQueued': True}])

    resp = flask_client.delete(f'/api/v1/chat/queue/{conv_id}/{res["queueId"]}')
    assert resp.status_code == 200
    assert resp.get_json().get('ok') is True

    body = _body(conv_id)
    assert [m['_msgId'] for m in body] == ['a', 'b']
