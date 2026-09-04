"""Human Goal Mode controls use Flow/turn authority, never classic autopilot."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from lib.goal_runs.continuation import continue_goal_mode
from lib.goal_runs.control import arm_goal_mode, cancel_goal_mode


pytestmark = pytest.mark.unit


def test_arm_queues_continuation_without_mutating_ordinary_live_task(
    monkeypatch,
):
    from lib.tasks_pkg.manager import runtime as runtime_module

    ordinary = {
        'id': 'ordinary-task', 'convId': 'conv-control',
        'status': 'running', 'config': {'autopilot': False},
    }
    monkeypatch.setattr(
        runtime_module.chat_task_runtime,
        'snapshot_owned',
        lambda **_kwargs: [ordinary],
    )
    monkeypatch.setattr(
        'lib.message_queue.clear_autopilot_marker',
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        'lib.goal_runs.continuation.continue_goal_mode',
        lambda *_args, **_kwargs: {
            'taskId': None, 'queued': True, 'queueId': 'queue-goal'},
    )

    result = arm_goal_mode('conv-control', user_id=5)

    assert result == {
        'armed': True,
        'taskIds': [],
        'deferred': False,
        'continuationQueued': True,
        'queueId': 'queue-goal',
        'error': None,
        'markerAdded': False,
        'markerCleared': True,
    }
    assert ordinary['config']['autopilot'] is False


def test_arm_surfaces_failed_continuation_instead_of_claiming_takeover(
    monkeypatch,
):
    from lib.tasks_pkg.manager import runtime as runtime_module

    monkeypatch.setattr(
        runtime_module.chat_task_runtime,
        'snapshot_owned',
        lambda **_kwargs: [{
            'id': 'ordinary-task', 'convId': 'conv-control',
            'status': 'running', 'config': {'autopilot': False},
        }],
    )
    monkeypatch.setattr(
        'lib.message_queue.clear_autopilot_marker',
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        'lib.goal_runs.continuation.continue_goal_mode',
        lambda *_args, **_kwargs: {
            'taskId': None, 'error': 'goal_objective_unavailable'},
    )

    result = arm_goal_mode('conv-control', user_id=5)

    assert result['armed'] is False
    assert result['deferred'] is True
    assert result['error'] == 'goal_objective_unavailable'


def test_disarm_aborts_flow_goal_with_typed_human_reason(monkeypatch):
    from lib.tasks_pkg.manager import runtime as runtime_module

    abort_event = threading.Event()
    goal = {
        'id': 'goal-task', 'convId': 'conv-control', 'status': 'running',
        'config': {'autopilot': True}, 'flow_mode': True,
        '_goalRunId': 'goal_goal-task', 'abort_event': abort_event,
    }
    monkeypatch.setattr(
        runtime_module.chat_task_runtime,
        'snapshot_owned',
        lambda **_kwargs: [goal],
    )
    monkeypatch.setattr(
        runtime_module.chat_task_runtime,
        'abort_owned',
        lambda task_id, **_kwargs: abort_event.set() or task_id == 'goal-task',
    )

    def update(task_id, *, fields, **_kwargs):
        assert task_id == 'goal-task'
        goal.update(fields)
        return True

    monkeypatch.setattr(runtime_module.chat_task_runtime, 'update_fields', update)
    monkeypatch.setattr(
        'lib.message_queue.clear_autopilot_marker',
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        'lib.message_queue.clear_queue_kind',
        lambda *_args, **_kwargs: 0,
    )

    result = cancel_goal_mode('conv-control', user_id=5)

    assert result['taskIds'] == ['goal-task']
    assert goal['aborted'] is True
    assert goal['_abort_reason'] == 'human_stop'
    assert abort_event.is_set()
    assert goal['config']['autopilot'] is False


def test_disarm_removes_only_queued_goal_continuations(monkeypatch):
    from lib.tasks_pkg.manager import runtime as runtime_module

    monkeypatch.setattr(
        runtime_module.chat_task_runtime,
        'snapshot_owned',
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        'lib.message_queue.clear_autopilot_marker',
        lambda *_args, **_kwargs: False,
    )
    cleared = []

    def clear_kind(conversation_id, kind, *, user_id):
        cleared.append((conversation_id, kind, user_id))
        return 1

    monkeypatch.setattr('lib.message_queue.clear_queue_kind', clear_kind)

    result = cancel_goal_mode('conv-control', user_id=5)

    assert cleared == [('conv-control', 'goal_continuation', 5)]
    assert result['queuedContinuationsCleared'] == 1
    assert result['disarmed'] is True


def test_idle_continuation_creates_turn_native_goal_command(monkeypatch):
    from lib.tasks_pkg.manager import runtime as runtime_module
    from lib.tasks_pkg.conv_message_builder import api as message_builder

    monkeypatch.setattr(
        runtime_module.chat_task_runtime,
        'snapshot_owned',
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        message_builder,
        'build_api_messages_from_db',
        lambda *_args, **_kwargs: [
            {'role': 'user', 'content': 'old objective'},
            {'role': 'assistant', 'content': 'partial work'},
            {'role': 'user', 'content': 'latest human objective'},
            {
                'role': 'user', 'content': 'synthetic review',
                '_isVirtualUser': True,
            },
        ],
    )
    calls = []

    class Commands:
        def create_turn(self, *args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(value={
                'attempt': {'taskId': 'flow-goal-task'},
                'submittedTurn': {'actor': 'virtual_user'},
                'turn': {'actor': 'assistant'},
            })

    result = continue_goal_mode(
        'conv-control', {
            'model': 'test',
            '_turnId': 'stale-turn',
            '_attemptId': 'stale-attempt',
            'contentPrefix': 'stale partial output',
        }, user_id=5,
        command_service=Commands(),
    )

    assert result['taskId'] == 'flow-goal-task'
    assert result['goalObjective'] == 'latest human objective'
    args, kwargs = calls[0]
    assert args[0:2] == ('conv-control', 5)
    body = args[2]
    assert body['config']['autopilot'] is True
    assert '_turnId' not in body['config']
    assert '_attemptId' not in body['config']
    assert 'contentPrefix' not in body['config']
    assert body['inputActor'] == 'virtual_user'
    assert body['inputTurn']['_goalContinuation'] is True
    assert kwargs['trusted_goal_objective'] == 'latest human objective'
