from __future__ import annotations

import json

import pytest


pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]


@pytest.fixture()
def v2_api_db(tmp_path):
    from lib.database import _core as core

    snapshot = core.reset_sqlite_for_tests(str(tmp_path / 'turns-v2-api.db'))
    db = core._new_sqlite_connection()
    db.execute(
        'INSERT INTO conversations(id,user_id,title,messages,created_at,'
        'updated_at,settings,msg_count,search_text,rev,messages_rows_rev) '
        "VALUES ('conv-api-v2',1,'v2','[]',1,1,'{}',0,'',0,-1)")
    db.commit()
    db.close()
    try:
        yield
    finally:
        core.restore_db_state(snapshot)


def test_v2_ack_retry_stream_and_same_turn_regenerate(
        flask_client, v2_api_db, monkeypatch):
    from lib.turn_lifecycle import (
        build_api_messages,
        read_events,
        record_task_event,
    )
    from routes import chat as chat_routes

    task_ids = iter(('internal-task-1', 'internal-task-2'))
    starts = []

    def fake_start(conv_id, config, data):
        task_id = next(task_ids)
        starts.append((task_id, dict(config)))
        return task_id, None

    monkeypatch.setattr(chat_routes, '_start_task_for_conv', fake_start)
    body = {
        'commandId': 'lost-ack-command',
        'inputTurn': {'content': 'hello'},
        'config': {'model': 'gpt-4o'},
    }
    first_response = flask_client.post(
        '/api/v2/conversations/conv-api-v2/turns', json=body)
    assert first_response.status_code == 200
    first = first_response.get_json()
    assert first['ok'] is True
    assert first['turn']['status'] == 'running'
    assert first['submittedTurn']['actor'] == 'human'
    assert first['attempt']['operation'] == 'generate'
    assert first['streamCursor'] == 1
    assert '_needsStart' not in first
    attempt_id = first['attempt']['attemptId']
    turn_id = first['turn']['turnId']

    # POST ACK may be lost after durable events exist. Retrying the same
    # command returns the same identities and never starts another executor.
    durable = read_events(attempt_id)
    assert [event['type'] for event in durable] == [
        'status_changed', 'status_changed']
    retry = flask_client.post(
        '/api/v2/conversations/conv-api-v2/turns', json=body).get_json()
    assert retry['idempotentReplay'] is True
    assert retry['turn']['turnId'] == turn_id
    assert retry['attempt']['attemptId'] == attempt_id
    assert len(starts) == 1
    assert starts[0][1]['excludeLast'] is True
    context = build_api_messages(
        'conv-api-v2', turn_id, {'excludeLast': True})
    assert context[-1]['role'] == 'user'
    assert context[-1]['content'] == 'hello'

    task = {
        '_attemptId': attempt_id, '_turnProtocolV2': True,
        'id': 'internal-task-1', 'status': 'done', 'finishReason': 'stop',
        'content': 'answer', 'thinking': '', 'toolRounds': [],
        'model': 'gpt-4o', 'config': {'model': 'gpt-4o'},
    }
    assert record_task_event(task, {'type': 'done', 'finishReason': 'stop'})

    stream = flask_client.get(
        f'/api/v2/attempts/{attempt_id}/stream?after=1')
    assert stream.status_code == 200
    stream_text = stream.get_data(as_text=True)
    assert 'event: status_changed' in stream_text
    assert 'event: terminal_settlement' in stream_text
    envelopes = [json.loads(line[6:]) for line in stream_text.splitlines()
                 if line.startswith('data: ')]
    assert all(event['conversationId'] == 'conv-api-v2' for event in envelopes)
    assert all(event['turnId'] == turn_id for event in envelopes)
    assert all(event['attemptId'] == attempt_id for event in envelopes)

    settled = flask_client.get(
        '/api/v2/conversations/conv-api-v2/turns').get_json()
    latest = next(turn for turn in settled['turns'] if turn['turnId'] == turn_id)
    regenerated = flask_client.post(
        f'/api/v2/conversations/conv-api-v2/turns/{turn_id}/attempts',
        json={
            'commandId': 'regenerate-command', 'operation': 'regenerate',
            'expectedProjectionRevision': latest['projectionRevision'],
            'config': {'model': 'gpt-4o'},
        }).get_json()
    assert regenerated['turn']['turnId'] == turn_id
    assert regenerated['attempt']['attemptId'] != attempt_id
    assert regenerated['turn']['status'] == 'running'
    assert len(starts) == 2

    # A late event from the replaced executor is discarded at authority.
    task.update(status='running', content='stale overwrite')
    assert record_task_event(task, {'type': 'delta', 'content': 'stale'}) is False


def test_v2_attempt_conflict_returns_latest_authoritative_turn(
        flask_client, v2_api_db, monkeypatch):
    from routes import chat as chat_routes

    monkeypatch.setattr(
        chat_routes, '_start_task_for_conv',
        lambda *args, **kwargs: ('internal-conflict-task', None))
    created = flask_client.post(
        '/api/v2/conversations/conv-api-v2/turns', json={
            'commandId': 'create-conflict-turn',
            'inputTurn': {'content': 'hello'}, 'config': {'model': 'gpt-4o'},
        }).get_json()
    turn = created['turn']
    response = flask_client.post(
        f"/api/v2/conversations/conv-api-v2/turns/{turn['turnId']}/attempts",
        json={
            'commandId': 'stale-operation', 'operation': 'regenerate',
            'expectedProjectionRevision': turn['projectionRevision'] - 1,
        })
    assert response.status_code == 409
    payload = response.get_json()
    assert payload['error']['kind'] == 'stale_projection'
    assert payload['latestTurn']['turnId'] == turn['turnId']


def test_v2_start_failure_settles_attempt_and_returns_latest_turn(
        flask_client, v2_api_db, monkeypatch):
    from routes import chat as chat_routes

    starts = []

    def fail_start(*args, **kwargs):
        starts.append(1)
        return None, object()

    monkeypatch.setattr(chat_routes, '_start_task_for_conv', fail_start)
    body = {
        'commandId': 'start-failure-command',
        'inputTurn': {'content': 'hello'}, 'config': {'model': 'gpt-4o'},
    }
    response = flask_client.post(
        '/api/v2/conversations/conv-api-v2/turns', json=body)
    assert response.status_code == 500
    payload = response.get_json()
    assert payload['error']['kind'] == 'task_start_failed'
    assert payload['latestTurn']['status'] == 'failed'
    assert payload['latestTurn']['settlement']['cause'] == 'generation_error'

    # Failed commands are terminal and idempotent; a retry cannot issue a
    # second provider request.
    retry = flask_client.post(
        '/api/v2/conversations/conv-api-v2/turns', json=body).get_json()
    assert retry['idempotentReplay'] is True
    assert retry['turn']['turnId'] == payload['latestTurn']['turnId']
    assert len(starts) == 1


def test_v2_abort_targets_only_the_named_attempt_and_commits_terminal_event(
        flask_client, v2_api_db, monkeypatch):
    from lib.turn_lifecycle import read_events
    from routes import chat as chat_routes

    monkeypatch.setattr(
        chat_routes, '_start_task_for_conv',
        lambda *args, **kwargs: ('detached-abort-task', None))
    created = flask_client.post(
        '/api/v2/conversations/conv-api-v2/turns', json={
            'commandId': 'abort-only-this-attempt',
            'inputTurn': {'content': 'hello'}, 'config': {'model': 'gpt-4o'},
        }).get_json()
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']

    response = flask_client.post(
        f'/api/v2/attempts/{attempt_id}/abort')
    assert response.status_code == 200
    assert response.get_json()['attemptId'] == attempt_id

    snapshot = flask_client.get(
        '/api/v2/conversations/conv-api-v2/turns').get_json()
    turn = next(item for item in snapshot['turns']
                if item['turnId'] == turn_id)
    assert turn['status'] == 'interrupted'
    assert turn['settlement']['cause'] == 'user_abort'
    terminal = read_events(attempt_id)[-1]
    assert terminal['type'] == 'terminal_settlement'
    assert terminal['payload']['status'] == 'interrupted'


def test_v2_first_message_creates_conversation_and_turns_atomically(
        flask_client, v2_api_db, monkeypatch):
    from lib.database import DOMAIN_CHAT, pooled_write_transaction
    from routes import chat as chat_routes

    with pooled_write_transaction(DOMAIN_CHAT, label='remove api fixture conv') as db:
        db.execute("DELETE FROM conversations WHERE id='conv-api-v2'")
    monkeypatch.setattr(
        chat_routes, '_start_task_for_conv',
        lambda *args, **kwargs: ('first-v2-task', None))

    response = flask_client.post(
        '/api/v2/conversations/fresh-v2-conv/turns', json={
            'commandId': 'fresh-first-command',
            'inputTurn': {'content': 'first'},
            'config': {'model': 'gpt-4o'},
            'conversation': {
                'allowCreate': True, 'title': 'First',
                'settings': {'model': 'gpt-4o'}, 'createdAt': 123,
            },
        })
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['submittedTurn']['conversationId'] == 'fresh-v2-conv'
    assert payload['submittedTurn']['ordinal'] == 0
    assert payload['turn']['ordinal'] == 1
    snapshot = flask_client.get(
        '/api/v2/conversations/fresh-v2-conv/turns').get_json()
    assert [turn['turnId'] for turn in snapshot['turns']] == [
        payload['submittedTurn']['turnId'], payload['turn']['turnId']]


def test_v2_branch_lane_create_and_delete_use_parent_turn_identity(
        flask_client, v2_api_db, monkeypatch):
    from routes import chat as chat_routes

    monkeypatch.setattr(
        chat_routes, '_start_task_for_conv',
        lambda *args, **kwargs: ('branch-parent-task', None))
    created = flask_client.post(
        '/api/v2/conversations/conv-api-v2/turns', json={
            'commandId': 'branch-parent-command',
            'inputTurn': {'content': 'anchor'}, 'config': {},
        }).get_json()
    parent = created['submittedTurn']
    lane_response = flask_client.post(
        f"/api/v2/conversations/conv-api-v2/turns/{parent['turnId']}/lanes",
        json={
            'title': 'Side path', 'anchorText': 'anchor',
            'expectedProjectionRevision': parent['projectionRevision'],
        })
    assert lane_response.status_code == 200
    lane_payload = lane_response.get_json()
    lane_id = lane_payload['lane']['laneId']
    assert lane_payload['turn']['projection']['_branchLanes'][0]['laneId'] == lane_id

    deleted = flask_client.delete(
        f"/api/v2/conversations/conv-api-v2/turns/{parent['turnId']}/lanes/{lane_id}")
    assert deleted.status_code == 200
    assert deleted.get_json()['deletedLaneId'] == lane_id
    snapshot = flask_client.get(
        '/api/v2/conversations/conv-api-v2/turns').get_json()
    latest_parent = next(turn for turn in snapshot['turns']
                         if turn['turnId'] == parent['turnId'])
    assert latest_parent['projection']['_branchLanes'] == []


def test_v2_delete_uses_stable_turn_ids_after_attempt_settlement(
        flask_client, v2_api_db, monkeypatch):
    from lib.turn_lifecycle import record_task_event
    from routes import chat as chat_routes

    monkeypatch.setattr(
        chat_routes, '_start_task_for_conv',
        lambda *args, **kwargs: ('delete-v2-task', None))
    created = flask_client.post(
        '/api/v2/conversations/conv-api-v2/turns', json={
            'commandId': 'delete-v2-command',
            'inputTurn': {'content': 'remove this exchange'}, 'config': {},
        }).get_json()
    record_task_event({
        '_attemptId': created['attempt']['attemptId'],
        '_turnProtocolV2': True, 'id': 'delete-v2-task', 'status': 'done',
        'finishReason': 'stop', 'content': 'done', 'thinking': '',
        'toolRounds': [], 'config': {},
    }, {'type': 'done', 'finishReason': 'stop'})

    turn_ids = [created['submittedTurn']['turnId'], created['turn']['turnId']]
    response = flask_client.post(
        '/api/v2/conversations/conv-api-v2/turns/delete',
        json={'turnIds': turn_ids})
    assert response.status_code == 200
    assert set(response.get_json()['deletedTurnIds']) == set(turn_ids)
    snapshot = flask_client.get(
        '/api/v2/conversations/conv-api-v2/turns').get_json()
    assert snapshot['turns'] == []


def test_cutover_marker_retires_all_legacy_generation_entrypoints(
        flask_client, v2_api_db):
    from lib.database import DOMAIN_CHAT, pooled_write_transaction

    with pooled_write_transaction(DOMAIN_CHAT, label='activate turn test cutover') as db:
        db.execute(
            "INSERT INTO schema_meta(key,value) VALUES ('_turn_schema_version','2') "
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value')

    requests = [
        ('/api/v1/chat/start', {'convId': 'conv-api-v2'}),
        ('/api/v1/chat/send', {'convId': 'conv-api-v2'}),
        ('/api/v1/chat/continue', {'convId': 'conv-api-v2'}),
        ('/api/v1/chat/regenerate', {'convId': 'conv-api-v2'}),
        ('/api/v1/chat/branch', {'convId': 'conv-api-v2'}),
    ]
    for path, body in requests:
        response = flask_client.post(path, json=body)
        assert response.status_code == 410, path
        assert response.get_json()['error']['kind'] == 'legacy_turn_protocol_retired'


def test_v2_stream_holds_through_transient_storage_error(
        flask_client, v2_api_db, monkeypatch):
    """Regression pin for the 2026-08-19 SSE die-off: a wedged sidecar read
    surfaces as a classified RETRYABLE StorageError; the stream generator
    must back off and keep polling instead of propagating through the ASGI
    layer and killing the user's live view."""
    from lib.storage.errors import StorageError
    from routes import turns_v2 as turns_v2_routes

    calls = {'reads': 0}

    def flaky_read_events(attempt_id, *, after=0, user_id=1, limit=1000):
        calls['reads'] += 1
        if calls['reads'] == 2:
            raise StorageError(
                'database_timeout', 'Storage request timed out', True, 50)
        if calls['reads'] == 3:
            return [{'seq': 1, 'type': 'status_changed'}]
        return []

    monkeypatch.setattr(turns_v2_routes, 'read_events', flaky_read_events)
    monkeypatch.setattr(
        turns_v2_routes, 'attempt_is_terminal',
        lambda attempt_id, *, user_id=1: True)

    stream = flask_client.get('/api/v2/attempts/attempt-stall/stream?after=0')
    assert stream.status_code == 200
    assert 'event: status_changed' in stream.get_data(as_text=True)
    # probe + stalled poll + recovered poll + terminal poll
    assert calls['reads'] == 4
