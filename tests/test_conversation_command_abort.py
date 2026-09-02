"""Conversation Sync v3 cancellation during the pre-ACK startup window."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]

CONV = 'conv-command-abort'


def _registered_start(task_id):
    def start(*args, **kwargs):
        kwargs['on_task_registered'](task_id)
        return task_id, None

    return start


@pytest.fixture()
def conversation_command_authority(flask_client):
    from tests._seed import delete_conversation, seed_conversation

    delete_conversation(CONV, user_id=1)
    seed_conversation(CONV, user_id=1, title='command cancellation')
    try:
        yield
    finally:
        from lib.runtime_state_store import reset_for_test
        delete_conversation(CONV, user_id=1)
        reset_for_test()


def _plant_abort_marker(conv_id=CONV):
    from lib.conversation_sync.pending_abort import mark_pending_abort
    mark_pending_abort(conv_id, 1)


def test_abort_marker_drops_turn_before_create(flask_client, conversation_command_authority,
                                               monkeypatch):
    """A Stop landing mid-translate (marker newer than the request start)
    drops the turn: aborted ACK, no turn pair, no executor start."""
    import lib.chat as chat_lib
    import lib.conversation_sync.task_start as task_start_runtime
    from lib.turn_lifecycle import list_turns

    starts = []
    monkeypatch.setattr(
        task_start_runtime, 'start_conversation_attempt_executor',
        lambda *a, **kw: starts.append((a, kw)) or ('task-x', None))

    def fake_build(payload, config, *, user_id, conv_id=None):
        # The abort-conv request lands while the handler is still inside its
        # synchronous translate stretch — AFTER _request_started_at.
        _plant_abort_marker(conv_id)
        return {'role': 'user', 'content': payload.get('text', 'hi')}

    monkeypatch.setattr(chat_lib, 'build_user_msg_from_payload', fake_build)

    resp = flask_client.post(
        f'/api/v3/conversations/{CONV}/turns',
        json={'commandId': 'cmd-drop',
              'message': {'text': '翻译我'},
              'config': {'model': 'gpt-4o'}})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] is True
    assert body['aborted'] is True
    assert body['conversationId'] == CONV
    # Nothing committed, nothing started.
    assert list_turns(CONV, user_id=1)['turns'] == []
    assert starts == []


def test_no_marker_creates_turn_and_forwards_abort_after_ts(flask_client,
                                                            conversation_command_authority,
                                                            monkeypatch):
    """Positive control + wiring pin: a clean send creates the pair and the
    start adapter forwards the request-start ts for the post-registration
    re-check (NEUTER: drop the kwarg and this goes red)."""
    import lib.conversation_sync.task_start as task_start_runtime

    captured = {}

    def fake_start(conv_id, config, **kwargs):
        captured.update(kwargs)
        kwargs['on_task_registered']('task-clean')
        return 'task-clean', None

    monkeypatch.setattr(task_start_runtime, 'start_conversation_attempt_executor', fake_start)
    resp = flask_client.post(
        f'/api/v3/conversations/{CONV}/turns',
        json={'commandId': 'cmd-clean',
              'inputTurn': {'content': 'hello'},
              'config': {'model': 'gpt-4o'}})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get('aborted') is not True
    assert body['turn']['status'] == 'running'
    assert isinstance(captured.get('abort_after_ts'), float)


def test_stale_marker_never_drops_a_new_send(flask_client, conversation_command_authority,
                                             monkeypatch):
    """The marker is timestamped: an abort from BEFORE this request must not
    kill it (a user who stopped the previous task can still send)."""
    import lib.conversation_sync.task_start as task_start_runtime

    _plant_abort_marker()  # lands BEFORE the request's start ts
    monkeypatch.setattr(task_start_runtime, 'start_conversation_attempt_executor',
                        _registered_start('task-later'))
    resp = flask_client.post(
        f'/api/v3/conversations/{CONV}/turns',
        json={'commandId': 'cmd-after-stop',
              'inputTurn': {'content': 'fresh intent'},
              'config': {'model': 'gpt-4o'}})
    assert resp.status_code == 200
    assert resp.get_json().get('aborted') is not True


def test_start_attempt_forwards_abort_after_ts(monkeypatch):
    """The shared command service forwards the startup abort watermark."""
    import lib.conversation_sync.command_service as command_module
    from lib.conversation_sync.command_service import ConversationTurnCommandService

    captured = {'order': []}
    monkeypatch.setattr(
        command_module, 'claim_attempt_start',
        lambda attempt_id, *, user_id=1: True)
    monkeypatch.setattr(
        command_module, 'bind_task',
        lambda attempt_id, task_id, *, user_id=1: (
            captured['order'].append('durable-bind')
            or {'attemptId': attempt_id, 'taskId': task_id}
        ))

    def start_task(conv_id, config, data, abort_after_ts, on_registered):
        captured['abort_after_ts'] = abort_after_ts
        captured['order'].append('registered')
        on_registered('tid')
        captured['order'].append('worker-started')
        return 'tid', None

    service = ConversationTurnCommandService(
        build_user_message=lambda payload, config, conv_id, user_id: payload,
        was_aborted_after=lambda conv_id, timestamp: False,
        start_task=start_task,
    )

    result = {
        '_needsStart': True,
        'turn': {'turnId': 'turn-1', 'conversationId': 'conv-x',
                 'actor': 'assistant', 'kind': 'reply',
                 'projection': {}},
        'attempt': {'attemptId': 'att-1', 'operation': 'generate'},
    }
    assert service._start_accepted_attempt(
        result, 1, {}, {}, abort_after_ts=123.0,
    ) is None
    assert captured.get('abort_after_ts') == 123.0
    assert captured['order'] == [
        'registered', 'durable-bind', 'worker-started',
    ]


def test_start_attempt_rejects_post_spawn_compatibility_binding(monkeypatch):
    """An executor must bind through the pre-spawn hook; no fallback exists."""
    import lib.conversation_sync.command_service as command_module
    from lib.conversation_sync.command_service import (
        AttemptStartFailure,
        ConversationTurnCommandService,
    )

    bound = []
    failed = []
    monkeypatch.setattr(
        command_module, 'claim_attempt_start',
        lambda attempt_id, *, user_id: True,
    )
    monkeypatch.setattr(
        command_module, 'bind_task',
        lambda *args, **kwargs: bound.append((args, kwargs)),
    )
    monkeypatch.setattr(
        command_module, 'fail_start',
        lambda attempt_id, error, *, user_id: failed.append(
            (attempt_id, user_id)
        ),
    )
    monkeypatch.setattr(
        command_module,
        'get_turn',
        lambda *args, **kwargs: {'turnId': 'turn-1', 'status': 'failed'},
    )
    service = ConversationTurnCommandService(
        build_user_message=lambda payload, config, conv_id, user_id: payload,
        was_aborted_after=lambda conv_id, timestamp: False,
        # Returning a task id without invoking on_registered models the old
        # unsafe adapter that could start a worker before durable binding.
        start_task=lambda *args: ('orphan-task', None),
    )
    result = {
        '_needsStart': True,
        'turn': {
            'turnId': 'turn-1', 'conversationId': 'conv-x',
            'actor': 'assistant', 'kind': 'reply', 'projection': {},
        },
        'attempt': {'attemptId': 'att-1', 'operation': 'generate'},
    }

    with pytest.raises(AttemptStartFailure):
        service._start_accepted_attempt(
            result, 7, {}, {}, abort_after_ts=123.0,
        )
    assert bound == []
    assert failed == [('att-1', 7)]


def test_abort_attempt_publishes_conversation_wake(
    flask_client, conversation_command_authority, monkeypatch,
):
    import lib.conversation_sync.task_start as task_start_runtime

    monkeypatch.setattr(task_start_runtime, 'start_conversation_attempt_executor',
                        _registered_start('task-busy'))
    ack = flask_client.post(
        f'/api/v3/conversations/{CONV}/turns',
        json={'commandId': 'cmd-busy',
              'inputTurn': {'content': 'run'},
              'config': {'model': 'gpt-4o'}})
    assert ack.status_code == 200
    attempt_id = ack.get_json()['attempt']['attemptId']

    from lib.conversations import change_notifications
    notified = []
    monkeypatch.setattr(change_notifications, 'notify_conv_changed',
                        lambda conv_id, **kw: notified.append(conv_id))

    resp = flask_client.post(f'/api/v3/attempts/{attempt_id}/abort')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['status'] == 'abort_signaled'
    assert CONV in notified


def test_abort_by_task_id_denies_foreign_registry_task(flask_client):
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks

    task_id = 'foreign-abort-task'
    task = {
        'id': task_id,
        '_userId': 2,
        'aborted': False,
        'status': 'running',
    }
    with tasks_lock:
        tasks[task_id] = task
    try:
        response = flask_client.post(f'/api/v1/chat/abort/{task_id}')
        assert response.status_code == 404
        assert task['aborted'] is False
    finally:
        with tasks_lock:
            tasks.pop(task_id, None)
