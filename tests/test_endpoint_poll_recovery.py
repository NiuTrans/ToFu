"""tests/test_endpoint_poll_recovery.py — Endpoint multi-turn survives the
poll-fallback terminal path even after the in-memory task is evicted.

Regression coverage for the endpoint state-desync fix.  Endpoint-mode
(Planner→Worker→Critic) results are persisted into the CONVERSATION
``messages`` (by ``_sync_endpoint_turns_to_conversation``), NOT into the
single ``task_results`` content blob.  ``/api/chat/poll`` used to surface
``endpointMode``/``endpointTurns`` ONLY from the in-memory ``tasks`` dict;
its DB branch (task evicted past TTL / server restart) returned the row's
single content blob with no endpoint fields.  So when SSE timed out
mid-endpoint-run and the poll fallback outlived the in-memory task, the
frontend ``_pollFallback`` couldn't rebuild the multi-turn structure and
overwrote a single bubble with the last in-progress turn — a display-state
desync that only a manual refresh repaired.

The fix:
  • ``build_result_meta`` persists ``endpointMode`` + ``endpointStopReason``
    into ``task_results.metadata``.
  • ``load_endpoint_turns_from_conversation(conv_id)`` reconstructs the
    trailing endpoint-turn slice from the durable conversation messages.
  • The poll DB branch echoes ``endpointMode`` + reconstructed
    ``endpointTurns`` + ``endpointStopReason`` (covers done AND interrupted).
  • The in-memory poll branch also surfaces ``endpointPhase`` /
    ``endpointIteration`` / ``endpointStopReason`` so BOTH transports hand
    the frontend the same baton as the SSE state snapshot.

These tests inject synthetic state straight into the DB + the in-memory
registry and assert the poll route's JSON, with no live LLM / orchestrator.
"""

import time

import pytest


def _seed_conv(conv_id, messages):
    """Write a conversation row with the given messages."""
    from lib.database import (DOMAIN_CHAT, get_thread_db, db_execute_with_retry,
                              json_dumps_pg)
    from lib.database._core_schema import CONVERSATIONS, upsert
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'ep-poll-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _seed_task_result(task_id, conv_id, *, status='done', content='', metadata=None):
    """Write a task_results row (the DB-poll source after eviction)."""
    import json
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.database._core_schema import TASK_RESULTS, upsert
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, TASK_RESULTS, {
        'task_id': task_id, 'conv_id': conv_id, 'content': content,
        'thinking': '', 'error': None, 'status': status, 'tool_rounds': None,
        'metadata': json.dumps(metadata or {}, ensure_ascii=False),
        'created_at': now_ms, 'completed_at': now_ms,
    }, insert_cols=['task_id', 'conv_id', 'content', 'thinking', 'error',
                    'status', 'tool_rounds', 'metadata', 'created_at',
                    'completed_at'], retry=True)
    db.commit()


def _cleanup(conv_id):
    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry
    db = get_thread_db(DOMAIN_CHAT)
    db_execute_with_retry(db, 'DELETE FROM task_results WHERE conv_id=?', (conv_id,))
    db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db.commit()


def _ep_conv_messages():
    """A conversation with an original user msg + a full endpoint run."""
    return [
        {'role': 'user', 'content': 'Build X', 'timestamp': 1},
        {'role': 'assistant', 'content': 'PLAN', '_isEndpointPlanner': True,
         '_epPlannerIteration': 1, 'timestamp': 2},
        {'role': 'assistant', 'content': 'worker turn 1', '_epIteration': 1,
         'timestamp': 3},
        {'role': 'user', 'content': 'Looks good. [VERDICT: STOP]',
         '_isEndpointReview': True, '_epIteration': 1, '_epApproved': True,
         '_epNextPhase': 'stop', 'done': True, 'timestamp': 4},
    ]


# ── load_endpoint_turns_from_conversation ──────────────────────────────

def test_load_endpoint_turns_slices_trailing_turns():
    from lib.tasks_pkg.manager import load_endpoint_turns_from_conversation
    conv_id = 'cv-ep-slice'
    _seed_conv(conv_id, _ep_conv_messages())
    try:
        turns = load_endpoint_turns_from_conversation(conv_id)
        # Should return exactly the 3 endpoint turns (planner + worker + critic),
        # NOT the original user message.
        assert len(turns) == 3, f'expected 3 endpoint turns, got {len(turns)}'
        assert turns[0].get('_isEndpointPlanner') is True
        assert turns[1].get('_epIteration') == 1 and turns[1]['role'] == 'assistant'
        assert turns[2].get('_isEndpointReview') is True
        assert turns[2].get('_epApproved') is True
        # The original user message must NOT leak into the turns slice.
        assert all(m.get('content') != 'Build X' for m in turns)
    finally:
        _cleanup(conv_id)


def test_load_endpoint_turns_empty_when_no_endpoint_msgs():
    from lib.tasks_pkg.manager import load_endpoint_turns_from_conversation
    conv_id = 'cv-ep-none'
    _seed_conv(conv_id, [
        {'role': 'user', 'content': 'hi', 'timestamp': 1},
        {'role': 'assistant', 'content': 'plain reply', 'timestamp': 2},
    ])
    try:
        assert load_endpoint_turns_from_conversation(conv_id) == []
    finally:
        _cleanup(conv_id)


def test_load_endpoint_turns_missing_conv():
    from lib.tasks_pkg.manager import load_endpoint_turns_from_conversation
    assert load_endpoint_turns_from_conversation('does-not-exist-zzz') == []
    assert load_endpoint_turns_from_conversation('') == []


# ── build_result_meta persists the endpoint terminal signal ────────────

def test_build_result_meta_records_endpoint_signal():
    from lib.tasks_pkg.manager import build_result_meta
    task = {
        'id': 'ep-meta-1', 'finishReason': 'stop',
        'endpoint_mode': True, '_endpoint_stop_reason': 'approved',
    }
    meta = build_result_meta(task)
    assert meta.get('endpointMode') is True
    assert meta.get('endpointPhase') == 'planning'
    assert meta.get('endpointIteration') == 0
    assert meta.get('endpointStopReason') == 'approved'


def test_build_result_meta_records_flow_current_turn_identity():
    from lib.tasks_pkg.manager import build_result_meta
    task = {
        'id': 'flow-meta-1', 'endpoint_mode': True, 'flow_mode': True,
        '_flow_projection': 'autopilot', '_endpoint_phase': 'reviewing',
        '_endpoint_iteration': 3,
        '_flow_current_turn': {
            'flowProjection': 'autopilot',
            'turnRole': 'virtual_user', 'emits': 'user',
            'vuMsgId': 'vu-meta-1', 'autopilotRunId': 'run-meta-1',
        },
    }
    meta = build_result_meta(task)
    assert meta['flowMode'] is True
    assert meta['flowProjection'] == 'autopilot'
    assert meta['endpointPhase'] == 'reviewing'
    assert meta['endpointIteration'] == 3
    assert meta['turnRole'] == 'virtual_user'
    assert meta['emits'] == 'user'
    assert meta['vuMsgId'] == 'vu-meta-1'
    assert meta['autopilotRunId'] == 'run-meta-1'


def test_build_result_meta_no_endpoint_for_plain_task():
    from lib.tasks_pkg.manager import build_result_meta
    meta = build_result_meta({'id': 'plain-1', 'finishReason': 'stop'})
    assert 'endpointMode' not in meta
    assert 'endpointStopReason' not in meta


# ── In-memory poll branch: symmetry with the SSE state snapshot ────────

@pytest.fixture()
def put_task():
    from lib.tasks_pkg import tasks, tasks_lock
    added = []

    def _put(task):
        # Mirror manager._registry's production task shape. Poll snapshots
        # content/thinking under this lock so a synthetic registry entry must
        # carry it as well.
        import threading
        task.setdefault('content_lock', threading.Lock())
        with tasks_lock:
            tasks[task['id']] = task
        added.append(task['id'])
        return task['id']

    yield _put

    with tasks_lock:
        for tid in added:
            tasks.pop(tid, None)


def _poll(client, task_id):
    resp = client.get(f'/api/v1/chat/poll/{task_id}')
    return resp.status_code, resp.get_json()


@pytest.mark.api
def test_inmemory_poll_carries_endpoint_terminal_signal(flask_client, put_task):
    """A finished endpoint task still in memory surfaces the same baton the
    SSE state snapshot does: endpointMode + phase + iteration + stopReason."""
    turns = _ep_conv_messages()[1:]  # the 3 endpoint turns
    put_task({
        'id': 'ep-inmem-1', 'status': 'done',
        'content': 'worker turn 1', 'thinking': '', 'finishReason': 'stop',
        'endpoint_mode': True,
        '_endpoint_turns': turns,
        '_endpoint_phase': 'done',
        '_endpoint_iteration': 1,
        '_endpoint_stop_reason': 'approved',
    })
    status, body = _poll(flask_client, 'ep-inmem-1')
    assert status == 200, body
    assert body['endpointMode'] is True
    assert body['endpointTurns'] and len(body['endpointTurns']) == 3
    assert body['endpointPhase'] == 'done'
    assert body['endpointStopReason'] == 'approved'
    assert body.get('endpointIteration') == 1


@pytest.mark.api
def test_inmemory_poll_carries_flow_vu_identity(flask_client, put_task):
    put_task({
        'id': 'flow-vu-inmem-1', 'status': 'running',
        'content': 'Keep going', 'thinking': '',
        'endpoint_mode': True, 'flow_mode': True,
        '_flow_projection': 'autopilot',
        '_endpoint_phase': 'reviewing', '_endpoint_iteration': 2,
        '_flow_current_turn': {
            'flowProjection': 'autopilot',
            'turnRole': 'virtual_user', 'emits': 'user',
            'vuMsgId': 'vu-inmem-1', 'autopilotRunId': 'run-inmem-1',
        },
    })
    status, body = _poll(flask_client, 'flow-vu-inmem-1')
    assert status == 200, body
    assert body['flowMode'] is True
    assert body['flowProjection'] == 'autopilot'
    assert body['endpointPhase'] == 'reviewing'
    assert body['turnRole'] == 'virtual_user'
    assert body['vuMsgId'] == 'vu-inmem-1'
    assert body['autopilotRunId'] == 'run-inmem-1'


# ── DB poll branch: reconstruct turns after the task is evicted ────────

@pytest.mark.api
def test_db_poll_reconstructs_endpoint_turns_after_eviction(flask_client):
    """Task NOT in memory (evicted/restart) → poll DB branch rebuilds
    endpointTurns from the conversation + carries the terminal signal."""
    conv_id = 'cv-ep-db-done'
    task_id = 'ep-db-done-1'
    _seed_conv(conv_id, _ep_conv_messages())
    _seed_task_result(task_id, conv_id, status='done',
                      content='worker turn 1',
                      metadata={'finishReason': 'stop', 'model': 'm',
                                'endpointMode': True,
                                'endpointStopReason': 'approved'})
    try:
        status, body = _poll(flask_client, task_id)
        assert status == 200, body
        assert body['status'] == 'done'
        assert body['endpointMode'] is True
        assert body['endpointStopReason'] == 'approved'
        # The multi-turn structure was reconstructed from the conversation.
        assert body.get('endpointTurns'), 'endpointTurns missing on DB branch'
        assert len(body['endpointTurns']) == 3
        assert body['endpointTurns'][-1].get('_isEndpointReview') is True
        assert body['endpointTurns'][-1].get('_epApproved') is True
    finally:
        _cleanup(conv_id)


@pytest.mark.api
def test_db_poll_reconstructs_endpoint_turns_when_interrupted(flask_client):
    """The interrupted/server-crash DB sub-branch must ALSO carry the
    reconstructed endpoint turns — that's exactly when the rebuild matters
    most.  A DB row with status='running' but no in-memory task is reported
    as 'interrupted'."""
    conv_id = 'cv-ep-db-intr'
    task_id = 'ep-db-intr-1'
    # Mid-run conversation: planner + one worker turn, no critic yet.
    _seed_conv(conv_id, [
        {'role': 'user', 'content': 'Build X', 'timestamp': 1},
        {'role': 'assistant', 'content': 'PLAN', '_isEndpointPlanner': True,
         '_epPlannerIteration': 1, 'timestamp': 2},
        {'role': 'assistant', 'content': 'worker turn 1', '_epIteration': 1,
         'timestamp': 3},
    ])
    # status='running' in DB + not in memory → route reports 'interrupted'.
    _seed_task_result(task_id, conv_id, status='running',
                      content='worker turn 1',
                      metadata={'endpointMode': True})
    try:
        status, body = _poll(flask_client, task_id)
        assert status == 200, body
        assert body['status'] == 'interrupted', body['status']
        assert body['endpointMode'] is True
        assert body.get('endpointTurns'), 'endpointTurns missing on interrupted branch'
        assert len(body['endpointTurns']) == 2  # planner + worker, no critic
    finally:
        _cleanup(conv_id)


@pytest.mark.api
def test_db_poll_plain_task_unaffected(flask_client):
    """A plain (non-endpoint) DB-backed task must NOT grow endpoint fields."""
    conv_id = 'cv-plain-db'
    task_id = 'plain-db-1'
    _seed_conv(conv_id, [
        {'role': 'user', 'content': 'hi', 'timestamp': 1},
        {'role': 'assistant', 'content': 'reply', 'timestamp': 2},
    ])
    _seed_task_result(task_id, conv_id, status='done', content='reply',
                      metadata={'finishReason': 'stop', 'model': 'm'})
    try:
        status, body = _poll(flask_client, task_id)
        assert status == 200, body
        assert 'endpointMode' not in body
        assert 'endpointTurns' not in body
        assert 'endpointStopReason' not in body
    finally:
        _cleanup(conv_id)
