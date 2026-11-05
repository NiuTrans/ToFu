"""Conversation Sync HTTP error taxonomy: who may answer 400.

Only a typed client fault answers 400: ``BadRequest`` raised by code that
knows the value came from the caller, or a request-schema violation rejected
before the command runs. Any other exception that escapes a command or read
is a server fault: a logged 500 whose body names the stable category and the
exception type, never the raw exception text.

Regression (2026-10-31): a mixed-version process raised
``TypeError: TaskRuntime.abort_owned() got an unexpected keyword argument
'reason'`` on every Stop. ``_command_response`` mapped the builtin to a 400
carrying that text, so the browser was told its request was wrong and the
server log never recorded the failure.
"""

from __future__ import annotations

import logging

import pytest

pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]
pytest_plugins = ('tests._chat_sidecar',)

_INCIDENT = "TaskRuntime.abort_owned() got an unexpected keyword argument 'reason'"

# Every route that executes a conversation command through ``_command_response``.
_COMMAND_ROUTES = [
    ('post', '/api/v4/conversations/conv-a/turns', 'create_turn'),
    ('post', '/api/v4/conversations/conv-a/turns/settled', 'append_settled_turn'),
    ('patch', '/api/v4/conversations/conv-a/turns/turn-a', 'update_turn'),
    ('post', '/api/v4/conversations/conv-a/turns/turn-a/perception', 'record_perception'),
    ('post', '/api/v4/conversations/conv-a/turns/turn-a/plan/execute', 'execute_plan'),
    ('get', '/api/v4/conversations/conv-a/turns/turn-a/usage-recovery', 'usage_recovery'),
    ('post', '/api/v4/conversations/conv-a/turns/turn-a/usage-recovery', 'recover_usage'),
    ('post', '/api/v4/conversations/conv-a/turns/turn-a/file-changes/undo',
     'mutate_turn_file_changes'),
    ('post', '/api/v4/conversations/conv-a/turns/turn-a/file-changes/redo',
     'mutate_turn_file_changes'),
    ('post', '/api/v4/conversations/conv-a/turns/turn-a/attempts', 'create_attempt'),
    ('post', '/api/v4/conversations/conv-a/turns/turn-a/lanes', 'create_lane'),
    ('delete', '/api/v4/conversations/conv-a/turns/turn-a/lanes/lane-a', 'delete_lane'),
    ('post', '/api/v4/conversations/conv-a/turns/delete', 'delete_turns'),
    ('post', '/api/v4/attempts/attempt-a/abort', 'abort_attempt'),
]


def _route_ids():
    return [f'{method.upper()} {path}' for method, path, _ in _COMMAND_ROUTES]


def _raising(error):
    def command(*args, **kwargs):
        raise error
    return command


def _call(flask_client, method, path):
    send = getattr(flask_client, method)
    if method in {'get', 'delete'}:
        return send(path)
    return send(path, json={})


def _arm(monkeypatch, attribute, error):
    from routes import conversation_sync_v3 as routes

    monkeypatch.setattr(routes, '_validate_body', lambda _schema: None)
    monkeypatch.setattr(routes.conversation_turn_commands, attribute, _raising(error))


@pytest.mark.parametrize(('method', 'path', 'attribute'), _COMMAND_ROUTES, ids=_route_ids())
@pytest.mark.parametrize('error', [
    TypeError(_INCIDENT),
    ValueError('internal invariant: accepted attempt config is not an object'),
], ids=['TypeError', 'ValueError'])
def test_untyped_command_failure_is_a_logged_redacted_500(
    flask_client, monkeypatch, caplog, method, path, attribute, error,
):
    _arm(monkeypatch, attribute, error)
    with caplog.at_level(logging.ERROR):
        response = _call(flask_client, method, path)

    assert response.status_code == 500
    text = response.get_data(as_text=True)
    assert str(error) not in text
    assert type(error).__name__ in text
    assert str(error) in caplog.text
    assert 'Traceback' in caplog.text


def test_queue_cancel_failure_is_a_logged_redacted_500(flask_client, monkeypatch, caplog):
    from routes import conversation_sync_v3 as routes

    monkeypatch.setattr(routes, 'cancel_queued_turn_pair', _raising(TypeError(_INCIDENT)))
    with caplog.at_level(logging.ERROR):
        response = flask_client.delete(_QUEUE)

    assert response.status_code == 500
    assert _INCIDENT not in response.get_data(as_text=True)
    assert _INCIDENT in caplog.text


@pytest.mark.parametrize(('method', 'path', 'attribute'), _COMMAND_ROUTES, ids=_route_ids())
def test_typed_client_fault_keeps_its_400_message_and_field(
    flask_client, monkeypatch, method, path, attribute,
):
    from lib.error_envelope import BadRequest

    _arm(monkeypatch, attribute, BadRequest(
        'humanResponse is only valid for answer_guidance', field='humanResponse'))
    response = _call(flask_client, method, path)

    assert response.status_code == 400
    body = response.get_json()
    assert body['error'] == 'humanResponse is only valid for answer_guidance'
    assert body['field'] == 'humanResponse'


def test_request_parser_and_domain_share_one_client_fault_type():
    from lib.error_envelope import BadRequest
    from lib.request_parser import BadRequest as parser_bad_request

    assert parser_bad_request is BadRequest
    assert issubclass(BadRequest, ValueError)


def test_durable_contract_drift_inside_a_command_is_a_typed_500(flask_client, monkeypatch):
    from lib.conversation_sync.validation import ContractViolation

    _arm(monkeypatch, 'update_turn', ContractViolation(
        'TurnRecord', ['projection.segments[0].kind: required']))
    response = _call(flask_client, 'patch', '/api/v4/conversations/conv-a/turns/turn-a')

    assert response.status_code == 500
    body = response.get_json()
    assert body['error']['contractSchema'] == 'TurnRecord'
    assert body['error']['contractViolations'] == ['projection.segments[0].kind: required']


@pytest.mark.parametrize(('error', 'status'), [
    ('conflict', 409),
    ('not_found', 404),
    ('storage_timeout', 503),
    ('attempt_start', 500),
    ('runtime', 500),
])
def test_typed_domain_outcomes_keep_their_status(flask_client, monkeypatch, error, status):
    from lib.conversation_sync.command_service import AttemptStartFailure
    from lib.storage.errors import StorageError
    from lib.turn_lifecycle import LifecycleConflict, LifecycleNotFound

    errors = {
        'conflict': LifecycleConflict('stale_projection', 'Turn changed', None),
        'not_found': LifecycleNotFound('Attempt not found'),
        'storage_timeout': StorageError(
            'database_timeout', 'writer timed out', True, 25, 'storage-op'),
        'attempt_start': AttemptStartFailure({'turnId': 'turn-a'}),
        'runtime': RuntimeError('file changes command gateway is unavailable'),
    }
    _arm(monkeypatch, 'abort_attempt', errors[error])
    response = _call(flask_client, 'post', '/api/v4/attempts/attempt-a/abort')

    assert response.status_code == status
    if error == 'runtime':
        assert 'gateway is unavailable' not in response.get_data(as_text=True)


_CONV_A = '/api/v4/conversations/conv-a'
_ROUNDS = f'{_CONV_A}/turns/rounds'
_HISTORY = f'{_CONV_A}/turns/history'
_QUEUE = f'{_CONV_A}/queue/queue-a'
_PAGE_STREAM = '/api/v4/conversation-events'

_OPERATION_ID = 'f' * 32

# Every read route, the service method that produces its body, the query its
# request decoding requires, and whether it is fenced by the owner scope.
_READ_ROUTES = [
    (f'{_CONV_A}/sync', 'snapshot', {}, False),
    (_ROUNDS, 'round_directory', {'laneId': 'main', 'syncSeq': '1'}, False),
    (_HISTORY, 'turn_page', {'laneId': 'main', 'syncSeq': '1'}, False),
    (f'{_CONV_A}/turns/turn-a', 'turn_read', {}, False),
    (f'{_CONV_A}/turns/turn-a/tool-effects/{_OPERATION_ID}', 'inspect_tool_effect',
     {}, True),
    (f'{_CONV_A}/turns/turn-a/tool-results/0', 'turn_tool_result',
     {'projectionRevision': '1'}, True),
    (f'{_CONV_A}/turns/turn-a/images/0', 'turn_image', {'projectionRevision': '1'}, True),
    (f'{_CONV_A}/events', 'changes', {}, False),
]


@pytest.mark.parametrize(('query', 'message'), [
    ('laneId=main&syncSeq=abc', 'Invalid syncSeq'),
    ('laneId=main&syncSeq=1&offset=x', 'Invalid offset'),
    ('laneId=main&syncSeq=1&limit=1.5', 'Invalid limit'),
    ('laneId=main', 'syncSeq is required'),
])
def test_round_directory_rejects_malformed_query_with_an_authored_400(
    flask_client, query, message,
):
    response = flask_client.get(f'{_ROUNDS}?{query}')

    assert response.status_code == 400
    body = response.get_json()
    assert body['error'] == message
    assert 'invalid literal' not in response.get_data(as_text=True)


@pytest.mark.parametrize(('path', 'service_method', 'query', 'owner_fenced'), _READ_ROUTES,
                         ids=[route[1] for route in _READ_ROUTES])
def test_read_service_fault_is_a_server_fault(
    flask_client, monkeypatch, caplog, path, service_method, query, owner_fenced,
):
    from lib.conversation_sync.turn_images import turn_image_owner_scope
    from routes import conversation_sync_v3 as routes

    if owner_fenced:
        query = {**query, 'ownerScope': turn_image_owner_scope(1, 'conv-a')}
    leaked = 'internal invariant: lane index is not monotonic'
    monkeypatch.setattr(routes._service, service_method, _raising(ValueError(leaked)))
    with caplog.at_level(logging.ERROR):
        response = flask_client.get(path, query_string=query)

    assert response.status_code == 500
    assert leaked not in response.get_data(as_text=True)
    assert leaked in caplog.text


def test_page_stream_service_fault_closes_its_stream_as_a_server_fault(
    flask_client, monkeypatch, caplog,
):
    """The page stream answers inside its response: a builtin failure closes
    that conversation's stream as a logged, redacted 500."""
    import json

    from routes import conversation_sync_v3 as routes
    from tests.conversation.test_conversation_sync_multiplex import _frames

    leaked = 'internal invariant: lane index is not monotonic'
    monkeypatch.setattr(routes._service, 'changes', _raising(ValueError(leaked)))
    with caplog.at_level(logging.ERROR):
        response = flask_client.get(_PAGE_STREAM, query_string={
            'streamClientId': 'page-a',
            'streams': json.dumps([{'conversationId': 'conv-a', 'streamGeneration': 1}]),
        })

    assert response.status_code == 200
    assert leaked not in response.get_data(as_text=True)
    assert [frame['data'] for frame in _frames(response.data)] == [{
        'contract': 'tofu.conversation-sync.stream-closed/v1',
        'type': 'stream.closed', 'conversationId': 'conv-a',
        'status': 500, 'code': 'internal_error',
    }]
    assert leaked in caplog.text


def test_every_conversation_sync_route_has_a_taxonomy_spec(flask_app):
    """A builtin exception may be translated or logged, never answered as 4xx.

    The mounted route table decides coverage: a new route must join one of
    the behavioral tables above, which drive builtin failures through it.
    """
    values = {
        'conversation_id': 'conv-a', 'turn_id': 'turn-a', 'lane_id': 'lane-a',
        'attempt_id': 'attempt-a', 'queue_id': 'queue-a',
        'operation_id': _OPERATION_ID, 'round_index': 0, 'image_index': 0,
    }
    urls = flask_app.url_map.bind('localhost')
    mounted = {
        (method, urls.build(rule.endpoint, {name: values[name] for name in rule.arguments}))
        for rule in flask_app.url_map.iter_rules()
        if rule.endpoint.startswith('conversation_sync_v3.')
        for method in rule.methods - {'HEAD', 'OPTIONS'}
    }
    specified = {(method.upper(), path) for method, path, _ in _COMMAND_ROUTES}
    specified.update({('DELETE', _QUEUE), ('GET', _PAGE_STREAM)})
    specified.update(('GET', route[0]) for route in _READ_ROUTES)
    assert mounted == specified


_CONV = 'conv-error-taxonomy'
_CONV_PATH = f'/api/v4/conversations/{_CONV}'


@pytest.fixture()
def owned_conversation(chat_sidecar):
    from tests._seed import delete_conversation, seed_conversation

    delete_conversation(_CONV)
    seed_conversation(_CONV, title='Error taxonomy', messages=[])
    try:
        yield
    finally:
        delete_conversation(_CONV)


def _registered_start(task_id):
    def start(*args, **kwargs):
        kwargs['on_task_registered'](task_id)
        return task_id, None
    return start


def _settled_turn(flask_client, command_id, actor):
    response = flask_client.post(f'{_CONV_PATH}/turns/settled', json={
        'commandId': command_id, 'actor': actor, 'kind': 'fixture',
        'projection': {'content': f'{actor} fixture'},
    })
    assert response.status_code == 200
    return response.get_json()['turn']


def test_stop_reaching_a_programming_error_is_a_500_not_a_400(
    flask_client, owned_conversation, monkeypatch, caplog,
):
    """The real Stop chain from the 2026-10-31 incident, end to end."""
    import lib.conversation_sync.task_start as task_start
    import lib.tasks_pkg.manager.cancellation as cancellation

    monkeypatch.setattr(
        task_start, 'start_conversation_attempt_executor', _registered_start('taxonomy-task'))
    created = flask_client.post(f'{_CONV_PATH}/turns', json={
        'commandId': 'taxonomy-stop', 'inputTurn': {'content': 'hello'},
        'config': {'model': 'gpt-4o'},
    })
    assert created.status_code == 200
    attempt_id = created.get_json()['attempt']['attemptId']

    monkeypatch.setattr(cancellation, 'cancel_task', _raising(TypeError(_INCIDENT)))
    with caplog.at_level(logging.ERROR):
        response = flask_client.post(f'/api/v4/attempts/{attempt_id}/abort')

    assert response.status_code == 500
    text = response.get_data(as_text=True)
    assert _INCIDENT not in text
    assert 'TypeError' in text
    assert _INCIDENT in caplog.text


def _execute_plan_from_human_turn(flask_client):
    source = _settled_turn(flask_client, 'taxonomy-human-source', 'human')
    return flask_client.post(f"{_CONV_PATH}/turns/{source['turnId']}/plan/execute", json={
        'commandId': 'taxonomy-execute', 'planId': 'plan_000000000000000000000000',
        'expectedProjectionRevision': source['projectionRevision'],
        'contextMode': 'current', 'config': {'model': 'gpt-4o'},
    })


def _undo_without_file_changes(flask_client):
    source = _settled_turn(flask_client, 'taxonomy-assistant-source', 'assistant')
    return flask_client.post(
        f"{_CONV_PATH}/turns/{source['turnId']}/file-changes/undo",
        json={'commandId': 'taxonomy-undo',
              'expectedProjectionRevision': source['projectionRevision']})


def _delete_no_turns(flask_client):
    return flask_client.post(f'{_CONV_PATH}/turns/delete', json={'turnIds': []})


def _human_response_on_regenerate(flask_client):
    return flask_client.post(f'{_CONV_PATH}/turns/turn-x/attempts', json={
        'commandId': 'taxonomy-regenerate', 'operation': 'regenerate',
        'expectedProjectionRevision': 1, 'config': {'model': 'gpt-4o'},
        'humanResponse': 'yes',
    })


def _workspace_path_not_a_string(flask_client):
    return flask_client.post(f'{_CONV_PATH}/turns', json={
        'commandId': 'taxonomy-workspace', 'inputTurn': {'content': 'hello'},
        'config': {'model': 'gpt-4o', 'chatMode': 'chat', 'projectPath': 7},
    })


def _video_frame_bytes_not_an_integer(flask_client):
    return flask_client.post(f'{_CONV_PATH}/turns', json={
        'commandId': 'taxonomy-video', 'config': {'model': 'gpt-4o'},
        'message': {'text': 'see video', 'videos': [{
            'video_id': 'v1',
            'frames': [{'url': '/api/images/frame.jpg', 't': 0, 'bytes': 'many'}],
        }]},
    })


@pytest.mark.parametrize(('send', 'message', 'field'), [
    (_execute_plan_from_human_turn, 'plan source must be an assistant or planner turn', None),
    (_undo_without_file_changes, 'turn has no authoritative file changes block', None),
    (_delete_no_turns, 'turnIds required', 'turnIds'),
    (_human_response_on_regenerate, 'humanResponse is only valid for answer_guidance',
     'humanResponse'),
    (_workspace_path_not_a_string, 'projectPath must be a string', 'config.projectPath'),
    (_video_frame_bytes_not_an_integer, 'Video frame bytes must be an integer', 'message'),
], ids=lambda value: getattr(value, '__name__', None))
def test_real_client_faults_keep_their_400_and_message(
    flask_client, owned_conversation, monkeypatch, send, message, field,
):
    import lib.conversation_sync.task_start as task_start

    starts = []
    monkeypatch.setattr(
        task_start, 'start_conversation_attempt_executor',
        lambda *args, **kwargs: starts.append(True))
    response = send(flask_client)

    assert response.status_code == 400
    body = response.get_json()
    assert body['error'] == message
    assert body.get('field') == field
    assert starts == []
