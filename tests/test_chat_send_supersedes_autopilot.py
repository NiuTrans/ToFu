#!/usr/bin/env python3
"""tests/test_chat_send_supersedes_autopilot.py — a real user send must NOT
queue behind an in-flight AUTOPILOT turn; it supersedes it.

WHY
---
The server-side queue gate in ``routes/chat.py::chat_send`` used to enqueue a
new user message whenever ANY running (non-aborted) task existed for the conv.
But an autopilot follow-up is a background worker turn the virtual-user loop
spawns AFTER the conversation looks ended to the user — it can run for minutes.
So a human who typed a follow-up got silently stuck in the queue behind an
invisible autopilot turn (observed in prod: conv sat "QUEUED" for ~8.5 min
while a ¥74 autopilot follow-up finished, only THEN dispatching the human msg).

Fix (Option A, root cause): the queue gate now discriminates task type. A human
message ALWAYS outranks autopilot (the queue already sorts KIND_REAL ahead of
KIND_AUTOPILOT); we extend that to the IN-FLIGHT turn. When the ONLY running
task(s) for the conv are autopilot turns, ``chat_send``:
  1. marks them ``aborted`` (reason ``superseded_by_user_send``) — a REAL
     backend abort so the invisible turn actually stops (also reclaims the
     zombie), not a frontend hide, and
  2. calls ``disarm_autopilot`` (clears the armed marker, flips live config
     off, writes the concluded record), then
  3. starts the human message immediately (not queued).

A genuine NORMAL worker turn still causes enqueue — a human correctly waits
behind a real in-progress reply. That is the negative control below.

Tests (drive the REAL route via the sync test client; stub ``spawn_task`` so no
LLM/orchestrator runs):
  1. ``test_send_supersedes_inflight_autopilot`` — THE FIX. Running autopilot
     follow-up + armed marker → send returns ``taskId`` (NOT queued), the
     autopilot task is aborted, the marker is cleared, queue depth is 0.
  2. ``test_send_queues_behind_normal_worker`` — ★ negative control. Running
     NORMAL worker turn → send returns ``{queued: true}``, the worker is NOT
     aborted, and the message is in the queue. Proves the discriminator bites.
  3. ``test_normal_worker_wins_over_coexisting_autopilot`` — guard: a normal
     worker + an autopilot task both running → still enqueues, and the send
     gate does NOT force-abort the autopilot task (worker-bucket precedence).
"""

import time

import pytest

from lib.tasks_pkg import tasks, tasks_lock
from lib.message_queue import (
    arm_autopilot_marker,
    clear_autopilot_marker,
    clear_queue,
    get_queue_depth,
    has_autopilot_marker,
)


def _cid():
    return f'test-sup-{time.time_ns()}'


@pytest.fixture()
def put_task():
    """Insert synthetic tasks into the in-memory registry; auto-cleanup."""
    added = []

    def _put(task):
        with tasks_lock:
            tasks[task['id']] = task
        added.append(task['id'])
        return task['id']

    yield _put

    with tasks_lock:
        for tid in added:
            tasks.pop(tid, None)


@pytest.fixture()
def seeded_conv():
    """Create a real conversation row with one prior exchange; auto-cleanup.

    ``build_api_messages_from_db`` (called on the immediate-start path) needs
    real messages, so we seed a user+assistant turn.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    conv_id = _cid()
    db = get_thread_db(DOMAIN_CHAT)
    now = int(time.time() * 1000)
    messages = [
        {'role': 'user', 'content': 'first question', 'timestamp': now - 2000},
        {'role': 'assistant', 'content': 'first answer', 'timestamp': now - 1000,
         'finishReason': 'stop'},
    ]
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'supersede-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now, 'updated_at': now, 'search_text': '',
        'settings': json_dumps_pg({'model': 'test-model'}),
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'search_text', 'settings'],
       retry=True)
    db.commit()

    yield conv_id

    # Cleanup: conv row + any queued rows + marker.
    try:
        from lib.database import db_execute_with_retry
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1',
                              (conv_id,))
        db.commit()
    except Exception:
        pass
    try:
        clear_queue(conv_id)
    except Exception:
        pass
    try:
        clear_autopilot_marker(conv_id)
    except Exception:
        pass


@pytest.fixture()
def stub_spawn():
    """Stub ``lib.tasks_pkg.spawn_task`` so the immediate-start path doesn't
    run a real orchestrator/LLM. Marks the task done synchronously."""
    import lib.tasks_pkg as pkg
    orig = pkg.spawn_task

    def _fake_spawn(task):
        task['status'] = 'done'
        task['finishReason'] = 'stop'
        task['content'] = task.get('content') or 'stub reply'

    pkg.spawn_task = _fake_spawn
    yield
    pkg.spawn_task = orig


def _running_autopilot_task(tid, conv_id):
    """An autopilot follow-up worker turn (the shape that blocked the user)."""
    return {
        'id': tid, 'convId': conv_id, 'status': 'running', 'aborted': False,
        'config': {'model': 'test-model', 'autopilot': True},
        '_autopilotParent': 'parent-' + tid,
        'created_at': time.time(),
    }


def _running_worker_task(tid, conv_id):
    """A NORMAL (non-autopilot) worker turn."""
    return {
        'id': tid, 'convId': conv_id, 'status': 'running', 'aborted': False,
        'config': {'model': 'test-model'},  # no autopilot markers
        'created_at': time.time(),
    }


def _running_armed_primary_task(tid, conv_id):
    """The PRIMARY, VISIBLE worker turn the user is watching, after they armed
    autopilot mid-reply.

    ``arm_autopilot`` flips ``config['autopilot']=True`` on this live task so
    its end-of-turn hook fires — but it carries NONE of the background markers
    (``_autopilotParent`` / ``_vu_subtask`` / ``_autopilot_kick``) that a
    VU-spawned follow-up carries. A human send while THIS is streaming must
    QUEUE behind it, not abort the reply the user is watching.
    """
    return {
        'id': tid, 'convId': conv_id, 'status': 'running', 'aborted': False,
        'config': {'model': 'test-model', 'autopilot': True},  # armed, but PRIMARY
        'created_at': time.time(),
    }


@pytest.mark.api
def test_send_supersedes_inflight_autopilot(flask_client, put_task, seeded_conv,
                                            stub_spawn):
    """★ THE FIX: send with only an autopilot turn running → immediate start."""
    conv_id = seeded_conv
    clear_autopilot_marker(conv_id)
    arm_autopilot_marker(conv_id, {'model': 'test-model'})
    assert has_autopilot_marker(conv_id) is True

    ap_tid = 'ap-follow-1'
    put_task(_running_autopilot_task(ap_tid, conv_id))

    resp = flask_client.post('/api/v1/chat/send', json={
        'convId': conv_id,
        'message': {'text': 'my real follow-up question'},
        'config': {'model': 'test-model'},
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()

    # Started immediately — NOT queued.
    assert body.get('queued') is not True, f'expected immediate start, got {body}'
    assert body.get('taskId'), f'no taskId in response: {body}'

    # The in-flight autopilot turn was really aborted (backend stop, not a hide).
    with tasks_lock:
        assert tasks[ap_tid]['aborted'] is True
        assert tasks[ap_tid]['_abort_reason'] == 'superseded_by_user_send'

    # Autopilot was disarmed and nothing is left queued.
    assert has_autopilot_marker(conv_id) is False
    assert get_queue_depth(conv_id) == 0


@pytest.mark.api
def test_send_queues_behind_normal_worker(flask_client, put_task, seeded_conv,
                                          stub_spawn):
    """★ NEGATIVE CONTROL: a normal worker turn running → send still queues."""
    conv_id = seeded_conv
    clear_queue(conv_id)

    worker_tid = 'worker-1'
    put_task(_running_worker_task(worker_tid, conv_id))

    resp = flask_client.post('/api/v1/chat/send', json={
        'convId': conv_id,
        'message': {'text': 'a message while a real turn runs'},
        'config': {'model': 'test-model'},
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()

    # Queued behind the real in-progress reply — correct behaviour.
    assert body.get('queued') is True, f'expected queued, got {body}'
    assert body.get('queueId'), f'no queueId: {body}'

    # The normal worker was NOT aborted by the send gate.
    with tasks_lock:
        assert tasks[worker_tid].get('aborted') is not True

    # The message is in the queue.
    assert get_queue_depth(conv_id) == 1


@pytest.mark.api
def test_armed_primary_turn_queues_not_superseded(flask_client, put_task,
                                                  seeded_conv, stub_spawn):
    """★ REGRESSION GUARD (the reported bug): a human send while the PRIMARY,
    VISIBLE turn is streaming — with autopilot armed on it — must QUEUE, not
    abort the reply the user is watching.

    The armed primary turn has ``config.autopilot=True`` but NO background
    marker. Keying the supersede on the marker (not the bare flag) means it is
    treated as a real in-progress reply, so the send queues behind it.
    """
    conv_id = seeded_conv
    clear_queue(conv_id)
    arm_autopilot_marker(conv_id, {'model': 'test-model'})

    primary_tid = 'armed-primary-1'
    put_task(_running_armed_primary_task(primary_tid, conv_id))

    resp = flask_client.post('/api/v1/chat/send', json={
        'convId': conv_id,
        'message': {'text': 'typed while my reply was still generating'},
        'config': {'model': 'test-model'},
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()

    # Queued behind the visible reply — NOT superseded/aborted.
    assert body.get('queued') is True, f'expected queued, got {body}'
    assert body.get('queueId'), f'no queueId: {body}'
    with tasks_lock:
        assert tasks[primary_tid].get('aborted') is not True
        assert tasks[primary_tid].get('_abort_reason') != 'superseded_by_user_send'
    assert get_queue_depth(conv_id) == 1


@pytest.mark.api
def test_normal_worker_wins_over_coexisting_autopilot(flask_client, put_task,
                                                      seeded_conv, stub_spawn):
    """Guard: a normal worker + an autopilot task both running → still enqueue,
    and the send gate does NOT force-abort the autopilot task (worker wins)."""
    conv_id = seeded_conv
    clear_queue(conv_id)

    worker_tid = 'worker-coexist'
    ap_tid = 'ap-coexist'
    put_task(_running_worker_task(worker_tid, conv_id))
    put_task(_running_autopilot_task(ap_tid, conv_id))

    resp = flask_client.post('/api/v1/chat/send', json={
        'convId': conv_id,
        'message': {'text': 'coexist send'},
        'config': {'model': 'test-model'},
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()

    assert body.get('queued') is True, f'expected queued, got {body}'
    # Worker precedence: the send gate must not supersede here, so the
    # autopilot task is left untouched by THIS path.
    with tasks_lock:
        assert tasks[worker_tid].get('aborted') is not True
        assert tasks[ap_tid].get('_abort_reason') != 'superseded_by_user_send'
    assert get_queue_depth(conv_id) == 1
